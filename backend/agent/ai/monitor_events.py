"""
monitor_events.py — gor://a Live AI Builder Progress Stream

Purpose:
- Log every milestone: planning, coding, patching, deploying
- Emit event rows → Supabase Realtime
- Frontend subscribes and shows progress terminal
"""

from __future__ import annotations
import os
import uuid
from datetime import datetime
from supabase import create_client, Client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class MonitorEvents:

    def emit(self, project_id: uuid.UUID, event_type: str, message: str):
        """
        Insert an event log into Postgres (realtime triggers push to frontend)
        """
        supabase.table("agent_events").insert(
            {
                "project_id": str(project_id),
                "type": event_type,
                "message": message,
                "created_at": datetime.utcnow().isoformat()
            }
        ).execute()
