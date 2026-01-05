"""
coder.py — gor://a AI Code Generation Engine

Responsible for converting a structured build plan into code artifacts.
Receives section-by-section tasks and produces:
- new file contents
- file patches
- diff replacements
- rewritten versions

LLM Provider: GROQ ONLY
"""

from __future__ import annotations

import os
from typing import List, Dict, Any
import json
import httpx

GROQ_API_KEY = os.getenv("GROQ_API_KEY","")
GROQ_MODEL = os.getenv("GROQ_MODEL_CODE", "openai/gpt-oss-120b")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY must be configured in the environment")


class Coder:

    def __init__(self):
        self.groq_url = "https://api.groq.com/openai/v1/chat/completions"

    async def _call_groq(self, system: str, user: str) -> str:
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

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(self.groq_url, json=payload, headers=headers)
            resp.raise_for_status()
            content = resp.json()
            return content["choices"][0]["message"]["content"]

    async def generate_code(
        self,
        plan_section: str,
        plan_text: str,
        file_tree: Dict[str, str],
        project_name: str,
    ) -> Dict[str, Any]:
        """
        plan_section: e.g., "Frontend", "Backend", "Database"
        plan_text: plain description of what must be done
        file_tree: dict[path -> content]
        returns: list of {action, path, content/diff}
        """

        system_prompt = (
            "You are the code generation engine for gor://a.\n"
            "Output instructions in JSON ONLY with an array `operations`.\n\n"
            "Each operation must be one of the following:\n"
            "{\n"
            '  "action": "create_file", "path": "...", "content": "..." \n'
            "}\n"
            "{\n"
            '  "action": "overwrite_file", "path": "...", "content": "..." \n'
            "}\n"
            "{\n"
            '  "action": "patch_file", "path": "...", "diff": "..." \n'
            "}\n\n"
            "NO markdown. NO commentary. NO code fences.\n"
            "Return only raw JSON. Each file should be really big and above all expectations, with the best UI (if not specified)\n"
        )

        file_summary = "\n".join([f"- {p}" for p in file_tree])

        user_msg = (
            f"Project: {project_name}\n"
            f"Section Task: {plan_section}\n\n"
            f"Task Details:\n{plan_text}\n\n"
            f"Current Files:\n{file_summary}\n\n"
            "Generate the required JSON operations."
        )

        raw = await self._call_groq(system_prompt, user_msg)

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # fallback if model adds format issues
            cleaned = raw.strip().removeprefix("```json").removesuffix("```").strip()
            parsed = json.loads(cleaned)

        if "operations" not in parsed:
            raise ValueError("Model output missing 'operations' field")

        return parsed


"""
Usage Example (logic only, not executed here):

coder = Coder()
ops = await coder.generate_code(
    plan_section="Frontend",
    plan_text="- Create login screen\n- Update landing text",
    file_tree={"index.html": "...", "auth.html": "..."},
    project_name="gor-app"
)

`ops` returns JSON with patch/create/overwrite instructions.
"""
