"""
planner.py — gor://a AI Planning Module

This module turns user intent into a structured build plan (todo.md style).
The plan serves as the blueprint for:
- code generation (frontend + backend)
- folder creation
- db modifications
- feature updates

LLM Provider: GROQ ONLY
"""

from __future__ import annotations
import os
from typing import Dict, Any
import json
import httpx

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL_PLAN", "mixtral-8x7b")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY must be configured in the environment")


class Planner:
    """
    The Planner receives:
        - raw user prompt
        - context (existing fs, feature, goal)
    It returns:
        - structured build plan
        - todo.md content
    """

    def __init__(self):
        self.groq_url = "https://api.groq.com/openai/v1/chat/completions"

    async def _call_groq(self, system: str, user: str) -> str:
        """
        Calls Groq chat-completion endpoint directly via HTTP.
        """
        payload = {
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.15,
        }

        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=40.0) as client:
            resp = await client.post(self.groq_url, json=payload, headers=headers)
            resp.raise_for_status()
            content = resp.json()
            return content["choices"][0]["message"]["content"]

    @staticmethod
    def _post_process(plan: str) -> Dict[str, Any]:
        """
        Converts structured markdown to JSON-like dict.
        The coder and generator use this dict to coordinate.
        """
        lines = plan.splitlines()
        sections = {}
        current_header = None
        buffer = []

        for line in lines:
            if line.startswith("# "):
                if current_header:
                    sections[current_header] = "\n".join(buffer).strip()
                    buffer = []
                current_header = line[2:].strip()
            else:
                buffer.append(line)

        if current_header:
            sections[current_header] = "\n".join(buffer).strip()

        return {
            "raw": plan,
            "structured": sections,
        }

    async def generate_plan(self, user_request: str, project_context: Dict) -> Dict[str, Any]:
        """
        Generates a todo plan based on the user request + project context.
        
        Example input:
        - user_request: "Add a login screen and ChatGPT-style chat area"
        - project_context: {"files": [...], "project_name": "..."}
        """

        system = (
            "You are the planning brain for gor://a, the future of AI app building.\n"
            "Write a clear, efficient plan in TODO.md format.\n"
            "Plan must include sections if applicable:\n"
            "- Frontend\n"
            "- Backend\n"
            "- Database\n"
            "- AI interactions\n"
            "- UX adjustments\n"
            "- Deployment\n"
            "Be concise but precise. Avoid code — list what must be created or changed."
        )

        user_msg = (
            f"User request: {user_request}\n\n"
            f"Current project summary:\n{json.dumps(project_context, indent=2)}"
        )

        response = await self._call_groq(system, user_msg)
        return self._post_process(response)
