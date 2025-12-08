"""
monitor_events.py — Emits events to DB + WS

Purpose:
- Every builder step logs to DB (Supabase)
- Also sends WebSocket push
"""

from __future__ import annotations
import os
import uuid
from datetime import datetime
from supabase import create_client, Client
from .ws_progress import broadcast
from .settings import get_settings

settings = get_settings()
supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

class MonitorEvents:

    def emit(self, project_id: uuid.UUID, event_type: str, message: str):
        # DB log
        supabase.table("agent_events").insert(
            {
                "project_id": str(project_id),
                "type": event_type,
                "message": message,
                "created_at": datetime.utcnow().isoformat()
            }
        ).execute()

        # WS stream
        payload = {
            "project_id": str(project_id),
            "type": event_type,
            "message": message,
            "ts": datetime.utcnow().isoformat()
        }
        # Websocket broadcast
        try:
            import asyncio
            if asyncio.get_event_loop().is_running():
                asyncio.create_task(broadcast(str(project_id), payload))
        except:
            pass
