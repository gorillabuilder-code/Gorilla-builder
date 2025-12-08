"""
projects_api.py — gor://a Projects CRUD

Tables expected in Supabase:
- projects: id (uuid), owner_id (uuid), name (text), slug (text),
            description (text), created_at, updated_at
"""

from __future__ import annotations
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from supabase import create_client, Client

from ..settings import get_settings
from ..auth import get_current_user, User

settings = get_settings()
supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectBase(BaseModel):
    name: str
    description: str = ""


class Project(ProjectBase):
    id: uuid.UUID
    slug: str


class ProjectCreate(ProjectBase):
    pass


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


@router.get("", response_model=List[Project])
def list_projects(user: User = Depends(get_current_user)):
    resp = (
        supabase.table("projects")
        .select("id,name,slug,description")
        .eq("owner_id", str(user.id))
        .order("created_at", desc=True)
        .execute()
    )
    data = resp.data or []
    return [
        Project(
            id=uuid.UUID(row["id"]),
            name=row["name"],
            slug=row["slug"],
            description=row.get("description", ""),
        )
        for row in data
    ]


@router.post("", response_model=Project, status_code=status.HTTP_201_CREATED)
def create_project(body: ProjectCreate, user: User = Depends(get_current_user)):
    slug = body.name.lower().replace(" ", "-")[:40]
    resp = (
        supabase.table("projects")
        .insert(
            {
                "owner_id": str(user.id),
                "name": body.name,
                "slug": slug,
                "description": body.description,
            }
        )
        .select("id,name,slug,description")
        .single()
        .execute()
    ).data
    return Project(
        id=uuid.UUID(resp["id"]),
        name=resp["name"],
        slug=resp["slug"],
        description=resp.get("description", ""),
    )


@router.get("/{project_id}", response_model=Project)
def get_project(project_id: uuid.UUID, user: User = Depends(get_current_user)):
    resp = (
        supabase.table("projects")
        .select("id,name,slug,description,owner_id")
        .eq("id", str(project_id))
        .maybe_single()
        .execute()
    )
    data = resp.data
    if not data or data["owner_id"] != str(user.id):
        raise HTTPException(status_code=404, detail="Project not found")
    return Project(
        id=uuid.UUID(data["id"]),
        name=data["name"],
        slug=data["slug"],
        description=data.get("description", ""),
    )


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: uuid.UUID, user: User = Depends(get_current_user)):
    resp = (
        supabase.table("projects")
        .select("owner_id")
        .eq("id", str(project_id))
        .maybe_single()
        .execute()
    )
    data = resp.data
    if not data or data["owner_id"] != str(user.id):
        raise HTTPException(status_code=404, detail="Project not found")
    supabase.table("projects").delete().eq("id", str(project_id)).execute()
    # Optionally: cascade delete files, events etc.
    return
