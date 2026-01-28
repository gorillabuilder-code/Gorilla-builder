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
import re
import urllib.parse
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Set

import httpx
import resend
from fastapi import FastAPI, Request, HTTPException, Form, BackgroundTasks
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
    FileResponse,
    JSONResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
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
from backend.deployer import Deployer

# ==========================================================================
# CONSTANTS & PATHS
# ==========================================================================
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")
FRONTEND_TEMPLATES_DIR = os.path.join(FRONTEND_DIR, "templates")
FRONTEND_STYLES_DIR = os.path.join(FRONTEND_DIR, "styles")

# 👇 DEFINING BOILERPLATE DIR (Fixes the Error) 👇
# Checks if it's in 'backend/boilerplate' (Root structure) or just 'boilerplate' (Nested structure)
if os.path.isdir(os.path.join(ROOT_DIR, "backend", "boilerplate")):
    BOILERPLATE_DIR = os.path.join(ROOT_DIR, "backend", "boilerplate")
else:
    BOILERPLATE_DIR = os.path.join(ROOT_DIR, "boilerplate")

if not os.path.isdir(FRONTEND_DIR):
    # Adjust for relative path if running from root but folders are deeper
    ROOT_DIR = os.path.dirname(ROOT_DIR)
    FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")
    FRONTEND_TEMPLATES_DIR = os.path.join(FRONTEND_DIR, "templates")
    FRONTEND_STYLES_DIR = os.path.join(FRONTEND_DIR, "styles")

DEV_MODE = os.getenv("DEV_MODE", "1") == "1"

# --- UPDATED: DEFAULT LIMIT IS NOW 250k ---
DEFAULT_TOKEN_LIMIT = int(os.getenv("MONTHLY_TOKEN_LIMIT", "250000"))

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

# --- GLOBAL STATE FOR RUNTIME MANAGEMENT ---
# [CRITICAL FIX] Track projects currently booting to prevent Race Conditions
_BOOTING_PROJECTS: Set[str] = set()
_LAST_ACCESS: Dict[str, float] = {} # project_id -> timestamp
SHUTDOWN_TIMEOUT_SECONDS = 600 # 10 Minutes

# ==========================================================================
# APP INITIALIZATION & LIFECYCLE
# ==========================================================================
app = FastAPI(title="GOR://A Backend ASGI")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("AUTH_SECRET_KEY", secrets.token_hex(32)),
)

if os.path.isdir(FRONTEND_STYLES_DIR):
    app.mount("/styles", StaticFiles(directory=FRONTEND_STYLES_DIR), name="styles")

templates = Jinja2Templates(directory=FRONTEND_TEMPLATES_DIR)

# --- BACKGROUND TASK: CLEANUP INACTIVE SANDBOXES ---
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(_monitor_inactivity())

async def _monitor_inactivity():
    """Runs every 60s to kill sandboxes unused for >10 mins."""
    while True:
        try:
            now = time.time()
            # Copy keys to avoid modification during iteration
            active_projects = list(_LAST_ACCESS.keys())
            
            for pid in active_projects:
                last_active = _LAST_ACCESS.get(pid, 0)
                
                # If idle for too long
                if (now - last_active) > SHUTDOWN_TIMEOUT_SECONDS:
                    # Check if actually running before stopping
                    # Note: We reference run_manager later, ensuring it's initialized
                    try:
                        if "run_manager" in globals():
                            is_running, _ = run_manager.is_running(pid)
                            if is_running:
                                print(f"💤 Idle Cleanup: Stopping project {pid} (Inactive > 10m)")
                                await run_manager.stop(pid)
                    except: pass
                    
                    # Stop tracking it
                    _LAST_ACCESS.pop(pid, None) 
                    
            await asyncio.sleep(60) # Check every minute
        except Exception as e:
            print(f"Inactivity Monitor Error: {e}")
            await asyncio.sleep(60)


# ==========================================================================
# EXCEPTION HANDLERS
# ==========================================================================
@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc):
    if exc.status_code == 404:
        # Check if template exists, fallback if not
        if os.path.exists(os.path.join(FRONTEND_TEMPLATES_DIR, "404.html")):
             return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
        return HTMLResponse("<h1>404 - Not Found</h1>", status_code=404)
    
    # For other errors, return JSON
    return JSONResponse({"detail": str(exc.detail)}, status_code=exc.status_code)


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
            "from": "Gor://a Auth Verification <auth@gorillabuilder.dev>", # Use your verified domain in production
            "to": [to_email],
            "subject": "Your Verification Code for Gor://a Builder",
            "html": f"""
            <!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Verification Email</title>

</head>
<body style="margin: 0; padding: 0; background-color: #0b1020; font-family: Tahoma, Geneva, Verdana, sans-serif;">
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
    "/": "landing/index.html",
    "/login": "auth/login.html",
    "/signup": "auth/signup.html", # Used for both steps
    "/forgot-password": "forgot.html",
    "/pricing": "freemium/pricing.html",
    "/checkout/tokens": "freemium/checkout/tokens.html",
    "/checkout/premium": "freemium/checkout/premium.html",
    "/help": "help.html",
    "/about": "about.html",
}
app.mount("/assets", StaticFiles(directory="frontend/templates/landing/assets"), name="assets")

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
# 📚 DOCUMENTATION & COMPANY ROUTES
# ==========================================================================

# 1. Master Route for Docs
@app.get("/docs/{page}", response_class=HTMLResponse)
async def docs_page(request: Request, page: str):
    valid_pages = [
        "intro", "dashboard", "billing", 
        "prompting", "editor", "agent-workflow", "files",
        "x-mode", "deployment", "troubleshooting",
        "about", "contact" # Mapped here for sidebar convenience
    ]
    
    if page not in valid_pages:
        # Default to intro if page is invalid, or 404
        return RedirectResponse("/docs/intro")
        
    return templates.TemplateResponse(
        f"docs/{page}.html", 
        {"request": request, "page": page}
    )
@app.get("/docs", response_class=HTMLResponse)
async def about_page(request: Request):
    return templates.TemplateResponse("docs/intro.html", {"request": request, "page": "about"})

# 2. Direct Shortcuts for About/Contact
@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    return templates.TemplateResponse("docs/about.html", {"request": request, "page": "about"})

@app.get("/contact", response_class=HTMLResponse)
async def contact_page(request: Request):
    return templates.TemplateResponse("docs/contact.html", {"request": request, "page": "contact"})

# 3. Redirect /docs root to intro
@app.get("/docs")
async def docs_root():
    return RedirectResponse("/docs/intro")

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
    
    # Fetch latest Plan from DB
    try:
        db_user = db_select_one("users", {"id": user["id"]}, "plan")
        if db_user:
            user["plan"] = db_user.get("plan", "free")
    except Exception:
        user["plan"] = "free"
    
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

# 1. CREATE PAGE (Stash prompt in session)
@app.get("/projects/createit", response_class=HTMLResponse)
async def project_create_page(request: Request, prompt: Optional[str] = None):
    user = get_current_user(request)
    if prompt:
        request.session["stashed_prompt"] = prompt
        
    return templates.TemplateResponse(
        "projects/project-create.html", 
        {"request": request, "user": user, "initial_prompt": prompt}
    )

# 2. CREATE ACTION
@app.post("/projects/create")
async def create_project(
    request: Request, 
    prompt: Optional[str] = Form(None), 
    name: Optional[str] = Form(None), 
    description: str = Form("")
):
    user = get_current_user(request)

    if prompt and not name:
        return RedirectResponse(f"/projects/createit?prompt={urllib.parse.quote(prompt)}", status_code=303)
    
    final_prompt = prompt or request.session.pop("stashed_prompt", None)
    project_name = name or "Untitled Project"
    
    def _heavy_lift_create():
        # A. Create Project Record
        res = supabase.table("projects").insert({
            "owner_id": user["id"], 
            "name": project_name, 
            "description": description or (final_prompt[:200] if final_prompt else "")
        }).execute()
        
        if not res.data: raise Exception("DB Insert Failed")
        pid = res.data[0]['id']
        
        # B. Setup Subdomain
        clean_name = re.sub(r'[^a-z0-9-]', '-', project_name.lower()).strip('-') or "app"
        final_subdomain = f"{clean_name}-{pid}" 
        supabase.table("projects").update({"subdomain": final_subdomain}).eq("id", pid).execute()
        
        # C. Inject Boilerplate (OPTIMIZED)
        bp_dir = globals().get("BOILERPLATE_DIR")
        if not bp_dir or not os.path.isdir(bp_dir):
            bp_dir = os.path.join(ROOT_DIR, "backend", "boilerplate")
            if not os.path.isdir(bp_dir):
                bp_dir = os.path.join(ROOT_DIR, "boilerplate")

        if os.path.isdir(bp_dir):
            files_to_insert = []
            
            for root, dirs, files in os.walk(bp_dir):
                # Skip massive folders
                dirs[:] = [d for d in dirs if d not in ["node_modules", ".git", "dist", "build"]]
                
                for file in files:
                    if file.startswith('.'): continue
                    
                    abs_path = os.path.join(root, file)
                    rel_path = os.path.relpath(abs_path, bp_dir).replace("\\", "/")
                    
                    try:
                        with open(abs_path, "r", encoding="utf-8") as f:
                            content = f.read()
                            files_to_insert.append({
                                "project_id": pid,
                                "path": rel_path,
                                "content": content
                            })
                    except Exception: 
                        continue 

            if files_to_insert:
                try:
                    # Batch insert for speed
                    supabase.table("files").insert(files_to_insert).execute()
                except Exception:
                    # Retry individually on failure
                    for f in files_to_insert:
                        try: supabase.table("files").upsert(f, on_conflict="project_id,path").execute()
                        except: pass
        
        return pid

    # Execute Async
    try:
        pid = await asyncio.to_thread(_heavy_lift_create)
        
        target_url = f"/projects/{pid}/editor"
        if final_prompt: 
            target_url += f"?prompt={urllib.parse.quote(final_prompt)}"
        return RedirectResponse(target_url, status_code=303)
        
    except Exception as e:
        print(f"Create Error: {e}")
        return RedirectResponse("/dashboard?error=creation_failed", status_code=303)
            
@app.get("/projects/{project_id}/editor", response_class=HTMLResponse)
async def project_editor(request: Request, project_id: str, file: str = "index.html", prompt: Optional[str] = None):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    try:
        res = supabase.table("projects").select("*").eq("id", project_id).single().execute()
        project = res.data
    except Exception:
        project = {} 
    
    used, limit = get_token_usage_and_limit(user["id"])
    user["tokens"] = {"used": used, "limit": limit}

    return templates.TemplateResponse(
        "projects/project-editor.html",
        {
            "request": request, 
            "project_id": project_id, 
            "project": project, 
            "file": file, 
            "user": user,
            "initial_prompt": prompt
        }
    )
    
@app.get("/projects/{project_id}/xmode", response_class=HTMLResponse)
async def project_xmode(request: Request, project_id: str, file: str = "index.html"):
    """
    Same as editor but activates 'xmode' (Purple Theme).
    """
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    # [FIX] Initialize variable first to prevent NameError
    project = {}
    
    try:
        res = supabase.table("projects").select("*").eq("id", project_id).single().execute()
        if res.data:
            project = res.data
    except Exception:
        print(f"⚠️ Failed to load project details for {project_id}")
    
    used, limit = get_token_usage_and_limit(user["id"])
    user["tokens"] = {"used": used, "limit": limit}

    return templates.TemplateResponse(
        "projects/project-editor.html",
        {
            "request": request, 
            "project_id": project_id, 
            "project": project, # Now guaranteed to exist
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
        project = None
        
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
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    user_record = db_select_one("users", {"id": user["id"]}, "plan")
    current_plan = user_record.get("plan") if user_record else "free"
    
    if current_plan != "premium":
        raise HTTPException(status_code=403, detail="Exporting to ZIP is a Premium feature. Please upgrade.")

    res = (
        supabase.table("files")
        .select("path,content")
        .eq("project_id", project_id)
        .execute()
    )
    files = res.data if res and res.data else []

    if not files:
        raise HTTPException(status_code=404, detail="No files found in this project.")

    zip_buffer = io.BytesIO()
    try:
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in files:
                path = file.get("path", "unknown.txt").strip("/")
                content = file.get("content") or ""
                zf.writestr(path, content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create ZIP: {e}")

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
# EVENT BUS & UTILS
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
# GATEKEEPER: LINTING
# ==========================================================================
def lint_code_with_esbuild(content: str, filename: str) -> str | None:
    """Runs esbuild to check for syntax errors. (Blocking)"""
    if not filename.startswith("static/") or not filename.endswith(".js"):
        return None

    try:
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False, mode='w', encoding='utf-8') as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        result = subprocess.run(
            ["npx", "esbuild", tmp_path, "--loader=jsx", "--format=esm", "--log-level=error"],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        os.remove(tmp_path)

        if result.returncode != 0:
            return result.stderr.strip()
            
        return None
    except Exception as e:
        print(f"⚠️ Linter failed to run: {e}")
        return None


# ==========================================================================
# AI AGENT WORKFLOW (THE FIXER)
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

async def _start_server_with_retry(project_id: str, triggered_error: str = None):
    """
    Robust Starter:
    1. Marks as BOOTING.
    2. Runs AI fixes (if needed) in a thread.
    3. Starts server.
    """
    _BOOTING_PROJECTS.add(project_id)
    
    try:
        max_retries = 3
        current_error = triggered_error 
        history = []
        coder = Coder()

        for attempt in range(1, max_retries + 2):
            # Yield control to allow other requests (like dashboard nav) to process
            await asyncio.sleep(0.1)

            # --- PHASE A: FIXING ---
            if current_error:
                emit_phase(project_id, "fixing")
                emit_status(project_id, f"Analyzing Crash ({attempt})...")
                
                hint = ""
                if "ENOENT" in current_error or "no such file" in current_error:
                    hint = "\nHint: Server can't find a file. Check paths in server.js."
                elif "require is not defined" in current_error:
                    hint = "\nHint: 'require' is not supported. Use 'import' or delete server.js."

                fix_prompt = (
                    f"The app crashed with this error:\n\n{current_error}\n{hint}\n\n"
                    "Fix the code (server.js, package.json) to resolve this."
                )
                
                try:
                    # Fetch files async
                    file_tree = await _fetch_file_tree(project_id)
                    
                    # Run AI in thread (Non-blocking)
                    res = await asyncio.to_thread(
                        coder.generate_code,
                        plan_section="Crash Fix",
                        plan_text=fix_prompt,
                        file_tree=file_tree,
                        project_name=project_id,
                        history=history
                    )
                    
                    if res.get("message"):
                        history.append({"role": "assistant", "content": res.get("message")})

                    ops = res.get("operations", [])
                    for op in ops:
                        p = op.get("path"); c = op.get("content")
                        if p and c:
                            db_upsert("files", {"project_id": project_id, "path": p, "content": c}, on_conflict="project_id,path")
                            emit_file_changed(project_id, p)
                    
                    emit_log(project_id, "coder", "Fix applied. Retrying start...")
                    current_error = None
                    
                except Exception as e:
                    print(f"Fixing failed: {e}")

            # --- PHASE B: STARTING ---
            try:
                if attempt == 1 and not current_error:
                     emit_status(project_id, "Booting server...")
                else:
                     emit_status(project_id, "Restarting server...")

                # Yield control again before heavy lifting
                await asyncio.sleep(0.1)

                file_tree = await _fetch_file_tree(project_id)
                info = await run_manager.start(project_id, file_tree)
                
                emit_status(project_id, "Server Running")
                emit_progress(project_id, "Ready", 100)
                return

            except Exception as e:
                err_str = str(e)
                
                if "E2B" in err_str or "API Key" in err_str:
                    emit_status(project_id, "System Error")
                    emit_log(project_id, "system", f"Infrastructure Error: {err_str}")
                    return

                clean_error = err_str.replace("App crashed during startup:", "").strip()
                emit_log(project_id, "system", f"Crash detected: {clean_error[:100]}...")
                
                current_error = clean_error
                await asyncio.sleep(1) 

        emit_status(project_id, "❌ Auto-fix failed.")
        
    finally:
        _BOOTING_PROJECTS.discard(project_id)

@app.post("/api/project/{project_id}/agent/start")
async def agent_start(
    request: Request, 
    project_id: str, 
    prompt: str = Form(...),
    xmode: bool = Form(False)
):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    # --- [FIX] RELAY TOKEN ERRORS TO CHAT ---
    try:
        enforce_token_limit_or_raise(user["id"])
    except HTTPException as e:
        if e.status_code == 402:
            # Emit the error as a chat message so the user sees it
            emit_log(
                project_id, 
                "assistant", 
                "🛑 <b>You have run out of credits.</b><br>"
                "Please <a href='/pricing' target='_blank' style='color:#ffd700; text-decoration:underline; font-weight:bold;'>Upgrade to Pro</a> to continue building."
            )
            return {"started": False}
        raise e
    # ----------------------------------------
    
    emit_status(project_id, "Agent received prompt")
    emit_log(project_id, "user", prompt)

    async def _run():
        nonlocal prompt
        total_run_tokens = 0
        try:
            await asyncio.sleep(0.5)
            file_tree = await _fetch_file_tree(project_id)
            
            # [SELF-HEALING] Check for runtime errors
            try:
                projects_dir = os.path.join(ROOT_DIR, "projects")
                error_log_path = os.path.join(projects_dir, project_id, "server_errors.txt")
                if os.path.exists(error_log_path):
                    with open(error_log_path, 'r', encoding="utf-8") as f:
                        errors = f.read().strip()
                        if errors:
                            prompt += f"\n\n[CRITICAL RUNTIME ERRORS DETECTED]\n{errors}\n"
                            emit_log(project_id, "system", "🩺 Auto-Healing: Found crash logs.")
                    with open(error_log_path, 'w', encoding="utf-8") as f: f.write("")
            except: pass

            planner = Planner()
            coder = XCoder() if xmode else Coder()
            
            # --- PHASE 1: PLANNER ---
            emit_phase(project_id, "planner")
            emit_progress(project_id, "Planning...", 10)
            
            plan_res = await asyncio.to_thread(
                planner.generate_plan,
                user_request=prompt, 
                project_context={"project_id": project_id, "files": list(file_tree.keys())}
            )
            
            tk = plan_res.get("usage", {}).get("total_tokens", 0)
            if tk: add_monthly_tokens(user["id"], tk)
            
            tasks = plan_res["plan"].get("todo", [])
            if not tasks:
                emit_status(project_id, "Response Complete")
                emit_progress(project_id, "Done", 100)
                return

            # --- PHASE 2: CODER ---
            emit_phase(project_id, "coder")
            total = len(tasks)
            
            for i, task in enumerate(tasks, 1):
                # Check tokens again before every step
                try:
                    enforce_token_limit_or_raise(user["id"])
                except HTTPException:
                    emit_log(project_id, "system", "⚠️ Token limit reached mid-generation. Stopping.")
                    return

                pct = 10 + (90 * (i / total))
                emit_progress(project_id, f"Building step {i}/{total}...", pct)
                emit_status(project_id, f"Implementing task {i}/{total}...")
                
                code_res = await coder.generate_code(
                    plan_section="Implementation",
                    plan_text=task,
                    file_tree=file_tree,
                    project_name=project_id
                )
                
                tk = code_res.get("usage", {}).get("total_tokens", 0)
                if tk: add_monthly_tokens(user["id"], tk)

                if code_res.get("message"):
                    emit_log(project_id, "coder", code_res.get("message"))

                ops = code_res.get("operations", [])
                emit_phase(project_id, "files")
                
                for op in ops:
                    path = op.get("path")
                    content = op.get("content")
                    
                    if path and content is not None:
                        # LINTING
                        if path.startswith("static/") and path.endswith(".js"):
                            try:
                                lint_err = await asyncio.to_thread(lint_code_with_esbuild, content, path)
                                if lint_err:
                                    emit_log(project_id, "system", f"❌ Syntax Error in {path}:\n{lint_err}")
                                    raise Exception(f"Syntax Error in {path}: {lint_err}")
                            except Exception as e:
                                if "Syntax Error" in str(e): raise e

                        db_upsert(
                            "files", 
                            {"project_id": project_id, "path": path, "content": content}, 
                            on_conflict="project_id,path"
                        )
                        emit_file_changed(project_id, path)
                
                file_tree = await _fetch_file_tree(project_id)
            
            # --- FINISH ---
            emit_status(project_id, "Coding Complete. Starting Server...")
            emit_progress(project_id, "Booting...", 100)
            
            # Trigger clean boot (no error passed)
            await _start_server_with_retry(project_id)
            
        except Exception as e:
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
# MIDDLEWARE: ASSET RESCUE (CRITICAL FOR REACT/VITE)
# ==========================================================================
@app.middleware("http")
async def asset_rescue_middleware(request: Request, call_next):
    """
    catches 404s for assets like '/src/main.tsx' or '/vite.svg'
    and redirects them to the correct project preview URL.
    """
    response = await call_next(request)
    
    if response.status_code == 404:
        path = request.url.path
        
        # If it looks like a file extension (js, css, png, svg, json, tsx)
        if "." in path.split("/")[-1]: 
            referer = request.headers.get("referer")
            
            if referer:
                # Case 1: Request from DEV PREVIEW (/run/{id})
                match_run = re.search(r"/run/([a-zA-Z0-9-]+)", referer)
                if match_run:
                    project_id = match_run.group(1)
                    # Redirect /src/main.tsx -> /run/{id}/src/main.tsx
                    new_url = f"/run/{project_id}{path}"
                    return RedirectResponse(new_url)

                # Case 2: Request from PUBLIC APP (/app/{slug})
                match_app = re.search(r"/app/([a-zA-Z0-9-]+)", referer)
                if match_app:
                    slug = match_app.group(1)
                    new_url = f"/app/{slug}{path}"
                    return RedirectResponse(new_url)
                    
    return response


# ==========================================================================
# SERVER PREVIEW RUNNER & PROXY
# ==========================================================================
run_manager = ProjectRunManager()
deployer = Deployer(run_manager, supabase) 

@app.post("/api/project/{project_id}/run/start")
async def run_start(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    if not DEV_MODE: 
        raise HTTPException(403, "Server preview is DEV_MODE only.")
        
    # Check if already booting to prevent double-clicks
    if project_id in _BOOTING_PROJECTS:
        return {"ok": True, "status": "already_booting"}

    # Trigger the robust starter in background
    asyncio.create_task(_start_server_with_retry(project_id))
    
    # Touch access time
    _LAST_ACCESS[project_id] = time.time()
    
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
    booting = project_id in _BOOTING_PROJECTS
    return {"running": running, "port": port, "booting": booting}

@app.api_route("/run/{project_id}/{path:path}", methods=["GET","POST","PUT","DELETE","OPTIONS","PATCH"])
async def run_proxy(request: Request, project_id: str, path: str):
    """
    DEV MODE PROXY
    1. Proxies traffic IF the server is running.
    2. If booting -> Shows 'Booting'.
    3. If stopped -> Shows 'Server Offline' (Does NOT auto-start).
    """
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    if not DEV_MODE: raise HTTPException(403, "Dev only")

    _LAST_ACCESS[project_id] = time.time()
    
    # --- SCENARIO 1: CURRENTLY BOOTING ---
    if project_id in _BOOTING_PROJECTS:
        loading_path = os.path.join(FRONTEND_TEMPLATES_DIR, "agentloading.html")
        loading_html = "<h2>🚧 Booting...</h2><p>Server is starting up...</p>"
        if os.path.exists(loading_path):
            with open(loading_path, "r", encoding="utf-8") as f:
                loading_html = f.read()
        # Auto-refresh to check progress
        loading_html = loading_html.replace("<head>", '<head><meta http-equiv="refresh" content="2">', 1)
        return HTMLResponse(loading_html, status_code=200)

    # --- SCENARIO 2: RUNNING (Try to Proxy) ---
    try:
        r = await run_manager.proxy(
            project_id=project_id,
            path=path or "",
            method=request.method,
            headers=dict(request.headers),
            body=await request.body(),
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
        
        # --- SCENARIO 3: CRASH / STOPPED ---
        if "CRASH_DETECTED" in err_msg:
            clean_error = err_msg.replace("CRASH_DETECTED:", "").strip()
            
            # If it's just "Server not running", show the "Click Run" screen
            if "Server not running" in clean_error:
                return templates.TemplateResponse(
                    "projects/serverstopped.html", 
                    {"request": request, "project_id": project_id}
                )

            # If it's a REAL crash (connection failed while running), Auto-Fix it
            print(f"🔥 Proxy caught crash for {project_id}: {clean_error}")
            asyncio.create_task(_start_server_with_retry(project_id, triggered_error=clean_error))
            
            # Show "Fixing" Screen
            loading_path = os.path.join(FRONTEND_TEMPLATES_DIR, "agentloading.html")
            loading_html = "<h2>💥 Fixing...</h2><p>AI is resolving the crash...</p>"
            if os.path.exists(loading_path):
                with open(loading_path, "r", encoding="utf-8") as f:
                    loading_html = f.read()
            loading_html = loading_html.replace("<head>", '<head><meta http-equiv="refresh" content="3">', 1)
            return HTMLResponse(loading_html, status_code=200)

        raise HTTPException(502, f"Proxy failed: {e}")

@app.get("/projects/{project_id}/game", response_class=HTMLResponse)
async def project_game(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    return templates.TemplateResponse("projects/game.html", {"request": request, "project_id": project_id, "user": user})

@app.post("/api/project/{project_id}/agent/ping")
async def agent_ping(request: Request, project_id: str):
    emit_log(project_id, "system", "🔥 Pong from backend")
    return {"ok": True}


# ==========================================================================
# 🚀 PUBLIC APP HOSTING (Consolidated Route)
# ==========================================================================
@app.api_route("/app/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"])
async def public_app_catchall(request: Request, full_path: str):
    """
    Catches ALL traffic to /app/..., parses the slug, and routes it.
    """
    clean_path = full_path.strip("/")
    parts = clean_path.split("/", 1)
    
    if not clean_path:
        return HTMLResponse("<h1>404 - Not Found</h1>", status_code=404)
        
    project_slug = parts[0]
    remainder = parts[1] if len(parts) > 1 else ""

    # Redirect root to trailing slash for relative paths
    if len(parts) == 1 and not str(request.url.path).endswith("/"):
         return RedirectResponse(url=f"/app/{project_slug}/")

    try:
        # Access Tracking (Lazy)
        try:
             r = supabase.table("projects").select("id").eq("subdomain", project_slug).single().execute()
             if r.data:
                 _LAST_ACCESS[r.data['id']] = time.time()
        except: pass
            
        return await deployer.handle_request(request, project_slug, remainder)
        
    except RuntimeError as e:
        err_msg = str(e)
        if "CRASH_DETECTED" in err_msg:
            clean_error = err_msg.replace("CRASH_DETECTED:", "").strip()

            # --- LAZY LOADING ---
            try:
                res = supabase.table("projects").select("id").eq("subdomain", project_slug).single().execute()
                if res.data:
                    project_id = res.data['id']
                    
                    _LAST_ACCESS[project_id] = time.time()
                    
                    if project_id not in _BOOTING_PROJECTS:
                        print(f"🌍 Public Boot: {project_id}")
                        _BOOTING_PROJECTS.add(project_id)
                        task = asyncio.create_task(_start_server_with_retry(project_id, triggered_error=clean_error))
                        task.add_done_callback(lambda _: _BOOTING_PROJECTS.discard(project_id))
                    
                    return HTMLResponse("<html><head><meta http-equiv='refresh' content='3'></head><body><h2>Waking up app...</h2></body></html>")
            except Exception: pass
                
        raise HTTPException(502, "Service Unavailable")

@app.get("/health")
async def health():
    return {"ok": True, "ts": int(time.time()), "dev_mode": DEV_MODE}