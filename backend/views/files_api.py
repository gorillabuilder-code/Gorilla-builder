"""
files_api.py — gor://a File management API

Tables:
- files: id (uuid), project_id (uuid), path (text), content (text),
         updated_at timestamp, created_at timestamp
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

router = APIRouter(prefix="/files", tags=["files"])


class FileInfo(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    path: str
    updated_at: str | None = None


class FileDetail(FileInfo):
    content: str


class FileWrite(BaseModel):
    project_id: uuid.UUID
    path: str
    content: str


class FileRename(BaseModel):
    new_path: str


def _ensure_project_owner(project_id: uuid.UUID, user: User):
    res = (
        supabase.table("projects")
        .select("id,owner_id")
        .eq("id", str(project_id))
        .maybe_single()
        .execute()
    )
    data = res.data
    if not data or data["owner_id"] != str(user.id):
        raise HTTPException(status_code=404, detail="Project not found")


@router.get("/{project_id}", response_model=List[FileInfo])
def list_files(project_id: uuid.UUID, user: User = Depends(get_current_user)):
    _ensure_project_owner(project_id, user)
    res = (
        supabase.table("files")
        .select("id,project_id,path,updated_at")
        .eq("project_id", str(project_id))
        .order("path")
        .execute()
    )
    data = res.data or []
    return [
        FileInfo(
            id=uuid.UUID(row["id"]),
            project_id=uuid.UUID(row["project_id"]),
            path=row["path"],
            updated_at=row.get("updated_at"),
        )
        for row in data
    ]


@router.get("/{project_id}/read", response_model=FileDetail)
def read_file(
    project_id: uuid.UUID, path: str, user: User = Depends(get_current_user)
):
    _ensure_project_owner(project_id, user)
    res = (
        supabase.table("files")
        .select("id,project_id,path,content,updated_at")
        .eq("project_id", str(project_id))
        .eq("path", path)
        .maybe_single()
        .execute()
    )
    row = res.data
    if not row:
        raise HTTPException(status_code=404, detail="File not found")
    return FileDetail(
        id=uuid.UUID(row["id"]),
        project_id=uuid.UUID(row["project_id"]),
        path=row["path"],
        content=row["content"],
        updated_at=row.get("updated_at"),
    )


@router.post("", response_model=FileDetail)
def write_file(body: FileWrite, user: User = Depends(get_current_user)):
    _ensure_project_owner(body.project_id, user)
    res = (
        supabase.table("files")
        .upsert(
            {
                "project_id": str(body.project_id),
                "path": body.path,
                "content": body.content,
            },
            on_conflict="project_id,path",
        )
        .select("id,project_id,path,content,updated_at")
        .single()
        .execute()
    ).data
    return FileDetail(
        id=uuid.UUID(res["id"]),
        project_id=uuid.UUID(res["project_id"]),
        path=res["path"],
        content=res["content"],
        updated_at=res.get("updated_at"),
    )


@router.post("/{project_id}/rename")
def rename_file(
    project_id: uuid.UUID,
    path: str,
    body: FileRename,
    user: User = Depends(get_current_user),
):
    _ensure_project_owner(project_id, user)
    res = (
        supabase.table("files")
        .update({"path": body.new_path})
        .eq("project_id", str(project_id))
        .eq("path", path)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="File not found")
    return {"success": True}


@router.delete("/{project_id}")
def delete_file(
    project_id: uuid.UUID, path: str, user: User = Depends(get_current_user)
):
    _ensure_project_owner(project_id, user)
    supabase.table("files").delete().eq("project_id", str(project_id)).eq("path", path).execute()
    return {"success": True}
