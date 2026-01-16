from __future__ import annotations

import os
import json
import time
import uuid
import asyncio
import secrets
import mimetypes
import traceback
import random
import string
import urllib.parse  # Added for safe URL redirection
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
import resend  # pip install resend
from fastapi import FastAPI, Request, HTTPException, Form, BackgroundTasks
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
from backend.ai.Xcoder import XCoder


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
# Default limit for new users if not in DB
DEFAULT_TOKEN_LIMIT = int(os.getenv("MONTHLY_TOKEN_LIMIT", "100000"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# GOOGLE AUTH
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "https://silver-palm-tree-7v6grj4v6p4jhx4xg-8000.app.github.dev/auth/google/callback")

# ==========================================================================
# CONFIGURATION: RESEND & SUPABASE
# ==========================================================================
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# IN-MEMORY OTP STORE (For production, use Redis)
PENDING_SIGNUPS = {}

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
# TOKEN MANAGEMENT LOGIC
# ==========================================================================
def get_token_usage_and_limit(user_id: str) -> Tuple[int, int]:
    """Fetches used tokens and total limit from DB."""
    user = db_select_one("users", {"id": user_id}, "tokens_used, tokens_limit")
    if not user:
        return 0, DEFAULT_TOKEN_LIMIT
    
    used = int(user.get("tokens_used") or 0)
    limit = int(user.get("tokens_limit") or DEFAULT_TOKEN_LIMIT)
    return used, limit

def add_monthly_tokens(user_id: str, tokens_to_add: int) -> int:
    """Adds tokens used. If user missing, creates them automatically."""
    if tokens_to_add <= 0:
        used, _ = get_token_usage_and_limit(user_id)
        return used
    
    try:
        current_used, current_limit = get_token_usage_and_limit(user_id)
        new_total = current_used + int(tokens_to_add)
        
        db_upsert(
            "users",
            {
                "id": user_id, 
                "tokens_used": new_total,
                # Preserve limit if exists, else default
                "updated_at": "now()"
            }, 
            on_conflict="id"
        )
        return new_total
    except Exception as e:
        print(f"Token Update Error: {e}")
        return 0

def enforce_token_limit_or_raise(user_id: str) -> Tuple[int, int]:
    """Checks usage against the user's specific limit."""
    used, limit = get_token_usage_and_limit(user_id)
    remaining = max(0, limit - used)
    
    if used >= limit:
        raise HTTPException(
            status_code=402,
            detail=f"Token limit reached ({limit}). Used={used}. Please upgrade or top-up.",
        )
    return used, remaining

def set_user_plan_and_limit(user_id: str, plan: str, limit: int):
    """Updates user plan and token limit (for upgrades)."""
    # Force direct update
    db_upsert(
        "users",
        {
            "id": user_id,
            "plan": plan,
            "tokens_limit": limit,
            "updated_at": "now()"
        },
        on_conflict="id"
    )

def decrease_tokens_used(user_id: str, amount: int):
    """'Top up' by reducing the 'used' counter (simulates adding balance)."""
    used, _ = get_token_usage_and_limit(user_id)
    new_used = max(0, used - amount)
    
    db_upsert(
        "users",
        {"id": user_id, "tokens_used": new_used, "updated_at": "now()"},
        on_conflict="id"
    )


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
    """Ensures the user exists in the public.users table WITHOUT overwriting existing data."""
    try:
        # --- FIX: Check existence first! ---
        existing = db_select_one("users", {"id": user_id}, "id")
        if existing:
            return # User exists, do NOT touch their plan/limits
            
        # Default new users to free plan and default limit
        db_upsert(
            "users", 
            {"id": user_id, "email": email, "plan": "free", "tokens_limit": DEFAULT_TOKEN_LIMIT}, 
            on_conflict="id"
        )
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

# --- RESEND EMAIL LOGIC ---
def send_otp_email(to_email: str, code: str):
    if not RESEND_API_KEY:
        print(f"⚠️ Resend Key missing. Code for {to_email}: {code}")
        return
    try:
        params = {
            "from": "Gor://a <talk@gorillabuilder.dev>", # Use your verified domain in production
            "to": [to_email],
            "subject": "Your Verification Code for Gor://a",
            "html": f"""
            <!DOCTYPE html>
            <html>
            <body style="margin: 0; padding: 0; background-color: #0b1020; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;">
            <div style="width: 100%; padding: 40px 0; background-color: #0b1020;">
                <div style="max-width: 420px; margin: 0 auto; background-color: #0f1530; padding: 40px; border-radius: 18px; color: #ffffff;">
                    
                    <h1 style="margin: 0 0 10px; font-size: 24px; font-weight: 400; letter-spacing: -0.3px;">Welcome to Gor://a</h1>
                    <p style="margin: 0 0 30px; opacity: 0.7; font-size: 14px; line-height: 1.5;">
                        Start building AI apps faster. Use the code below to verify your account.
                    </p>

                    <div style="background-color: #141a3a; padding: 20px; border-radius: 10px; text-align: center; margin-bottom: 30px;">
                        <span style="display: block; font-size: 12px; opacity: 0.5; margin-bottom: 5px; text-transform: uppercase; letter-spacing: 1px;">Verification Code</span>
                        <strong style="font-size: 32px; color: #3b6cff; letter-spacing: 5px;">{code}</strong>
                    </div>

                    <p style="font-size: 12px; opacity: 0.4; text-align: center; margin-top: 40px;">
                        If you didn't request this, you can safely ignore this email.
                    </p>
                </div>
            </div>
            </body>
            </html>
            """,
        }
        resend.Emails.send(params)
        print(f"✅ OTP sent to {to_email}")
    except Exception as e:
        print(f"❌ Resend Error: {e}")


# ==========================================================================
# PUBLIC ROUTES (Templates)
# ==========================================================================
PUBLIC_PAGES = {
    "/": "index.html",
    "/login": "auth/login.html",
    "/signup": "auth/signup.html", # Used for both steps
    "/forgot-password": "forgot.html",
    "/pricing": "freemium/pricing.html",
    "/checkout/tokens": "freemium/checkout/tokens.html",
    "/checkout/premium": "freemium/checkout/premium.html",
    "/help": "help.html",
    "/about": "about.html",
}

for route, template_name in PUBLIC_PAGES.items():
    def make_handler(t_name):
        async def handler(request: Request):
            return templates.TemplateResponse(t_name, {"request": request, "step": "initial"})
        return handler
        
    app.get(route, response_class=HTMLResponse)(make_handler(template_name))

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    p = os.path.join(FRONTEND_DIR, "assets", "favicon.png")
    if os.path.exists(p): 
        return FileResponse(p)
    raise HTTPException(status_code=404)


# ==========================================================================
# AUTH API ROUTES (UPDATED FOR 2FA)
# ==========================================================================

# STEP 1: INITIAL SIGNUP (Generates OTP)
@app.post("/auth/signup")
async def auth_signup_init(
    request: Request, 
    background_tasks: BackgroundTasks,
    email: str = Form(...), 
    password: str = Form(...)
):
    email = (email or "").strip().lower()
    
    # Generate 6-digit OTP
    otp = "".join(random.choices(string.digits, k=6))
    
    # Store in memory (Temporary)
    PENDING_SIGNUPS[email] = {
        "password": password,
        "otp": otp,
        "ts": time.time()
    }
    
    # Send Email via Resend
    background_tasks.add_task(send_otp_email, email, otp)
    
    # Render the SAME template, but switch step to 'verify'
    return templates.TemplateResponse(
        "auth/signup.html", 
        {
            "request": request, 
            "step": "verify", 
            "email": email
        }
    )

# STEP 2: VERIFY OTP (Creates User)
@app.post("/auth/verify")
async def auth_verify_otp(
    request: Request,
    email: str = Form(...),
    code: str = Form(...)
):
    email = email.strip().lower()
    record = PENDING_SIGNUPS.get(email)
    
    # Validation
    if not record:
        return templates.TemplateResponse("auth/signup.html", {"request": request, "step": "initial", "error": "Session expired. Please sign up again."})
    
    if record["otp"] != code:
        # Return to verify step with error
        return templates.TemplateResponse("auth/signup.html", {"request": request, "step": "verify", "email": email, "error": "Invalid code. Try again."})
    
    # OTP Valid: Create User in Supabase
    try:
        password = record["password"]
        
        # Check if user exists in Supabase Auth first
        try:
            created = supabase.auth.admin.create_user({
                "email": email,
                "password": password,
                "email_confirm": True
            })
            user_id = created.user.id
        except Exception:
            # If user already exists in Auth, just get ID (or fail if password wrong in login flow)
            user_id = _stable_user_id_for_email(email)

        # Create session
        request.session["user"] = {"id": user_id, "email": email}
        ensure_public_user(user_id, email)
        
        # Cleanup memory
        del PENDING_SIGNUPS[email]
        
        return RedirectResponse("/dashboard", status_code=303)
        
    except Exception as e:
        return templates.TemplateResponse("auth/signup.html", {"request": request, "step": "initial", "error": f"Error creating account: {e}"})

@app.post("/auth/login")
async def auth_login(request: Request, email: str = Form(...), password: str = Form(...)):
    email = (email or "").strip().lower()
    
    if DEV_MODE:
        user_id = _stable_user_id_for_email(email)
        request.session["user"] = {"id": user_id, "email": email}
        ensure_public_user(user_id, email)
        return RedirectResponse("/dashboard", status_code=303)
        
    try:
        # Attempt Supabase Login
        res = supabase.auth.sign_in_with_password({"email": email, "password": password})
        if res.user:
            request.session["user"] = {"id": res.user.id, "email": email}
            ensure_public_user(res.user.id, email)
            return RedirectResponse("/dashboard", status_code=303)
    except Exception:
        pass
        
    return RedirectResponse("/login?error=Invalid credentials", status_code=303)

@app.get("/auth/google")
async def auth_google(request: Request):
    """Initiates Google OAuth flow."""
    if not GOOGLE_CLIENT_ID or not GOOGLE_REDIRECT_URI:
        raise HTTPException(500, "Google Auth not configured (GOOGLE_CLIENT_ID/GOOGLE_REDIRECT_URI missing).")
        
    scope = "openid email profile"
    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?"
        f"response_type=code&client_id={GOOGLE_CLIENT_ID}&redirect_uri={GOOGLE_REDIRECT_URI}&scope={scope}"
    )
    return RedirectResponse(auth_url)

@app.get("/auth/google/callback")
async def auth_google_callback(request: Request, code: str):
    """Handles Google OAuth callback."""
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not GOOGLE_REDIRECT_URI:
        raise HTTPException(500, "Google Auth not configured.")
        
    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    
    async with httpx.AsyncClient() as client:
        res = await client.post(token_url, data=data)
        if res.status_code != 200:
             raise HTTPException(400, f"Google Auth failed: {res.text}")
        
        tokens = res.json()
        access_token = tokens.get("access_token")
        
        user_info_res = await client.get(
            "https://www.googleapis.com/oauth2/v1/userinfo", 
            headers={"Authorization": f"Bearer {access_token}"}
        )
        if user_info_res.status_code != 200:
            raise HTTPException(400, "Failed to fetch Google user info")
            
        user_data = user_info_res.json()
        email = user_data.get("email")
        
        if not email:
            raise HTTPException(400, "No email provided by Google.")
            
        user_id = _stable_user_id_for_email(email)
        
        request.session["user"] = {"id": user_id, "email": email}
        ensure_public_user(user_id, email)
        
        return RedirectResponse("/dashboard", status_code=303)

@app.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# ==========================================================================
# BILLING ROUTES (Mock Payment Processing)
# ==========================================================================
@app.post("/billing/process-premium")
async def process_premium(request: Request):
    """Simulate upgrading to Premium (5M tokens/mo)."""
    user = get_current_user(request)
    
    # Update Plan to premium and set limit to 5,000,000
    set_user_plan_and_limit(user["id"], "premium", 5000000)
    
    # Redirect to dashboard with success param?
    return RedirectResponse("/dashboard", status_code=303)

@app.post("/billing/process-tokens")
async def process_tokens(request: Request, amount: int = Form(...)):
    """Simulate buying one-time token top-up."""
    user = get_current_user(request)
    
    # "Top up" logic: We decrease 'tokens_used' by the purchased amount
    decrease_tokens_used(user["id"], amount)
    
    return RedirectResponse("/dashboard", status_code=303)


# ==========================================================================
# DASHBOARD & WORKSPACE
# ==========================================================================
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_current_user(request)
    
    # --- FIX START: Fetch latest Plan from DB ---
    try:
        db_user = db_select_one("users", {"id": user["id"]}, "plan")
        if db_user:
            user["plan"] = db_user.get("plan", "free")
    except Exception:
        user["plan"] = "free"
    # --- FIX END ---
    
    # Token Data
    used, limit = get_token_usage_and_limit(user["id"])
    user["tokens"] = {
        "used": used, 
        "limit": limit, 
        "remaining": max(0, limit - used)
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
    used, limit = get_token_usage_and_limit(user["id"])
    user["tokens"] = {"used": used, "limit": limit}

    try:
        res = (
            supabase.table("projects")
            .select("id,name,updated_at")
            .eq("owner_id", user["id"])
            .order("updated_at", desc=True)
            .execute()
        )
        projects = res.data if res and res.data else []
    except Exception:
        projects = []
        
    return templates.TemplateResponse(
        "dashboard/workspace.html",
        {"request": request, "projects": projects, "user": user}
    )


# ==========================================================================
# PROJECT ROUTES
# ==========================================================================
import io
import zipfile

# --- UPDATED CREATE ROUTE FOR DASHBOARD PROMPTS ---
@app.post("/projects/create")
async def create_project(
    request: Request, 
    prompt: Optional[str] = Form(None), 
    name: Optional[str] = Form(None),
    description: str = Form("")
):
    user = get_current_user(request)
    ensure_public_user(user["id"], user.get("email") or "unknown@local")

    # 1. Determine Project Name
    # If using the dashboard input, 'name' is empty but 'prompt' has text.
    project_name = name
    if not project_name:
        if prompt:
             # Auto-generate name based on timestamp
             project_name = f"Project {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        else:
             project_name = "Untitled Project"
    
    # 2. Insert into DB
    try:
        # We store the initial prompt in the description for now, or just keep it for the redirects
        desc_to_save = description or (prompt[:200] if prompt else "")
        
        res = (
            supabase.table("projects")
            .insert({"owner_id": user["id"], "name": project_name, "description": desc_to_save})
            .execute()
        )
        if not res or not res.data:
            raise Exception("No data returned from insert")
            
        project = res.data[0]
        pid = project['id']
        
        # 3. Redirect to Editor
        # If we have a prompt, pass it as a query param so the frontend can read it and auto-start.
        target_url = f"/projects/{pid}/editor"
        if prompt:
            # Safe URL encoding
            safe_prompt = urllib.parse.quote(prompt)
            target_url += f"?prompt={safe_prompt}"
            
        return RedirectResponse(target_url, status_code=303)
        
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
    
    # --- FIX START: Fetch latest Plan from DB ---
    try:
        db_user = db_select_one("users", {"id": user["id"]}, "plan")
        if db_user:
            user["plan"] = db_user.get("plan", "free")
    except Exception:
        pass
    # --- FIX END ---
    
    used, limit = get_token_usage_and_limit(user["id"])
    user["tokens"] = {"used": used, "limit": limit}

    return templates.TemplateResponse(
        "projects/project-editor.html",
        {"request": request, "project_id": project_id, "file": file, "user": user}
    )

@app.get("/projects/{project_id}/xmode", response_class=HTMLResponse)
async def project_xmode(request: Request, project_id: str, file: str = "index.html"):
    """
    Same as editor but activates 'xmode' (Purple Theme).
    """
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    try:
        db_user = db_select_one("users", {"id": user["id"]}, "plan")
        if db_user:
            user["plan"] = db_user.get("plan", "free")
    except Exception:
        pass
    
    used, limit = get_token_usage_and_limit(user["id"])
    user["tokens"] = {"used": used, "limit": limit}

    return templates.TemplateResponse(
        "projects/project-editor.html",
        {
            "request": request, 
            "project_id": project_id, 
            "file": file, 
            "user": user,
            "xmode": True
        }
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

@app.get("/api/project/{project_id}/export")
async def project_export(request: Request, project_id: str):
    """
    Premium Feature: Export project files as a ZIP archive.
    """
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    # 1. Verify Premium Status (Double-check DB to ensure recent upgrade is caught)
    user_record = db_select_one("users", {"id": user["id"]}, "plan")
    current_plan = user_record.get("plan") if user_record else "free"
    
    if current_plan != "premium":
        raise HTTPException(status_code=403, detail="Exporting to ZIP is a Premium feature. Please upgrade.")

    # 2. Fetch all files for the project
    res = (
        supabase.table("files")
        .select("path,content")
        .eq("project_id", project_id)
        .execute()
    )
    files = res.data if res and res.data else []

    if not files:
        raise HTTPException(status_code=404, detail="No files found in this project.")

    # 3. Create ZIP in memory
    zip_buffer = io.BytesIO()
    try:
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in files:
                # Ensure path is clean
                path = file.get("path", "unknown.txt").strip("/")
                content = file.get("content") or ""
                # Write file to zip
                zf.writestr(path, content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create ZIP: {e}")

    # 4. Return as downloadable file
    zip_buffer.seek(0)
    filename = f"gorilla_project_{project_id[:8]}.zip"
    
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Cache-Control": "no-cache"
        }
    )


# ==========================================================================
# FILE API ROUTES
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
    supabase.table("projects").update({"updated_at": "now()"}).eq("id", project_id).execute()
    return {"success": True}

@app.get("/api/project/{project_id}/tokens")
async def check_tokens(request: Request, project_id: str):
    user = get_current_user(request)
    used, limit = get_token_usage_and_limit(user["id"])
    return {"used": used, "limit": limit}


# ==========================================================================
# STATIC FILE SERVING
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
            try: q.put_nowait(event)
            except Exception: pass

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
# AI AGENT WORKFLOW
# ==========================================================================
async def _fetch_file_tree(project_id: str) -> Dict[str, str]:
    res = (
        supabase.table("files")
        .select("path,content")
        .eq("project_id", project_id)
        .execute()
    )
    rows = res.data if res and res.data else []
    return {r["path"]: (r.get("content") or "") for r in rows}

# --- NEW: Internal Robust Starter Function ---
async def _start_server_with_retry(project_id: str):
    """
    Internal robust server starter.
    Loops automatically to catch crashes, fix them via AI, and restart.
    Emits events to the frontend so the user sees 'Booting -> Crash -> Fixing -> Retry'.
    """
    emit_status(project_id, "Booting server...")
    
    max_retries = 8
    history = []
    
    # Initial attempt to start
    try:
        file_tree = await _fetch_file_tree(project_id)
        info = await run_manager.start(project_id, file_tree)
        emit_status(project_id, "Server Running")
        emit_log(project_id, "system", f"Server running on port {info.port}")
        emit_progress(project_id, "Ready", 100)
        return # Success on first try
    except Exception as e:
        # Initial crash detected, enter fix loop
        err_msg = str(e)
        clean_error = err_msg.replace("App crashed during startup:", "").strip()
        emit_status(project_id, "⚠️ Startup Failed. Auto-fixing...")
        emit_log(project_id, "system", f"Crash: {clean_error[:200]}...")
    
    # Enter Retry Loop
    coder = Coder()
    
    for attempt in range(max_retries):
        emit_phase(project_id, "fixing")
        emit_progress(project_id, f"Auto-Fixing (Attempt {attempt+1}/{max_retries})", 50)
        
        try:
            # 1. Fetch latest code (it changes every loop)
            file_tree = await _fetch_file_tree(project_id)
            
            # 2. Generate Fix
            fix_prompt = (
                f"The application crashed during startup (Attempt {attempt+1}/{max_retries}) with this error:\n\n{clean_error}\n\n"
                "Fix the code (app.py, requirements.txt, etc) to resolve this startup error. "
                "If a module is missing, add it to requirements.txt."
            )
            
            res = await coder.generate_code(
                plan_section="Startup Fix",
                plan_text=fix_prompt,
                file_tree=file_tree,
                project_name=project_id,
                history=history
            )
            
            if res.get("message"):
                history.append({"role": "assistant", "content": res.get("message")})
                emit_log(project_id, "coder", f"Applying fix: {res.get('message')}")
            
            # 3. Apply Fixes
            ops = res.get("operations", [])
            for op in ops:
                p = op.get("path")
                c = op.get("content")
                if p and c:
                    db_upsert("files", {"project_id": project_id, "path": p, "content": c}, on_conflict="project_id,path")
                    emit_file_changed(project_id, p)
            
            # 4. Retry Start
            emit_status(project_id, f"Restaring Server (Attempt {attempt+1})...")
            
            # Refresh tree for start
            new_tree = await _fetch_file_tree(project_id)
            
            info = await run_manager.start(project_id, new_tree)
            
            # If we get here, it started!
            emit_status(project_id, "Server Fixed & Running")
            emit_log(project_id, "system", f"Server running on port {info.port}")
            emit_progress(project_id, "Ready", 100)
            return
            
        except Exception as retry_err:
            # Fix failed or Server crashed again
            err_str = str(retry_err)
            if "App crashed" in err_str:
                 clean_error = err_str.replace("App crashed during startup:", "").strip()
            else:
                 clean_error = err_str
            
            emit_log(project_id, "system", f"Fix attempt failed: {clean_error[:100]}...")
            history.append({"role": "user", "content": f"That fix didn't work. The server crashed again with: {clean_error}"})
            await asyncio.sleep(1) 

    emit_status(project_id, "❌ Auto-fix failed after max retries.")


@app.post("/api/project/{project_id}/agent/start")
async def agent_start(
    request: Request, 
    project_id: str, 
    prompt: str = Form(...),
    xmode: bool = Form(False)
):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    # 1. Enforce Token Limit
    enforce_token_limit_or_raise(user["id"])
    
    emit_status(project_id, "Agent received prompt")
    emit_log(project_id, "user", prompt)

    async def _run():
        total_run_tokens = 0
        try:
            await asyncio.sleep(0.5)
            emit_progress(project_id, "Reading project files...", 5)
            file_tree = await _fetch_file_tree(project_id)
            
            planner = Planner()
            
            # CHOOSE CODER BASED ON XMODE
            if xmode:
                emit_status(project_id, "🔮 X-MODE ENGAGED")
                coder = XCoder()
            else:
                coder = Coder()
            
            # --- PHASE 1: PLANNER ---
            emit_phase(project_id, "planner")
            emit_progress(project_id, "Planning...", 10)
            
            plan_res = planner.generate_plan(
                user_request=prompt, 
                project_context={"project_id": project_id, "files": list(file_tree.keys())}
            )
            
            ptokens = plan_res.get("usage", {}).get("total_tokens", 0)
            if int(ptokens) > 0:
                total_run_tokens += int(ptokens)
                new_total = add_monthly_tokens(user["id"], int(ptokens))
                emit_token_update(project_id, new_total)
            
            assistant_msg = plan_res.get("assistant_message")
            if assistant_msg:
                emit_log(project_id, "assistant", assistant_msg)
            
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
                enforce_token_limit_or_raise(user["id"])
                
                pct = 10 + (90 * (i / total))
                emit_progress(project_id, f"Building step {i}/{total}...", pct)
                emit_status(project_id, f"Implementing task {i}/{total}...")
                
                code_res = await coder.generate_code(
                    plan_section="Implementation",
                    plan_text=task,
                    file_tree=file_tree,
                    project_name=project_id
                )
                
                ctokens = code_res.get("usage", {}).get("total_tokens", 0)
                if ctokens > 0:
                    total_run_tokens += ctokens
                    new_total = add_monthly_tokens(user["id"], ctokens)
                    emit_token_update(project_id, new_total)

                coder_msg = code_res.get("message")
                if coder_msg:
                    emit_log(project_id, "coder", coder_msg)

                ops = code_res.get("operations", [])
                emit_phase(project_id, "files")
                
                for op in ops:
                    path = op.get("path")
                    content = op.get("content")
                    if path and content is not None:
                        db_upsert(
                            "files", 
                            {"project_id": project_id, "path": path, "content": content}, 
                            on_conflict="project_id,path"
                        )
                        emit_file_changed(project_id, path)
                
                file_tree = await _fetch_file_tree(project_id)
            
            # --- FINISH CODING ---
            emit_status(project_id, "Coding Complete. Starting Server...")
            emit_progress(project_id, "Booting...", 100)
            
            # --- PHASE 3: AUTO-START WITH RETRY ---
            await _start_server_with_retry(project_id)
            
        except Exception as e:
            if total_run_tokens > 0:
                add_monthly_tokens(user["id"], 0)
            
            emit_status(project_id, "Error")
            emit_log(project_id, "system", f"Workflow failed: {e}")
            print(traceback.format_exc())

    asyncio.create_task(_run())
    return {"started": True}

@app.get("/api/project/{project_id}/events")
async def agent_events(request: Request, project_id: str):
    if not DEV_MODE:
        user = get_current_user(request)
        _require_project_owner(user, project_id)

    async def _gen():
        q = progress_bus.subscribe(project_id)
        try:
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
        
    # Trigger the robust starter in background so request returns fast
    # but the user gets SSE updates
    asyncio.create_task(_start_server_with_retry(project_id))
    
    return {"ok": True, "status": "starting"}

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
        return Response(
            content=r.content,
            status_code=r.status_code,
            headers={k: v for k, v in r.headers.items() if k.lower() not in ("content-encoding", "transfer-encoding", "connection")},
            media_type=r.headers.get("content-type")
        )
    except RuntimeError as e:
        err_msg = str(e)
        
        if "CRASH_DETECTED" in err_msg:
            clean_error = err_msg.replace("CRASH_DETECTED:", "").strip()
            
            # --- CASE 1: SERVER OFFLINE (BUILDING) ---
            if "Server not running" in clean_error or "Connection refused" in clean_error:
                loading_path = os.path.join(FRONTEND_TEMPLATES_DIR, "agentloading.html")
                loading_html = "<h2>🚧 Building...</h2><p>The agent is working on your app.</p>"
                if os.path.exists(loading_path):
                    with open(loading_path, "r", encoding="utf-8") as f:
                        loading_html = f.read()
                return HTMLResponse(loading_html, status_code=200)

            # --- CASE 2: RUNTIME CRASH (AUTO-FIX) ---
            # NOTE: We can trigger the same robust starter here!
            print(f"🔥 RUNTIME AUTO-FIX TRIGGERED for {project_id}")
            asyncio.create_task(_start_server_with_retry(project_id))
            
            loading_path = os.path.join(FRONTEND_TEMPLATES_DIR, "agentloading.html")
            loading_html = "<h2>💥 App Crashed</h2><p>AI is analyzing and fixing the code...</p>"
            if os.path.exists(loading_path):
                with open(loading_path, "r", encoding="utf-8") as f:
                    loading_html = f.read()
            
            return HTMLResponse(loading_html, status_code=200)

        raise HTTPException(502, f"Proxy failed: {e}")

@app.get("/projects/{project_id}/game", response_class=HTMLResponse)
async def project_game(request: Request, project_id: str):
    """
    Renders the Waiting Room Game (Snake).
    """
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    try:
        db_user = db_select_one("users", {"id": user["id"]}, "plan")
        if db_user:
            user["plan"] = db_user.get("plan", "free")
    except Exception:
        pass

    return templates.TemplateResponse(
        "projects/game.html",
        {"request": request, "project_id": project_id, "user": user}
    )

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