"""
planner.py — gor://a Deterministic AI Capability Planner

Purpose:
- Translate user intent into required AI capability modules
- Define backend glue + orchestration steps
- Produce a strict machine-readable plan
- Generate todo.md for downstream agents

IMPORTANT:
- Planner does NOT invent UIs
- Planner does NOT invent providers
- Planner does NOT invent endpoints
- Planner only composes EXISTING AI modules
"""

from __future__ import annotations
import os
import json
from typing import Dict, Any, List

from groq import Groq

# -------------------------------------------------
# Configuration
# -------------------------------------------------

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PLANNER_MODEL = os.getenv("MODEL_PLANNER", "openai/gpt-oss-120b")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY must be set")

client = Groq(api_key=GROQ_API_KEY)

# -------------------------------------------------
# Canonical AI Capability Registry
# -------------------------------------------------

AI_CAPABILITIES = {
    "voice_input": "voice_router.py",
    "voice_output": "voice_router.py",
    "audio_processing": "audio_router.py",
    "vision": "vision_router.py",
    "image_generation": "image_generation.py",
    "image_upscale": "image_upscale.py",
    "background_removal": "bg_removal.py",
    "document_processing": "document_ai.py",
    "embeddings": "embed_router.py",
    "media_handling": "media_router.py",
    "progress_updates": "progress_stream.py",
    "async_queue": "queue_dispatcher.py",
    "cost_tracking": "ai_cost_tracker.py",
    "event_monitoring": "monitor_events.py",
    "asset_storage": "asset_storage.py",
}

# -------------------------------------------------
# Planner
# -------------------------------------------------

class Planner:
    """
    Planner outputs a deterministic AI build plan.
    """

    # -----------------------------
    # Intent classification (NO LLM)
    # -----------------------------

    def _infer_capabilities(self, user_request: str) -> List[str]:
        text = user_request.lower()
        caps = set()

        if "chat" in text:
            caps.add("embeddings")

        if "voice" in text or "speech" in text:
            caps.update(["voice_input", "voice_output"])

        if "image" in text:
            caps.add("image_generation")

        if "remove background" in text or "bg remove" in text:
            caps.add("background_removal")

        if "scan" in text or "document" in text or "pdf" in text:
            caps.add("document_processing")

        if "vision" in text or "photo" in text:
            caps.add("vision")

        if "upload" in text or "media" in text:
            caps.add("media_handling")

        # Always-on infrastructure
        caps.update([
            "progress_updates",
            "async_queue",
            "cost_tracking",
            "asset_storage",
        ])

        return sorted(caps)

    # -----------------------------
    # LLM expansion (STRICT)
    # -----------------------------

    def _expand_tasks(
        self,
        base_plan: Dict[str, Any],
        user_request: str,
        project_context: Dict[str, Any],
    ) -> List[str]:
        """
        LLM is used ONLY to expand TODO steps.
        It may NOT change structure.
        """

        system = (
            "You are expanding a build task list.\n"
            "Rules:\n"
            "- Only output a JSON array of task strings\n"
            "- Do NOT mention providers, APIs, or models\n"
            "- Do NOT invent new modules\n"
            "- Tasks should reference files and orchestration steps only\n"
        )

        payload = {
            "user_request": user_request,
            "existing_files": project_context.get("files", []),
            "capabilities": base_plan["capabilities"],
        }

        response = client.chat.completions.create(
            model=PLANNER_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload)},
            ],
            temperature=0.1,
        )

        return json.loads(response.choices[0].message.content)

    # -----------------------------
    # Public API
    # -----------------------------

    def generate_plan(
        self,
        user_request: str,
        project_context: Dict[str, Any],
    ) -> Dict[str, Any]:

        capabilities = self._infer_capabilities(user_request)

        modules = sorted(
            {AI_CAPABILITIES[c] for c in capabilities if c in AI_CAPABILITIES}
        )

        base_plan = {
            "capabilities": capabilities,
            "ai_modules": modules,
            "glue_files": [
                "core_api.py",
                "queue_dispatcher.py",
                "progress_stream.py",
            ],
            "todo": [],
        }

        tasks = self._expand_tasks(
            base_plan=base_plan,
            user_request=user_request,
            project_context=project_context,
        )

        base_plan["todo"] = tasks

        return {
            "plan": base_plan,
            "todo_md": self._to_todo_md(base_plan),
        }

    # -----------------------------
    # todo.md generator
    # -----------------------------

    @staticmethod
    def _to_todo_md(plan: Dict[str, Any]) -> str:
        lines = []
        lines.append("# Build Plan\n")

        lines.append("## AI Capabilities")
        for cap in plan["capabilities"]:
            lines.append(f"- {cap}")

        lines.append("\n## AI Modules")
        for mod in plan["ai_modules"]:
            lines.append(f"- agent/ai/{mod}")

        lines.append("\n## Orchestration")
        for g in plan["glue_files"]:
            lines.append(f"- {g}")

        lines.append("\n## Tasks")
        for task in plan["todo"]:
            lines.append(f"- {task}")

        return "\n".join(lines)
