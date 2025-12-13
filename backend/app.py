from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import httpx
import os
import secrets

# ------------------------
# APP INIT
# ------------------------

app = FastAPI(title="GOR://A Backend ASGI")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("AUTH_SECRET_KEY", secrets.token_hex(32)),
)

app.mount("/styles", StaticFiles(directory="templates/styles"), name="styles")
app.mount("/scripts", StaticFiles(directory="templates/scripts"), name="scripts")
app.mount("/assets", StaticFiles(directory="templates/assets"), name="assets")

templates = Jinja2Templates(directory="templates")

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

# ------------------------
# STATIC PAGE ROUTES
# ------------------------

PAGES = {
    "/": "index.html",
    "/login": "login.html",
    "/signup": "signup.html",
    "/forgot-password": "forgot.html",
    "/settings": "settings.html",
    "/dashboard": "dashboard.html",
    "/workspace": "workspace.html",
    "/pricing": "pricing.html",
    "/help": "help.html",
    "/about": "about.html",
    "/builder": "builder-home.html",
    "/builder/new": "builder-new.html",
    "/builder/import": "builder-import.html",
    "/builder/marketplace": "marketplace.html",
    "/builder/docs": "builder-docs.html",
    "/projects": "projects-list.html",
    "/projects/create": "project-create.html",
}

for route, template in PAGES.items():
    async def page(request: Request, template=template):
        return templates.TemplateResponse(template, {"request": request})
    app.get(route, response_class=HTMLResponse)(page)

# ------------------------
# AUTH — EMAIL/PASSWORD (basic)
# ------------------------

@app.post("/auth/signup")
async def signup(email: str = Form(...), password: str = Form(...)):
    # 🔒 Replace with DB later
    return RedirectResponse("/dashboard", status_code=303)

@app.post("/auth/login")
async def login(email: str = Form(...), password: str = Form(...)):
    # 🔒 Replace with DB later
    return RedirectResponse("/dashboard", status_code=303)

# ------------------------
# AUTH — OAUTH PROVIDERS
# ------------------------

OAUTH_CONFIG = {
    "google": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "user_url": "https://www.googleapis.com/oauth2/v2/userinfo",
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
        "scope": "openid email profile",
    },
    "github": {
        "auth_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "user_url": "https://api.github.com/user",
        "client_id": os.getenv("GITHUB_CLIENT_ID"),
        "client_secret": os.getenv("GITHUB_CLIENT_SECRET"),
        "scope": "read:user user:email",
    },
}

@app.get("/auth/{provider}")
async def oauth_start(provider: str, request: Request):
    if provider not in OAUTH_CONFIG:
        raise HTTPException(404)

    cfg = OAUTH_CONFIG[provider]
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state

    redirect_uri = f"{BASE_URL}/auth/callback/{provider}"

    url = (
        f"{cfg['auth_url']}?"
        f"client_id={cfg['client_id']}&"
        f"redirect_uri={redirect_uri}&"
        f"response_type=code&"
        f"scope={cfg['scope']}&"
        f"state={state}"
    )

    return RedirectResponse(url)

@app.get("/auth/callback/{provider}")
async def oauth_callback(provider: str, request: Request, code: str, state: str):
    cfg = OAUTH_CONFIG.get(provider)
    if not cfg:
        raise HTTPException(404)

    if state != request.session.get("oauth_state"):
        raise HTTPException(403, "Invalid OAuth state")

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            cfg["token_url"],
            data={
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "code": code,
                "redirect_uri": f"{BASE_URL}/auth/callback/{provider}",
            },
            headers={"Accept": "application/json"},
        )

        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(400, "OAuth failed")

        user_resp = await client.get(
            cfg["user_url"],
            headers={"Authorization": f"Bearer {access_token}"},
        )

    request.session["user"] = user_resp.json()
    return RedirectResponse("/dashboard")

# ------------------------
# PROJECT SYSTEM (UNCHANGED)
# ------------------------

@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail(request: Request, project_id: str):
    return templates.TemplateResponse(
        "project-detail.html", {"request": request, "project_id": project_id}
    )

@app.get("/projects/{project_id}/editor", response_class=HTMLResponse)
async def project_editor(request: Request, project_id: str, file: str = "index.html"):
    return templates.TemplateResponse(
        "project-editor.html",
        {"request": request, "project_id": project_id, "file": file},
    )

@app.get("/projects/{project_id}/preview", response_class=HTMLResponse)
async def project_preview(request: Request, project_id: str):
    return templates.TemplateResponse(
        "project-preview.html", {"request": request, "project_id": project_id}
    )

# ------------------------
# DEPLOYED APP SIMULATION
# ------------------------

DEPLOY_DIR = "user_apps"

@app.get("/app/{project_id}/{path:path}")
async def serve_deployed_app(project_id: str, path: str):
    app_path = os.path.join(DEPLOY_DIR, project_id, path)

    if os.path.isdir(app_path):
        app_path = os.path.join(app_path, "index.html")

    if not os.path.exists(app_path):
        raise HTTPException(404, "File not found")

    return FileResponse(app_path)

# ------------------------
# API — FILE OPS
# ------------------------

@app.post("/api/project/{project_id}/save")
async def save_file(project_id: str, file: str, content: str):
    project_dir = os.path.join(DEPLOY_DIR, project_id)
    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, file), "w", encoding="utf-8") as f:
        f.write(content)
    return {"success": True}

@app.post("/api/project/{project_id}/generate-file")
async def generate_new_file(project_id: str, filename: str):
    project_dir = os.path.join(DEPLOY_DIR, project_id)
    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, filename), "w") as f:
        f.write("<!-- Generated by gor://a -->")
    return {"success": True}
