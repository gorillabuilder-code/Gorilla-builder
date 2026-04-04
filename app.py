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
DEFAULT_TOKEN_LIMIT = int(os.getenv("MONTHLY_TOKEN_LIMIT", "500000"))

# 4. Google Auth Keys
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "https://corrinne-turbid-illustratively.ngrok-free.dev/auth/google/callback")

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

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allows apps hosted anywhere to use the API
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if os.path.isdir(FRONTEND_STYLES_DIR):
    app.mount("/styles", StaticFiles(directory=FRONTEND_STYLES_DIR), name="styles")

templates = Jinja2Templates(directory=FRONTEND_TEMPLATES_DIR)

@app.exception_handler(403)
async def custom_403_handler(request: Request, __):
    # If the user isn't authorized, just send them home
    return RedirectResponse(url="/", status_code=303)

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

# ==========================================================================
# DB UPSERT (Integrity Guard)
# ==========================================================================
def db_upsert(table: str, data: Dict[str, Any], on_conflict: str = "id"):
    """
    Enhanced upsert that blocks binary/giant files from entering the DB.
    """
    path = data.get("path", "")
    content = data.get("content", "")

    # 1. 🛑 THE LOCKFILE BLOCKER
    # Blocks the Agent or Boilerplate from saving giant lockfiles
    if path and any(x in path for x in ["package-lock.json", "yarn.lock", "pnpm-lock.yaml"]):
        print(f"⏩ Skipping {path} (Handled by WebContainer runtime)")
        return None

    # 2. 🛑 BINARY FILTER
    # Prevents binary data from being stored in the text column
    if path and path.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.ico', '.pdf', '.zip')):
        print(f"⏩ Skipping binary file: {path}")
        return None

    # 3. 🛑 INTEGRITY CHECK
    # Ensure we aren't saving empty strings as files
    if table == "files" and not content:
        print(f"⚠️ Warning: Attempting to save empty file content for {path}")

    try:
        return supabase.table(table).upsert(data, on_conflict=on_conflict).execute()
    except Exception as e:
        print(f"❌ DB Upsert Error for {path}: {e}")
        return None


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

from fastapi import Request, HTTPException, status
from typing import Dict, Any

def get_current_user(request: Request) -> Dict[str, Any]:
    """Retrieves user from session. Strictly enforces authentication and blocks dev accounts."""
    user = request.session.get("user")
    
    # 1. No user in session? Boot them to the homepage/login.
    if not user or not user.get("id"):
        raise HTTPException(
            status_code=403,
            detail="Invalid user session."
        )

    # 2. THE KILLSWITCH: Catch old dev@local cookies and destroy them
    if user.get("email") == "dev@local":
        request.session.clear() # Completely wipe the ghost session from their browser
        raise HTTPException(
            status_code=403,
            detail="Invalid user session."
        )

    # 3. Valid, real user found. Ensure public record exists.
    ensure_public_user(user["id"], user.get("email") or "unknown@local")
    return user

def _require_project_owner(user: Dict[str, Any], project_id: str) -> None:
    """Verifies that the current user owns the project."""
    res = db_select_one("projects", {"id": project_id}, "id, owner_id")
    
    if not res:
        raise HTTPException(status_code=404, detail="Project not found")
        
    if res.get("owner_id") != user["id"]:
        # We use 403 to signal "You aren't allowed here" 
        # JavaScript will catch this and trigger the redirect.
        raise HTTPException(status_code=403, detail="Unauthorized Access")

# --- RESEND EMAIL LOGIC ---

def send_otp_email(to_email: str, code: str):
    if not RESEND_API_KEY:
        print(f"⚠️ Resend Key missing. Code for {to_email}: {code}")
        return
    try:
        # 1. Send the Verification Email
        params = {
            "from": "Gor://a OAuth Verification <auth@gorillabuilder.dev>", # Use your verified domain
            "to": [to_email],
            "subject": f"{code} - Your Verification Code for Gor://a Builder",
            "html": f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Verification Email</title>
            </head>
            <body style="margin: 0; padding: 0; background-color: #0b1020; font-family: 'Google Sans','Calibri','Roboto', Monospace, sans-serif;">
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
        # 2. Create/Update Resend Contact (Fixed: No Audience ID needed)
        try:
            contact_params = {
                "audience_id": "638e2a6b-cf64-4efc-bece-1973216a2825",
                "email": to_email,
                "unsubscribed": False
            }
            resend.Contacts.create(contact_params)
            print(f"✅ Added contact {to_email} to Resend")
            resend.Emails.send(params)
            print(f"✅ OTP sent to {to_email}")
        except Exception as contact_error:
            # We catch this separately so auth doesn't fail if contact creation fails
            print(f"⚠️ Resend Contact Error: {contact_error}")

    except Exception as e:
        print(f"❌ Resend Error: {e}")

# ==========================================================================
# FIGMA OAUTH INTEGRATION
# ==========================================================================
import secrets
import httpx
import urllib.parse

FIGMA_CLIENT_ID = os.getenv("FIGMA_CLIENT_ID")
FIGMA_CLIENT_SECRET = os.getenv("FIGMA_CLIENT_SECRET")
# e.g., https://app.gorillabuilder.dev/auth/figma/callback (must match Figma exact)
FIGMA_REDIRECT_URI = os.getenv("FIGMA_REDIRECT_URI")

@app.get("/auth/figma")
async def figma_login(request: Request):
    """Initiates the Figma OAuth flow."""
    user = get_current_user(request) 
    
    state = secrets.token_urlsafe(16)
    request.session["figma_oauth_state"] = state
    
    # 🛑 THE FIX: Changed scope=file_read to scope=file_content:read
    url = f"https://www.figma.com/oauth?client_id={FIGMA_CLIENT_ID}&redirect_uri={urllib.parse.quote(FIGMA_REDIRECT_URI)}&scope=file_content:read&state={state}&response_type=code"
    
    return RedirectResponse(url)

@app.get("/auth/figma/callback")
async def figma_callback(request: Request, code: str, state: str):
    """Catches the code from Figma, trades it for a token, and saves it to DB."""
    try:
        user = get_current_user(request)
        
        # Verify the state matches what we sent (CSRF protection)
        saved_state = request.session.pop("figma_oauth_state", None)
        if not saved_state or state != saved_state:
            return RedirectResponse("/dashboard?error=figma_invalid_state", status_code=303)
        
        # 🛑 THE FIX: Changed from www.figma.com to api.figma.com/v1/
        async with httpx.AsyncClient() as client:
            res = await client.post("https://api.figma.com/v1/oauth/token", data={
                "client_id": FIGMA_CLIENT_ID,
                "client_secret": FIGMA_CLIENT_SECRET,
                "redirect_uri": FIGMA_REDIRECT_URI,
                "code": code,
                "grant_type": "authorization_code"
            })
            
            if res.status_code != 200:
                print(f"⚠️ Figma OAuth Error: {res.text}")
                return RedirectResponse("/dashboard?error=figma_token_exchange_failed", status_code=303)
                
            tokens = res.json()
            access_token = tokens.get("access_token")
            refresh_token = tokens.get("refresh_token")
            
            if access_token:
                # Save the tokens to the user's record in Supabase
                supabase.table("users").update({
                    "figma_access_token": access_token,
                    "figma_refresh_token": refresh_token
                }).eq("id", user["id"]).execute()
                
        return RedirectResponse("/dashboard?success=figma_linked", status_code=303)
        
    except Exception as e:
        print(f"⚠️ Figma Auth Callback crashed: {e}")
        return RedirectResponse("/dashboard?error=figma_auth_crash", status_code=303)

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
    "/privacy-policy": "legal/privacy-policy.html",
    "/terms-of-service": "legal/terms-of-service.html"
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

from fastapi.responses import HTMLResponse, RedirectResponse


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

from fastapi.responses import Response

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # The raw Gorilla SVG from your frontend, colored with the accent blue
    svg_content = """<svg version="1.0" xmlns="http://www.w3.org/2000/svg" width="800" height="803" viewBox="0 0 600 550" preserveAspectRatio="xMidYMid meet">
        <g transform="translate(0.000000,603.000000) scale(0.100000,-0.100000)" fill="#3b6cff" stroke="none"> 
            <path d="M1497 6023 c827 -2 2179 -2 3005 0 827 1 151 2 -1502 2 -1653 0 -2329 -1 -1503 -2z"/> 
            <path d="M2802 5273 c-43 -24 -131 -143 -182 -246 -99 -202 -106 -410 -19 -598 59 -128 153 -189 246 -158 56 18 81 37 108 84 34 57 32 112 -6 216 -54 145 -51 327 7 490 41 113 27 176 -46 214 -38 20 -68 19 -108 -2z"/> 
            <path d="M3290 5268 c-74 -50 -185 -230 -228 -369 -44 -142 -37 -293 19 -440 52 -134 141 -208 234 -195 131 20 194 151 136 281 -67 150 -67 316 0 522 26 80 29 98 20 126 -28 83 -116 119 -181 75z"/> 
            <path d="M685 4560 c-171 -34 -283 -139 -331 -306 -15 -50 -18 -129 -23 -519 -4 -321 -10 -466 -18 -480 -32 -56 -64 -70 -205 -90 -26 -4 -46 -17 -72 -45 -33 -36 -36 -45 -36 -99 0 -98 62 -161 157 -161 59 0 119 -26 146 -64 22 -31 22 -35 28 -496 5 -424 8 -471 26 -531 11 -37 31 -87 46 -111 72 -124 228 -198 418 -198 125 0 200 111 151 223 -26 59 -64 82 -148 90 -73 7 -116 27 -148 70 -20 28 -21 41 -27 490 -6 505 -8 526 -68 632 l-28 50 28 50 c60 106 62 128 68 630 6 427 7 462 25 491 26 44 68 65 139 71 81 7 131 35 157 88 26 53 25 91 -2 145 -32 61 -73 80 -168 79 -41 -1 -93 -5 -115 -9z"/> 
            <path d="M5102 4550 c-68 -42 -92 -135 -54 -209 26 -52 60 -72 140 -81 83 -9 128 -32 157 -80 19 -33 20 -56 26 -500 6 -505 6 -513 66 -621 l26 -45 -26 -45 c-59 -103 -61 -113 -67 -619 -5 -461 -5 -465 -28 -501 -28 -45 -72 -67 -156 -77 -111 -14 -160 -66 -153 -165 4 -66 26 -103 78 -130 28 -15 50 -18 124 -13 218 12 351 101 416 278 22 61 23 74 28 533 6 503 6 500 58 547 19 18 78 34 155 43 33 4 56 15 78 35 29 27 30 31 30 118 0 88 -1 91 -31 116 -33 28 -45 32 -127 40 -66 7 -110 31 -137 76 -19 33 -20 56 -26 505 -6 496 -6 500 -55 598 -70 138 -221 217 -414 217 -57 0 -83 -5 -108 -20z"/> 
            <path d="M1965 3849 c-113 -9 -166 -24 -280 -79 -312 -150 -493 -462 -471 -810 13 -209 89 -379 238 -529 161 -163 321 -236 545 -248 269 -14 478 70 667 267 61 65 95 111 130 179 70 136 89 217 89 386 0 124 -3 151 -27 230 -53 176 -145 314 -282 425 -174 139 -372 197 -609 179z m214 -324 c173 -46 319 -185 372 -354 23 -76 28 -212 9 -282 -50 -186 -208 -340 -391 -384 -181 -42 -372 15 -499 152 -100 106 -144 218 -143 363 1 290 228 517 518 519 44 0 104 -6 134 -14z"/> 
            <path d="M3880 3849 c-190 -17 -341 -86 -488 -224 -112 -104 -200 -249 -239 -395 -25 -92 -25 -339 0 -430 78 -281 316 -519 597 -597 101 -28 339 -25 443 5 152 44 285 128 397 250 146 158 211 332 212 557 0 244 -82 436 -259 606 -181 175 -402 251 -663 228z m213 -324 c282 -73 454 -362 382 -640 -45 -171 -179 -312 -355 -371 -80 -27 -243 -25 -324 5 -202 73 -337 252 -353 466 -17 243 150 475 388 539 71 19 191 20 262 1z"/> 
        </g> 
    </svg>"""
    
    # Return it explicitly as an SVG image
    return Response(content=svg_content, media_type="image/svg+xml")

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
import os
import random
import string
import time
import secrets
import httpx
from fastapi import APIRouter, Request, Form, BackgroundTasks, HTTPException, Response
from fastapi.responses import RedirectResponse

# --- OAuth Environment Variables ---
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")
GITHUB_REDIRECT_URI = os.getenv("GITHUB_REDIRECT_URI")

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
        
        # Fallback to session (for dev/google/github auth) if used
        if "user" in request.session:
            return request.session["user"]
            
    except:
        pass
    return None

def _ensure_gorilla_api_key(user_id: str):
    """
    PHASE 1: AI PROXY GATEWAY
    Checks if a user has a gb_live_ API key. If not, generates and saves one.
    """
    try:
        user_data = db_select_one("users", {"id": user_id}, "gorilla_api_key")
        if not user_data or not user_data.get("gorilla_api_key"):
            # Generate a secure 48-character hex string (total key length ~56 chars)
            new_key = f"gb_live_{secrets.token_hex(24)}"
            supabase.table("users").update({"gorilla_api_key": new_key}).eq("id", user_id).execute()
            print(f"🔑 Generated new Gorilla API Key for user: {user_id}")
    except Exception as e:
        print(f"⚠️ Failed to generate gorilla_api_key for {user_id}: {e}")

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
    
    # --- FIX: SEND ACTUAL EMAIL VIA BACKGROUND TASK ---
    background_tasks.add_task(send_otp_email, email, otp)
    
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
        
        # 🚨 AI PROXY: Generate their Master Key
        _ensure_gorilla_api_key(res.user.id)
        
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
        
        # 🚨 AI PROXY: Ensure they have a Master Key
        _ensure_gorilla_api_key(res.user.id)

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
            # Check if user exists but uses Google/GitHub Auth (Passwordless)
            users = supabase.auth.admin.list_users()
            target_user = next((u for u in users if u.email == email), None)
            
            if target_user:
                identities = getattr(target_user, "identities", [])
                providers = [i.provider for i in identities]
                
                # If they only have OAuth and no password set
                if "google" in providers and "email" not in providers:
                    error_msg = "This account uses Google Login. Please click 'Continue with Google'."
                elif "github" in providers and "email" not in providers:
                    error_msg = "This account uses GitHub Login. Please click 'Continue with GitHub'."
                elif "google" in providers or "github" in providers:
                    error_msg = "Invalid password. Try logging in with your connected OAuth provider."
        except:
            pass # Keep generic error if admin check fails

        # 4. STRICT FAILURE: Return to login page with error
        return templates.TemplateResponse("auth/login.html", {
            "request": request, 
            "error": error_msg,
            "email_prefill": email
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
# 3. FORGOT PASSWORD
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
        
        # 🚨 AI PROXY: Ensure they have a Master Key
        _ensure_gorilla_api_key(user_id)
        
        request.session["user"] = {"id": user_id, "email": email}
        
        return RedirectResponse("/dashboard", status_code=303)

# --------------------------------------------------------------------------
# 5. GITHUB OAUTH (Crucial for Vercel Deployment pipeline)
# --------------------------------------------------------------------------

@app.get("/auth/github")
async def auth_github(request: Request):
    if not GITHUB_CLIENT_ID or not GITHUB_REDIRECT_URI:
        raise HTTPException(500, "GitHub Auth config missing.")
    
    # Scope 'repo' is REQUIRED to push code on the user's behalf
    scope = "user:email repo"
    auth_url = f"https://github.com/login/oauth/authorize?client_id={GITHUB_CLIENT_ID}&redirect_uri={GITHUB_REDIRECT_URI}&scope={scope}"
    return RedirectResponse(auth_url)

@app.get("/auth/github/callback")
async def auth_github_callback(request: Request, code: str):
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        raise HTTPException(500, "GitHub Auth config missing.")
    
    async with httpx.AsyncClient() as client:
        # 1. Exchange code for GitHub Access Token
        res = await client.post(
            "https://github.com/login/oauth/access_token", 
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": GITHUB_REDIRECT_URI
            },
            headers={"Accept": "application/json"}
        )
        
        if res.status_code != 200:
             raise HTTPException(400, "GitHub Login Failed")
        
        tokens = res.json()
        access_token = tokens.get("access_token")
        
        if not access_token:
            error_msg = tokens.get("error_description", "Failed to retrieve GitHub access token")
            raise HTTPException(400, error_msg)

        # 2. Get User Profile Data
        user_res = await client.get(
            "https://api.github.com/user", 
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github.v3+json"
            }
        )
        user_data = user_res.json()
        email = user_data.get("email")
        github_username = user_data.get("login", "unknown_github_user") # Grab their username just in case!

        # 3. If primary email is private, hit the emails endpoint directly
        if not email:
            emails_res = await client.get(
                "https://api.github.com/user/emails",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github.v3+json"
                }
            )
            if emails_res.status_code == 200:
                emails_data = emails_res.json()
                if isinstance(emails_data, list):
                    # Attempt A: Find the Primary & Verified email
                    for em in emails_data:
                        if isinstance(em, dict) and em.get("primary") and em.get("verified"):
                            email = em.get("email")
                            break
                    
                    # Attempt B: Just find ANY Verified email
                    if not email:
                        for em in emails_data:
                            if isinstance(em, dict) and em.get("verified"):
                                email = em.get("email")
                                break
                    
                    # Attempt C: Just take the very first email they have listed
                    if not email and len(emails_data) > 0 and isinstance(emails_data[0], dict):
                        email = emails_data[0].get("email")

        # 🚨 4. THE ULTIMATE FALLBACK 🚨
        # If their privacy settings are on maximum lockdown, we build a proxy email
        # so they can still create an account and build apps!
        if not email:
            email = f"{github_username}@noreply.github.com"

        # 5. Sync User in Database
        user_id = _stable_user_id_for_email(email)
        ensure_public_user(user_id, email)
        
        # 🚨 AI PROXY: Ensure they have a Master Key
        _ensure_gorilla_api_key(user_id)
        
        # 6. Store the GitHub access token so we can push code later
        try:
            supabase.table("users").update({"github_access_token": access_token}).eq("id", user_id).execute()
        except Exception as e:
            print(f"⚠️ Failed to save github_access_token for {email}: {e}")

        # 7. Finalize Login
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
from fastapi.responses import HTMLResponse, JSONResponse

# ==========================================================================
# SUPABASE MANAGEMENT OAUTH (Phase 1)
# ==========================================================================
SUPABASE_MGMT_CLIENT_ID = os.getenv("SUPABASE_MGMT_CLIENT_ID")
SUPABASE_MGMT_CLIENT_SECRET = os.getenv("SUPABASE_MGMT_CLIENT_SECRET")
SUPABASE_MGMT_REDIRECT_URI = os.getenv("SUPABASE_MGMT_REDIRECT_URI")

@app.get("/auth/supabase/link")
async def link_supabase_account(request: Request):
    """Initiates the Supabase Management API OAuth flow."""
    user = get_current_user(request)
    if not SUPABASE_MGMT_CLIENT_ID or not SUPABASE_MGMT_REDIRECT_URI:
        raise HTTPException(500, "Supabase Management Auth config missing.")
    
    state = secrets.token_urlsafe(16)
    request.session["supabase_oauth_state"] = state
    
    # Send them to Supabase to authorize Gorilla Builder
    auth_url = f"https://api.supabase.com/v1/oauth/authorize?client_id={SUPABASE_MGMT_CLIENT_ID}&response_type=code&redirect_uri={urllib.parse.quote(SUPABASE_MGMT_REDIRECT_URI)}&state={state}"
    return RedirectResponse(auth_url)

@app.get("/auth/supabase/callback")
async def auth_supabase_callback(request: Request, code: str, state: str):
    """Exchanges the auth code for Management Tokens and saves them to the DB."""
    try:
        user = get_current_user(request)
        saved_state = request.session.pop("supabase_oauth_state", None)
        
        if not saved_state or state != saved_state:
            return RedirectResponse("/dashboard?error=supabase_invalid_state", status_code=303)
            
        async with httpx.AsyncClient() as client:
            res = await client.post(
                "https://api.supabase.com/v1/oauth/token",
                data={
                    "client_id": SUPABASE_MGMT_CLIENT_ID,
                    "client_secret": SUPABASE_MGMT_CLIENT_SECRET,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": SUPABASE_MGMT_REDIRECT_URI
                },
                headers={"Accept": "application/json"}
            )
            
            # 🛑 THE FIX: Allow 201 Created in addition to 200 OK
            if res.status_code not in [200, 201]:
                print(f"⚠️ Supabase OAuth Error ({res.status_code}): {res.text}")
                return RedirectResponse("/dashboard?error=supabase_token_exchange_failed", status_code=303)
                
            tokens = res.json()
            access_token = tokens.get("access_token")
            refresh_token = tokens.get("refresh_token")
            
            if access_token:
                # Save the management tokens directly to the user's profile
                supabase.table("users").update({
                    "supabase_access_token": access_token,
                    "supabase_refresh_token": refresh_token
                }).eq("id", user["id"]).execute()
                print(f"✅ Supabase tokens successfully saved for user {user['id']}")
                
        return RedirectResponse("/dashboard?success=supabase_linked", status_code=303)
        
    except Exception as e:
        print(f"⚠️ Supabase Auth Callback crashed: {e}")
        return RedirectResponse("/dashboard?error=supabase_auth_crash", status_code=303)

# ==========================================================================
# DASHBOARD & WORKSPACE
# ==========================================================================
import random
from datetime import datetime, timezone
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_current_user(request)
    
    # Fetch latest Plan, Agent Skills, and Last Spin Date from DB
    has_skills = False
    last_spin_date = None
    try:
        res = supabase.table("users").select("plan, agent_skills, last_spin_date").eq("id", user["id"]).single().execute()
        if res and res.data:
            user["plan"] = res.data.get("plan", "free")
            last_spin_date = res.data.get("last_spin_date")
            if res.data.get("agent_skills"):
                has_skills = True
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
        # select("*") automatically pulls the new snapshot_b64 column
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
        {
            "request": request, 
            "projects": projects, 
            "user": user,
            "has_skills": has_skills,
            "last_spin_date": last_spin_date
        }
    )

@app.post("/api/tokens/spin")
async def spin_wheel(request: Request):
    user = get_current_user(request)
    payload = await request.json()
    
    try:
        wager = int(payload.get("wager", 0))
    except ValueError:
        raise HTTPException(400, "Invalid wager amount.")
        
    if wager < 0 or wager > 500000:
        raise HTTPException(400, "Wager must be between 0 and 500,000.")

    # 1. Fetch current token data & spin status securely from DB
    user_data = supabase.table("users").select("last_spin_date").eq("id", user["id"]).single().execute().data
    used, limit = get_token_usage_and_limit(user["id"])
    remaining = max(0, limit - used)
    
    if wager > remaining:
        raise HTTPException(400, "You do not have enough credits for this wager.")
        
    # 2. Check if they already spun today (Server-Side Enforcement)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if user_data and user_data.get("last_spin_date") == today_str:
        raise HTTPException(400, "You have already used your daily spin.")

    # 3. The Secure Math
    rand = random.random()
    if rand < 0.50:
        multiplier = 0.0  # Lose: 50% chance
    elif rand < 0.80:
        multiplier = 1.5  # Win 1.5x: 30% chance
    else:
        multiplier = 2.0  # Win 2x: 20% chance

    # 4. Calculate Net Change
    if multiplier == 0.0:
        net_change = -wager
    else:
        net_change = int(wager * multiplier) - wager 

    # 5. Apply the outcome to the DB 
    new_used = used - net_change 
    
    # ACTUAL DB UPDATE
    supabase.table("users").update({
        "last_spin_date": today_str,
        "tokens_used": new_used
    }).eq("id", user["id"]).execute()

    return {
        "status": "success",
        "multiplier": multiplier,
        "net_change": net_change
    }

# ==========================================================================
# SETTINGS & AGENT SKILLS ROUTES
# ==========================================================================
import secrets
from fastapi import Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = get_current_user(request)
    
    try:
        res = supabase.table("users").select(
            "plan, email, gorilla_api_key, github_access_token, figma_access_token, supabase_access_token"
        ).eq("id", user["id"]).single().execute()
        
        if res and res.data:
            db_user = res.data
            
            raw_plan = db_user.get("plan") or "free"
            user["plan"] = str(raw_plan).lower().strip()
            
            user["email"] = db_user.get("email") or user.get("email", "")
            user["gorilla_api_key"] = db_user.get("gorilla_api_key") or ""
            user["has_github"] = bool(db_user.get("github_access_token"))
            user["has_figma"] = bool(db_user.get("figma_access_token"))
            user["has_supabase"] = bool(db_user.get("supabase_access_token"))
        else:
            user["plan"] = "free"
            user["gorilla_api_key"] = ""
            user["has_github"] = False
            user["has_figma"] = False
            user["has_supabase"] = False
            
    except Exception as e:
        print(f"Error loading settings: {e}")
        user["plan"] = "free"
        user["gorilla_api_key"] = ""
        
    success_msg = request.query_params.get("success")
    error_msg = request.query_params.get("error")
        
    return templates.TemplateResponse("dashboard/settings.html", {
        "request": request, 
        "user": user,
        "success": success_msg,
        "error": error_msg
    })


# 3. REGENERATE API KEY ROUTE
@app.post("/api/user/regenerate-key")
async def regenerate_api_key(request: Request):
    user = get_current_user(request)
    new_key = f"gb_live_{secrets.token_hex(24)}"
    try:
        supabase.table("users").update({"gorilla_api_key": new_key}).eq("id", user["id"]).execute()
        return RedirectResponse("/settings?success=API+Key+regenerated+successfully", status_code=303)
    except Exception as e:
        return RedirectResponse("/settings?error=Failed+to+regenerate+key", status_code=303)


@app.get("/settings/skills", response_class=HTMLResponse)
async def agent_skills_page(request: Request):
    user = get_current_user(request)
    api_key = ""
    try:
        res = supabase.table("users").select("plan, gorilla_api_key").eq("id", user["id"]).single().execute()
        if res and res.data:
            user["plan"] = res.data.get("plan", "free")
            api_key = res.data.get("gorilla_api_key", "")
        else:
            user["plan"] = "free"
    except Exception:
        user["plan"] = "free"
        
    return templates.TemplateResponse(
        "dashboard/agentskills.html", 
        {
            "request": request, 
            "user": user,
            "gorilla_api_key": api_key 
        }
    )

@app.post("/api/user/skills")
async def save_agent_skills(request: Request):
    user = get_current_user(request)
    try:
        payload = await request.json()
        supabase.table("users").update({"agent_skills": payload}).eq("id", user["id"]).execute()
        return JSONResponse({"status": "success", "message": "Skills saved successfully"})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"detail": f"Failed to save skills: {str(e)}"}, status_code=500)


# ==========================================================================
# PROJECT ROUTES (RLS-COMPLIANT)
# ==========================================================================
import io
import zipfile
import urllib.parse
import re
import os
import asyncio
import traceback
import json
import time
import mimetypes
import tempfile
import subprocess
import base64 # Added for handling image data
import httpx # Needed for the background task API call
from typing import Dict, Any, List, Optional
from fastapi import Request, Form, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, Response, JSONResponse

# --- NEW: BACKGROUND TASK FOR SNAPSHOTS ---
async def generate_project_snapshot(project_id: str, prompt: str, user_api_key: str):
    try:
        print(f"📸 Snapshot generation started for project {project_id}...")
        
        payload = {"prompt": f"Professional web UI dashboard preview: {prompt}", "samples": 1}
        headers = {
            "Authorization": f"Bearer {user_api_key}",
            "Content-Type": "application/json",
            "ngrok-skip-browser-warning": "true" # Bypass Ngrok landing page
        }
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://corrinne-turbid-illustratively.ngrok-free.dev/api/v1/images/generations", 
                json=payload, 
                headers=headers, 
                timeout=60.0
            )
            
            if resp.status_code != 200:
                print(f"⚠️ Proxy Error ({resp.status_code}): {resp.text}")
                return

            data = resp.json()
            snapshot_b64_data = None
            
            # --- ROBUST PARSING LOGIC ---
            # 1. Check if it's a list (Fireworks Native Format: [{"base64": "..."}])
            if isinstance(data, list) and len(data) > 0:
                snapshot_b64_data = data[0].get("base64")
            
            # 2. Check if it's a dict with 'data' (OpenAI Format: {"data": [{"b64_json": "..."}]})
            elif isinstance(data, dict):
                if "data" in data and len(data["data"]) > 0:
                    snapshot_b64_data = data["data"][0].get("b64_json") or data["data"][0].get("url")
                elif "base64" in data:
                    snapshot_b64_data = data["base64"]

            # 3. Save to Database
            if snapshot_b64_data:
                # Add prefix if missing
                if not snapshot_b64_data.startswith("data:image"):
                    snapshot_b64_data = f"data:image/jpeg;base64,{snapshot_b64_data}"

                supabase.table("projects").update({"snapshot_b64": snapshot_b64_data}).eq("id", project_id).execute()
                print(f"✅ Snapshot saved to Supabase for {project_id}!")
            else:
                print(f"⚠️ PARSE FAIL. Raw response was: {str(data)[:200]}")
                
    except Exception as e:
        print(f"⚠️ Task crashed: {e}")

# 1. CREATE PAGE (Stash prompt in session & detect Figma)
@app.get("/projects/createit", response_class=HTMLResponse)
async def project_create_page(request: Request, prompt: Optional[str] = None):
    user = get_current_user(request)
    
    is_figma_link = False
    
    if prompt:
        request.session["stashed_prompt"] = prompt
        # Detect if the initial prompt is a figma URL so the template knows which animation to show
        if "figma.com" in prompt:
            is_figma_link = True
        
    return templates.TemplateResponse(
        "projects/project-create.html", 
        {
            "request": request, 
            "user": user, 
            "initial_prompt": prompt,
            "is_figma_link": is_figma_link # 🛑 Passes the flag to HTML!
        }
    )

# 2. CREATE ACTION (Backend Insert)
from backend.figma_import import fetch_and_compress_figma, compile_figma_to_react
import re
import urllib.parse
import asyncio
import os
import time
import uuid
import secrets
import httpx

@app.post("/projects/create")
async def create_project(
    request: Request,
    background_tasks: BackgroundTasks,
    prompt: Optional[str] = Form(None), 
    name: Optional[str] = Form(None), 
    description: str = Form(""),
    xmode: Optional[str] = Form(None),
    image_base64: Optional[str] = Form(None),
    figma_url: Optional[str] = Form(None),
    use_supabase: Optional[str] = Form(None)  # <-- INTERCEPTING THE DB FLAG
):
    user = get_current_user(request)

    def check_project_limit():
        res = supabase.table("projects").select("id", count="exact").eq("owner_id", user["id"]).execute()
        count = res.count if hasattr(res, 'count') and res.count is not None else len(res.data)
        if count >= 3:
            return supabase.table("projects").select("*").eq("owner_id", user["id"]).order("created_at", desc=True).execute().data
        return None

    # --- 1. FREE TIER LIMIT CHECK ---
    if user.get("plan") != "premium":
        try:
            projects_data = await asyncio.to_thread(check_project_limit)
            if projects_data is not None:
                return templates.TemplateResponse("dashboard.html", {
                    "request": request, 
                    "user": user,
                    "projects": projects_data,
                    "error": "Free Limit Reached (3/3). Upgrade to Pro to create unlimited projects."
                })
        except Exception as e:
            print(f"⚠️ Project limit check failed: {e}")
            pass

    # --- 2. PROMPT & IMAGE STASHING ---
    if prompt and not name:
        if figma_url:
            request.session["stashed_figma_url"] = figma_url
            
        # Re-detect figma status here just in case they arrived via POST instead of GET
        is_figma_link = False
        if figma_url or (prompt and "figma.com" in prompt):
            is_figma_link = True
            
        return templates.TemplateResponse(
            "projects/project-create.html", 
            {
                "request": request, 
                "user": user, 
                "initial_prompt": prompt,
                "stashed_image": image_base64,
                "is_figma_link": is_figma_link
            }
        )
    
    final_prompt = prompt or request.session.pop("stashed_prompt", None)
    final_figma_url = figma_url or request.session.pop("stashed_figma_url", None)
    
    project_name = name or "Untitled Project"
    final_image = image_base64
    final_figma_json = None 
    
    # ====================================================================
    # 🎨 3. FIGMA INTERCEPTOR (PRODUCTION MODE)
    # ====================================================================
    potential_url = final_figma_url or ""
    if not potential_url and final_prompt and "figma.com/" in final_prompt:
        match = re.search(r'(https://[^\s^?]*figma\.com/[^\s]*)', final_prompt)
        if match:
            potential_url = match.group(0)

    if potential_url:
        try:
            print(f"🎯 Figma Link Detected: {potential_url}")
            
            def get_figma_token():
                return supabase.table("users").select("figma_access_token").eq("id", user["id"]).single().execute()
            
            user_data = await asyncio.to_thread(get_figma_token)
            figma_token = user_data.data.get("figma_access_token") if user_data.data else None
            
            if not figma_token:
                return RedirectResponse("/dashboard?error=figma_not_linked", status_code=303)
                
            final_figma_json, figma_img_b64 = await fetch_and_compress_figma(potential_url, figma_token)
            print(f"✅ Figma extraction successful ({len(final_figma_json)} characters)")
            
            if figma_img_b64:
                final_image = figma_img_b64
            
            if "figma.com" in final_prompt:
                final_prompt = "Build a pixel-perfect React and Tailwind replica of the design structure. I have provided the exact layout rules, spacing, typography, and hex colors in the `.gorilla/figma.json` file. Read that file and implement it exactly."

        except Exception as e:
            print(f"⚠️ Figma Import Failed: {e}")
            return RedirectResponse(f"/dashboard?error={urllib.parse.quote(str(e))}", status_code=303)

    # --- 4. HEAVY LIFTING (DB & Files) ---
    def _heavy_lift_create():
        compiled_react_code = None
        figma_tokens_used = 0 
        
        # ⚡ 4A. SUPABASE PROVISIONING ENGINE ⚡
        supabase_env_content = ""
        project_ref_to_save = None

        if use_supabase == "true":
            print(f"🗄️ Provisioning remote Supabase DB for {project_name}...")
            user_keys = supabase.table("users").select("supabase_access_token").eq("id", user["id"]).single().execute()
            supa_token = user_keys.data.get("supabase_access_token") if user_keys.data else None

            if supa_token:
                try:
                    headers = {"Authorization": f"Bearer {supa_token}", "Content-Type": "application/json"}
                    with httpx.Client() as client:
                        # 1. Get or Create Organization
                        orgs_res = client.get("https://api.supabase.com/v1/organizations", headers=headers)
                        orgs = orgs_res.json() if orgs_res.status_code == 200 else []
                        org_id = orgs[0]["id"] if orgs else None

                        if not org_id:
                            new_org = client.post("https://api.supabase.com/v1/organizations", headers=headers, json={"name": "Gorilla Apps"}).json()
                            org_id = new_org.get("id")

                        # 2. Spin up the Database
                        if org_id:
                            db_pass = secrets.token_urlsafe(16)
                            # Supabase restricts names to 32 chars
                            safe_db_name = re.sub(r'[^a-zA-Z0-9 ]', '', project_name)[:32].strip() or "Gorilla App"
                            
                            proj_payload = {
                                "organization_id": org_id,
                                "name": safe_db_name,
                                "db_pass": db_pass,
                                "region": "us-east-1",
                                "plan": "free"
                            }
                            proj_res = client.post("https://api.supabase.com/v1/projects", headers=headers, json=proj_payload)
                            
                            if proj_res.status_code == 201:
                                proj_data = proj_res.json()
                                project_ref_to_save = proj_data.get("id")
                                supa_url = f"https://{project_ref_to_save}.supabase.co"
                                
                                # 3. Poll for the Anon Key (Takes a few seconds during provisioning)
                                supa_anon_key = "PROVISIONING_IN_PROGRESS"
                                for _ in range(5):
                                    keys_res = client.get(f"https://api.supabase.com/v1/projects/{project_ref_to_save}/api-keys", headers=headers)
                                    if keys_res.status_code == 200:
                                        keys = keys_res.json()
                                        anon_obj = next((k for k in keys if k.get("name") == "anon"), None)
                                        if anon_obj:
                                            supa_anon_key = anon_obj.get("api_key")
                                            break
                                    time.sleep(1.5)

                                supabase_env_content = f"\nVITE_SUPABASE_URL={supa_url}\nVITE_SUPABASE_ANON_KEY={supa_anon_key}\n"
                                print(f"✅ Supabase Project Provisioned! Ref: {project_ref_to_save}")
                            else:
                                print(f"⚠️ Supabase Provisioning Failed: {proj_res.text}")
                except Exception as e:
                    print(f"⚠️ Supabase Mgmt API Error: {e}")

        # ⚡ 4B. THE GEMINI COMPILER STEP ⚡
        if final_figma_json:
            or_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("GORILLA_API_KEY")
            if or_key:
                try:
                    compiled_react_code, figma_tokens_used = asyncio.run(compile_figma_to_react(final_figma_json, or_key))
                    if figma_tokens_used and figma_tokens_used > 0:
                        try:
                            add_monthly_tokens(user["id"], figma_tokens_used)
                            print(f"💰 Deducted {figma_tokens_used} tokens for Gemini Figma Compiler")
                        except Exception as tk_err:
                            print(f"⚠️ Failed to deduct tokens: {tk_err}")
                except Exception as e:
                    print(f"❌ Gemini Compiler thread failed: {e}")

        # 🛑 CHAT HISTORY HACK
        initial_history = []
        if compiled_react_code:
            initial_history.append({
                "role": "system",
                "content": "A Figma design was imported. A pre-compiler has already converted the design into the starting React code located in src/App.tsx. Your job is to help the user refine it, add state/interactivity, or split it into components as requested."
            })
            initial_history.append({
                "role": "assistant",
                "content": "✨ I have successfully compiled your Figma design into React! The preview is loading. What functionality or state would you like to add?"
            })
            if final_prompt and "figma.com" not in final_prompt:
                initial_history.append({"role": "user", "content": final_prompt})
        elif final_figma_json:
            initial_history.append({
                "role": "system",
                "content": f"FIGMA DESIGN DATA: You MUST use this exact structural JSON to build the React/Tailwind UI. Do not hallucinate classes. Rely on the 'layoutMode', 'itemSpacing', and hex 'fills'. Data:\n{final_figma_json}"
            })

        # --- Generate Unique Auth ID for the App ---
        gorilla_auth_id = str(uuid.uuid4())

        # --- UPDATE DB: Save the supabase_project_ref ---
        res = supabase.table("projects").insert({
            "owner_id": user["id"], 
            "name": project_name, 
            "gorilla_auth_id": gorilla_auth_id, 
            "supabase_project_ref": project_ref_to_save,  # <-- SAVED SO EDITOR KNOWS TO OPEN AGENT
            "description": description or (final_prompt[:200] if final_prompt else ""),
            "prompt_image": final_image,
            "snapshot_b64": final_image, 
            "chat_history": initial_history 
        }).execute()
        
        if not res.data: 
            raise Exception("DB Insert Failed - Check Service Role Key")
            
        pid = res.data[0]['id']
        
        clean_name = re.sub(r'[^a-z0-9-]', '-', project_name.lower()).strip('-') or "app"
        final_subdomain = f"{clean_name}-{pid}" 
        supabase.table("projects").update({"subdomain": final_subdomain}).eq("id", pid).execute()
        
        bp_dir = globals().get("BOILERPLATE_DIR")
        if not bp_dir or not os.path.isdir(bp_dir):
            bp_dir = os.path.join(ROOT_DIR, "backend", "boilerplate")
            if not os.path.isdir(bp_dir):
                bp_dir = os.path.join(ROOT_DIR, "boilerplate")

        if os.path.isdir(bp_dir):
            files_to_insert = []
            
            # --- INJECT .ENV WITH BOTH AUTH ID AND SUPABASE KEYS ---
            files_to_insert.append({
                "project_id": pid,
                "path": ".env",
                "content": f"VITE_GORILLA_AUTH_ID={gorilla_auth_id}\n{supabase_env_content}"
            })

            for root, dirs, files in os.walk(bp_dir):
                dirs[:] = [d for d in dirs if d not in ["node_modules", ".git", "dist", "build"]]
                for file in files:
                    if file.startswith('.'): continue
                    abs_path = os.path.join(root, file)
                    rel_path = os.path.relpath(abs_path, bp_dir).replace("\\", "/")
                    try:
                        with open(abs_path, "r", encoding="utf-8") as f:
                            files_to_insert.append({
                                "project_id": pid,
                                "path": rel_path,
                                "content": f.read()
                            })
                    except: continue 

            if files_to_insert and compiled_react_code:
                for f in files_to_insert:
                    if f["path"] in ["src/App.tsx", "src/App.jsx"]:
                        f["content"] = compiled_react_code
                        print(f"💉 Injected Gemini React code perfectly into {f['path']}")
                        break

            if files_to_insert:
                try:
                    supabase.table("files").insert(files_to_insert).execute()
                except Exception as e:
                    for f in files_to_insert:
                        try: supabase.table("files").upsert(f, on_conflict="project_id,path").execute()
                        except: pass

        if final_image:
            try:
                supabase.table("files").insert({
                    "project_id": pid,
                    "path": ".gorilla/prompt_image.b64",
                    "content": final_image
                }).execute()
            except Exception as e:
                print(f"⚠️ Failed to save image to virtual FS: {e}")
                
        if final_figma_json:
            try:
                supabase.table("files").insert({
                    "project_id": pid,
                    "path": ".gorilla/figma.json",
                    "content": final_figma_json
                }).execute()
            except Exception as e:
                print(f"⚠️ Failed to save figma.json to virtual FS: {e}")
        
        return pid

    # --- 5. EXECUTION ---
    try:
        pid = await asyncio.to_thread(_heavy_lift_create)
        
        if final_prompt and not final_figma_json and not final_image:
            try:
                def get_api_key():
                    return supabase.table("users").select("gorilla_api_key").eq("id", user["id"]).single().execute()
                
                user_api_data = await asyncio.to_thread(get_api_key)
                api_key = user_api_data.data.get("gorilla_api_key", "") if user_api_data and user_api_data.data else ""
                
                if api_key.startswith("gb_live_"):
                    background_tasks.add_task(generate_project_snapshot, pid, final_prompt, api_key)
            except Exception as e:
                pass
        
        if xmode == "true":
            target_url = f"/projects/{pid}/editor/xmode"
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
    
    # 1. Fetch User Data (API Keys & Integrations)
    user_data = db_select_one("users", {"id": user["id"]}, "gorilla_api_key, github_access_token, supabase_access_token")
    api_key = user_data.get("gorilla_api_key", "") if user_data else ""
    has_github = bool(user_data and user_data.get("github_access_token"))
    has_supabase = bool(user_data and user_data.get("supabase_access_token"))
    
    # 2. Fetch Project Data
    try:
        res = supabase.table("projects").select("*").eq("id", project_id).single().execute()
        project = res.data
    except Exception:
        project = {} 
    
    # 3. Token Check
    used, limit = get_token_usage_and_limit(user["id"])
    user["tokens"] = {"used": used, "limit": limit}
    
    chat_history = project.get("chat_history", []) if project else []
    
    # 4. DB Agent Resume Logic
    project_has_db = bool(project and project.get("supabase_project_ref"))
    last_user_msg = next((msg.get("content", "").lower() for msg in reversed(chat_history) if msg.get("role") == "user"), "")
    
    resume_db_agent = project_has_db or ("database" in last_user_msg) or ("supabase" in last_user_msg)

    return templates.TemplateResponse(
        "projects/project-editor.html",
        {
            "request": request, 
            "project_id": project_id, 
            "project": project, 
            "project_name": project.get("name", "Untitled Project") if project else "Untitled Project",
            "file": file, 
            "user": user,
            "initial_prompt": prompt,
            "has_github": has_github,
            "has_supabase": has_supabase,
            "project_has_db": project_has_db,
            "resume_db_agent": resume_db_agent,
            "gorilla_api_key": api_key,
            "chat_history": json.dumps(chat_history) 
        }
    )

# 5. PREVIEW PAGE
@app.get("/projects/{project_id}/preview", response_class=HTMLResponse)
async def project_preview(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    return templates.TemplateResponse(
        "projects/project-preview.html",
        {"request": request, "project_id": project_id, "project_name": project.get("name", "Untitled Project") if project else "Untitled Project", "user": user}
    )

# 6. SETTINGS PAGE
import base64
import mimetypes
from typing import Optional
from fastapi import Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse

# 6. SETTINGS PAGE (GET Route remains exactly the same)
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
        {
            "request": request, 
            "project_id": project_id, 
            "project": project, 
            "project_name": project.get("name", "Untitled Project") if project else "Untitled Project", 
            "user": user
        }
    )

# UPGRADED: Save Route with Base64 Image Processing
@app.post("/projects/{project_id}/settings")
async def project_settings_save(
    request: Request, 
    project_id: str, 
    name: str = Form(...), 
    description: str = Form(""),
    snapshot: Optional[UploadFile] = File(None)  # Added the file catcher
):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    update_data = {
        "name": name, 
        "description": description
    }

    # If the user uploaded a new image, process it into Base64
    if snapshot and snapshot.filename:
        file_bytes = await snapshot.read()
        mime_type, _ = mimetypes.guess_type(snapshot.filename)
        
        if mime_type and mime_type.startswith('image/'):
            encoded_str = base64.b64encode(file_bytes).decode('utf-8')
            update_data["snapshot_b64"] = f"data:{mime_type};base64,{encoded_str}"
    
    supabase.table("projects").update(update_data).eq("id", project_id).execute()
    
    return RedirectResponse(f"/projects/{project_id}/settings", status_code=303)

# 7. EXPORT TO ZIP
@app.get("/api/project/{project_id}/export")
async def project_export(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    try:
        user_record = db_select_one("users", {"id": user["id"]}, "plan")
        current_plan = user_record.get("plan") if user_record else "free"
    except:
        current_plan = "free"

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
# PROJECT DELETION
# ==========================================================================
@app.post("/api/project/{project_id}/delete")
async def delete_project(request: Request, project_id: str):
    """Completely wipes a project and its associated files from the DB."""
    user = get_current_user(request)
    _require_project_owner(user, project_id)

    try:
        # 1. Delete all virtual files associated with the project
        supabase.table("files").delete().eq("project_id", project_id).execute()
        
        # 2. Delete the project record itself
        supabase.table("projects").delete().eq("id", project_id).execute()
        
        # Note: If a remote Supabase DB was provisioned for this project, 
        # we leave it active on the user's Supabase account for safety, 
        # but the Gorilla linkage is destroyed.
        
        return JSONResponse({"status": "success", "detail": "Project annihilated."})
    except Exception as e:
        print(f"❌ Project Deletion Error: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete project.")

# ==========================================================================
# REUSABLE AGENT LOOP - Streamlined Version (Unified Full-Stack & DB)
# ==========================================================================
import httpx
import re
import asyncio
from typing import List, Dict

# Import both Agent classes for conditional usage
from backend.ai.agent import Agent as RegularAgent, _render_token_limit_message
from backend.ai.Xagent import XAgent
from backend.ai.supabase_agent import SupabaseAgentSwarm

# Global tracking for active AI fixes
active_ai_fixes = set()

@app.post("/api/project/{project_id}/agent/start")
async def agent_start(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
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

    # Use multi-part parsing to safely handle Base64 strings
    form_data = await request.form()
    prompt = str(form_data.get("prompt", ""))
    xmode = str(form_data.get("xmode", "false")).lower() == "true"
    image_base64 = form_data.get("image_base64")
    
    # 🛑 CATCH THE DB REQUEST FLAG FROM HTML
    is_db_request = str(form_data.get("is_db_request", "false")).lower() == "true"

    # 🛑 Guarantee the old image is overwritten
    if image_base64:
        try:
            db_upsert("files", {"project_id": project_id, "path": ".gorilla/prompt_image.b64", "content": image_base64}, on_conflict="project_id,path")
            print(f"📸 Mid-chat image successfully overwritten in DB for project {project_id}")
        except Exception as err:
            print(f"⚠️ Failed to save mid-chat image: {err}")

    emit_status(project_id, "Agent received prompt")
    emit_log(project_id, "user", prompt)

    asyncio.create_task(
        run_agent_loop(project_id, prompt, user["id"], xmode, None, False, is_db_request)
    )
    return {"started": True}

async def run_agent_loop(project_id: str, prompt: str, user_id: str, is_xmode: bool = False, history: List[Dict] = None, skip_planner: bool = False, is_db_request: bool = False, is_system_task: bool = False):
    try:
        await asyncio.sleep(0.5)
        file_tree = await _fetch_file_tree(project_id)
        
        proj_data = db_select_one("projects", {"id": project_id}, "name, chat_history, supabase_project_ref")
        project_ref = proj_data.get("supabase_project_ref") if proj_data else None
        project_name = proj_data.get("name", "Gorilla App") if proj_data else "Gorilla App"
        
        # 🛑 AMNESIA FIX: Instantly save the user's prompt to the DB so it's never lost
        db_history = proj_data.get("chat_history", []) if proj_data and proj_data.get("chat_history") else []
        if is_system_task:
            db_history.append({"role": "system", "content": f"SYSTEM AUTOMATION TASK: {prompt}"})
        else:
            db_history.append({"role": "user", "content": prompt})
        supabase.table("projects").update({"chat_history": db_history}).eq("id", project_id).execute()
        
        user_api_data = db_select_one("users", {"id": user_id}, "gorilla_api_key, supabase_access_token")
        api_key = user_api_data.get("gorilla_api_key", "") if user_api_data else ""
        supa_token = user_api_data.get("supabase_access_token") if user_api_data else None
        
        # 🛑 THE AMNESIA FIX: Memory Injector
        # We weave the recent conversation directly into the prompt so the Swarm never loses context
        contextual_prompt = prompt
        if len(db_history) > 1:
            # Grab the last 6 messages (excluding the one we just added)
            past_msgs = db_history[-7:-1]
            history_text = "\n".join([f"{m.get('role', 'user').upper()}: {m.get('content', '')}" for m in past_msgs])
            contextual_prompt = f"--- PREVIOUS CONVERSATION CONTEXT ---\n{history_text}\n\n--- CURRENT USER REQUEST ---\n{prompt}"

        # ==========================================================================
        # ⚡ MID-CHAT SUPABASE PROVISIONING ENGINE ⚡
        # ==========================================================================
        if is_db_request and not project_ref and supa_token:
            emit_status(project_id, "Provisioning Remote Database...")
            emit_log(project_id, "system", "Spinning up dedicated PostgreSQL instance...")
            try:
                import secrets
                import re
                headers = {"Authorization": f"Bearer {supa_token}", "Content-Type": "application/json"}
                async with httpx.AsyncClient() as client:
                    orgs = (await client.get("https://api.supabase.com/v1/organizations", headers=headers)).json()
                
                    # 🛑 THE FIX: If Supabase returns an error dictionary instead of a list, catch it cleanly!
                    if isinstance(orgs, dict) and "message" in orgs:
                        raise Exception(f"Supabase Error: {orgs.get('message')}. Please re-link your Supabase account in the Dashboard.")
                
                    # Safely grab the first org, or create one if the user has an empty list
                    if isinstance(orgs, list) and len(orgs) > 0:
                        org_id = orgs[0].get("id")
                    else:
                        new_org = (await client.post("https://api.supabase.com/v1/organizations", headers=headers, json={"name": "Gorilla Apps"})).json()
                        org_id = new_org.get("id")

                    if not org_id:
                        raise Exception("Could not find or create a Supabase organization.")

                    # Proceed with provisioning now that we safely have an org_id
                    db_pass = secrets.token_urlsafe(16)
                    safe_db_name = re.sub(r'[^a-zA-Z0-9 ]', '', project_name)[:32].strip() or "Gorilla App"
                    proj_res = await client.post("https://api.supabase.com/v1/projects", headers=headers, json={"organization_id": org_id, "name": safe_db_name, "db_pass": db_pass, "region": "us-east-1", "plan": "free"})
                
                    if proj_res.status_code == 201:
                        project_ref = proj_res.json().get("id")
                        supabase.table("projects").update({"supabase_project_ref": project_ref}).eq("id", project_id).execute()
                    
                        supa_anon_key = "PROVISIONING_IN_PROGRESS"
                        emit_log(project_id, "system", "Waiting for Postgres instance to boot (this can take 1-2 minutes)...")
                        
                        # 🛑 THE ANON_KEY FIX: Poll up to 30 times (120 seconds) for the DB to fully boot
                        for attempt in range(30):
                            keys_res = await client.get(f"https://api.supabase.com/v1/projects/{project_ref}/api-keys", headers=headers)
                            if keys_res.status_code == 200:
                                keys_list = keys_res.json()
                                # Make sure the DB is actually returning the keys array, not an empty list
                                if isinstance(keys_list, list) and len(keys_list) > 0:
                                    anon_obj = next((k for k in keys_list if k.get("name") == "anon"), None)
                                    if anon_obj and anon_obj.get("api_key"):
                                        supa_anon_key = anon_obj.get("api_key")
                                        emit_log(project_id, "system", "✅ Database keys generated successfully!")
                                        break
                            
                            # Keep the user informed that it's still polling
                            if attempt % 3 == 0:
                                emit_log(project_id, "debugger", f"Instance still booting... (Attempt {attempt+1}/30)")
                            await asyncio.sleep(4)

                        # Write exactly what Vite expects
                        env_content = f"\nVITE_SUPABASE_URL=https://{project_ref}.supabase.co\nVITE_SUPABASE_ANON_KEY={supa_anon_key}\n"
                    
                        # Safely inject into existing .env
                        existing_env = file_tree.get(".env", "")
                        db_upsert("files", {"project_id": project_id, "path": ".env", "content": existing_env + env_content}, on_conflict="project_id,path")
                        emit_file_changed(project_id, ".env")
                        emit_log(project_id, "system", f"Database successfully provisioned! Ref: {project_ref}")
                    else:
                        raise Exception(f"Project creation failed: {proj_res.text}")
                    
            except Exception as e:
                emit_log(project_id, "system", f"Failed to provision database: {e}")

        # ==========================================================================
        # PATH A: SUPABASE FULL-STACK MODE
        # ==========================================================================
        if project_ref and supa_token:
            emit_status(project_id, "Initializing Full-Stack DB Architect...")
            emit_phase(project_id, "planner")
            emit_progress(project_id, "Designing Architecture...", 20)

            swarm = SupabaseAgentSwarm(project_id)
            
            # Pass the contextual_prompt so it remembers the conversation
            result = await swarm.solve(user_request=contextual_prompt, file_tree=file_tree)
            
            if result.get("status") == "needs_clarification":
                # 🛑 THE ORGANIC FIX: Stop appending manual questions. 
                # Also strip the robotic "Plan created." text
                assistant_msg = result.get("assistant_message", "I need a bit more clarification.")
                assistant_msg = assistant_msg.replace("Plan created.\n", "").replace("Plan created.", "").strip()
                
                db_history.append({"role": "assistant", "content": assistant_msg})
                supabase.table("projects").update({"chat_history": db_history}).eq("id", project_id).execute()
                
                emit_log(project_id, "assistant", assistant_msg)
                emit_status(project_id, "Waiting for User") 
                emit_progress(project_id, "Input Required", 100)
                return

            operations = result.get("operations", [])
            assistant_msg = result.get("assistant_message", "Applying Schema and Code updates.")
            
            # 🛑 AMNESIA FIX: Instantly save Assistant Message before operations start
            db_history.append({"role": "assistant", "content": assistant_msg})
            supabase.table("projects").update({"chat_history": db_history}).eq("id", project_id).execute()
            emit_log(project_id, "assistant", assistant_msg)

            for op in operations:
                path = op.get("path", "")
                content = op.get("content", "")
                if not path or not content: continue

                db_upsert("files", {"project_id": project_id, "path": path, "content": content}, on_conflict="project_id,path")
                emit_file_changed(project_id, path)

                if path.endswith(".sql") and path.startswith("migrations/"):
                    emit_phase(project_id, "coder")
                    emit_progress(project_id, f"Running Remote Migration: {path}", 70)

                    # 🛑 THE FIX: Wait on 404s, use correct database URL
                    for attempt in range(3):
                        try:
                            headers = {"Authorization": f"Bearer {supa_token}", "Content-Type": "application/json"}
                            async with httpx.AsyncClient(timeout=30.0) as client:
                                res = await client.post(f"https://api.supabase.com/v1/projects/{project_ref}/database/query", headers=headers, json={"query": content})
                                
                                if res.status_code == 404:
                                    if attempt == 2:
                                        emit_log(project_id, "system", "❌ Fatal Error: Supabase project not found. It may have been deleted manually.")
                                        emit_log(project_id, "assistant", "I couldn't connect to the database. Did you delete it from your Supabase dashboard? I've unlinked it so we can provision a new one on your next request.")
                                        supabase.table("projects").update({"supabase_project_ref": None}).eq("id", project_id).execute()
                                        break
                                    else:
                                        emit_log(project_id, "debugger", f"Database booting up (404). Waiting 5s... (Attempt {attempt+1}/3)")
                                        await asyncio.sleep(5)
                                        continue
                                
                                if res.status_code in [200, 201]:
                                    emit_log(project_id, "system", f"✅ Postgres Migration Successful: {path}")
                                    break 
                                else:
                                    err_text = res.text
                                    emit_log(project_id, "debugger", f"⚠️ SQL Error: {err_text}. Auto-healing... (Attempt {attempt+1}/3)")
                                    debug_result = await swarm.debug(err_text, {path: content})
                                    debug_ops = debug_result.get("operations", [])
                                    
                                    if debug_ops:
                                        content = debug_ops[0].get("content", content)
                                        db_upsert("files", {"project_id": project_id, "path": path, "content": content}, on_conflict="project_id,path")
                                        emit_file_changed(project_id, path)
                        except Exception as e:
                            emit_log(project_id, "debugger", f"Network Error hitting Remote DB: {e}. Retrying...")
                            await asyncio.sleep(3)

            emit_status(project_id, "Done")
            emit_progress(project_id, "Ready", 100)
            return

        # ==========================================================================
        # PATH B: STANDARD MODE
        # ==========================================================================
        agent = XAgent() if is_xmode else RegularAgent()
        
        if not skip_planner:
            emit_phase(project_id, "planner")
            emit_progress(project_id, "Thinking...", 10)
            
            plan_context = {"project_id": project_id, "files": list(file_tree.keys()), "api_key": api_key, "history": db_history}
            if ".gorilla/prompt_image.b64" in file_tree:
                plan_context["image_context"] = file_tree[".gorilla/prompt_image.b64"]
                plan_context["image_filename"] = ".gorilla/prompt_image.b64"

            # Pass the contextual_prompt
            plan_res = await asyncio.to_thread(agent.plan, user_request=contextual_prompt, project_context=plan_context)
            
            tk = plan_res.get("usage", {}).get("total_tokens", 0)
            if tk and user_id: add_monthly_tokens(user_id, tk)
            
            base_msg = plan_res.get("assistant_message", "I'm working on that...")
            
            # 🛑 THE ORGANIC FIX FOR STANDARD MODE
            assistant_msg = base_msg.replace("Plan created.\n", "").replace("Plan created.", "").strip()

            # 🛑 AMNESIA FIX: Instantly save Assistant Message for standard mode too
            db_history.append({"role": "assistant", "content": assistant_msg})
            supabase.table("projects").update({"chat_history": db_history}).eq("id", project_id).execute()
            emit_log(project_id, "assistant", assistant_msg)
            
            if plan_res.get("needs_clarification"):
                emit_status(project_id, "Waiting for User")
                emit_progress(project_id, "Input Required", 100)
                return

            tasks = plan_res.get("plan", {}).get("todo", [])
            if not tasks:
                emit_status(project_id, "Done")
                emit_progress(project_id, "Ready", 100)
                return
        else:
            emit_phase(project_id, "coder")
            emit_log(project_id, "assistant", "Applying fix...")
            assistant_msg = "Applying fix..."
        
        emit_phase(project_id, "coder")
        emit_progress(project_id, "Building...", 30)
        
        batch_files = {}
        tasks = plan_res.get("plan", {}).get("todo", []) if not skip_planner else [prompt]
        total = len(tasks)
        figma_context = next((msg["content"] for msg in db_history if msg.get("role") == "system" and "Figma design was imported" in msg.get("content", "")), None)

        for i, task in enumerate(tasks, 1):
            if user_id:
                try: enforce_token_limit_or_raise(user_id)
                except Exception: return # Changed to generic Exception to avoid undefined HTTPException if not imported globally

            pct = 30 + (60 * (i / total))
            emit_progress(project_id, f"Building...", pct)
            
            final_task_text = task
            if figma_context and i == 1:
                final_task_text = f"CRITICAL SYSTEM CONTEXT: {figma_context}\n\nUSER REQUEST: {task}"

            code_res = await agent.code(
                plan_section="" if skip_planner else f"Step {i}",
                plan_text=final_task_text,
                file_tree=file_tree,
                project_name=project_id,
                history=db_history
            )
            
            tk = code_res.get("usage", {}).get("total_tokens", 0)
            if tk and user_id: add_monthly_tokens(user_id, tk)

            ops = code_res.get("operations", [])

            # 🛑 THE FATAL ERROR CATCHER
            if not ops and i == 1 and not skip_planner:
                emit_log(project_id, "system", "❌ Agent workflow aborted due to unrecoverable formatting errors.")
                emit_status(project_id, "Fatal Error")
                emit_progress(project_id, "Failed", 100)
                return # Stop the loop so it doesn't hang!

            for op in ops:
                path = op.get("path")
                content = op.get("content")
                if path and content is not None:
                    batch_files[path] = content
                    file_tree[path] = content

        emit_phase(project_id, "files")
        emit_progress(project_id, "Saving...", 95)
        
        for path, content in batch_files.items():
            if path.startswith("static/") and path.endswith(".js"):
                try:
                    lint_err = await asyncio.to_thread(lint_code_with_esbuild, content, path)
                    if lint_err: emit_log(project_id, "system", f"Syntax Error in {path}:\n{lint_err}")
                except Exception: pass

            db_upsert("files", {"project_id": project_id, "path": path, "content": content}, on_conflict="project_id,path")
            emit_file_changed(project_id, path)

        emit_status(project_id, "Done")
        emit_progress(project_id, "Ready", 100)
        
    except Exception as e:
        emit_status(project_id, "Fatal Error")
        emit_log(project_id, "system", f"Workflow failed: {e}")
        import traceback
        print(traceback.format_exc())

# ==========================================================================
# AUTO-FIXING LOG ENDPOINT
# ==========================================================================

@app.post("/api/project/{project_id}/log")
async def log_browser_event(project_id: str, request: Request, background_tasks: BackgroundTasks):
    global active_ai_fixes
    try:
        form = await request.form()
        message = form.get("message", "")
        level = form.get("level", "INFO")
        print(f"[{level}] Browser Event: {message[:100]}...")

        if "error" in message.lower() or "failed" in message.lower() or "syntax error" in message.lower():
            
            # THE BACKEND MUTEX: Block duplicate AI agents
            if project_id in active_ai_fixes:
                print(f"[{project_id}] AI is already fixing this. Ignoring duplicate request.")
                return JSONResponse({"status": "ignored", "detail": "Fix already in progress"})
            
            active_ai_fixes.add(project_id)
            
            emit_log(project_id, "system", f"Browser Error Detected. Analyzing...")
            
            chat_history = []
            owner_id = None
            try:
                proj = db_select_one("projects", {"id": project_id}, "chat_history, owner_id")
                if proj: 
                    owner_id = proj.get("owner_id")
                    chat_history = proj.get("chat_history", [])
            except Exception as db_err:
                print(f"DB Fetch Error in Log Route: {db_err}")

            if not owner_id:
                active_ai_fixes.remove(project_id)
                return JSONResponse({"status": "error", "detail": "Owner not found"}, status_code=404)

            # Wrapper to ensure the lock is always removed
            async def run_and_unlock(*args, **kwargs):
                try:
                    await run_agent_loop(*args, **kwargs)
                finally:
                    if project_id in active_ai_fixes:
                        active_ai_fixes.remove(project_id)

            background_tasks.add_task(
                run_and_unlock, 
                project_id=project_id, 
                prompt=message,
                user_id=owner_id,
                history=chat_history,
                is_xmode=False, 
                skip_planner=True,
                is_system_task=True
            )
            
    except Exception as e:
        print(f"Logging Error: {e}")
        if project_id in active_ai_fixes:
            active_ai_fixes.remove(project_id)
        import traceback
        traceback.print_exc()
        
    return JSONResponse({"status": "ok"})

# ==========================================================================
# DEPLOYMENT ROUTES (VERCEL & GITHUB)
# ==========================================================================

# 1. Dedicated Route to securely LINK GitHub without logging out the active Google user
@app.get("/auth/github/link")
async def link_github_account(request: Request):
    user = get_current_user(request)
    if not GITHUB_CLIENT_ID or not GITHUB_REDIRECT_URI:
        raise HTTPException(500, "GitHub Auth config missing.")
    
    # We pass 'link' in state so the callback knows we are just attaching a token
    scope = "user:email repo"
    auth_url = f"https://github.com/login/oauth/authorize?client_id={GITHUB_CLIENT_ID}&redirect_uri={GITHUB_REDIRECT_URI}&scope={scope}&state=link_{user['id']}"
    return RedirectResponse(auth_url)

# 2. Render Deploy Wizard
@app.get("/projects/{project_id}/deploy", response_class=HTMLResponse)
async def project_deploy_page(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    try:
        res = supabase.table("projects").select("*").eq("id", project_id).single().execute()
        project = res.data
    except Exception:
        project = {}
        
    user_data = db_select_one("users", {"id": user["id"]}, "github_access_token, gorilla_api_key")
    has_github = bool(user_data and user_data.get("github_access_token"))
    api_key = user_data.get("gorilla_api_key", "")

    return templates.TemplateResponse(
        "projects/deploy.html",
        {
            "request": request,
            "project_id": project_id,
            "project": project,
            "has_github": has_github,
            "gorilla_api_key": api_key, # <--- Passing the key to the template
            "user": user
        }
    )

# 3. Optimize Codebase for Vercel
import json
from fastapi import Request, HTTPException

# ==========================================================================
# DEPLOY OPTIMIZE FIX (With Supabase Vercel Injection)
# ==========================================================================
import json
import urllib.parse
import httpx
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

@app.post("/api/project/{project_id}/deploy-optimize")
async def optimize_for_vercel(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    try:
        user_data = db_select_one("users", {"id": user["id"]}, "gorilla_api_key")
        api_key = user_data.get("gorilla_api_key", "") if user_data else ""

        # FETCH THE UNIQUE AUTH ID FOR THIS PROJECT
        project_data = db_select_one("projects", {"id": project_id}, "gorilla_auth_id")
        auth_id = project_data.get("gorilla_auth_id", "") if project_data else ""

        # ⚡ FETCH SUPABASE KEYS FROM VFS .ENV FILE ⚡
        env_file = db_select_one("files", {"project_id": project_id, "path": ".env"})
        supa_url = ""
        supa_key = ""
        if env_file and env_file.get("content"):
            for line in env_file.get("content", "").split("\n"):
                if line.startswith("VITE_SUPABASE_URL="): 
                    supa_url = line.split("=", 1)[1].strip()
                if line.startswith("VITE_SUPABASE_ANON_KEY="): 
                    supa_key = line.split("=", 1)[1].strip()

        vercel_json_content = {
            "version": 2,
            "builds": [
                {"src": "server.js", "use": "@vercel/node"},
                {"src": "package.json", "use": "@vercel/static-build"}
            ],
            "rewrites": [
                {"source": "/api/(.*)", "destination": "/server.js"},
                {"source": "/(.*)", "destination": "/index.html"}
            ],
            "env": {
                "GORILLA_API_KEY": api_key,
                "VITE_GORILLA_AUTH_ID": auth_id,
                "VITE_SUPABASE_URL": supa_url,         # <--- INJECTED FOR VERCEL
                "VITE_SUPABASE_ANON_KEY": supa_key     # <--- INJECTED FOR VERCEL
            }
        }
        
        # PROPERLY UPSERT INTO THE FILES TABLE
        db_upsert(
            "files", 
            {
                "project_id": project_id, 
                "path": "vercel.json", 
                "content": json.dumps(vercel_json_content, indent=2)
            }, 
            on_conflict="project_id,path"
        )

        optimization_prompt = """
        We are deploying this full-stack application to Vercel Serverless. You MUST perform this exactly:
        Overwrite `server.js` completely. Do NOT call `app.listen(...)` at the bottom. Vercel requires you to EXPORT the express app instead. End the file with `export default app;` or `module.exports = app;`.
        Do NOT create or modify `vercel.json` (it has already been handled).   
        """
        
        await run_agent_loop(
            project_id=project_id,
            prompt=optimization_prompt,
            user_id=user["id"],
            is_xmode=False,
            skip_planner=True,
            is_system_task=True
        )
        
        try:
            supabase.table("projects").update({"vercel_optimized": True}).eq("id", project_id).execute()
        except Exception:
            pass

        return {"status": "ok", "detail": "Optimized for Vercel"}
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, detail=str(e))

# ==========================================================================
# DEPLOY PUSH (Ships the optimized code to GitHub to trigger Vercel)
# ==========================================================================
@app.post("/api/project/{project_id}/deploy-push")
async def push_for_deployment(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    try:
        user_data = db_select_one("users", {"id": user["id"]}, "github_access_token")
        if not user_data or not user_data.get("github_access_token"):
            return JSONResponse({"detail": "GitHub account not connected. Please link GitHub in settings."}, status_code=400)
        
        token = user_data["github_access_token"]
        
        res = supabase.table("projects").select("*").eq("id", project_id).single().execute()
        project = res.data
        
        # Sanitize to replace spaces and special characters with hyphens (required by GitHub)
        raw_name = project.get("name", "gorilla-project")
        repo_name = re.sub(r'[^a-z0-9-]', '-', raw_name.lower()).strip('-')
        
        if not repo_name:
            repo_name = f"gorilla-project-{project_id[:6]}"
            
        files_res = supabase.table("files").select("path,content").eq("project_id", project_id).execute()
        files = getattr(files_res, "data", [])
        if not files and isinstance(files_res, list): files = files_res

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"}
        
        async with httpx.AsyncClient() as client:
            # A. Create or Find Repository
            repo_res = await client.post(
                "https://api.github.com/user/repos", 
                json={"name": repo_name, "private": True, "auto_init": True}, 
                headers=headers
            )
            if repo_res.status_code not in [201, 422]:
                return JSONResponse({"detail": f"GitHub Repo Creation Failed: {repo_res.text}"}, status_code=500)
            
            repo_data = repo_res.json()
            full_name = repo_data.get("full_name")
            
            # If repo already exists (422)
            if repo_res.status_code == 422:
                user_info_res = await client.get("https://api.github.com/user", headers=headers)
                if user_info_res.status_code == 200:
                    login = user_info_res.json().get("login")
                    full_name = f"{login}/{repo_name}"
                else:
                    return JSONResponse({"detail": "Failed to fetch GitHub username for existing repo."}, status_code=500)
                
                # SAFETY NET: Check if the existing repo is completely empty!
                branch_res = await client.get(f"https://api.github.com/repos/{full_name}/branches/main", headers=headers)
                if branch_res.status_code == 404:
                    import base64
                    readme_content = base64.b64encode(b"# Init\n").decode("utf-8")
                    await client.put(
                        f"https://api.github.com/repos/{full_name}/contents/README.md",
                        json={"message": "Initialize empty repository", "content": readme_content},
                        headers=headers
                    )

            # B. Bulk Create Blobs & Tree
            tree = []
            for f in files:
                path = f.get("path", "")
                if path.startswith(".gorilla/"): continue
                
                # GitHub API rejects strictly empty strings for inline blobs. Give it a single space.
                content = f.get("content")
                if not content:
                    content = " "
                    
                tree.append({
                    "path": path.lstrip("/"),
                    "mode": "100644",
                    "type": "blob",
                    "content": content
                })
            
            if len(tree) == 0:
                tree.append({
                    "path": "README.md",
                    "mode": "100644",
                    "type": "blob",
                    "content": f"# {project.get('name', 'Gorilla Project')}\n\nAuto-generated by Gor://a Builder."
                })
                
            tree_res = await client.post(f"https://api.github.com/repos/{full_name}/git/trees", json={"tree": tree}, headers=headers)
            
            if tree_res.status_code != 201:
                return JSONResponse({"detail": f"Git Tree Error: {tree_res.text}"}, status_code=500)
                
            tree_sha = tree_res.json()["sha"]
            
            # C. Create Commit
            commit_res = await client.post(
                f"https://api.github.com/repos/{full_name}/git/commits", 
                json={"message": "Git Commit via Gor://a Builder", "tree": tree_sha}, 
                headers=headers
            )
            if commit_res.status_code != 201:
                return JSONResponse({"detail": f"Commit Error: {commit_res.text}"}, status_code=500)
            
            commit_sha = commit_res.json()["sha"]
            
            # D. Update Reference (Create or Update Main Branch)
            ref_res = await client.post(f"https://api.github.com/repos/{full_name}/git/refs", json={"ref": "refs/heads/main", "sha": commit_sha}, headers=headers)
            if ref_res.status_code == 422: # Reference already exists, force update it
                await client.patch(f"https://api.github.com/repos/{full_name}/git/refs/heads/main", json={"sha": commit_sha, "force": True}, headers=headers)
            
            repo_url = f"https://github.com/{full_name}"
            
            # E. Save to DB
            supabase.table("projects").update({"github_repo_url": repo_url}).eq("id", project_id).execute()
            
            return JSONResponse({
                "status": "ok", 
                "detail": "Code successfully pushed to GitHub.",
                "repo_url": repo_url,
                "full_name": full_name
            })
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"detail": str(e)}, status_code=500)

# ==========================================================================
# APP AUTH GATEWAY (For Generated Apps)
# ==========================================================================

@app.get("/api/v1/app-auth/login", response_class=HTMLResponse)
async def app_auth_login_page(request: Request, auth_id: str, return_url: str = ""):
    """Renders the Hosted Login Page for the generated app."""
    proj = db_select_one("projects", {"gorilla_auth_id": auth_id}, "name")
    if not proj:
        return HTMLResponse("<h1>Invalid App Auth ID</h1>", status_code=404)
    
    request.session["app_auth_pending"] = {"auth_id": auth_id, "return_url": return_url}
    
    return templates.TemplateResponse("auth/appauth.html", {
        "request": request,
        "project_name": proj.get("name", "this app"),
        "auth_id": auth_id,
        "step": "login"
    })

@app.get("/api/v1/app-auth/{auth_id}/google")
async def app_auth_google_init(request: Request, auth_id: str):
    scope = "openid email profile"
    site_url = os.getenv('SITE_URL', 'https://gorillabuilder.dev')
    redirect_uri = urllib.parse.quote(f"{site_url}/api/v1/app-auth/google/callback")
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?response_type=code&client_id={GOOGLE_CLIENT_ID}&redirect_uri={redirect_uri}&scope={scope}&state={auth_id}"
    return RedirectResponse(auth_url)

@app.get("/api/v1/app-auth/{auth_id}/github")
async def app_auth_github_init(request: Request, auth_id: str):
    scope = "user:email"
    site_url = os.getenv('SITE_URL', 'https://gorillabuilder.dev')
    redirect_uri = urllib.parse.quote(f"{site_url}/api/v1/app-auth/github/callback")
    auth_url = f"https://github.com/login/oauth/authorize?client_id={GITHUB_CLIENT_ID}&redirect_uri={redirect_uri}&scope={scope}&state={auth_id}"
    return RedirectResponse(auth_url)

@app.get("/api/v1/app-auth/google/callback")
async def app_auth_google_callback(request: Request, code: str, state: str):
    site_url = os.getenv('SITE_URL', 'https://gorillabuilder.dev')
    async with httpx.AsyncClient() as client:
        res = await client.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": f"{site_url}/api/v1/app-auth/google/callback",
            "grant_type": "authorization_code",
        })
        tokens = res.json()
        access_token = tokens.get("access_token")
        
        user_res = await client.get(
            "https://www.googleapis.com/oauth2/v1/userinfo", 
            headers={"Authorization": f"Bearer {access_token}"}
        )
        google_user = user_res.json()
        
    user_payload = {
        "id": google_user.get("id"),
        "email": google_user.get("email"),
        "name": google_user.get("name"),
        "avatar": google_user.get("picture"),
        "provider": "google"
    }
    
    return templates.TemplateResponse("auth/appauth.html", {
        "request": request,
        "step": "success",
        "user_data": json.dumps(user_payload)
    })

@app.get("/api/v1/app-auth/github/callback")
async def app_auth_github_callback(request: Request, code: str, state: str):
    site_url = os.getenv('SITE_URL', 'https://gorillabuilder.dev')
    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://github.com/login/oauth/access_token", 
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": f"{site_url}/api/v1/app-auth/github/callback"
            },
            headers={"Accept": "application/json"}
        )
        tokens = res.json()
        access_token = tokens.get("access_token")
        
        user_res = await client.get(
            "https://api.github.com/user", 
            headers={"Authorization": f"Bearer {access_token}"}
        )
        github_user = user_res.json()
        
    user_payload = {
        "id": str(github_user.get("id")),
        "email": github_user.get("email"),
        "name": github_user.get("name") or github_user.get("login"),
        "avatar": github_user.get("avatar_url"),
        "provider": "github"
    }
    
    return templates.TemplateResponse("auth/appauth.html", {
        "request": request,
        "step": "success",
        "user_data": json.dumps(user_payload)
    })

# ==========================================================================
# GITHUB PUBLISH
# ==========================================================================
@app.post("/projects/{project_id}/github/publish")
async def publish_to_github(request: Request, project_id: str):
    try:
        user = get_current_user(request)
        
        proj_check = supabase.table("projects").select("owner_id").eq("id", project_id).single().execute()
        if not proj_check.data or proj_check.data["owner_id"] != user["id"]:
            return JSONResponse({"detail": "Unauthorized"}, status_code=403)
        
        user_data = db_select_one("users", {"id": user["id"]}, "github_access_token")
        if not user_data or not user_data.get("github_access_token"):
            return JSONResponse({"detail": "GitHub account not connected."}, status_code=400)
        
        token = user_data["github_access_token"]
        
        res = supabase.table("projects").select("*").eq("id", project_id).single().execute()
        project = res.data
        
        # --- UPDATED LOGIC: Make repo name match the Gorilla project name ---
        raw_name = project.get("name", "gorilla-project")
        # Sanitize to replace spaces and special characters with hyphens (required by GitHub)
        repo_name = re.sub(r'[^a-z0-9-]', '-', raw_name.lower()).strip('-')
        
        # Fallback just in case the name was completely invalid characters
        if not repo_name:
            repo_name = f"gorilla-project-{project_id[:6]}"
        
        files_res = supabase.table("files").select("path,content").eq("project_id", project_id).execute()
        files = getattr(files_res, "data", [])
        if not files and isinstance(files_res, list): files = files_res

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"}
        
        async with httpx.AsyncClient() as client:
            # A. Create or Find Repository
            repo_res = await client.post(
                "https://api.github.com/user/repos", 
                json={"name": repo_name, "private": True, "auto_init": True}, 
                headers=headers
            )
            if repo_res.status_code not in [201, 422]:
                return JSONResponse({"detail": f"GitHub Repo Creation Failed: {repo_res.text}"}, status_code=500)
            
            repo_data = repo_res.json()
            full_name = repo_data.get("full_name")
            
            # If repo already exists (422)
            if repo_res.status_code == 422:
                user_info_res = await client.get("https://api.github.com/user", headers=headers)
                if user_info_res.status_code == 200:
                    login = user_info_res.json().get("login")
                    full_name = f"{login}/{repo_name}"
                else:
                    return JSONResponse({"detail": "Failed to fetch GitHub username for existing repo."}, status_code=500)
                
                # SAFETY NET: Check if the existing repo is completely empty!
                branch_res = await client.get(f"https://api.github.com/repos/{full_name}/branches/main", headers=headers)
                if branch_res.status_code == 404:
                    import base64
                    # The repo exists but is empty! Initialize it manually via the Contents API
                    readme_content = base64.b64encode(b"# Init\n").decode("utf-8")
                    await client.put(
                        f"https://api.github.com/repos/{full_name}/contents/README.md",
                        json={"message": "Initialize empty repository", "content": readme_content},
                        headers=headers
                    )

            # B. Bulk Create Blobs & Tree
            tree = []
            for f in files:
                path = f.get("path", "")
                if path.startswith(".gorilla/"): continue
                
                # GitHub API rejects strictly empty strings for inline blobs. Give it a single space.
                content = f.get("content")
                if not content:
                    content = " "
                    
                tree.append({
                    "path": path.lstrip("/"),
                    "mode": "100644",
                    "type": "blob",
                    "content": content
                })
            
            # If the tree is completely empty, inject a default README so the commit always succeeds.
            if len(tree) == 0:
                tree.append({
                    "path": "README.md",
                    "mode": "100644",
                    "type": "blob",
                    "content": f"# {project.get('name', 'Gorilla Project')}\n\nAuto-generated by Gor://a Builder."
                })
                
            tree_res = await client.post(f"https://api.github.com/repos/{full_name}/git/trees", json={"tree": tree}, headers=headers)
            
            if tree_res.status_code != 201:
                return JSONResponse({"detail": f"Git Tree Error: {tree_res.text}"}, status_code=500)
                
            tree_sha = tree_res.json()["sha"]
            
            # C. Create Commit
            commit_res = await client.post(f"https://api.github.com/repos/{full_name}/git/commits", json={"message": "Publish via Gor://a Builder", "tree": tree_sha}, headers=headers)
            if commit_res.status_code != 201:
                return JSONResponse({"detail": f"Commit Error: {commit_res.text}"}, status_code=500)
            commit_sha = commit_res.json()["sha"]
            
            # D. Update Reference (Create or Update Main Branch)
            ref_res = await client.post(f"https://api.github.com/repos/{full_name}/git/refs", json={"ref": "refs/heads/main", "sha": commit_sha}, headers=headers)
            if ref_res.status_code == 422: # Reference already exists, force update it
                await client.patch(f"https://api.github.com/repos/{full_name}/git/refs/heads/main", json={"sha": commit_sha, "force": True}, headers=headers)
            
            repo_url = f"https://github.com/{full_name}"
            
            # E. Save to DB
            supabase.table("projects").update({"github_repo_url": repo_url}).eq("id", project_id).execute()
            
            return JSONResponse({"status": "ok", "repo_url": repo_url})
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"detail": str(e)}, status_code=500)

# ==========================================================================
# FILE API ROUTES
# ==========================================================================

@app.get("/api/project/{project_id}/files")
async def get_project_files(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    
    res = (
        supabase.table("files")
        .select("path,content")
        .eq("project_id", project_id)
        .execute()
    )
    if asyncio.iscoroutine(res): res = await res
    
    rows = getattr(res, "data", [])
    if not rows and isinstance(res, list): rows = res
        
    # 🛑 FRONTEND SHARK FILTER: 
    # Stop the WebContainer from downloading corrupted lockfiles or useless bloat
    clean_rows = []
    for r in rows:
        path = r.get("path", "")
        # If it's a lockfile, git dir, or node_modules, do NOT send it to the browser VM
        if any(x in path for x in ["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "node_modules", ".git"]):
            continue
        clean_rows.append(r)
        
    return {"files": clean_rows}


@app.get("/api/project/{project_id}/file")
async def get_file_content(request: Request, project_id: str, path: str):
    # 🛑 SHARK FILTER FOR DIRECT FETCH:
    # Prevent the editor from manually requesting giant lockfiles and crashing
    if any(x in path for x in ["package-lock.json", "yarn.lock", "pnpm-lock.yaml"]):
        return JSONResponse({"content": "// Lockfiles are hidden by the system to prevent network truncation."})

    try:
        res = supabase.table("files").select("content").eq("project_id", project_id).eq("path", path).execute()
        if asyncio.iscoroutine(res): res = await res
        
        content = ""
        if res.data and len(res.data) > 0:
            content = res.data[0].get("content", "")

        return JSONResponse({"content": content})
    except Exception as e:
        return JSONResponse({"content": f"// Error loading file: {e}"})

import base64
import mimetypes
from fastapi import Request, Form, UploadFile, File

import base64
import mimetypes
from fastapi import Request, HTTPException

@app.post("/api/project/{project_id}/save")
async def save_file(request: Request, project_id: str):
    # user = get_current_user(request)
    # _require_project_owner(user, project_id)
    
    # 1. Parse the raw form data to bypass strict FastAPI 422 type validation
    form_data = await request.form()
    file_path = form_data.get("file")
    content_obj = form_data.get("content")
    
    if not file_path or content_obj is None:
        raise HTTPException(status_code=400, detail="Missing file path or content")
        
    # 2. Dynamically handle the payload (File/Blob vs String)
    if hasattr(content_obj, "filename"):
        # The frontend sent a physical File or Blob
        file_bytes = await content_obj.read()
        try:
            # Try to decode it as text (for code files disguised as Blobs)
            final_content = file_bytes.decode('utf-8')
        except UnicodeDecodeError:
            # If decode fails, it's actual binary image data. Convert to base64!
            mime_type, _ = mimetypes.guess_type(str(file_path))
            encoded_str = base64.b64encode(file_bytes).decode('utf-8')
            final_content = f"data:{mime_type or 'application/octet-stream'};base64,{encoded_str}"
    else:
        # The frontend sent a standard URL-encoded string
        final_content = str(content_obj)
        
    # 3. Save to database
    db_upsert(
        "files", 
        {"project_id": project_id, "path": str(file_path), "content": final_content}, 
        on_conflict="project_id,path"
    )
    
    # supabase.table("projects").update({"updated_at": "now()"}).eq("id", project_id).execute()
    
    return {"success": True}


@app.get("/api/project/{project_id}/tokens")
async def check_tokens(request: Request, project_id: str):
    user = get_current_user(request)
    used, limit = get_token_usage_and_limit(user["id"])
    return {"used": used, "limit": limit}


# ==========================================================================
# STATIC FILE SERVING & WEBCONTAINER SUPPORT
# ==========================================================================
import mimetypes

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
import json
import time

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

def emit_log(pid: str, role: str, text: str) -> None:
    """Emits log to UI via SSE and saves to virtual file system."""
    progress_bus.emit(pid, {"type": "log", "role": role, "text": text})
    
    if role not in ["user", "assistant", "system"] and pid:
        try:
            existing = db_select_one("files", {"project_id": pid, "path": ".gorilla/thoughts.json"})
            logs = []
            if existing and existing.get("content"):
                try: logs = json.loads(existing.get("content"))
                except: pass
                
            logs.append({"role": role, "text": text, "ts": time.time()})
            if len(logs) > 100: logs = logs[-100:]
            
            db_upsert("files", {"project_id": pid, "path": ".gorilla/thoughts.json", "content": json.dumps(logs)}, on_conflict="project_id,path")
        except Exception as e:
            pass

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

# ⚡ THE TELEPHONE WIRE ⚡
# This injects the emit_log function directly into the agents' brains!
import backend.ai.agent as regular_agent_module
import backend.ai.supabase_agent as supabase_agent_module
import re

def filtered_log_callback(pid: str, role: str, message: str):
    """Filters out noisy/internal logs before they hit the UI."""
    role = role.lower()
    
    # 1. Drop purely internal or overly noisy roles completely
    if role in ["mcp", "llm"]:
        return
        
    msg_lower = message.lower()
    
    # 2. Drop useless repetitive execution logs
    if "single task execution" in msg_lower: return
    if "executing 0 tasks" in msg_lower: return
    if "solving" in msg_lower: return
    if "Task" in msg_lower: return
    if "Concerns" in msg_lower: return
    if "�" in msg_lower: return
    if "Conversational agent swarm initialized" in msg_lower: return
    if "attempt" in msg_lower and "failed: model output" in msg_lower: return
    if "attempt" in msg_lower and "failed: could not extract" in msg_lower: return
    
    # 3. Beautify specific technical logs
    clean_msg = message
    if "read file:" in msg_lower:
        match = re.search(r'read file:\s*([^\s]+)', message, re.IGNORECASE)
        if match: clean_msg = f"Analyzed {match.group(1)}"
    elif "overwrite_file:" in msg_lower or "create_file:" in msg_lower:
        match = re.search(r'(overwrite_file|create_file):\s*([^\s]+)', message, re.IGNORECASE)
        if match: clean_msg = f"Updated {match.group(2)}"
    elif "reading" in msg_lower and "file(s)..." in msg_lower:
        clean_msg = "Gathering file context..."
    
    # 4. Clean up ugly AI prompt prefixes
    # Turns "[1/4] Step 1: Step 1: [Project: AppName...] Build UI" into "Building: Build UI"
    clean_msg = re.sub(r'\[Project:.*?\]\s*', '', clean_msg)
    clean_msg = re.sub(r'\[\d+/\d+\]\s*(Step\s*\d+:)?\s*(Step\s*\d+:)?\s*', 'Building: ', clean_msg).strip()
    
    emit_log(pid, role, clean_msg)

regular_agent_module.set_log_callback(filtered_log_callback)
supabase_agent_module.set_log_callback(filtered_log_callback)


# ==========================================================================
# GATEKEEPER: LINTING
# ==========================================================================
import tempfile
import subprocess
import os

def lint_code_with_esbuild(content: str, filename: str) -> str | None:
    if not filename.startswith("static/") or not filename.endswith(".js"):
        return None

    try:
        with tempfile.NamedTemporaryFile(suffix=".js", delete=False, mode='w', encoding='utf-8') as tmp:
            tmp.write(content)
            tmp.write_path = tmp.name

        result = subprocess.run(
            ["npx", "esbuild", tmp.name, "--loader=jsx", "--format=esm", "--log-level=error"],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        os.remove(tmp.name)

        if result.returncode != 0:
            return result.stderr.strip()
            
        return None
    except Exception as e:
        print(f"⚠️ Linter failed to run: {e}")
        return None


# ==========================================================================
# AI AGENT WORKFLOW (TRIGGER)
# ==========================================================================
async def _fetch_file_tree(project_id: str) -> Dict[str, str]:
    """
    Fetches the project files but strictly filters out massive lockfiles 
    to prevent AI context bloat and truncation errors.
    """
    try:
        query = supabase.table("files").select("path,content").eq("project_id", project_id)
        res = query.execute()
        
        if asyncio.iscoroutine(res):
            res = await res
            
        rows = getattr(res, "data", [])
        if not rows and isinstance(res, list):
            rows = res
            
        # 🛑 SHARK FILTER: Exclude files that cause 'Expected , or }' errors
        filtered_tree = {}
        for r in rows:
            path = r.get("path", "")
            if not path: continue
            
            # Skip giant cloggers that the AI doesn't need to read
            if any(x in path for x in ["package-lock.json", "yarn.lock", "node_modules", ".git"]):
                continue
                
            filtered_tree[path] = (r.get("content") or "")
            
        return filtered_tree
        
    except Exception as e:
        print(f"⚠️ Fetch Error: {e}")
        return {}


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

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        _gen(), 
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache", 
            "Connection": "keep-alive", 
            "X-Accel-Buffering": "no"
        }
    )

from fastapi.responses import HTMLResponse
@app.get("/projects/{project_id}/game", response_class=HTMLResponse)
async def project_game(request: Request, project_id: str):
    user = get_current_user(request)
    _require_project_owner(user, project_id)
    return templates.TemplateResponse("projects/game.html", {"request": request, "project_id": project_id, "user": user})

@app.post("/api/project/{project_id}/agent/ping")
async def agent_ping(request: Request, project_id: str):
    emit_log(project_id, "system", "🔥 Pong from backend")
    return {"ok": True}

import time
@app.get("/health")
async def health():
    return {"ok": True, "ts": int(time.time()), "dev_mode": DEV_MODE}


from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

# ==========================================================================
# THE MONKE NEGOTIATOR ROUTES
# ==========================================================================

class NegotiationResult(BaseModel):
    agreed_price: float

@app.get("/pricing/negotiate", response_class=HTMLResponse)
async def serve_negotiator(request: Request):
    """Serves the monkey.html UI with FULL user data fetched from the DB."""
    session_user = get_current_user(request) 
    if not session_user:
        return RedirectResponse(url="/auth/login")
        
    user_id = session_user["id"]
    
    # 🛑 THE FIX: We MUST fetch the API key and live stats from the database!
    try:
        res = supabase.table("users").select("*").eq("id", user_id).single().execute()
        if not res or not res.data:
            return RedirectResponse(url="/auth/login")
            
        db_user = res.data
        
        # Calculate live tokens for the UI
        used, limit = get_token_usage_and_limit(user_id) 
        db_user["tokens"] = {"remaining": max(0, limit - used)}
        
    except Exception as e:
        print(f"Error fetching user for negotiation: {e}")
        return RedirectResponse(url="/dashboard")

    # Now db_user absolutely contains 'gorilla_api_key' and 'tokens'
    return templates.TemplateResponse("freemium/monkey.html", {
        "request": request, 
        "user": db_user
    })

@app.post("/api/pricing/save-negotiation")
async def save_negotiation(request: Request, data: NegotiationResult):
    """
    Saves the final agreed price to the users table.
    Includes anti-cheat to ensure they don't spoof below $0.67.
    """
    session_user = get_current_user(request)
    if not session_user:
        raise HTTPException(status_code=401, detail="Not logged in")
        
    final_price = data.agreed_price
    
    # 🛑 ANTI-CHEAT: The absolute floor is $0.67. 
    if final_price < 0.67:
        final_price = 0.67
        
    try:
        # Save as TEXT to the first_month_price column
        supabase.table("users").update({
            "first_month_price": str(final_price)
        }).eq("id", session_user["id"]).execute()
        
        print(f"💰 User {session_user['id']} successfully negotiated first month to ${final_price}")
        
        return JSONResponse({
            "status": "success", 
            "checkout_url": f"/checkout/premium" # Redirects to Stripe logic
        })
    except Exception as e:
        print(f"Error saving negotiated price: {e}")
        raise HTTPException(status_code=500, detail="Database error while saving price.")

# ==========================================================================
# THE GORILLA AI PROXY GATEWAY
# ==========================================================================

# --- Proxy Environment Variables ---
FIREWORKS_API_KEY = os.getenv("FIREWORKS_API_KEY")
REMBG_API_KEY = os.getenv("REMBG_API_KEY", "") 
REMBG_API_URL = os.getenv("REMBG_API_URL", "http://localhost:5000/api/remove")

# Add these missing OpenRouter variables!
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_URL = os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
SITE_URL = os.getenv("SITE_URL", "https://gorillabuilder.dev")
SITE_NAME = os.getenv("SITE_NAME", "Gorilla Builder")

import math
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse, Response, StreamingResponse

security = HTTPBearer()

# --- Proxy Environment Variables ---
FIREWORKS_API_KEY = os.getenv("FIREWORKS_API_KEY")
REMBG_API_KEY = os.getenv("REMBG_API_KEY", "") # If your RemBG has a key
REMBG_API_URL = os.getenv("REMBG_API_URL", "http://localhost:5000/api/remove")

def _deduct_proxy_tokens(user_id: str, cost: float, feature: str):
    """Helper to safely deduct tokens for API Gateway usage."""
    if cost <= 0: return
    try:
        tokens_to_add = math.ceil(cost) # Round up fractional tokens
        
        # 1. Fetch current tokens_used from the users table
        res = supabase.table("users").select("tokens_used").eq("id", user_id).single().execute()
        current_used = res.data.get("tokens_used", 0) if res.data and res.data.get("tokens_used") else 0
        
        # 2. Add the cost and update the database
        new_total = current_used + tokens_to_add
        supabase.table("users").update({"tokens_used": new_total}).eq("id", user_id).execute()
        
        print(f"💰 Deducted {tokens_to_add} tokens for {feature} (User: {user_id})")
    except Exception as e:
        print(f"⚠️ Failed to deduct {cost} tokens for {user_id}: {e}")

async def verify_gorilla_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    THE BOUNCER: Intercepts all /api/v1 requests, verifies the gb_live_ key,
    and checks if the user has enough Gorilla Credits.
    """
    api_key = credentials.credentials
    if not api_key.startswith("gb_live_"):
        raise HTTPException(status_code=401, detail="Invalid API Key format. Must start with 'gb_live_'")
    
    # 1. Look up user by key
    res = supabase.table("users").select("id, plan").eq("gorilla_api_key", api_key).single().execute()
    if not res or not res.data:
        raise HTTPException(status_code=401, detail="Invalid API Key. Unauthorized.")
    
    user = res.data
    user_id = user["id"]
    
    # 2. Check Token Balance
    used, limit = get_token_usage_and_limit(user_id)
    if used >= limit:
        # 402 Payment Required perfectly matches OpenAI's out-of-credits error!
        raise HTTPException(status_code=402, detail="Payment Required: Gorilla Credits limit reached. Top up to continue.")
        
    return {"user_id": user_id, "plan": user.get("plan")}


# --- 1. LLM CHAT (OpenRouter / 0.5 tokens per 1 API token) ---
@app.post("/api/v1/chat/completions")
async def proxy_chat_completions(request: Request, auth=Depends(verify_gorilla_key)):
    user_id = auth["user_id"]
    payload = await request.json()
    
    # Force the model to OpenRouter's massive 120b model as requested
    payload["model"] = "meta-llama/llama-3.2-11b-vision-instruct" # Replace with your exact OpenRouter model string
    
    # Ask OpenRouter to send usage stats back even if it's a stream
    if "stream_options" not in payload:
        payload["stream_options"] = {"include_usage": True}
        
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": SITE_URL,
        "X-Title": SITE_NAME
    }
    
    # Handle Streaming Responses
    is_stream = payload.get("stream", False)
    
    if is_stream:
        async def stream_generator():
            total_tokens = 0
            async with httpx.AsyncClient() as client:
                async with client.stream("POST", OPENROUTER_URL, json=payload, headers=headers) as resp:
                    if resp.status_code != 200:
                        yield f"data: {json.dumps({'error': 'Upstream provider error'})}\n\n"
                        return
                    
                    async for chunk in resp.aiter_text():
                        yield chunk
                        # OpenRouter includes {"usage": {"total_tokens": X}} in the final SSE chunk
                        if '"usage":' in chunk and '"total_tokens":' in chunk:
                            try:
                                # Quick and dirty parse of the usage chunk
                                parts = chunk.split('"total_tokens":')
                                if len(parts) > 1:
                                    token_val = parts[1].split(',')[0].split('}')[0].strip()
                                    total_tokens = int(token_val)
                            except: pass
            
            # Bill the user after the stream closes (0.5 tokens per 1 API token)
            if total_tokens > 0:
                _deduct_proxy_tokens(user_id, total_tokens * 0.051, "chat_stream")
                
        return StreamingResponse(stream_generator(), media_type="text/event-stream")
    
    # Handle Standard Non-Streaming Responses
    else:
        async with httpx.AsyncClient() as client:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)
            
            data = resp.json()
            total_tokens = data.get("usage", {}).get("total_tokens", 0)
            
            # Bill the user (0.5 tokens per 1 API token)
            _deduct_proxy_tokens(user_id, total_tokens * 0.3, "chat")
            
            return JSONResponse(data)


@app.post("/api/v1/images/generations")
async def proxy_image_generations(request: Request, auth=Depends(verify_gorilla_key)):
    user_id = auth["user_id"]
    payload = await request.json()
    
    # Fireworks Native Parameters
    fireworks_payload = {
        "prompt": payload.get("prompt", ""),
        "samples": 1,
        "height": 1024,
        "width": 1024
    }
    
    headers = {
        "Authorization": f"Bearer {FIREWORKS_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"  # <--- CRITICAL: Ensures we get JSON back
    }
    
    url = "https://api.fireworks.ai/inference/v1/image_generation/accounts/fireworks/models/playground-v2-5-1024px-aesthetic"
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=fireworks_payload, headers=headers, timeout=60.0)
        
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Fireworks Error: {resp.text}")
        
        # Deduct tokens only on success
        _deduct_proxy_tokens(user_id, 250, "image_gen")
        
        return JSONResponse(resp.json())

# --- 3. SPEECH TO TEXT (Fireworks Whisper / 100 tokens per min) ---
@app.post("/api/v1/audio/transcriptions")
async def proxy_audio_transcriptions(
    file: UploadFile = File(...),
    model: str = Form("accounts/fireworks/models/whisper-v3-turbo"),
    auth=Depends(verify_gorilla_key)
):
    user_id = auth["user_id"]
    file_bytes = await file.read()
    
    # Heuristic Duration Calculation:
    # A standard mp3/m4a voice memo is roughly 1MB per minute.
    # We use file size to estimate minutes (minimum 1 minute)
    file_size_mb = len(file_bytes) / (1024 * 1024)
    estimated_minutes = max(1, math.ceil(file_size_mb)) 
    cost = estimated_minutes * 100
    
    headers = {"Authorization": f"Bearer {FIREWORKS_API_KEY}"}
    files_payload = {"file": (file.filename, file_bytes, file.content_type)}
    data_payload = {"model": "accounts/fireworks/models/whisper-v3-turbo"}
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.fireworks.ai/inference/v1/audio/transcriptions", 
            files=files_payload, 
            data=data_payload, 
            headers=headers
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        
        # Bill 100 tokens per estimated minute
        _deduct_proxy_tokens(user_id, cost, "stt_whisper")
        
        return JSONResponse(resp.json())


# --- 4. BACKGROUND REMOVAL (RemBG / 0 tokens / Free Forever) ---
@app.post("/api/v1/images/remove-background")
async def proxy_remove_background(file: UploadFile = File(...), auth=Depends(verify_gorilla_key)):
    # Verify key, but we don't bill them for this!
    file_bytes = await file.read()
    
    headers = {}
    if REMBG_API_KEY:
        headers["x-api-key"] = REMBG_API_KEY
        
    files_payload = {"file": (file.filename, file_bytes, file.content_type)}
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(REMBG_API_URL, files=files_payload, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=f"RemBG Error: {resp.text}")
        
        # Returns the raw PNG image file directly to the frontend
        return Response(content=resp.content, media_type="image/png")

@app.post("/api/v1/chat/completions/bargain")
async def proxy_chat_completions(request: Request, auth=Depends(verify_gorilla_key)):
    user_id = auth["user_id"]
    payload = await request.json()
    
    # Force the model to OpenRouter's massive 120b model as requested
    payload["model"] = "deepseek/deepseek-v3.2" # Replace with your exact OpenRouter model string
    
    # Ask OpenRouter to send usage stats back even if it's a stream
    if "stream_options" not in payload:
        payload["stream_options"] = {"include_usage": True}
        
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": SITE_URL,
        "X-Title": SITE_NAME
    }
    
    # Handle Streaming Responses
    is_stream = payload.get("stream", False)
    
    if is_stream:
        async def stream_generator():
            total_tokens = 0
            async with httpx.AsyncClient() as client:
                async with client.stream("POST", OPENROUTER_URL, json=payload, headers=headers) as resp:
                    if resp.status_code != 200:
                        yield f"data: {json.dumps({'error': 'Upstream provider error'})}\n\n"
                        return
                    
                    async for chunk in resp.aiter_text():
                        yield chunk
                        # OpenRouter includes {"usage": {"total_tokens": X}} in the final SSE chunk
                        if '"usage":' in chunk and '"total_tokens":' in chunk:
                            try:
                                # Quick and dirty parse of the usage chunk
                                parts = chunk.split('"total_tokens":')
                                if len(parts) > 1:
                                    token_val = parts[1].split(',')[0].split('}')[0].strip()
                                    total_tokens = int(token_val)
                            except: pass
            
            # Bill the user after the stream closes (0.5 tokens per 1 API token)
            if total_tokens > 0:
                _deduct_proxy_tokens(user_id, total_tokens * 0.3, "chat_stream")
                
        return StreamingResponse(stream_generator(), media_type="text/event-stream")
    
    # Handle Standard Non-Streaming Responses
    else:
        async with httpx.AsyncClient() as client:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)
            
            data = resp.json()
            total_tokens = data.get("usage", {}).get("total_tokens", 0)
            
            # Bill the user (0.5 tokens per 1 API token)
            _deduct_proxy_tokens(user_id, total_tokens * 0.3, "chat")
            
            return JSONResponse(data)


if __name__ == "__main__":
    import uvicorn
    from pyngrok import ngrok
    import sys

    # Open a tunnel to port 8000
    # This URL bypasses GitHub's Proxy completely
    public_url = ngrok.connect(8000).public_url
    print(f"\n🚀 \033[92mYOUR BYPASS URL: {public_url}\033[0m 🚀\n")

    uvicorn.run(app, host="0.0.0.0", port=8000)