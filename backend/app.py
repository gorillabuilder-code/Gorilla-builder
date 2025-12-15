# app.py — gor://a (FastAPI + Supabase + Projects + CodeMirror + Agent SSE)
from __future__ import annotations

import os
import json
import time
import asyncio
import secrets
import mimetypes
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from supabase import create_client, Client


# ==========================================================
# APP INIT
# ==========================================================

app = FastAPI(title="GOR://A Backend ASGI")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("AUTH_SECRET_KEY", secrets.token_hex(32)),
)

# Static (your actual UI templates live in templates/)
app.mount("/styles", StaticFiles(directory="templates/styles"), name="styles")
app.mount("/scripts", StaticFiles(directory="templates/scripts"), name="scripts")
app.mount("/assets", StaticFiles(directory="templates/assets"), name="assets")

templates = Jinja2Templates(directory="templates")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")

# ==========================================================
# SUPABASE INIT
# ==========================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# ==========================================================
# AUTH HELPERS (session-based for now)
# ==========================================================

def get_current_user(request: Request) -> Dict[str, Any]:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def _require_project_owner(user: Dict[str, Any], project_id: str) -> None:
    # Will raise if not found
    supabase.table("projects").select("id").eq("id", project_id).eq(
        "owner_id", user["id"]
    ).single().execute()


# ==========================================================
# PUBLIC PAGES
# ==========================================================

PUBLIC_PAGES = {
    "/": "index.html",
    "/login": "login.html",
    "/signup": "signup.html",
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


# ==========================================================
# AUTH ROUTES (placeholder; keeps your UI flowing)
# Replace these with Supabase Auth later if desired.
# ==========================================================

@app.post("/auth/login")
async def auth_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    # Placeholder session user (you likely already set this via OAuth elsewhere)
    # Make sure user["id"] matches auth.users.id in Supabase.
    # For dev: allow overriding with DEV_USER_ID to test.
    dev_user_id = os.getenv("DEV_USER_ID")
    if not dev_user_id:
        raise HTTPException(
            400,
            detail="DEV_USER_ID not set. Implement real auth or set DEV_USER_ID for dev.",
        )

    request.session["user"] = {"id": dev_user_id, "email": email}
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/auth/signup")
async def auth_signup(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    # Same dev-mode approach
    dev_user_id = os.getenv("DEV_USER_ID")
    if not dev_user_id:
        raise HTTPException(
            400,
            detail="DEV_USER_ID not set. Implement real auth or set DEV_USER_ID for dev.",
        )

    request.session["user"] = {"id": dev_user_id, "email": email}
    return RedirectResponse("/dashboard", status_code=303)


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

    projects = (
        supabase.table("projects")
        .select("*")
        .eq("owner_id", user["id"])
        .order("updated_at", desc=True)
        .execute()
        .data
    )

    return templates.TemplateResponse(
        "dashboard.html",
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
        "workspace.html",
        {"request": request, "projects": projects, "user": user},
    )


# ==========================================================
# PROJECTS (templates live under templates/projects/)
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
    return templates.TemplateResponse(
        "projects/project-create.html",
        {"request": request, "user": user},
    )


@app.post("/projects/create")
async def create_project(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
):
    user = get_current_user(request)

    project = (
        supabase.table("projects")
        .insert(
            {"owner_id": user["id"], "name": name, "description": description}
        )
        .execute()
        .data[0]
    )

    # Optionally seed a default index.html so preview works immediately
    supabase.table("files").upsert(
        {
            "project_id": project["id"],
            "path": "index.html",
            "content": "<!doctype html><html><head><meta charset='utf-8'><title>New App</title></head><body><h1>New gor://a app</h1></body></html>",
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
        .eq("owner_id", user["id"])
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
async def project_editor(
    request: Request,
    project_id: str,
    file: str = "index.html",
):
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

    return {
        "path": row["path"],
        "content": row.get("content", ""),
        "updated_at": row.get("updated_at"),
    }


@app.post("/api/project/{project_id}/save")
async def save_file(
    request: Request,
    project_id: str,
    file: str = Form(...),
    content: str = Form(...),
):
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    supabase.table("files").upsert(
        {"project_id": project_id, "path": file, "content": content},
        on_conflict="project_id,path",
    ).execute()

    # touch project updated_at
    supabase.table("projects").update(
        {"updated_at": "now()"}
    ).eq("id", project_id).execute()

    return {"success": True}


# ==========================================================
# DEPLOYED APP (SERVE FROM SUPABASE "files" TABLE)
# This makes /app/{project_id}/index.html work without writing to disk.
# ==========================================================

def _guess_media_type(path: str) -> str:
    mt, _ = mimetypes.guess_type(path)
    if mt:
        return mt
    # sensible fallbacks
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

    # directory -> index.html
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

    media_type = _guess_media_type(path)
    return Response(content=row.get("content", ""), media_type=media_type)


# ==========================================================
# AGENT STREAM (planner → coder → files) — REAL SSE
# Endpoints expected by your project-editor.html:
#   POST  /api/project/{id}/agent/start
#   GET   /api/project/{id}/events
# ==========================================================

# ---- Progress bus (in-memory) ----
class _ProgressBus:
    def __init__(self):
        self._queues: Dict[str, list[asyncio.Queue]] = {}

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


# ---- Agent imports (safe) ----
# Your repo shows backend/agent/{planner.py,coder.py,...}
try:
    from backend.agent.planner import Planner  # type: ignore
except Exception:
    Planner = None  # type: ignore

try:
    from backend.agent.coder import Coder  # type: ignore
except Exception:
    Coder = None  # type: ignore


@app.post("/api/project/{project_id}/agent/start")
async def agent_start(
    request: Request,
    project_id: str,
    prompt: str = Form(...),
):
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    if Planner is None or Coder is None:
        progress_bus.emit(project_id, {"type": "status", "text": "Agent unavailable"})
        progress_bus.emit(
            project_id,
            {
                "type": "log",
                "role": "system",
                "text": "Planner/Coder import failed. Check backend/agent/*.py paths and PYTHONPATH.",
            },
        )
        return {"started": False, "error": "agent_import_failed"}

    async def _run():
        try:
            progress_bus.emit(project_id, {"type": "status", "text": "Planning…"})
            progress_bus.emit(project_id, {"type": "log", "role": "you", "text": prompt})

            # Gather minimal context (file list)
            ctx_files = (
                supabase.table("files")
                .select("path,updated_at")
                .eq("project_id", project_id)
                .order("updated_at", desc=True)
                .execute()
                .data
            )

            planner = Planner()
            plan = await planner.generate_plan(
                user_request=prompt,
                project_context={"project_id": project_id, "files": ctx_files},
            )

            progress_bus.emit(project_id, {"type": "log", "role": "planner", "text": plan.get("raw", "")})
            progress_bus.emit(project_id, {"type": "status", "text": "Coding…"})

            coder = Coder(project_id=project_id, supabase=supabase)

            # We support either:
            #  - async generator: async for event in coder.apply_plan(plan)
            #  - coroutine returning list of events
            #  - sync generator returning events
            apply_fn = getattr(coder, "apply_plan", None)
            if not apply_fn:
                raise RuntimeError("Coder missing apply_plan(plan)")

            result = apply_fn(plan)

            if hasattr(result, "__aiter__"):
                async for event in result:
                    _emit_agent_event(project_id, event)
            elif asyncio.iscoroutine(result):
                events = await result
                if isinstance(events, list):
                    for event in events:
                        _emit_agent_event(project_id, event)
            else:
                # sync iterable
                for event in result:
                    _emit_agent_event(project_id, event)

            progress_bus.emit(project_id, {"type": "status", "text": "Done"})
        except Exception as exc:
            progress_bus.emit(project_id, {"type": "status", "text": "Agent error"})
            progress_bus.emit(project_id, {"type": "log", "role": "system", "text": f"{exc}"})


    def _emit_agent_event(pid: str, event: Any) -> None:
        """
        Normalize agent events to what your editor expects.
        Expected types:
          - {type:'status', text:'...'}
          - {type:'log', role:'planner|coder|files|agent', text:'...'}
          - {type:'file_changed', path:'...', action:'upsert|delete'}
        """
        if isinstance(event, dict):
            if "type" in event:
                progress_bus.emit(pid, event)
                # if file changed, make sure project timestamp is touched
                if event.get("type") == "file_changed":
                    supabase.table("projects").update({"updated_at": "now()"}).eq("id", pid).execute()
                return

            # fallback mapping
            if "path" in event and ("content" in event or "action" in event):
                progress_bus.emit(pid, {"type": "file_changed", "path": event["path"], "action": event.get("action", "upsert")})
                return

            progress_bus.emit(pid, {"type": "log", "role": "agent", "text": json.dumps(event)})
            return

        progress_bus.emit(pid, {"type": "log", "role": "agent", "text": str(event)})


    asyncio.create_task(_run())
    return {"started": True}


@app.get("/api/project/{project_id}/events")
async def agent_events(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    async def _gen():
        q = progress_bus.subscribe(project_id)

        # initial hello so UI shows activity instantly
        yield f"data: {json.dumps({'type':'status','text':'Streaming…'})}\n\n"

        try:
            while True:
                ev = await q.get()
                yield f"data: {json.dumps(ev)}\n\n"
        except asyncio.CancelledError:
            progress_bus.unsubscribe(project_id, q)
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
    return {"ok": True, "ts": int(time.time())}
