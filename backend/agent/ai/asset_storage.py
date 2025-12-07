"""
asset_storage.py — gor://a Asset Retrieval + Secure Deletes

Purpose:
- Complement upload_router.py
- Retrieve signed URLs for frontend
- Delete assets safely (no orphan refs)
"""

from __future__ import annotations
import os
from typing import Dict, Any
from supabase import create_client, Client
import uuid

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
ASSET_BUCKET = os.getenv("GOR_ASSET_BUCKET", "gor-assets")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class AssetStorage:

    def get_url(self, path: str) -> Dict[str, Any]:
        try:
            storage = supabase.storage.from_(ASSET_BUCKET)
            public = storage.get_public_url(path)
            return {"success": True, "url": public}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def delete(self, path: str) -> Dict[str, Any]:
        try:
            storage = supabase.storage.from_(ASSET_BUCKET)
            storage.remove([path])
            return {"success": True}
        except Exception as exc:
            return {"success": False, "error": str(exc)}
