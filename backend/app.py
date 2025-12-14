from fastapi import FastAPI, Request, HTTPException, Form, Depends
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from supabase import create_client, Client
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
# SUPABASE INIT
# ------------------------

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY"),
)

# ------------------------
# AUTH HELPERS
# ------------------------

def get_current_user(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401)
    return user

# ------------------------
# STATIC PAGE ROUTES
# ------------------------

PAGES = {
    "/": "index.html",
    "/login": "login.html",
    "/signup": "signup.html",
    "/forgot-password": "forgot.html",
    "/pricing": "pricing.html",
    "/help": "help.html",
    "/about": "about.html",
}

for route, template in PAGES.items():
    async def page(request: Request, template=template):
        return templates.TemplateResponse(template, {"request": request})
    app.get(route, response_class=HTMLResponse)(page)

# ------------------------
# DASHBOARD (REAL DATA)
# ------------------------

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
        .execute()
        .data
    )

    return templates.TemplateResponse(
        "workspace.html",
        {"request": request, "projects": projects},
    )

# ------------------------
# PROJECT ROUTES
# ------------------------

@app.get("/projects", response_class=HTMLResponse)
async def projects_list(request: Request):
    user = get_current_user(request)

    projects = (
        supabase.table("projects")
        .select("*")
        .eq("owner_id", user["id"])
        .execute()
        .data
    )

    return templates.TemplateResponse(
        "projects-list.html",
        {"request": request, "projects": projects},
    )

@app.get("/projects/create", response_class=HTMLResponse)
async def project_create(request: Request):
    return templates.TemplateResponse("project-create.html", {"request": request})

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
            {
                "owner_id": user["id"],
                "name": name,
                "description": description,
            }
        )
        .execute()
        .data[0]
    )

    return RedirectResponse(f"/projects/{project['id']}", status_code=303)

@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail(request: Request, project_id: str):
    user = get_current_user(request)

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
        .execute()
        .data
    )

    return templates.TemplateResponse(
        "project-detail.html",
        {
            "request": request,
            "project": project,
            "files": files,
        },
    )

# ------------------------
# FILE API (SUPABASE)
# ------------------------

@app.post("/api/project/{project_id}/save")
async def save_file(
    request: Request,
    project_id: str,
    file: str = Form(...),
    content: str = Form(...),
):
    user = get_current_user(request)

    supabase.table("files").upsert(
        {
            "project_id": project_id,
            "path": file,
            "content": content,
        },
        on_conflict="project_id,path",
    ).execute()

    return {"success": True}

@app.get("/api/project/{project_id}/files")
async def list_files(request: Request, project_id: str):
    user = get_current_user(request)

    files = (
        supabase.table("files")
        .select("path,updated_at")
        .eq("project_id", project_id)
        .execute()
        .data
    )

    return {"files": files}
