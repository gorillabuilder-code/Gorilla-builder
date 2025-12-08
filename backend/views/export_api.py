"""
export_api.py — gor://a Export API

Single endpoint:
- GET /export/{project_id}

Returns: binary zip (gor_app_<id>.zip)
"""

from __future__ import annotations
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from supabase import create_client, Client

from ..settings import get_settings
from ..auth import get_current_user, User
from ..agent.deployer import Deployer

settings = get_settings()
supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
router = APIRouter(prefix="/export", tags=["export"])
deployer = Deployer()


@router.get("/{project_id}")
def export_project(project_id: uuid.UUID, user: User = Depends(get_current_user)):
    res = (
        supabase.table("projects")
        .select("id,owner_id,name")
        .eq("id", str(project_id))
        .maybe_single()
        .execute()
    )
    data = res.data
    if not data or data["owner_id"] != str(user.id):
        raise HTTPException(status_code=404, detail="Project not found")

    pkg = deployer.package_project(project_id)
    if not pkg["success"]:
        raise HTTPException(status_code=400, detail=pkg["error"])

    return Response(
        content=pkg["bytes"],
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{pkg["filename"]}"'},
    )
