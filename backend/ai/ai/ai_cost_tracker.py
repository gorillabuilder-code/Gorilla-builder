"""
ai_cost_tracker.py — gor://a Free-tier friendly AI usage logging

NOT financial cost — track # calls to:
- LLM
- vision
- bg remove
- pdf
- uploads
- audio

So user sees usage + plan limits.
"""

from __future__ import annotations
import uuid
from datetime import datetime
from supabase import create_client, Client
import os

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class AICostTracker:

    def log(self, user_id: uuid.UUID, service: str):
        supabase.table("ai_usage").insert(
            {
                "user_id": str(user_id),
                "service": service,
                "used_at": datetime.utcnow().isoformat()
            }
        ).execute()
