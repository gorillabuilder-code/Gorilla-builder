"""
builder_api.py — gor://a Preview and Builder Controls
"""

from __future__ import annotations
import uuid
from fastapi import APIRouter, Depends, HTTPException
from ..auth import User, get_current_user
from ..preview_server import preview_manager

router = APIRouter(prefix="/builder", tags=["builder"])


@router.post("/preview/start/{project_id}")
def start_preview(project_id: uuid.UUID, user: User = Depends(get_current_user)):
    port = preview_manager.start_preview(project_id)
    return {
        "success": True,
        "url": f"http://localhost:{port}/"
    }


@router.post("/preview/stop/{project_id}")
def stop_preview(project_id: uuid.UUID, user: User = Depends(get_current_user)):
    preview_manager.stop_preview(project_id)
    return {"success": True}
