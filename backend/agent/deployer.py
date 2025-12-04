"""
deployer.py — gor://a Application Packager

Purpose:
- Prepare projects for deployment or export
- Assemble a clean, reproducible, versioned bundle
- Include:
  - frontend files
  - backend handlers
  - migrations
  - config manifests
  - safety snapshot
- Ensure write-ahead log is empty or resolved

Supported Export Format:
  → .zip archive with full directory structure
"""

from __future__ import annotations

import io
import os
import uuid
import zipfile
from datetime import datetime
from typing import Dict, List, Any
from supabase import create_client, Client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Supabase environment keys missing for deployer.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class Deployer:

    def __init__(self):
        pass

    def _fetch_project_files(self, project_id: uuid.UUID) -> List[Dict[str, Any]]:
        """
        Reads all project files.
        """
        resp = (
            supabase.table("files")
            .select("path,content,updated_at")
            .eq("project_id", str(project_id))
            .order("path")
            .execute()
        )
        return resp.data or []

    def _fetch_snapshots(self, project_id: uuid.UUID) -> List[Dict[str, Any]]:
        resp = (
            supabase.table("snapshots")
            .select("id,label,created_at")
            .eq("project_id", str(project_id))
            .order("created_at", desc=True)
            .execute()
        )
        return resp.data or []

    def _wal_unresolved(self, project_id: uuid.UUID) -> bool:
        resp = (
            supabase.table("wal")
            .select("id")
            .eq("project_id", str(project_id))
            .eq("applied", False)
            .execute()
        )
        return bool(resp.data)

    def package_project(self, project_id: uuid.UUID) -> Dict[str, Any]:
        """
        Creates a .zip containing the entire project.
        Blocks deployment if WAL has unresolved entries.
        """

        if self._wal_unresolved(project_id):
            return {
                "success": False,
                "error": "Cannot export; unresolved WAL entries exist. Resolve or revert first.",
            }

        files = self._fetch_project_files(project_id)
        snapshots = self._fetch_snapshots(project_id)
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")

        zip_name = f"gor_app_{project_id}_{ts}.zip"
        mem_zip = io.BytesIO()

        with zipfile.ZipFile(mem_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:

            # ---------------------------
            # Export project files
            # ---------------------------
            for file in files:
                archive.writestr(file["path"], file["content"])

            # ---------------------------
            # Export metadata manifest
            # ---------------------------
            manifest = {
                "project_id": str(project_id),
                "exported_at": ts,
                "file_count": len(files),
                "snapshot_count": len(snapshots),
                "format": "gor://a-export-v1",
            }
            archive.writestr("project.manifest.json", str(manifest))

            # ---------------------------
            # Include snapshot markers (not heavy data)
            # ---------------------------
            snapshot_index = {
                "snapshots": [
                    {"id": s["id"], "label": s["label"], "created_at": s["created_at"]}
                    for s in snapshots
                ]
            }
            archive.writestr("snapshots/index.json", str(snapshot_index))

        mem_zip.seek(0)

        # Optionally store export artifact to Storage bucket
        # (Skip for now — return archive raw)
        return {
            "success": True,
            "filename": zip_name,
            "bytes": mem_zip.getvalue(),
        }
