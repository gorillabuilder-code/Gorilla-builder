"""
core_api.py — gor://a Core Backend API

This service provides the core HTTP API that the gor://a frontend
(landing page, projects view, IDE, etc.) talks to.

Responsibilities:
- Project CRUD
- Virtual file system (list/read/write/delete)
- Snapshots & rollback scaffolding
- WAL (write-ahead-log) for durability

Notes:
- Persistence is handled via Supabase (Postgres + Storage).
- Authentication is expected to be via Supabase JWT; in this file
  we keep a small, easily swappable `get_current_user` dependency.
- LLM usage (Groq) is *not* in this file; it lives in `backend/agent/`.
"""

from __future__ import annotations

import os
import uuid
from typing import List, Optional

from fastapi import (
    FastAPI,
    HTTPException,
    Depends,
    Header,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from supabase import create_client, Client


# -------------------------------------------------------------------
# Supabase Client
# -------------------------------------------------------------------

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError(
        "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in the environment."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# -------------------------------------------------------------------
# FastAPI App
# -------------------------------------------------------------------

app = FastAPI(
    title="GOR://A Core API",
    description="Core backend for gor://a full-stack AI app studio.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -------------------------------------------------------------------
# Auth / User Context
# -------------------------------------------------------------------

class User(BaseModel):
    id: uuid.UUID


def get_current_user(authorization: Optional[str] = Header(None)) -> User:
    """
    In production, validate the Supabase JWT here and extract user id.
    For now, accept a bare UUID in the Authorization header for simplicity.
    """
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    token = authorization.replace("Bearer ", "").strip()
    try:
        user_id = uuid.UUID(token)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid auth token")

    return User(id=user_id)


# -------------------------------------------------------------------
# Pydantic Schemas
# -------------------------------------------------------------------

class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None


class ProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    description: Optional[str]
    created_at: str
    updated_at: str


class FileReadRequest(BaseModel):
    path: str


class FileOut(BaseModel):
    id: uuid.UUID
    path: str
    content: str
    updated_at: str


class FileWriteRequest(BaseModel):
    path: str
    content: str


class FileDeleteRequest(BaseModel):
    path: str


class SnapshotCreateRequest(BaseModel):
    label: Optional[str] = None


class SnapshotOut(BaseModel):
    id: uuid.UUID
    label: Optional[str]
    created_at: str


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def _ensure_project_ownership(project_id: uuid.UUID, user: User) -> None:
    """
    Raise HTTP 404 if project does not exist or is not owned by the user.
    """
    resp = (
        supabase.table("projects")
        .select("*")
        .eq("id", str(project_id))
        .eq("owner_id", str(user.id))
        .single()
        .execute()
    )
    if resp.data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")


# -------------------------------------------------------------------
# Health
# -------------------------------------------------------------------

@app.get("/health", tags=["system"])
def health_check() -> dict:
    return {"status": "ok", "service": "gor://a-core-api"}


# -------------------------------------------------------------------
# Projects
# -------------------------------------------------------------------

@app.get("/projects", response_model=List[ProjectOut], tags=["projects"])
def list_projects(user: User = Depends(get_current_user)) -> List[ProjectOut]:
    resp = (
        supabase.table("projects")
        .select("*")
        .eq("owner_id", str(user.id))
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data or []


@app.post(
    "/projects",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    tags=["projects"],
)
def create_project(payload: ProjectCreate, user: User = Depends(get_current_user)) -> ProjectOut:
    resp = (
        supabase.table("projects")
        .insert(
            {
                "owner_id": str(user.id),
                "name": payload.name,
                "description": payload.description,
            }
        )
        .select("*")
        .single()
        .execute()
    )
    return resp.data


@app.patch("/projects/{project_id}", response_model=ProjectOut, tags=["projects"])
def update_project(
    project_id: uuid.UUID, payload: ProjectUpdate, user: User = Depends(get_current_user)
) -> ProjectOut:
    _ensure_project_ownership(project_id, user)

    update_data = {k: v for k, v in payload.dict().items() if v is not None}
    if not update_data:
        resp = (
            supabase.table("projects")
            .select("*")
            .eq("id", str(project_id))
            .single()
            .execute()
        )
        return resp.data

    update_data["updated_at"] = "now()"

    resp = (
        supabase.table("projects")
        .update(update_data)
        .eq("id", str(project_id))
        .select("*")
        .single()
        .execute()
    )
    return resp.data


@app.delete(
    "/projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["projects"],
)
def delete_project(project_id: uuid.UUID, user: User = Depends(get_current_user)) -> None:
    _ensure_project_ownership(project_id, user)
    supabase.table("projects").delete().eq("id", str(project_id)).execute()
    return


# -------------------------------------------------------------------
# Files (Virtual File System)
# -------------------------------------------------------------------

@app.get(
    "/projects/{project_id}/files/tree",
    response_model=List[str],
    tags=["files"],
)
def list_file_paths(project_id: uuid.UUID, user: User = Depends(get_current_user)) -> List[str]:
    _ensure_project_ownership(project_id, user)

    resp = (
        supabase.table("files")
        .select("path")
        .eq("project_id", str(project_id))
        .order("path")
        .execute()
    )
    return [row["path"] for row in (resp.data or [])]


@app.post(
    "/projects/{project_id}/files/read",
    response_model=FileOut,
    tags=["files"],
)
def read_file(
    project_id: uuid.UUID,
    payload: FileReadRequest,
    user: User = Depends(get_current_user),
) -> FileOut:
    _ensure_project_ownership(project_id, user)

    resp = (
        supabase.table("files")
        .select("*")
        .eq("project_id", str(project_id))
        .eq("path", payload.path)
        .single()
        .execute()
    )
    if resp.data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return resp.data


@app.put(
    "/projects/{project_id}/files/write",
    response_model=FileOut,
    tags=["files"],
)
def write_file(
    project_id: uuid.UUID,
    payload: FileWriteRequest,
    user: User = Depends(get_current_user),
) -> FileOut:
    """
    Atomic-style write:
    1. Insert WAL record with intended change.
    2. Upsert file row.
    3. Mark WAL entry as applied.
    """

    _ensure_project_ownership(project_id, user)

    # Step 1: WAL entry
    wal_insert = (
        supabase.table("wal")
        .insert(
            {
                "project_id": str(project_id),
                "operation": {
                    "type": "file_write",
                    "path": payload.path,
                    "content_preview": payload.content[:300],
                },
                "applied": False,
            }
        )
        .select("*")
        .single()
        .execute()
    )
    wal_id = wal_insert.data["id"]

    # Step 2: upsert file
    file_resp = (
        supabase.table("files")
        .upsert(
            {
                "project_id": str(project_id),
                "path": payload.path,
                "content": payload.content,
            },
            on_conflict="project_id,path",
        )
        .select("*")
        .single()
        .execute()
    )

    # Step 3: mark WAL as applied
    supabase.table("wal").update({"applied": True}).eq("id", wal_id).execute()

    return file_resp.data


@app.delete(
    "/projects/{project_id}/files/delete",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["files"],
)
def delete_file(
    project_id: uuid.UUID,
    payload: FileDeleteRequest,
    user: User = Depends(get_current_user),
) -> None:
    _ensure_project_ownership(project_id, user)

    # Record WAL
    supabase.table("wal").insert(
        {
            "project_id": str(project_id),
            "operation": {
                "type": "file_delete",
                "path": payload.path,
            },
            "applied": False,
        }
    ).execute()

    resp = (
        supabase.table("files")
        .delete()
        .eq("project_id", str(project_id))
        .eq("path", payload.path)
        .execute()
    )

    # If no row deleted, clean up WAL but treat as 404
    if not resp.data:
        supabase.table("wal").delete().eq("project_id", str(project_id)).eq(
            "operation->>path", payload.path
        ).execute()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    supabase.table("wal").update({"applied": True}).eq(
        "operation->>path", payload.path
    ).eq("project_id", str(project_id)).execute()
    return


# -------------------------------------------------------------------
# Snapshots
# -------------------------------------------------------------------

@app.get(
    "/projects/{project_id}/snapshots",
    response_model=List[SnapshotOut],
    tags=["snapshots"],
)
def list_snapshots(
    project_id: uuid.UUID, user: User = Depends(get_current_user)
) -> List[SnapshotOut]:
    _ensure_project_ownership(project_id, user)

    resp = (
        supabase.table("snapshots")
        .select("id,label,created_at")
        .eq("project_id", str(project_id))
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data or []


@app.post(
    "/projects/{project_id}/snapshots",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
    tags=["snapshots"],
)
def create_snapshot(
    project_id: uuid.UUID,
    payload: SnapshotCreateRequest,
    user: User = Depends(get_current_user),
) -> SnapshotOut:
    """
    Create a snapshot by reading all files for the project
    and storing them as a single compressed blob.
    Compression format can be defined in `durability/snapshots.py`;
    here we store a raw JSON-like structure for simplicity.
    """
    _ensure_project_ownership(project_id, user)

    files_resp = (
        supabase.table("files")
        .select("path,content,updated_at")
        .eq("project_id", str(project_id))
        .execute()
    )

    data_blob = {
        "project_id": str(project_id),
        "files": files_resp.data or [],
    }

    # NOTE: in production, compress & encode (e.g. zlib + base64).
    snapshot_insert = (
        supabase.table("snapshots")
        .insert(
            {
                "project_id": str(project_id),
                "label": payload.label or "Auto snapshot",
                "data": data_blob,
            }
        )
        .select("id,label,created_at")
        .single()
        .execute()
    )

    return snapshot_insert.data
