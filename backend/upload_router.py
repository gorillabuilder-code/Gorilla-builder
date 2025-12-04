"""
upload_router.py — gor://a Asset Upload & Storage

Handles:
- User uploads (images, pdfs, other files)
- Saving to Supabase Storage in a dedicated bucket
- Returning public (or signed) URLs back to the frontend

Notes:
- Bucket name: "gor-assets"
- Requires:
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
- This file is designed to be mounted inside core_api.py as a router.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, status
from pydantic import BaseModel
from supabase import create_client, Client

from .core_api import get_current_user, User  # reuse auth dependency

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
ASSET_BUCKET = os.getenv("GOR_ASSET_BUCKET", "gor-assets")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Supabase env vars missing for upload_router.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

router = APIRouter(prefix="/upload", tags=["uploads"])


class UploadResponse(BaseModel):
    id: str
    bucket: str
    path: str
    url: Optional[str]


def _ensure_bucket_exists() -> None:
    """
    Ensure the bucket exists (Supabase Python client currently doesn't expose
    full storage admin operations, so this is a no-op placeholder for infra).
    In production, create the bucket once via dashboard/CLI.
    """
    # If you later wire in a management client, you can attempt creation here.
    return


def _safe_extension(filename: str) -> str:
    if "." not in filename:
        return ""
    ext = filename.rsplit(".", 1)[-1].lower()
    return ext


@router.post("", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
) -> UploadResponse:
    """
    Accepts a file and stores it in Supabase Storage under:
        gor-assets/{user_id}/YYYY/MM/DD/{random_uuid}.{ext}
    """

    _ensure_bucket_exists()

    ext = _safe_extension(file.filename or "")
    if ext not in {"png", "jpg", "jpeg", "webp", "gif", "pdf", "txt"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported file type. Allowed: png, jpg, jpeg, webp, gif, pdf, txt",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file.")

    random_id = uuid.uuid4().hex
    path = f"{user.id}/{random_id}.{ext}"

    try:
        storage = supabase.storage.from_(ASSET_BUCKET)
        storage.upload(path, content, {"content-type": file.content_type or "application/octet-stream"})
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to upload file: {exc}",
        )

    # Try to generate a public URL if bucket is public; else return path only.
    url: Optional[str] = None
    try:
        public = storage.get_public_url(path)
        url = public
    except Exception:
        url = None

    return UploadResponse(
        id=random_id,
        bucket=ASSET_BUCKET,
        path=path,
        url=url,
    )
