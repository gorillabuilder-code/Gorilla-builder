# backend/ai/planner.py
"""
planner.py — gor://a Deterministic AI Capability Planner (Fireworks DeepSeek V3.2)
"""

from __future__ import annotations

import os
import json
import re
from typing import Dict, Any, List, Optional, TypedDict

import httpx

# -------------------------------------------------
# Configuration
# -------------------------------------------------

FIREWORKS_API_KEY = os.getenv("FIREWORKS_API_KEY")
# Using the model from your working snippet
PLANNER_MODEL = os.getenv("MODEL_PLANNER", "accounts/cogito/models/cogito-671b-v2-p1") 
FIREWORKS_URL = os.getenv("FIREWORKS_URL", "https://api.fireworks.ai/inference/v1/chat/completions")

if not FIREWORKS_API_KEY:
    raise RuntimeError("FIREWORKS_API_KEY must be set")

# -------------------------------------------------
# Helpers
# -------------------------------------------------

def _extract_json(text: str) -> Any:
    """
    Robustly extract the largest valid JSON object from a string.
    """
    text = text.strip()

    # 1. Try to find JSON inside ```json ... ``` blocks
    code_block_pattern = r"```(?:json)?\s*(\{.*?\})\s*```"
    match = re.search(code_block_pattern, text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 2. Try to find the outer-most { ... }
    try:
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end > start:
            json_str = text[start : end + 1]
            return json.loads(json_str)
    except json.JSONDecodeError:
        pass

    # 3. Fallback: try parsing the whole string
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    
    return None

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
        # 3. LLM Generation (Message + Tasks)
        system_prompt = (
            "You are the Lead Architect for a software project. Your goal is to create a detailed, step-by-step build plan for an AI Coder. \n"
            "CRITICAL: The AI Coder creates files one by one and has NO context of the previous steps. Therefore, every single task description must be EXTREMELY detailed and self-contained.\n\n"
            
            "Rules:\n"
            "MANDATORY OUTPUT FORMAT: JSON OBJECT (Do NOT output a list directly).DO NOT generate markdown like json'''...'''\n"
            "{\n"
            '  "assistant_message": "Here include a friendly summary of the thing that will be built.",\n'
            '  "tasks": [\n'
            '    "Create index.html...",\n'
            '    "Update app.py..."\n'
            "  ]\n"
            "}\n\n"
            
            "GUIDELINES FOR TASK GENERATION:\n"
            "1. **No Generic Tasks:** Never write 'Create app.py'. This is a failure.\n"
            "2. **Specifics & Context:** Every task must mention the PROJECT NAME, the FILE PURPOSE, and the EXACT CONTENTS.\n"
            "   - BAD: 'Create app.py with routes.'\n"
            "   - GOOD: 'Setup the app.py for the Educai AI education platform using FastAPI. Define routes for index.html (landing), learn.html (modules), dashboard.html (user stats), and play.html (gamified quiz). Initialize the AI chatbot endpoint using the Groq client to act as an education tutor.'\n"
            "3. **Interlocking Files:** If you ask for a route in `app.py`, you MUST also create the corresponding HTML file in a later task. Reference the specific filenames consistently.\n"
            "4. **Tech Stack Constraints:**\n"
            "   - Always use FastAPI for the backend (`app.py`).\n"
            "   - Always use HTML/CSS/JS for the frontend.\n"
            "   - Do NOT invent new modules or folders unless necessary.\n"
            "   - Never ever make a .env file. Use `os.getenv` directly in the code.\n"
            "   - Never ask to 'setup the repository'. Start immediately with creating the first file.\n"
            "5. **AI Integration Details:**\n"
            "   - If adding features, strictly use these: Use FIREWORKS_API_KEY process.env,  For chatbots use 'accounts/fireworks/models/qwen3-8b', and for stt use 'accounts/fireworks/models/whisper-v3-turbo', for vision use 'accounts/fireworks/models/qwen3-vl-30b-a3b-instruct', for image generation use 'accounts/fireworks/models/stable-diffusion-xl-1024-v1-0'and REM_BG_API_KEY for BG removal.\n"
            "6. **Volume:** Generate between 15 to 22 tasks for a complete application. For simple fixes, 5 tasks are sufficient.\n"
            "7. **Elaboration:** Invent specific features if they aren't provided. If building a 'Chat App', don't just make a chat; make a 'Real-time WebSocket Chat with Message History and Typing Indicators'. Make it elaborate and impressive. Also name it nicely, instead of calling it a FIREWORKS CHAT APP, call it CHATTY the Chatbot\n"
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
            # Construct payload exactly matching your working snippet
            payload = {
                "model": PLANNER_MODEL,
                "top_p": 1,
                "top_k": 40,
                "presence_penalty": 0,
                "frequency_penalty": 0,
                "temperature": 0.6,
                "messages": messages
            }
            
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {FIREWORKS_API_KEY}"
            }

            # Sync call wrapper
            with httpx.Client(timeout=120.0) as client:
                resp = client.post(FIREWORKS_URL, json=payload, headers=headers)
                resp.raise_for_status()
                data_api = resp.json()

            raw = data_api["choices"][0]["message"]["content"]
            
            data = _extract_json(raw)
            if not data:
                raise ValueError(f"Could not extract JSON from response: {raw[:100]}...")
            
            tasks = data.get("tasks", [])
            assistant_message = data.get("assistant_message", "I have updated the plan.")
            
            # Capture usage
            usage = data_api.get("usage", {})
            total_tokens = int(usage.get("total_tokens", 0))*6.76

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
                "usage": {"total_tokens": int(total_tokens)*2.725}# Adjusted token count estimate
            }

        except Exception as e:
            # Fallback if API fails
            fallback_msg = f"I encountered an error generating the plan: {str(e)}"
            if project_id:
                _append_history(project_id, "system", fallback_msg)
            
            print(f"Planner Error: {e}")
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