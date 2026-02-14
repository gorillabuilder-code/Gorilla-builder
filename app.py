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

# Load Environment Variables
load_dotenv()

# --------------------------------------------------------------------------
# IMPORTS: Backend Modules
# --------------------------------------------------------------------------
# Ensure these files exist in your backend/ directory
from backend.run_manager import ProjectRunManager
from backend.ai.planner import Planner
from backend.ai.coder import Coder
# from backend.ai.Xcoder import XCoder # Uncomment if you have this file
from backend.deployer import Deployer

# ==========================================================================
# CONSTANTS & PATHS
# ==========================================================================
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

# 1. Frontend Paths
FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")
if not os.path.isdir(FRONTEND_DIR):
    # Fallback: If running from inside 'backend' folder, go up one level
    ROOT_DIR = os.path.dirname(ROOT_DIR)
    FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")

FRONTEND_TEMPLATES_DIR = os.path.join(FRONTEND_DIR, "templates")
FRONTEND_STYLES_DIR = os.path.join(FRONTEND_DIR, "styles")

# 2. Boilerplate Path (Critical for Project Creation)
# We check multiple common locations to prevent "directory not found" errors
possible_bp_paths = [
    os.path.join(ROOT_DIR, "backend", "boilerplate"),
    os.path.join(ROOT_DIR, "boilerplate"),
    os.path.join(os.getcwd(), "backend", "boilerplate"),
]

BOILERPLATE_DIR = None
for path in possible_bp_paths:
    if os.path.isdir(path):
        BOILERPLATE_DIR = path
        break

# If still not found, default to backend/boilerplate and warn
if not BOILERPLATE_DIR:
    BOILERPLATE_DIR = os.path.join(ROOT_DIR, "backend", "boilerplate")
    print(f"⚠️ WARNING: Boilerplate directory not found. Expected at: {BOILERPLATE_DIR}")

# 3. Dev Mode & Limits
DEV_MODE = os.getenv("DEV_MODE", "1") == "1"
DEFAULT_TOKEN_LIMIT = int(os.getenv("MONTHLY_TOKEN_LIMIT", "250000"))

# 4. Google Auth Keys
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "https://walter-yarest-theodore.ngrok-free.dev/auth/google/callback")

# ==========================================================================
# CONFIGURATION: RESEND & SUPABASE (CRITICAL FIX)
# ==========================================================================

# 1. Resend (Email)
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

# 2. Supabase (Database)
SUPABASE_URL = os.getenv("SUPABASE_URL")
# [CRITICAL] We MUST use the SERVICE_ROLE_KEY to bypass RLS policies in the backend
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("CRITICAL ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env to allow project creation.")

# Initialize Supabase with Admin Privileges
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==========================================================================
# GLOBAL STATE
# ==========================================================================

# In-Memory OTP Store (for Signup/Login verification)
PENDING_SIGNUPS = {}

# Runtime Management State
_BOOTING_PROJECTS: Set[str] = set()
_LAST_ACCESS: Dict[str, float] = {} 
SHUTDOWN_TIMEOUT_SECONDS = 600 # 10 Minutes

# ==========================================================================
# APP INITIALIZATION & LIFECYCLE
# ==========================================================================


app = FastAPI(
    title="Gorilla Backend",
    docs_url="/api/docs",       # <--- MOVES the Swagger UI to /api/docs
    redoc_url="/api/redoc",     # <--- MOVES ReDoc to /api/redoc
    openapi_url="/api/openapi.json" # <--- MOVES the JSON schema
)

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
        # 1. Send the Verification Email
        params = {
            "from": "Gor://a Auth Verification <auth@gorillabuilder.dev>", # Use your verified domain
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

        # 2. Create/Update Resend Contact (Fixed: No Audience ID needed)
        try:
            contact_params = {
                "email": to_email,
                "unsubscribed": False
            }
            resend.Contacts.create(contact_params)
            print(f"✅ Added contact {to_email} to Resend")
        except Exception as contact_error:
            # We catch this separately so auth doesn't fail if contact creation fails
            print(f"⚠️ Resend Contact Error: {contact_error}")

    except Exception as e:
        print(f"❌ Resend Error: {e}")

# ==========================================================================
# PUBLIC ROUTES (Templates & Redirects)
# ==========================================================================

# Define pages that don't require authentication (except root which has logic)
PUBLIC_PAGES = {
    "/login": "auth/login.html",
    "/signup": "auth/signup.html",
    "/forgot-password": "auth/forgot_password.html",
    "/pricing": "freemium/pricing.html",
    "/checkout/tokens": "freemium/checkout/tokens.html",
    "/checkout/premium": "freemium/checkout/premium.html",
    "/help": "help.html",
    "/about": "docs/about.html", 
    "/contact": "docs/contact.html",
}
# In app.py
from fastapi.staticfiles import StaticFiles # Make sure this is imported

app.mount("/static", StaticFiles(directory="frontend/static"), name="static")

# In app.py

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    
    # Force the "Flexible" headers
    response.headers["Cross-Origin-Embedder-Policy"] = "credentialless"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    
    # 🖨️ PRINT THE VALUE TO TERMINAL
    return response

app.mount("/assets", StaticFiles(directory="frontend/templates/landing/assets"), name="assets")

# 1. ROOT ROUTE REDIRECT LOGIC
@app.get("/")
async def root_redirect(request: Request):
    """
    Redirects based on auth status:
    - Logged in -> /dashboard
    - Not logged in (or dev/local) -> /signup
    """
    user = get_current_user_safe(request) # Helper function to get user without raising error
    
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    
    # Default landing is now the signup page
    return RedirectResponse("/signup", status_code=303)

# 2. GENERATE HANDLERS FOR OTHER PUBLIC PAGES
for route, template_name in PUBLIC_PAGES.items():
    # Skip creating a handler for root since we defined it manually above
    if route == "/": continue

    def make_handler(t_name):
        async def handler(request: Request):
            # Pass common variables like 'step' for signup flow
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
# 📚 DOCUMENTATION ROUTES
# ==========================================================================

@app.get("/docs/{page}", response_class=HTMLResponse)
async def docs_page(request: Request, page: str):
    valid_pages = [
        "intro", "dashboard", "billing", 
        "prompting", "editor", "agent-workflow", "files",
        "x-mode", "deployment", "troubleshooting",
        "about", "contact"
    ]
    
    if page not in valid_pages:
        return RedirectResponse("/docs/intro")
        
    return templates.TemplateResponse(
        f"docs/{page}.html", 
        {"request": request, "page": page}
    )

@app.get("/docs", response_class=HTMLResponse)
async def docs_root():
    return RedirectResponse("/docs/intro")

# ==========================================================================
# AUTHENTICATION ROUTES (Consolidated & Secured)
# ==========================================================================
import random
import string
import time
from fastapi import APIRouter, Request, Form, BackgroundTasks, HTTPException, Response
from fastapi.responses import RedirectResponse

# Global memory for storing OTPs during signup flow
PENDING_SIGNUPS = {}

def get_current_user_safe(request: Request):
    """
    Helper to safely check for a user session without raising an exception.
    Used for the root redirect logic.
    """
    try:
        # Check Supabase cookie first
        token = request.cookies.get("sb_access_token")
        if token:
             user = supabase.auth.get_user(token)
             if user: return user
        
        # Fallback to session (for dev/google auth) if used
        if "user" in request.session:
            return request.session["user"]
            
    except:
        pass
    return None

# --------------------------------------------------------------------------
# 1. SIGNUP FLOW (Secure)
# --------------------------------------------------------------------------

@app.post("/auth/signup")
async def auth_signup_init(
    request: Request, 
    background_tasks: BackgroundTasks,
    email: str = Form(...), 
    password: str = Form(...)
):
    email = (email or "").strip().lower()
    
    # [SECURITY] Check if user already exists
    try:
        # Admin check is most reliable. If not available, use a safe alternative.
        existing_users = supabase.auth.admin.list_users()
        user_exists = any(u.email == email for u in existing_users)
        
        if user_exists:
            # REDIRECT TO LOGIN if account exists
            return templates.TemplateResponse(
                "auth/login.html", 
                {
                    "request": request, 
                    "error": "Account exists. Please log in here.",
                    "email_prefill": email # Optional: pass back to template if supported
                }
            )
    except Exception as e:
        print(f"⚠️ User existence check warning: {e}")
        pass # Fail open or closed depending on policy, passing allows flow to continue

    # Proceed with OTP generation
    otp = "".join(random.choices(string.digits, k=6))
    
    PENDING_SIGNUPS[email] = {
        "password": password,
        "otp": otp,
        "ts": time.time()
    }
    
    # Send Email (Mock for Dev)
    print(f"📧 [DEV OTP] Code for {email}: {otp}") 
    
    return templates.TemplateResponse(
        "auth/signup.html", 
        {
            "request": request, 
            "step": "verify", 
            "email": email
        }
    )

@app.post("/auth/verify")
async def auth_verify_otp(
    request: Request,
    email: str = Form(...),
    code: str = Form(...)
):
    email = email.strip().lower()
    record = PENDING_SIGNUPS.get(email)
    
    # 1. Validate Session
    if not record:
        return templates.TemplateResponse("auth/signup.html", {"request": request, "step": "initial", "error": "Session expired. Please start over."})
    
    # 2. Validate OTP
    if record["otp"] != code:
        return templates.TemplateResponse("auth/signup.html", {"request": request, "step": "verify", "email": email, "error": "Invalid code."})
    
    try:
        password = record["password"]
        
        # 3. Create Supabase User
        try:
            supabase.auth.admin.create_user({
                "email": email,
                "password": password,
                "email_confirm": True
            })
        except Exception as e:
            # Should be caught by the initial check, but strictly handle race conditions
            return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Account exists. Please log in."})

        # 4. Auto-Login
        res = supabase.auth.sign_in_with_password({
            "email": email, 
            "password": password
        })

        if not res.session:
            raise Exception("Account created, but auto-login failed.")

        # 5. Sync Public DB
        ensure_public_user(res.user.id, email)
        
        # 6. Cleanup & Response
        if email in PENDING_SIGNUPS:
            del PENDING_SIGNUPS[email]
        
        # FIX: Force session set to avoid dev@local fallback
        request.session["user"] = {"id": res.user.id, "email": email}

        response = RedirectResponse("/dashboard", status_code=303)
        response.set_cookie(
            key="sb_access_token", 
            value=res.session.access_token, 
            max_age=86400, 
            httponly=True, 
            samesite="lax"
        )
        return response
        
    except Exception as e:
        print(f"Verify Error: {e}")
        return templates.TemplateResponse("auth/signup.html", {"request": request, "step": "verify", "email": email, "error": "System error. Try again."})

# --------------------------------------------------------------------------
# 2. LOGIN FLOW (Smart Redirect)
# --------------------------------------------------------------------------

@app.post("/auth/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        # 1. Attempt Real Authentication against Supabase
        res = supabase.auth.sign_in_with_password({
            "email": email, 
            "password": password
        })
        
        # If we get here, the password IS correct.
        if not res.session:
            raise Exception("Auth failed (No session)")

        # FIX: Force session set to avoid dev@local fallback
        request.session["user"] = {"id": res.user.id, "email": email}
        # FIX: Ensure user is synced
        ensure_public_user(res.user.id, email)

        # 2. Success: Set Cookie & Redirect
        response = RedirectResponse("/dashboard", status_code=303)
        response.set_cookie(
            key="sb_access_token", 
            value=res.session.access_token, 
            max_age=86400, 
            httponly=True, 
            samesite="lax"
        )
        return response

    except Exception as e:
        print(f"❌ Login Failed for {email}: {e}")
        
        # 3. Security Analysis: Determine why it failed
        error_msg = "Invalid email or password."
        
        try:
            # Check if user exists but uses Google Auth (Passwordless)
            # This prevents the "Dev User" bug by strictly identifying the account type
            users = supabase.auth.admin.list_users()
            target_user = next((u for u in users if u.email == email), None)
            
            if target_user:
                identities = getattr(target_user, "identities", [])
                providers = [i.provider for i in identities]
                
                # If they only have Google and no password set
                if "google" in providers and "email" not in providers:
                    error_msg = "This account uses Google Login. Please click 'Log in with Google'."
                # If they have Google but maybe typed the wrong password
                elif "google" in providers:
                    error_msg = "Invalid password. Try logging in with Google instead."
        except:
            pass # Keep generic error if admin check fails

        # 4. STRICT FAILURE: Return to login page with error
        # Absolutely NO redirects to dashboard here
        return templates.TemplateResponse("auth/login.html", {
            "request": request, 
            "error": error_msg,
            "email_prefill": email
        })
            
        # Default Error (Wrong password, etc.)
        return templates.TemplateResponse("auth/login.html", {
            "request": request, 
            "error": "Invalid email or password."
        })

@app.get("/auth/logout")
async def logout(request: Request):
    # Redirect to signup on logout
    response = RedirectResponse("/signup", status_code=303)
    response.delete_cookie("sb_access_token")
    request.session.clear()
    try:
        supabase.auth.sign_out()
    except: pass
    return response

# --------------------------------------------------------------------------
# 3. FORGOT PASSWORD & OAUTH (Simplified)
# --------------------------------------------------------------------------

@app.post("/auth/forgot-password")
async def forgot_password_action(request: Request, email: str = Form(...)):
    try:
        supabase.auth.reset_password_email(email, options={
            "redirect_to": f"{str(request.base_url).rstrip('/')}/auth/reset-callback" 
        })
        return templates.TemplateResponse("auth/login.html", {
            "request": request, 
            "error": "Reset link sent. Check your email."
        })
    except Exception as e:
        return templates.TemplateResponse("auth/forgot_password.html", {"request": request, "error": f"Error: {e}"})

# ... (Google Auth routes remain similar, ensure they redirect to /dashboard on success)
# --------------------------------------------------------------------------
# 4. GOOGLE OAUTH
# --------------------------------------------------------------------------

@app.get("/auth/google")
async def auth_google(request: Request):
    if not GOOGLE_CLIENT_ID or not GOOGLE_REDIRECT_URI:
        raise HTTPException(500, "Google Auth config missing.")
    scope = "openid email profile"
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?response_type=code&client_id={GOOGLE_CLIENT_ID}&redirect_uri={GOOGLE_REDIRECT_URI}&scope={scope}"
    return RedirectResponse(auth_url)

@app.get("/auth/google/callback")
async def auth_google_callback(request: Request, code: str):
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(500, "Google Auth config missing.")
    
    async with httpx.AsyncClient() as client:
        res = await client.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        })
        
        if res.status_code != 200:
             raise HTTPException(400, "Google Login Failed")
        
        tokens = res.json()
        access_token = tokens.get("access_token")
        
        user_res = await client.get(
            "https://www.googleapis.com/oauth2/v1/userinfo", 
            headers={"Authorization": f"Bearer {access_token}"}
        )
        user_data = user_res.json()
        email = user_data.get("email")
        
        if not email:
            raise HTTPException(400, "No email from Google")

        user_id = _stable_user_id_for_email(email)
        ensure_public_user(user_id, email)
        
        request.session["user"] = {"id": user_id, "email": email}
        
        return RedirectResponse("/dashboard", status_code=303)
        
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
# PROJECT ROUTES (RLS-COMPLIANT)
# ==========================================================================
import io
import zipfile
import urllib.parse
import re
import os
import asyncio
from fastapi.responses import StreamingResponse

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

# 2. CREATE ACTION (Backend Insert)
@app.post("/projects/create")
async def create_project(
    request: Request, 
    prompt: Optional[str] = Form(None), 
    name: Optional[str] = Form(None), 
    description: str = Form(""),
    xmode: Optional[str] = Form(None)  # Capture xmode flag
):
    user = get_current_user(request)

    # --- 1. FREE TIER LIMIT CHECK (Max 3 Projects) ---
    if user.get("plan") != "premium":
        try:
            # Count existing projects for this user
            res = supabase.table("projects") \
                .select("id", count="exact") \
                .eq("owner_id", user["id"]) \
                .execute()
            
            # Robust count retrieval (handles different Supabase client versions)
            current_count = res.count if hasattr(res, 'count') and res.count is not None else len(res.data)

            if current_count >= 3:
                # Limit Reached: Fetch existing projects to re-render the dashboard
                # We need this data because 'dashboard.html' expects a list of projects
                p_res = supabase.table("projects") \
                    .select("*") \
                    .eq("owner_id", user["id"]) \
                    .order("created_at", desc=True) \
                    .execute()
                
                existing_projects = p_res.data if p_res.data else []

                return templates.TemplateResponse("dashboard.html", {
                    "request": request, 
                    "user": user,
                    "projects": existing_projects,
                    "error": "Free Limit Reached (3/3). Upgrade to Pro to create unlimited projects."
                })
        except Exception as e:
            print(f"⚠️ Project limit check failed: {e}")
            # If check fails, we typically allow it to proceed or show a generic error.
            # Here we proceed to avoid blocking users due to temporary DB glitches.
            pass

    # --- 2. PROMPT STASHING & CONFIRMATION ---
    # Redirect back to name confirmation form if we have a prompt but no name
    if prompt and not name:
        target = f"/projects/createit?prompt={urllib.parse.quote(prompt)}"
        return RedirectResponse(target, status_code=303)
    
    final_prompt = prompt or request.session.pop("stashed_prompt", None)
    project_name = name or "Untitled Project"
    
    # --- 3. HEAVY LIFTING (DB & Files) ---
    def _heavy_lift_create():
        # A. Create Project Record (Service Role bypasses RLS)
        res = supabase.table("projects").insert({
            "owner_id": user["id"], 
            "name": project_name, 
            "description": description or (final_prompt[:200] if final_prompt else "")
        }).execute()
        
        if not res.data: 
            raise Exception("DB Insert Failed - Check Service Role Key")
            
        pid = res.data[0]['id']
        
        # B. Setup Subdomain
        clean_name = re.sub(r'[^a-z0-9-]', '-', project_name.lower()).strip('-') or "app"
        final_subdomain = f"{clean_name}-{pid}" 
        
        supabase.table("projects").update({"subdomain": final_subdomain}).eq("id", pid).execute()
        
        # C. Inject Boilerplate
        bp_dir = globals().get("BOILERPLATE_DIR")
        if not bp_dir or not os.path.isdir(bp_dir):
            bp_dir = os.path.join(ROOT_DIR, "backend", "boilerplate")
            if not os.path.isdir(bp_dir):
                bp_dir = os.path.join(ROOT_DIR, "boilerplate")

        if os.path.isdir(bp_dir):
            files_to_insert = []
            
            for root, dirs, files in os.walk(bp_dir):
                # Skip massive/hidden folders
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
                except Exception as e:
                    print(f"Batch insert failed ({e}), falling back to single upserts...")
                    for f in files_to_insert:
                        try: 
                            supabase.table("files").upsert(f, on_conflict="project_id,path").execute()
                        except: pass
        
        return pid

    # --- 4. EXECUTION ---
    try:
        # Run DB operations in thread to prevent blocking
        pid = await asyncio.to_thread(_heavy_lift_create)
        
        # Redirect Logic
        if xmode == "true":
            target_url = f"/projects/{pid}/xmode"
        else:
            target_url = f"/projects/{pid}/editor"
            
        if final_prompt: 
            target_url += f"?prompt={urllib.parse.quote(final_prompt)}"
            
        return RedirectResponse(target_url, status_code=303)
        
    except Exception as e:
        print(f"Create Error: {e}")
        return RedirectResponse("/dashboard?error=creation_failed_rls", status_code=303)

# 3. EDITOR PAGE
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
    
# 4. X-MODE EDITOR (Purple Theme)
@app.get("/projects/{project_id}/xmode", response_class=HTMLResponse)
async def project_xmode(request: Request, project_id: str, file: str = "index.html"):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
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
            "project": project, 
            "file": file, 
            "user": user,
            "xmode": True
        }
    )

# 5. PREVIEW PAGE
@app.get("/projects/{project_id}/preview", response_class=HTMLResponse)
async def project_preview(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    return templates.TemplateResponse(
        "projects/project-preview.html",
        {"request": request, "project_id": project_id, "user": user}
    )

# 6. SETTINGS PAGE
@app.get("/projects/{project_id}/settings", response_class=HTMLResponse)
async def project_settings(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    try:
        res = supabase.table("projects").select("*").eq("id", project_id).single().execute()
        project = res.data if res else None
    except Exception:
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
    
    # Update using Service Role or ensure RLS allows updates by owner
    supabase.table("projects").update(
        {"name": name, "description": description}
    ).eq("id", project_id).execute()
    
    return RedirectResponse(f"/projects/{project_id}/settings", status_code=303)

# 7. EXPORT TO ZIP
@app.get("/api/project/{project_id}/export")
async def project_export(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    # Check Plan (Optional)
    try:
        user_record = db_select_one("users", {"id": user["id"]}, "plan")
        current_plan = user_record.get("plan") if user_record else "free"
        
        # Uncomment to enforce premium requirement
        # if current_plan != "premium":
        #     raise HTTPException(status_code=403, detail="Exporting is a Premium feature.")
    except:
        current_plan = "free"

    # Fetch Files
    res = (
        supabase.table("files")
        .select("path,content")
        .eq("project_id", project_id)
        .execute()
    )
    files = res.data if res and res.data else []

    if not files:
        raise HTTPException(status_code=404, detail="No files found in this project.")

    # Create Zip
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
@app.post("/api/project/{project_id}/log")
async def log_browser_event(project_id: str, request: Request):
    """
    Receives logs from the browser (WebContainer).
    If the log contains 'failed' or 'error', it alerts the Agent/Coder.
    """
    try:
        form = await request.form()
        level = form.get("level", "INFO")
        message = form.get("message", "")
        
        # 1. Print to Python Terminal
        print(f"[{level}] Browser: {message}")
        
        # 2. Stream critical errors to the Chat UI (Coder)
        if "error" in message.lower() or "failed" in message.lower():
            emit_log(project_id, "system", f"⚠️ Browser Error: {message}")
            
    except:
        pass
    return JSONResponse({"status": "ok"})


@app.get("/api/project/{project_id}/files")
async def get_project_files(request: Request, project_id: str):
    """
    Returns the flat list of files for the WebContainer.
    """
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    # Fetch from Supabase
    res = (
        supabase.table("files")
        .select("path,content")
        .eq("project_id", project_id)
        .execute()
    )
    
    # Handle Async/Sync Supabase client differences
    if asyncio.iscoroutine(res): res = await res
    
    rows = getattr(res, "data", [])
    if not rows and isinstance(res, list): rows = res
        
    return {"files": rows}


@app.get("/api/project/{project_id}/file")
async def get_file_content(request: Request, project_id: str, path: str):
    print(f"📂 [BACKEND] Fetching file: {path} for project: {project_id}")
    
    try:
        res = supabase.table("files").select("content").eq("project_id", project_id).eq("path", path).execute()
        if asyncio.iscoroutine(res): res = await res
        
        content = ""
        if res.data and len(res.data) > 0:
            content = res.data[0].get("content", "")
        else:
            print(f"⚠️ [BACKEND] File not found in DB: {path}")

        return JSONResponse({"content": content})
        
    except Exception as e:
        print(f"❌ [BACKEND] Error: {e}")
        return JSONResponse({"content": f"// Error loading file: {e}"})


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
# STATIC FILE SERVING & WEBCONTAINER SUPPORT
# ==========================================================================
def _guess_media_type(path: str) -> str:
    mt, _ = mimetypes.guess_type(path)
    if mt: return mt
    if path.endswith(".js"): return "application/javascript"
    if path.endswith(".css"): return "text/css"
    if path.endswith(".html"): return "text/html"
    if path.endswith(".json"): return "application/json"
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
    if asyncio.iscoroutine(res): res = await res

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
    try:
        # 1. Execute Query
        query = supabase.table("files").select("path,content").eq("project_id", project_id)
        res = query.execute()
        
        # 2. Check if response is a coroutine
        if asyncio.iscoroutine(res):
            res = await res
            
        # 3. Normalize Data
        rows = getattr(res, "data", [])
        if not rows and isinstance(res, list):
            rows = res
            
        return {r["path"]: (r.get("content") or "") for r in rows if r.get("path")}
        
    except Exception as e:
        print(f"⚠️ Fetch Error: {e}")
        return {}

@app.post("/api/project/{project_id}/agent/start")
async def agent_start(
    request: Request, 
    project_id: str, 
    prompt: str = Form(...),
    xmode: bool = Form(False)
):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    # 1. TOKEN CHECK
    try:
        enforce_token_limit_or_raise(user["id"])
    except HTTPException as e:
        if e.status_code == 402:
            alert_html = (
                f'<div style="background:#0f172a; border:1px solid rgba(239,68,68,0.2); border-left:3px solid #ef4444; border-radius:12px; padding:24px; margin-top:20px; font-family:system-ui,-apple-system,sans-serif; box-shadow:0 10px 30px rgba(0,0,0,0.4);">'
                f'  <div style="display:flex; align-items:center; gap:12px; margin-bottom:16px; padding-bottom:16px; border-bottom:1px solid rgba(255,255,255,0.05);">'
                f'    <div style="width:24px; height:24px; display:flex; align-items:center; justify-content:center; background:rgba(239,68,68,0.1); color:#ef4444; border-radius:50%; font-size:14px; font-weight:bold;">!</div>'
                f'    <div style="color:#e2e8f0; font-size:16px; font-weight:400; letter-spacing:0.5px; text-transform:uppercase;">Usage Limit Reached</div>'
                f'  </div>'
                f'  <div style="color:#cbd5e1; font-size:14px; line-height:1.6; margin-bottom:20px;">You have reached your monthly token limit. Upgrade to Pro to continue generating code and accessing advanced features.</div>'
                f'  <a href="/pricing" target="_blank" style="display:inline-block; background:#ef4444; color:#ffffff; padding:10px 20px; border-radius:6px; font-size:13px; font-weight:500; text-decoration:none; letter-spacing:0.5px; box-shadow:0 4px 6px rgba(239,68,68,0.2);">Upgrade Plan</a>'
                f'</div>'
            )
            emit_log(project_id, "assistant", alert_html)
            return {"started": False}
        raise e
    
    emit_status(project_id, "Agent received prompt")
    emit_log(project_id, "user", prompt)

    async def _run():
        nonlocal prompt
        try:
            await asyncio.sleep(0.5)
            file_tree = await _fetch_file_tree(project_id)
            
            planner = Planner()
            coder = XCoder() if (xmode and 'XCoder' in globals()) else Coder()
            
            # --- PHASE 1: PLANNER ---
            emit_phase(project_id, "planner")
            emit_progress(project_id, "Architecting solution...", 10)
            
            plan_res = await asyncio.to_thread(
                planner.generate_plan,
                user_request=prompt, 
                project_context={"project_id": project_id, "files": list(file_tree.keys())}
            )
            
            tk = plan_res.get("usage", {}).get("total_tokens", 0)
            if tk: add_monthly_tokens(user["id"], tk)
            
            # --- DISPLAY PLAN ---
            raw_plan = plan_res.get("plan", {})
            real_assistant_msg = plan_res.get("assistant_message")
            
            emit_log(project_id, "assistant", real_assistant_msg or "I have created a plan for your application.")

            tasks = raw_plan.get("todo", [])
            if tasks:
                steps_html = ""
                for i, task in enumerate(tasks, 1):
                    task_content = task
                    if "]" in task:
                        parts = task.split("]", 1)
                        if len(parts) > 1:
                            task_content = parts[1].strip()

                    steps_html += (
                        f'<div style="display:flex; gap:25px; position:relative; z-index:2; margin-bottom:20px;">'
                        f'  <div style="width:12px; height:12px; background:#0f172a; border:2px solid #3b82f6; border-radius:50%; box-shadow:0 0 10px #3b82f6; flex-shrink:0; margin-top:6px; position:relative; z-index:2;"></div>'
                        f'  <div style="flex:1; background:rgba(30,41,59,0.3); border:1px solid rgba(255,255,255,0.05); border-radius:8px; padding:18px;">'
                        f'    <span style="color:#60a5fa; font-size:11px; font-weight:bold; letter-spacing:1px; margin-bottom:6px; display:block; font-family:monospace; opacity:0.8;">{i:02}</span>'
                        f'    <div style="color:#cbd5e1; font-size:14px; line-height:1.5;">{task_content}</div>'
                        f'  </div>'
                        f'</div>'
                    )
                
                full_html = (
                    f'  <div style="margin-bottom:30px; padding-bottom:15px; border-bottom:1px solid rgba(255,255,255,0.05);">'
                    f'    <div style="color:#e2e8f0; font-size:18px; font-weight:400; letter-spacing:1px; text-transform:uppercase;">✦ BluePrint</div>'
                    f'  </div>'
                    f'  <div style="position:relative; padding-left:5px;">'
                    f'    <div style="position:absolute; left:6px; top:10px; bottom:10px; width:1px; background:linear-gradient(to bottom,#3b82f6,rgba(59,130,246,0.1)); z-index:1;"></div>'
                    f'    {steps_html}'
                    f'  </div>'
                    f'</div>'
                )
                
                emit_log(project_id, "planner", full_html)

            if not tasks:
                emit_status(project_id, "Response Complete")
                emit_progress(project_id, "Done", 100)
                return

            # --- PHASE 2: CODER ---
            emit_phase(project_id, "coder")
            total = len(tasks)
            
            for i, task in enumerate(tasks, 1):
                try: enforce_token_limit_or_raise(user["id"])
                except HTTPException:
                    emit_log(project_id, "system", "⚠️ Token limit reached. Stopping.")
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
                        # Linting
                        if path.startswith("static/") and path.endswith(".js"):
                            try:
                                lint_err = await asyncio.to_thread(lint_code_with_esbuild, content, path)
                                if lint_err:
                                    emit_log(project_id, "system", f"❌ Syntax Error in {path}:\n{lint_err}")
                            except: pass

                        db_upsert(
                            "files", 
                            {"project_id": project_id, "path": path, "content": content}, 
                            on_conflict="project_id,path"
                        )
                        emit_file_changed(project_id, path)
                
                file_tree = await _fetch_file_tree(project_id)
            
            emit_status(project_id, "Coding Complete.")
            emit_progress(project_id, "Ready", 100)
            
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
                    yield f": keep-alive\n\n"
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

@app.get("/projects/{project_id}/game", response_class=HTMLResponse)
async def project_game(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    return templates.TemplateResponse("projects/game.html", {"request": request, "project_id": project_id, "user": user})

@app.post("/api/project/{project_id}/agent/ping")
async def agent_ping(request: Request, project_id: str):
    emit_log(project_id, "system", "🔥 Pong from backend")
    return {"ok": True}

@app.get("/health")
async def health():
    return {"ok": True, "ts": int(time.time()), "dev_mode": DEV_MODE}


if __name__ == "__main__":
    import uvicorn
    from pyngrok import ngrok
    import sys

    # Open a tunnel to port 8000
    # This URL bypasses GitHub's Proxy completely
    public_url = ngrok.connect(8000).public_url
    print(f"\n🚀 \033[92mYOUR BYPASS URL: {public_url}\033[0m 🚀\n")

    uvicorn.run(app, host="0.0.0.0", port=8000)