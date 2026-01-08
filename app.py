# app.py
from __future__ import annotations

import os
import json
import time
import uuid
import asyncio
import secrets
import mimetypes
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
    FileResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()
# --------------------------------------------------------------------------
# IMPORTS: Backend Modules
# --------------------------------------------------------------------------
from backend.run_manager import ProjectRunManager
from backend.ai.planner import Planner
from backend.ai.coder import Coder


# ==========================================================================
# CONSTANTS & PATHS
# ==========================================================================
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")
FRONTEND_TEMPLATES_DIR = os.path.join(FRONTEND_DIR, "templates")
FRONTEND_STYLES_DIR = os.path.join(FRONTEND_DIR, "styles")

if not os.path.isdir(FRONTEND_DIR):
    ROOT_DIR = os.path.dirname(ROOT_DIR)
    FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")
    FRONTEND_TEMPLATES_DIR = os.path.join(FRONTEND_DIR, "templates")
    FRONTEND_STYLES_DIR = os.path.join(FRONTEND_DIR, "styles")

DEV_MODE = os.getenv("DEV_MODE", "1") == "1"
MONTHLY_TOKEN_LIMIT = int(os.getenv("MONTHLY_TOKEN_LIMIT", "100000"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# ==========================================================================
# APP INITIALIZATION
# ==========================================================================
app = FastAPI(title="GOR://A Backend ASGI")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("AUTH_SECRET_KEY", secrets.token_hex(32)),
)

if os.path.isdir(FRONTEND_STYLES_DIR):
    app.mount("/styles", StaticFiles(directory=FRONTEND_STYLES_DIR), name="styles")

templates = Jinja2Templates(directory=FRONTEND_TEMPLATES_DIR)

# ==========================================================================
# SUPABASE CLIENT
# ==========================================================================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# ==========================================================================
# DATABASE HELPERS (Safe Access)
# ==========================================================================
def db_select_one(table: str, match: dict, select="*"):
    """Safe wrapper to fetch a single row."""
    if not supabase: return None
    try:
        q = supabase.table(table).select(select)
        for k, v in match.items(): q = q.eq(k, v)
        res = q.maybe_single().execute()
        return res.data if res else None
    except Exception: return None

def db_upsert(table: str, data: dict, on_conflict=None):
    """Safe wrapper to upsert data."""
    if not supabase: return None
    try:
        q = supabase.table(table).upsert(data, on_conflict=on_conflict)
        res = q.execute()
        return res.data
    except Exception as e:
        # Re-raise so specific error handling (like FK checks) can work
        raise e


# ==========================================================================
# TOKEN MANAGEMENT LOGIC (Simplified: Single Table)
# ==========================================================================
def get_monthly_tokens_used(user_id: str, month: Optional[str] = None) -> int:
    """Fetches tokens directly from the users table."""
    # Note: 'month' param kept for compatibility but logic is now persistent per-user
    user = db_select_one("users", {"id": user_id}, "tokens_used")
    return int(user.get("tokens_used") or 0) if user else 0

def add_monthly_tokens(user_id: str, tokens_to_add: int, month: Optional[str] = None) -> int:
    """Adds tokens. If user missing, creates them automatically (Upsert)."""
    if tokens_to_add <= 0:
        return get_monthly_tokens_used(user_id)
    
    try:
        current = get_monthly_tokens_used(user_id)
        new_total = current + int(tokens_to_add)
        
        # Single DB call: Update tokens OR create user if missing
        # This prevents the Foreign Key error completely.
        db_upsert(
            "users",
            {
                "id": user_id, 
                "tokens_used": new_total,
                "email": "dev@local", # Default for dev mode; ignored if user exists
                "updated_at": "now()"
            }, 
            on_conflict="id"
        )
        
        return new_total
    except Exception as e:
        print(f"Token Update Error: {e}")
        return 0

def enforce_token_limit_or_raise(user_id: str) -> Tuple[int, int]:
    """Checks limit using the new column."""
    used = get_monthly_tokens_used(user_id)
    remaining = max(0, MONTHLY_TOKEN_LIMIT - used)
    
    if used >= MONTHLY_TOKEN_LIMIT:
        raise HTTPException(
            status_code=402,
            detail=f"Monthly token limit reached ({MONTHLY_TOKEN_LIMIT}). Used={used}.",
        )
    return used, remaining


# ==========================================================================
# AUTHENTICATION & USER HELPERS
# ==========================================================================
_DEV_NAMESPACE = uuid.UUID("2b48c7cc-51c8-4b50-a5c6-2c4ac3f26cb1")

def _stable_user_id_for_email(email: str) -> str:
    """Generates a consistent UUIDv5 based on email for Dev Mode."""
    e = (email or "").strip().lower()
    if not e: 
        return str(uuid.uuid4())
    return str(uuid.uuid5(_DEV_NAMESPACE, e))

def ensure_public_user(user_id: str, email: str) -> None:
    """Ensures the user exists in the public.users table."""
    try:
        db_upsert("users", {"id": user_id, "email": email, "plan": "free"}, on_conflict="id")
    except Exception:
        pass

def get_current_user(request: Request) -> Dict[str, Any]:
    """Retrieves user from session or creates a Dev Mode user."""
    user = request.session.get("user")
    
    if user and user.get("id"):
        # Refresh public record just in case
        ensure_public_user(user["id"], user.get("email") or "unknown@local")
        return user

    if DEV_MODE:
        # Fallback for dev mode if session is empty
        anon_id = str(uuid.uuid4())
        user = {"id": anon_id, "email": "dev@local"}
        request.session["user"] = user
        ensure_public_user(user["id"], user["email"])
        return user

    raise HTTPException(status_code=401, detail="Not authenticated")

def _require_project_owner(user: Dict[str, Any], project_id: str) -> None:
    """Verifies that the current user owns the project."""
    if DEV_MODE:
        # In strict dev mode, we might relax this, but checking existence is good
        try:
            res = db_select_one("projects", {"id": project_id}, "id")
            if not res: raise Exception("Not found")
            return
        except Exception:
            raise HTTPException(status_code=404, detail="Project not found")
    
    # Production check
    res = db_select_one("projects", {"id": project_id, "owner_id": user["id"]}, "id")
    if not res:
        raise HTTPException(status_code=404, detail="Project not found or access denied")


# ==========================================================================
# PUBLIC ROUTES (Templates)
# ==========================================================================
PUBLIC_PAGES = {
    "/": "index.html",
    "/login": "auth/login.html",
    "/signup": "auth/signup.html",
    "/forgot-password": "forgot.html",
    "/pricing": "pricing.html",
    "/help": "help.html",
    "/about": "about.html",
}

for route, template_name in PUBLIC_PAGES.items():
    # We use a closure here to bind the template name variable
    def make_handler(t_name):
        async def handler(request: Request):
            return templates.TemplateResponse(t_name, {"request": request})
        return handler
        
    app.get(route, response_class=HTMLResponse)(make_handler(template_name))

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    p = os.path.join(FRONTEND_DIR, "assets", "favicon.png")
    if os.path.exists(p): 
        return FileResponse(p)
    raise HTTPException(status_code=404)


# ==========================================================================
# AUTH API ROUTES
# ==========================================================================
@app.post("/auth/signup")
async def auth_signup(request: Request, email: str = Form(...), password: str = Form(...)):
    email = (email or "").strip().lower()
    
    if DEV_MODE:
        try:
            # Try creating real supabase auth user first
            created = supabase.auth.admin.create_user({
                "email": email, 
                "password": password, 
                "email_confirm": True
            })
            user_id = created.user.id
        except Exception:
            # Fallback to deterministic ID
            user_id = _stable_user_id_for_email(email)
        
        request.session["user"] = {"id": user_id, "email": email}
        ensure_public_user(user_id, email)
        return RedirectResponse("/dashboard", status_code=303)
        
    raise HTTPException(400, detail="Signup requires DEV_MODE=1 or Supabase Auth setup.")

@app.post("/auth/login")
async def auth_login(request: Request, email: str = Form(...), password: str = Form(...)):
    email = (email or "").strip().lower()
    
    if DEV_MODE:
        user_id = _stable_user_id_for_email(email)
        request.session["user"] = {"id": user_id, "email": email}
        ensure_public_user(user_id, email)
        return RedirectResponse("/dashboard", status_code=303)
        
    raise HTTPException(400, detail="Login requires DEV_MODE=1 or Supabase Auth setup.")

@app.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# ==========================================================================
# DASHBOARD & WORKSPACE (FIXED 500)
# ==========================================================================
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_current_user(request)
    
    # Token Data
    used = get_monthly_tokens_used(user["id"])
    user["tokens"] = {
        "used": used, 
        "limit": MONTHLY_TOKEN_LIMIT, 
        "remaining": max(0, MONTHLY_TOKEN_LIMIT - used)
    }
    
    # Project Data
    try:
        res = (
            supabase.table("projects")
            .select("*")
            .eq("owner_id", user["id"])
            .order("updated_at", desc=True)
            .execute()
        )
        # --- FIX: CHECK IF DATA EXISTS ---
        projects = res.data if res and res.data else []
    except Exception:
        projects = []

    return templates.TemplateResponse(
        "dashboard/dashboard.html", 
        {"request": request, "projects": projects, "user": user}
    )

@app.get("/workspace", response_class=HTMLResponse)
async def workspace(request: Request):
    user = get_current_user(request)
    used = get_monthly_tokens_used(user["id"])
    user["tokens"] = {"used": used, "limit": MONTHLY_TOKEN_LIMIT}

    try:
        res = (
            supabase.table("projects")
            .select("id,name,updated_at")
            .eq("owner_id", user["id"])
            .order("updated_at", desc=True)
            .execute()
        )
        # --- FIX: CHECK IF DATA EXISTS ---
        projects = res.data if res and res.data else []
    except Exception:
        projects = []
        
    return templates.TemplateResponse(
        "dashboard/workspace.html",
        {"request": request, "projects": projects, "user": user}
    )


# ==========================================================================
# PROJECT ROUTES (FIXED 500)
# ==========================================================================
@app.post("/projects/create")
async def create_project(request: Request, name: str = Form(...), description: str = Form("")):
    user = get_current_user(request)
    ensure_public_user(user["id"], user.get("email") or "unknown@local")

    try:
        res = (
            supabase.table("projects")
            .insert({"owner_id": user["id"], "name": name, "description": description})
            .execute()
        )
        # --- FIX: CHECK IF DATA EXISTS ---
        if not res or not res.data:
            raise Exception("No data returned from insert")
            
        project = res.data[0]
        return RedirectResponse(f"/projects/{project['id']}/editor", status_code=303)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Create failed: {e}")

@app.get("/projects/createit", response_class=HTMLResponse)
async def project_create(request: Request):
    user = get_current_user(request)
    return templates.TemplateResponse(
        "projects/project-create.html", 
        {"request": request, "user": user}
    )
    
@app.get("/projects/{project_id}/editor", response_class=HTMLResponse)
async def project_editor(request: Request, project_id: str, file: str = "index.html"):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    used = get_monthly_tokens_used(user["id"])
    user["tokens"] = {"used": used, "limit": MONTHLY_TOKEN_LIMIT}

    return templates.TemplateResponse(
        "projects/project-editor.html",
        {"request": request, "project_id": project_id, "file": file, "user": user}
    )

@app.get("/projects/{project_id}/preview", response_class=HTMLResponse)
async def project_preview(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    return templates.TemplateResponse(
        "projects/project-preview.html",
        {"request": request, "project_id": project_id, "user": user}
    )

@app.get("/projects/{project_id}/settings", response_class=HTMLResponse)
async def project_settings(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    try:
        res = supabase.table("projects").select("*").eq("id", project_id).single().execute()
        # --- FIX: CHECK IF DATA EXISTS ---
        project = res.data if res else None
    except Exception as e:
        return templates.TemplateResponse(
            "projects/project-settings.html",
            {"request": request, "project_id": project_id, "error": str(e), "user": user}
        )
        
    return templates.TemplateResponse(
        "projects/project-settings.html",
        {"request": request, "project_id": project_id, "project": project, "user": user}
    )

@app.post("/projects/{project_id}/settings")
async def project_settings_save(
    request: Request, 
    project_id: str, 
    name: str = Form(...), 
    description: str = Form("")
):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    supabase.table("projects").update(
        {"name": name, "description": description}
    ).eq("id", project_id).execute()
    
    return RedirectResponse(f"/projects/{project_id}/settings", status_code=303)


# ==========================================================================
# FILE API ROUTES (FIXED 500)
# ==========================================================================
@app.get("/api/project/{project_id}/files")
async def list_files(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    res = (
        supabase.table("files")
        .select("path,updated_at")
        .eq("project_id", project_id)
        .order("updated_at", desc=True)
        .execute()
    )
    # --- FIX: CHECK IF DATA EXISTS ---
    return {"files": res.data if res and res.data else []}

@app.get("/api/project/{project_id}/file")
async def get_file(request: Request, project_id: str, path: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    res = (
        supabase.table("files")
        .select("path,content")
        .eq("project_id", project_id)
        .eq("path", path)
        .maybe_single()
        .execute()
    )
    
    # --- FIX: CHECK IF DATA EXISTS ---
    row = res.data if res else None
    if not row:
        return {"path": path, "content": ""}
    return row

@app.post("/api/project/{project_id}/save")
async def save_file(
    request: Request, 
    project_id: str, 
    file: str = Form(...), 
    content: str = Form(...)
):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    db_upsert(
        "files", 
        {"project_id": project_id, "path": file, "content": content}, 
        on_conflict="project_id,path"
    )
    
    # Update project timestamp
    supabase.table("projects").update({"updated_at": "now()"}).eq("id", project_id).execute()
    
    return {"success": True}

@app.get("/api/project/{project_id}/tokens")
async def check_tokens(request: Request, project_id: str):
    """Endpoint to get live token usage via polling if needed."""
    user = get_current_user(request)
    used = get_monthly_tokens_used(user["id"])
    return {"used": used, "limit": MONTHLY_TOKEN_LIMIT}


# ==========================================================================
# STATIC FILE SERVING (PREVIEW) (FIXED 500)
# ==========================================================================
def _guess_media_type(path: str) -> str:
    mt, _ = mimetypes.guess_type(path)
    if mt: return mt
    if path.endswith(".js"): return "application/javascript"
    if path.endswith(".css"): return "text/css"
    if path.endswith(".html"): return "text/html"
    return "text/plain"

@app.get("/app/{project_id}/{path:path}")
async def serve_project_file(request: Request, project_id: str, path: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    if not path or path.endswith("/"): 
        path = (path or "") + "index.html"
        
    res = (
        supabase.table("files")
        .select("content")
        .eq("project_id", project_id)
        .eq("path", path)
        .maybe_single()
        .execute()
    )
    
    # --- FIX: CHECK IF DATA EXISTS ---
    row = res.data if res else None
    
    if not row:
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
        
    return Response(
        content=row.get("content", ""), 
        media_type=_guess_media_type(path)
    )


# ==========================================================================
# SSE EVENT BUS
# ==========================================================================
class _ProgressBus:
    def __init__(self):
        self._queues: Dict[str, List[asyncio.Queue]] = {}

    def subscribe(self, project_id: str) -> asyncio.Queue:
        q = asyncio.Queue()
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

# Helper Emitters
def emit_log(pid: str, role: str, text: str) -> None:
    progress_bus.emit(pid, {"type": "log", "role": role, "text": text})

def emit_status(pid: str, text: str) -> None:
    progress_bus.emit(pid, {"type": "status", "text": text})

def emit_phase(pid: str, value: str) -> None:
    progress_bus.emit(pid, {"type": "phase", "value": value})

def emit_progress(pid: str, text: str, pct: float) -> None:
    progress_bus.emit(pid, {"type": "progress", "text": text, "pct": pct})

def emit_file_changed(pid: str, path: str) -> None:
    progress_bus.emit(pid, {"type": "file_changed", "path": path})

def emit_token_update(pid: str, used: int) -> None:
    progress_bus.emit(pid, {"type": "token_usage", "used": used})


# ==========================================================================
# AI AGENT WORKFLOW (FIXED SILENT MESSAGES)
# ==========================================================================
async def _fetch_file_tree(project_id: str) -> Dict[str, str]:
    """Downloads all project files to memory for the agent context."""
    res = (
        supabase.table("files")
        .select("path,content")
        .eq("project_id", project_id)
        .execute()
    )
    # --- FIX: CHECK IF DATA EXISTS ---
    rows = res.data if res and res.data else []
    return {r["path"]: (r.get("content") or "") for r in rows}

@app.post("/api/project/{project_id}/agent/start")
async def agent_start(request: Request, project_id: str, prompt: str = Form(...)):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    # 1. Enforce Token Limit
    enforce_token_limit_or_raise(user["id"])
    
    emit_status(project_id, "Agent received prompt")
    emit_log(project_id, "user", prompt)

    async def _run():
        total_run_tokens = 0
        try:
            # --- FIX: DELAY FOR FRONTEND CONNECTION ---
            await asyncio.sleep(0.5)
            
            emit_progress(project_id, "Reading project files...", 5)
            file_tree = await _fetch_file_tree(project_id)
            
            planner = Planner()
            coder = Coder()
            
            # --- PHASE 1: PLANNER ---
            emit_phase(project_id, "planner")
            emit_progress(project_id, "Planning...", 10)
            
            # Generate Plan
            plan_res = planner.generate_plan(
                user_request=prompt, 
                project_context={"project_id": project_id, "files": list(file_tree.keys())}
            )
            
            # Track Planner Tokens
            ptokens = plan_res.get("usage", {}).get("total_tokens", 0)
            if ptokens > 0:
                total_run_tokens += ptokens
                new_total = add_monthly_tokens(user["id"], ptokens)
                emit_token_update(project_id, new_total)
            
            # Show Assistant Message
            assistant_msg = plan_res.get("assistant_message")
            if assistant_msg:
                emit_log(project_id, "assistant", assistant_msg)
            
            # Show Tasks (Internal Log)
            tasks = plan_res["plan"].get("todo", [])
            todo_md = plan_res.get("todo_md", "")
            if todo_md:
                emit_log(project_id, "planner", todo_md[:5000])

            if not tasks:
                emit_status(project_id, "Response Complete")
                emit_progress(project_id, "Done", 100)
                return

            # --- PHASE 2: CODER ---
            emit_phase(project_id, "coder")
            total = len(tasks)
            
            for i, task in enumerate(tasks, 1):
                # Check limit before every heavy operation
                enforce_token_limit_or_raise(user["id"])
                
                pct = 10 + (90 * (i / total))
                emit_progress(project_id, f"Building step {i}/{total}...", pct)
                emit_status(project_id, f"Implementing task {i}/{total}...")
                
                # Generate Code
                code_res = await coder.generate_code(
                    plan_section="Implementation",
                    plan_text=task,
                    file_tree=file_tree,
                    project_name=f"proj-{project_id[:4]}"
                )
                
                # Track Coder Tokens
                ctokens = code_res.get("usage", {}).get("total_tokens", 0)
                if ctokens > 0:
                    total_run_tokens += ctokens
                    new_total = add_monthly_tokens(user["id"], ctokens)
                    emit_token_update(project_id, new_total)

                # Show Coder Logic
                coder_msg = code_res.get("message")
                if coder_msg:
                    emit_log(project_id, "coder", coder_msg)

                # Apply Changes
                ops = code_res.get("operations", [])
                emit_phase(project_id, "files")
                
                for op in ops:
                    path = op.get("path")
                    content = op.get("content")
                    if path and content is not None:
                        # Upsert to DB
                        db_upsert(
                            "files", 
                            {"project_id": project_id, "path": path, "content": content}, 
                            on_conflict="project_id,path"
                        )
                        emit_file_changed(project_id, path)
                
                # Refresh context for next step
                file_tree = await _fetch_file_tree(project_id)
            
            # --- FINISH ---
            emit_status(project_id, "All tasks completed.")
            emit_progress(project_id, "Done", 100)
            
        except Exception as e:
            # Save any pending tokens if crash happened
            if total_run_tokens > 0:
                add_monthly_tokens(user["id"], 0) # Just ensures sync if logic differed
            
            emit_status(project_id, "Error")
            emit_log(project_id, "system", f"Workflow failed: {e}")
            print(traceback.format_exc())

    asyncio.create_task(_run())
    return {"started": True}

@app.get("/api/project/{project_id}/events")
async def agent_events(request: Request, project_id: str):
    """SSE endpoint for streaming agent logs and status."""
    if not DEV_MODE:
        user = get_current_user(request)
        _require_project_owner(user, project_id)

    async def _gen():
        q = progress_bus.subscribe(project_id)
        try:
            # Initial connection ping
            yield f"data: {json.dumps({'type':'status', 'text':'Connected'})}\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            progress_bus.unsubscribe(project_id, q)

    return StreamingResponse(
        _gen(), 
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache", 
            "Connection": "keep-alive", 
            "X-Accel-Buffering": "no"
        }
    )


# ==========================================================================
# SERVER PREVIEW RUNNER
# ==========================================================================
run_manager = ProjectRunManager()

@app.post("/api/project/{project_id}/run/start")
async def run_start(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    if not DEV_MODE: 
        raise HTTPException(403, "Server preview is DEV_MODE only.")
        
    try:
        emit_status(project_id, "Booting server...")
        file_tree = await _fetch_file_tree(project_id)
        info = await run_manager.start(project_id, file_tree)
        emit_log(project_id, "system", f"Server running on port {info.port}")
        return {"ok": True, "port": info.port}
    except Exception as e:
        emit_log(project_id, "system", f"Server start failed: {e}")
        raise HTTPException(400, f"Start failed: {e}")

@app.post("/api/project/{project_id}/run/stop")
async def run_stop(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    await run_manager.stop(project_id)
    emit_log(project_id, "system", "Server stopped.")
    return {"ok": True}

@app.get("/api/project/{project_id}/run/status")
async def run_status(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    running, port = run_manager.is_running(project_id)
    return {"running": running, "port": port}

@app.api_route("/run/{project_id}/{path:path}", methods=["GET","POST","PUT","DELETE","OPTIONS","PATCH"])
async def run_proxy(request: Request, project_id: str, path: str):
    """Proxies requests to the running uvicorn instance for the project."""
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    if not DEV_MODE: 
        raise HTTPException(403, "Dev mode only.")
        
    body = await request.body()
    try:
        r = await run_manager.proxy(
            project_id=project_id,
            path=path or "",
            method=request.method,
            headers=dict(request.headers),
            body=body,
            query=request.url.query
        )
        # Forward response
        return Response(
            content=r.content,
            status_code=r.status_code,
            headers={k: v for k, v in r.headers.items() if k.lower() not in ("content-encoding", "transfer-encoding", "connection")},
            media_type=r.headers.get("content-type")
        )
    except Exception as e:
        raise HTTPException(502, f"Proxy failed: {e}")


# ==========================================================================
# HEALTH CHECK
# ==========================================================================
@app.get("/health")
async def health():
    return {
        "ok": True, 
        "ts": int(time.time()), 
        "dev_mode": DEV_MODE
    }

@app.post("/api/project/{project_id}/agent/ping")
async def agent_ping(request: Request, project_id: str):
    emit_log(project_id, "system", "🔥 Pong from backend")
    return {"ok": True}