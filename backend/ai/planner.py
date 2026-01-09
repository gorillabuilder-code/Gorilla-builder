# backend/ai/planner.py
"""
planner.py — gor://a Deterministic AI Capability Planner
"""

from __future__ import annotations

import os
import json
from typing import Dict, Any, List, Optional, TypedDict

from groq import Groq

# -------------------------------------------------
# Configuration
# -------------------------------------------------

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PLANNER_MODEL = os.getenv("MODEL_PLANNER", "meta-llama/llama-4-scout-17b-16e-instruct")

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
# Lightweight chat history (in-memory)
# -------------------------------------------------

class ChatMsg(TypedDict):
    role: str
    content: str

# project_id -> list[ChatMsg]
_HISTORY: Dict[str, List[ChatMsg]] = {}

def _norm_role(role: str) -> str:
    r = (role or "").strip().lower()
    if r in ("user", "you"):
        return "user"
    if r in ("assistant", "planner", "system", "coder", "agent"):
        return "assistant" if r in ("assistant", "planner", "agent", "coder") else "system"
    return "user"

def _append_history(project_id: str, role: str, content: str, max_items: int = 16) -> None:
    if not project_id:
        return
    msg = {"role": _norm_role(role), "content": (content or "").strip()}
    if not msg["content"]:
        return
    _HISTORY.setdefault(project_id, []).append(msg)
    if len(_HISTORY[project_id]) > max_items:
        _HISTORY[project_id] = _HISTORY[project_id][-max_items:]

def _get_history(project_id: str, max_items: int = 12) -> List[ChatMsg]:
    if not project_id:
        return []
    return list(_HISTORY.get(project_id, []))[-max_items:]

# -------------------------------------------------
# Planner
# -------------------------------------------------

class Planner:
    """
    Planner outputs a deterministic AI build plan.
    """

    def remember(self, project_id: str, role: str, text: str) -> None:
        _append_history(project_id, role, text)

    def _infer_capabilities(self, user_request: str) -> List[str]:
        """Heuristic-based capability detection for metadata."""
        text = (user_request or "").lower()
        caps = set()
        if "chat" in text: caps.add("embeddings")
        if "voice" in text or "speech" in text: caps.update(["voice_input", "voice_output"])
        if "image" in text: caps.add("image_generation")
        if "remove background" in text or "bg remove" in text: caps.add("background_removal")
        if "scan" in text or "document" in text or "pdf" in text: caps.add("document_processing")
        if "vision" in text or "photo" in text: caps.add("vision")
        if "upload" in text or "media" in text: caps.add("media_handling")
        caps.update(["progress_updates", "async_queue", "cost_tracking", "asset_storage"])
        return sorted(caps)

    def generate_plan(
        self,
        user_request: str,
        project_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Returns:
          {
            "assistant_message": str,
            "plan": {"todo": [str]},
            "todo_md": str,
            "usage": {"total_tokens": int}
          }
        """
        project_id = str(project_context.get("project_id") or "").strip()
        
        # 1. Update memory
        if project_id:
            _append_history(project_id, "user", user_request)

        # 2. Heuristics for metadata (Plan Header)
        capabilities = self._infer_capabilities(user_request)
        modules = sorted({AI_CAPABILITIES[c] for c in capabilities if c in AI_CAPABILITIES})

        # 3. LLM Generation (Message + Tasks)
        system_prompt = (
            "You are expanding a build task list. you must detail every file \n"
            "Rules:\n"
            "MANDATORY OUTPUT FORMAT: JSON OBJECT (Do NOT output a list directly).\n"
            "{\n"
            '  "assistant_message": "Here include a friendly summary of the thing that will be built.",\n'
            '  "tasks": [\n'
            '    "Create index.html...",\n'
            '    "Update app.py..."\n'
            "  ]\n"
            "}\n\n"
            "- try to make more than 15 and for bigger projects 22 maximum for debugging 5 tasks are enough, readme.md in a way for this is an ai coder. and try to ask for the best looking UI always use if asked to add these features use .env GROQ_API_KEY for chatbots use this model 'llama-3.1-8b-instant' for tts use 'canopylabs/orpheus-v1-english' and for stt use 'whisper-large-v3' and REM_BG_API_KEY for bg removal."
            "- Do NOT invent new modules or make a .env file\n"
            "- Tasks should reference files and orchestration steps only\n"
            "- Always use Fast API and HTML and have one file app.py\n"
            "- Always be very clear for a coding agent to follow instructions\n"
            "- Elaborate on the idea sometimes invent new features (when asked or nessacary, like Oauth not specified for a networking app) but otherwise try to invent things but make them really elaborate and nice\n"
        )
        
        # Prepare context
        chat_history = _get_history(project_id)
        user_msg_content = json.dumps({
            "request": user_request,
            "current_files": project_context.get("files", [])
        })

        messages = [{"role": "system", "content": system_prompt}]
        # Add history to context
        for h in chat_history:
             messages.append({"role": h["role"], "content": h["content"]})
        # Add current request
        messages.append({"role": "user", "content": user_msg_content})

        try:
            # Call Groq
            resp = client.chat.completions.create(
                model=PLANNER_MODEL,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=2048
            )

            raw = resp.choices[0].message.content
            data = json.loads(raw)
            
            tasks = data.get("tasks", [])
            assistant_message = data.get("assistant_message", "I have updated the plan.")
            
            # Capture usage
            total_tokens = resp.usage.total_tokens if resp.usage else 0

            # 4. Construct response objects
            base_plan = {
                "capabilities": capabilities,
                "ai_modules": modules,
                "glue_files": ["core_api.py", "queue_dispatcher.py"],
                "todo": tasks,
            }

            # 5. Save assistant reply to history
            if project_id:
                _append_history(project_id, "assistant", assistant_message)

            return {
                "assistant_message": assistant_message,
                "plan": base_plan,
                "todo_md": self._to_todo_md(base_plan, assistant_message),
                "usage": {"total_tokens": total_tokens}
            }

        except Exception as e:
            # Fallback if API fails
            fallback_msg = f"I encountered an error generating the plan: {str(e)}"
            if project_id:
                _append_history(project_id, "system", fallback_msg)
            
            return {
                "assistant_message": fallback_msg,
                "plan": {"todo": []},
                "todo_md": f"# Error\n{e}",
                "usage": {"total_tokens": 0}
            }

    @staticmethod
    def _to_todo_md(plan: Dict[str, Any], msg: str = "") -> str:
        lines = []
        
        # Include the assistant message at the top so it renders in the chat
        if msg:
            lines.append(f"**Planner:** {msg}\n")
            
        lines.append("# Build Plan\n")
        
        if plan.get("capabilities"):
            lines.append("## Capabilities")
            for cap in plan["capabilities"]:
                lines.append(f"- {cap}")
        
        lines.append("\n## Tasks")
        for task in plan.get("todo", []):
            lines.append(f"- {task}")
            
        return "\n".join(lines)