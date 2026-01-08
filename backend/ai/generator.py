"""
generator.py — gor://a Build Operator

Executes operations returned from coder.py:
- create_file
- overwrite_file
- patch_file (diff-style)

Responsibilities:
- Perform atomic, durable operations
- Record Write-Ahead Log (WAL)
- Apply changes to Supabase table `files`
- Return applied results for IDE view refresh
"""

from __future__ import annotations

import os
import uuid
from typing import Dict, List, Any
from supabase import create_client, Client
import difflib
import json

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Supabase environment keys not found.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class Generator:

    def __init__(self):
        pass

    def _wal_record(self, project_id: uuid.UUID, operation: dict) -> None:
        supabase.table("wal").insert(
            {
                "project_id": str(project_id),
                "operation": operation,
                "applied": False,
            }
        ).execute()

    def _wal_applied(self, wal_id: uuid.UUID) -> None:
        supabase.table("wal").update({"applied": True}).eq("id", str(wal_id)).execute()

    def _apply_patch(self, original: str, diff: str) -> str:
        """
        Apply a unified diff string to the original code.
        """
        patched = difflib.restore(diff.splitlines(), 1)
        return "\n".join(patched)

    def _atomic_write(self, project_id: uuid.UUID, path: str, content: str) -> Dict[str, Any]:
        """
        Insert or update file row in Supabase as an atomic write.
        """
        wal_id = (
            supabase.table("wal")
            .insert(
                {
                    "project_id": str(project_id),
                    "operation": {
                        "type": "atomic_write",
                        "path": path,
                        "content_preview": content[:200],
                    },
                    "applied": False,
                }
            )
            .select("id")
            .single()
            .execute()
        ).data["id"]

        row = (
            supabase.table("files")
            .upsert(
                {"project_id": str(project_id), "path": path, "content": content},
                on_conflict="project_id,path",
            )
            .select("id,path,content,updated_at")
            .single()
            .execute()
        ).data

        self._wal_applied(wal_id)
        return row

    def generate(
        self,
        project_id: uuid.UUID,
        operations: List[Dict[str, Any]],
        current_files: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        """
        Execute multiple operations sequentially.
        Returns results for IDE refresh: {path, content, action}
        """

        results = []

        for op in operations:

            action = op["action"]
            path = op["path"]

            # ------------------------------------------------
            # NEW FILE
            # ------------------------------------------------
            if action == "create_file":
                content = op["content"]
                row = self._atomic_write(project_id, path, content)
                results.append({"action": "created", "path": path, "content": content})
                continue

            # ------------------------------------------------
            # FULL OVERWRITE
            # ------------------------------------------------
            if action == "overwrite_file":
                content = op["content"]
                row = self._atomic_write(project_id, path, content)
                results.append({"action": "overwritten", "path": path, "content": content})
                continue

            # ------------------------------------------------
            # PATCH / DIFF
            # ------------------------------------------------
            if action == "patch_file":
                if path not in current_files:
                    raise FileNotFoundError(f"Cannot patch missing file: {path}")

                original = current_files[path]
                diff = op["diff"]

                # Reconstruct modified content
                patched_content = self._apply_patch(original, diff)

                row = self._atomic_write(project_id, path, patched_content)
                results.append({"action": "patched", "path": path, "content": patched_content})
                continue

            # ------------------------------------------------
            # Unknown Action
            # ------------------------------------------------
            raise ValueError(f"Unknown operation action: {action}")

        return results


"""
Example Usage:

generator = Generator()
results = generator.generate(
    project_id,
    [
      {"action": "create_file", "path": "frontend/new.html", "content": "<html>"}
    ],
    current_files={"index.html": "..."}
)
"""
