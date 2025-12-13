"""
planner.py — gor://a Deterministic Planning Module

Purpose:
- Classify user intent
- Expand from known app templates
- Output a strict build plan (JSON)
- Generate todo.md from that plan

LLM Runtime: Groq (text-only)
"""

from __future__ import annotations
import os
import json
from typing import Dict, Any

from groq import Groq

# -------------------------------------------------
# Configuration
# -------------------------------------------------

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PLANNER_MODEL = os.getenv("MODEL_LONGFORM", "gpt-oss-120b")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY must be set")

client = Groq(api_key=GROQ_API_KEY)

# -------------------------------------------------
# Canonical app templates (VERY IMPORTANT)
# -------------------------------------------------

APP_TEMPLATES = {
    "chatbot": {
        "frontend": ["chat.html", "settings.html"],
        "backend": ["chat_api.py", "history_api.py"],
        "database": ["messages", "sessions"],
        "ai_features": ["text-chat"],
        "tools": [],
    },
    "voice_chatbot": {
        "frontend": ["chat.html", "voice.html"],
        "backend": ["chat_api.py", "voice_api.py"],
        "database": ["messages", "sessions"],
        "ai_features": ["text-chat", "voice-input", "voice-output"],
        "tools": ["speech"],
    },
    "ocr_app": {
        "frontend": ["upload.html", "results.html"],
        "backend": ["ocr_api.py"],
        "database": ["documents"],
        "ai_features": ["ocr"],
        "tools": ["vision"],
    },
}

# -------------------------------------------------
# Planner
# -------------------------------------------------

class Planner:
    """
    Planner produces a deterministic build plan.
    """

    def _classify_intent(self, user_request: str) -> str:
        """
        VERY simple intent classifier.
        No LLM creativity here.
        """
        text = user_request.lower()

        if "voice" in text and "chat" in text:
            return "voice_chatbot"
        if "chat" in text:
            return "chatbot"
        if "ocr" in text or "scan" in text:
            return "ocr_app"

        # Safe default
        return "chatbot"

    def _expand_plan(
        self,
        base_plan: Dict[str, Any],
        user_request: str,
        project_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Uses LLM ONLY to refine wording and add optional tasks.
        NEVER to change structure.
        """

        system = (
            "You expand an existing app plan.\n"
            "Rules:\n"
            "- Do NOT change keys or structure\n"
            "- Do NOT introduce new APIs or providers\n"
            "- Do NOT invent endpoints\n"
            "- Only refine descriptions and add TODO steps\n"
            "- Output VALID JSON ONLY\n"
        )

        user = {
            "user_request": user_request,
            "project_context": project_context,
            "base_plan": base_plan,
        }

        response = client.chat.completions.create(
            model=PLANNER_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user)},
            ],
            temperature=0.1,
        )

        return json.loads(response.choices[0].message.content)

    def generate_plan(
        self,
        user_request: str,
        project_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Main entry point.
        """

        app_type = self._classify_intent(user_request)
        base_template = APP_TEMPLATES[app_type]

        base_plan = {
            "app_type": app_type,
            "frontend": base_template["frontend"],
            "backend": base_template["backend"],
            "database": base_template["database"],
            "ai_features": base_template["ai_features"],
            "tools": base_template["tools"],
            "todo": [],
        }

        expanded = self._expand_plan(
            base_plan=base_plan,
            user_request=user_request,
            project_context=project_context,
        )

        return {
            "plan": expanded,
            "todo_md": self._to_todo_md(expanded),
        }

    @staticmethod
    def _to_todo_md(plan: Dict[str, Any]) -> str:
        """
        Converts plan JSON into human-readable todo.md
        """

        lines = []
        lines.append(f"# Build plan: {plan['app_type']}\n")

        for section in ["frontend", "backend", "database", "ai_features", "tools"]:
            items = plan.get(section, [])
            if not items:
                continue
            lines.append(f"## {section.capitalize()}")
            for item in items:
                lines.append(f"- {item}")
            lines.append("")

        if plan.get("todo"):
            lines.append("## Tasks")
            for task in plan["todo"]:
                lines.append(f"- {task}")

        return "\n".join(lines)
