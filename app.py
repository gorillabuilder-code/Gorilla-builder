# app.py — gor://a (FastAPI + Supabase + Projects + CodeMirror + Agent SSE)
from __future__ import annotations

import os
import json
import time
import asyncio
import secrets
import mimetypes
from typing import Any, Dict, Optional, List, Callable

from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    JSONResponse,
    Response,
    StreamingResponse,
    FileResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()


# ==========================================================
# PATHS (supports running from repo root)
# ==========================================================

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")
FRONTEND_TEMPLATES_DIR = os.path.join(FRONTEND_DIR, "templates")
FRONTEND_STYLES_DIR = os.path.join(FRONTEND_DIR, "styles")

# If app.py is executed from backend/ (or frontend missing), try repo root parent
if not os.path.isdir(FRONTEND_DIR):
    ROOT_DIR = os.path.dirname(ROOT_DIR)
    FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")
    FRONTEND_TEMPLATES_DIR = os.path.join(FRONTEND_DIR, "templates")
    FRONTEND_STYLES_DIR = os.path.join(FRONTEND_DIR, "styles")


# ==========================================================
# APP INIT
# ==========================================================

app = FastAPI(title="GOR://A Backend ASGI")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("AUTH_SECRET_KEY", secrets.token_hex(32)),
)

if os.path.isdir(FRONTEND_STYLES_DIR):
    app.mount("/styles", StaticFiles(directory=FRONTEND_STYLES_DIR), name="styles")

templates = Jinja2Templates(directory=FRONTEND_TEMPLATES_DIR)
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")


# ==========================================================
# SUPABASE INIT (NO HARDCODED KEYS)
# ==========================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# ==========================================================
# DEV MODE SWITCH
# ==========================================================
# In Codespaces you typically want DEV_MODE=1 so the UI works even if auth/RLS isn't ready.
DEV_MODE = os.getenv("DEV_MODE", "1") == "1"


# ==========================================================
# AUTH HELPERS (session-based, dev-friendly)
# ==========================================================

def _dev_user() -> Dict[str, Any]:
    """
    Dev user (session fallback). IMPORTANT:
    If your DB schema references auth.users(id), inserts will fail unless that user exists.
    We'll create the auth user when you hit /auth/signup in dev mode.
    """
    return {
        "id": os.getenv("DEV_USER_ID", "00000000-0000-0000-0000-000000000001"),
        "email": os.getenv("DEV_USER_EMAIL", "dev@local"),
    }


def get_current_user(request: Request) -> Dict[str, Any]:
    user = request.session.get("user")
    if user:
        return user

    if DEV_MODE:
        user = _dev_user()
        request.session["user"] = user
        return user

    raise HTTPException(status_code=401, detail="Not authenticated")


def _project_exists_for_user(user: Dict[str, Any], project_id: str) -> bool:
    try:
        supabase.table("projects").select("id").eq("id", project_id).eq("owner_id", user["id"]).single().execute()
        return True
    except Exception:
        return False


def _require_project_owner(user: Dict[str, Any], project_id: str) -> None:
    """
    In DEV_MODE we allow access if project exists (even if owner_id mismatch),
    because service-role key bypasses RLS and you need UI to work.
    """
    if DEV_MODE:
        # allow if project exists at all (dev convenience)
        try:
            supabase.table("projects").select("id").eq("id", project_id).single().execute()
            return
        except Exception:
            raise HTTPException(status_code=404, detail="Project not found")

    # strict owner check
    supabase.table("projects").select("id").eq("id", project_id).eq("owner_id", user["id"]).single().execute()


# ==========================================================
# PUBLIC PAGES
# ==========================================================

PUBLIC_PAGES = {
    "/": "index.html",
    "/login": "auth/login.html",
    "/signup": "auth/signup.html",
    "/forgot-password": "forgot.html",
    "/pricing": "pricing.html",
    "/help": "help.html",
    "/about": "about.html",
}

def _page_handler(template_name: str):
    async def handler(request: Request):
        return templates.TemplateResponse(template_name, {"request": request})
    return handler

for route, template in PUBLIC_PAGES.items():
    app.get(route, response_class=HTMLResponse)(_page_handler(template))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    p = os.path.join(FRONTEND_DIR, "assets", "favicon.png")
    if os.path.exists(p):
        return FileResponse(p)
    raise HTTPException(status_code=404)


# ==========================================================
# AUTH ROUTES (DEV-FRIENDLY)
# ==========================================================
# NOTE:
# Your schema references auth.users(id). So in dev, we will create a Supabase Auth user.
# This ensures owner_id inserts work.

@app.post("/auth/signup")
async def auth_signup(request: Request, email: str = Form(...), password: str = Form(...)):
    if DEV_MODE:
        # Create actual supabase auth user so auth.users has the ID
        try:
            created = supabase.auth.admin.create_user(
                {"email": email, "password": password, "email_confirm": True}
            )
            user_id = created.user.id
            request.session["user"] = {"id": user_id, "email": email}
            return RedirectResponse("/dashboard", status_code=303)
        except Exception as e:
            # If user already exists, try to find it by logging in (still dev)
            request.session["user"] = _dev_user()
            request.session["user"]["email"] = email
            return RedirectResponse("/dashboard", status_code=303)

    raise HTTPException(400, detail="Real signup not wired yet (set DEV_MODE=1 for dev).")


@app.post("/auth/login")
async def auth_login(request: Request, email: str = Form(...), password: str = Form(...)):
    if DEV_MODE:
        # In dev, we just ensure session exists. You can wire real supabase sign-in later.
        # If you created user via /auth/signup, the session has real auth.users id already.
        user = request.session.get("user") or _dev_user()
        user["email"] = email
        request.session["user"] = user
        return RedirectResponse("/dashboard", status_code=303)

    raise HTTPException(400, detail="Real login not wired yet (set DEV_MODE=1 for dev).")


@app.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# ==========================================================
# DASHBOARD & WORKSPACE
# ==========================================================

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_current_user(request)

    # In DEV_MODE, if owner_id doesn't exist yet, you may see empty.
    projects = (
        supabase.table("projects")
        .select("*")
        .eq("owner_id", user["id"])
        .order("updated_at", desc=True)
        .execute()
        .data
    )

    return templates.TemplateResponse(
        "dashboard/dashboard.html",
        {"request": request, "projects": projects, "user": user},
    )


@app.get("/workspace", response_class=HTMLResponse)
async def workspace(request: Request):
    user = get_current_user(request)

    projects = (
        supabase.table("projects")
        .select("id,name,updated_at")
        .eq("owner_id", user["id"])
        .order("updated_at", desc=True)
        .execute()
        .data
    )

    return templates.TemplateResponse(
        "dashboard/workspace.html",
        {"request": request, "projects": projects, "user": user},
    )


# ==========================================================
# PROJECTS
# ==========================================================

@app.get("/projects", response_class=HTMLResponse)
async def projects_list(request: Request):
    user = get_current_user(request)

    projects = (
        supabase.table("projects")
        .select("*")
        .eq("owner_id", user["id"])
        .order("updated_at", desc=True)
        .execute()
        .data
    )

    return templates.TemplateResponse(
        "projects/projects-list.html",
        {"request": request, "projects": projects, "user": user},
    )


@app.get("/projects/create", response_class=HTMLResponse)
async def project_create(request: Request):
    user = get_current_user(request)
    return templates.TemplateResponse("projects/project-create.html", {"request": request, "user": user})


@app.post("/projects/create")
async def create_project(request: Request, name: str = Form(...), description: str = Form("")):
    user = get_current_user(request)

    # IMPORTANT FIX:
    # If auth.users row doesn't exist for this user ID, the FK will fail.
    # Solution: In DEV_MODE, if user id is dev placeholder, store projects under a "dev owner"
    # by forcing a valid owner_id from session or creating one via /auth/signup.
    if DEV_MODE and user["id"].startswith("00000000-0000-0000-0000-"):
        raise HTTPException(
            400,
            detail="Dev user ID is placeholder and not in auth.users. Use /signup once to create a real auth user.",
        )

    try:
        project = (
            supabase.table("projects")
            .insert({"owner_id": user["id"], "name": name, "description": description})
            .execute()
            .data[0]
        )
    except Exception as e:
        # Return readable error for debugging
        raise HTTPException(status_code=500, detail=f"Project create failed: {e}")

    # Seed index.html
    supabase.table("files").upsert(
        {
            "project_id": project["id"],
            "path": "index.html",
            "content": (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>New App</title></head>"
                "<body style='font-family:system-ui;padding:24px'>"
                "<h1>New gor://a app</h1><p>Edit me in the builder.</p>"
                "</body></html>"
            ),
        },
        on_conflict="project_id,path",
    ).execute()

    return RedirectResponse(f"/projects/{project['id']}/editor", status_code=303)


@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    project = (
        supabase.table("projects")
        .select("*")
        .eq("id", project_id)
        .single()
        .execute()
        .data
    )

    files = (
        supabase.table("files")
        .select("path,updated_at")
        .eq("project_id", project_id)
        .order("updated_at", desc=True)
        .execute()
        .data
    )

    return templates.TemplateResponse(
        "projects/project-detail.html",
        {"request": request, "project": project, "files": files, "user": user},
    )


# ==========================================================
# BUILDER / EDITOR / PREVIEW
# ==========================================================

@app.get("/projects/{project_id}/editor", response_class=HTMLResponse)
async def project_editor(request: Request, project_id: str, file: str = "index.html"):
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    return templates.TemplateResponse(
        "projects/project-editor.html",
        {"request": request, "project_id": project_id, "file": file, "user": user},
    )


@app.get("/projects/{project_id}/preview", response_class=HTMLResponse)
async def project_preview(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    return templates.TemplateResponse(
        "projects/project-preview.html",
        {"request": request, "project_id": project_id, "user": user},
    )


# ==========================================================
# FILE API — SUPABASE (CodeMirror)
# ==========================================================

@app.get("/api/project/{project_id}/files")
async def list_files(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    files = (
        supabase.table("files")
        .select("path,updated_at")
        .eq("project_id", project_id)
        .order("updated_at", desc=True)
        .execute()
        .data
    )
    return {"files": files}


@app.get("/api/project/{project_id}/file")
async def get_file(request: Request, project_id: str, path: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    row = (
        supabase.table("files")
        .select("path,content,updated_at")
        .eq("project_id", project_id)
        .eq("path", path)
        .maybe_single()
        .execute()
        .data
    )

    if not row:
        return {"path": path, "content": "", "updated_at": None}

    return {"path": row["path"], "content": row.get("content", ""), "updated_at": row.get("updated_at")}


@app.post("/api/project/{project_id}/save")
async def save_file(request: Request, project_id: str, file: str = Form(...), content: str = Form(...)):
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    supabase.table("files").upsert(
        {"project_id": project_id, "path": file, "content": content},
        on_conflict="project_id,path",
    ).execute()

    supabase.table("projects").update({"updated_at": "now()"}).eq("id", project_id).execute()
    return {"success": True}


# ==========================================================
# DEPLOYED APP (SERVE FROM SUPABASE "files" TABLE)
# ==========================================================

def _guess_media_type(path: str) -> str:
    mt, _ = mimetypes.guess_type(path)
    if mt:
        return mt
    if path.endswith(".js"):
        return "application/javascript"
    if path.endswith(".css"):
        return "text/css"
    if path.endswith(".html"):
        return "text/html"
    return "text/plain"


@app.get("/app/{project_id}/{path:path}")
async def serve_project_file(request: Request, project_id: str, path: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    if not path or path.endswith("/"):
        path = (path or "") + "index.html"

    row = (
        supabase.table("files")
        .select("content")
        .eq("project_id", project_id)
        .eq("path", path)
        .maybe_single()
        .execute()
        .data
    )

    if not row:
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    return Response(content=row.get("content", ""), media_type=_guess_media_type(path))


# ==========================================================
# AGENT SSE BUS
# ==========================================================

class _ProgressBus:
    def __init__(self):
        self._queues: Dict[str, List[asyncio.Queue]] = {}

    def subscribe(self, project_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues.setdefault(project_id, []).append(q)
        return q

    def unsubscribe(self, project_id: str, q: asyncio.Queue) -> None:
        if project_id in self._queues and q in self._queues[project_id]:
            self._queues[project_id].remove(q)

    def emit(self, project_id: str, event: Dict[str, Any]) -> None:
        for q in self._queues.get(project_id, []):
            try:
                q.put_nowait(event)
            except Exception:
                pass

progress_bus = _ProgressBus()


def _emit_agent_event(pid: str, event: Any) -> None:
    if isinstance(event, dict):
        if "type" in event:
            progress_bus.emit(pid, event)
            if event.get("type") == "file_changed":
                supabase.table("projects").update({"updated_at": "now()"}).eq("id", pid).execute()
            return

        if "path" in event and ("content" in event or "action" in event):
            progress_bus.emit(pid, {"type": "file_changed", "path": event["path"], "action": event.get("action", "upsert")})
            return

        progress_bus.emit(pid, {"type": "log", "role": "agent", "text": json.dumps(event)})
        return

    progress_bus.emit(pid, {"type": "log", "role": "agent", "text": str(event)})


# ---- Agent imports (matches your tree) ----
Planner = None
Coder = None

try:
    from backend.agent.planner import Planner as _Planner  # type: ignore
    Planner = _Planner
except Exception as e:
    Planner = None

try:
    from backend.agent.coder import Coder as _Coder  # type: ignore
    Coder = _Coder
except Exception as e:
    Coder = None


@app.post("/api/project/{project_id}/agent/start")
async def agent_start(request: Request, project_id: str, prompt: str = Form(...)):
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    if Planner is None or Coder is None:
        progress_bus.emit(project_id, {"type": "status", "text": "Agent unavailable"})
        progress_bus.emit(project_id, {"type": "log", "role": "system", "text": "Planner/Coder import failed. Check backend/agent/*.py"})
        return {"started": False, "error": "agent_import_failed"}

    async def _run():
        try:
            progress_bus.emit(project_id, {"type": "status", "text": "🚀 Agent task started"})
            progress_bus.emit(project_id, {"type": "log", "role": "you", "text": prompt})

            ctx_files = (
                supabase.table("files")
                .select("path,updated_at")
                .eq("project_id", project_id)
                .order("updated_at", desc=True)
                .execute()
                .data
            )

            progress_bus.emit(project_id, {"type": "status", "text": "Planning…"})
            planner = Planner()
            plan = await planner.generate_plan(
                user_request=prompt,
                project_context={"project_id": project_id, "files": ctx_files},
            )

            progress_bus.emit(project_id, {"type": "log", "role": "planner", "text": plan.get("raw", "")})
            progress_bus.emit(project_id, {"type": "status", "text": "Coding…"})
            coder = Coder(project_id=project_id, supabase=supabase)

            apply_fn = getattr(coder, "apply_plan", None)
            if not apply_fn:
                raise RuntimeError("Coder missing apply_plan(plan)")

            result = apply_fn(plan)

            if hasattr(result, "__aiter__"):
                async for ev in result:
                    _emit_agent_event(project_id, ev)
            elif asyncio.iscoroutine(result):
                events = await result
                if isinstance(events, list):
                    for ev in events:
                        _emit_agent_event(project_id, ev)
            else:
                for ev in result:
                    _emit_agent_event(project_id, ev)

            progress_bus.emit(project_id, {"type": "status", "text": "Done"})
        except Exception as exc:
            progress_bus.emit(project_id, {"type": "status", "text": "Agent error"})
            progress_bus.emit(project_id, {"type": "log", "role": "system", "text": str(exc)})

    asyncio.create_task(_run())
    return {"started": True}


@app.post("/api/project/{project_id}/agent/ping")
async def agent_ping(request: Request, project_id: str):
    # quick SSE test
    progress_bus.emit(project_id, {"type": "log", "role": "system", "text": "🔥 ping from backend"})
    return {"ok": True}


@app.get("/api/project/{project_id}/events")
async def agent_events(request: Request, project_id: str):
    """
    SSE must yield immediately or the browser says "stream disconnected".
    Also, don't hard-fail before first yield in dev mode.
    """
    if not DEV_MODE:
        user = get_current_user(request)
        _require_project_owner(user, project_id)

    async def _gen():
        q = progress_bus.subscribe(project_id)
        yield f"data: {json.dumps({'type':'status','text':'Connected'})}\n\n"
        try:
            while True:
                ev = await q.get()
                yield f"data: {json.dumps(ev)}\n\n"
        finally:
            progress_bus.unsubscribe(project_id, q)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ==========================================================
# HEALTH
# ==========================================================

@app.get("/health")
async def health():
    return {"ok": True, "ts": int(time.time()), "dev_mode": DEV_MODE}
