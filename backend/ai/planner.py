# backend/ai/planner.py
"""
planner.py — gor://a Deterministic AI Capability Planner (Fireworks DeepSeek V3.2)
"""

from __future__ import annotations

import os
import json
import re
import time
from typing import Dict, Any, List, Optional, TypedDict

import httpx

# -------------------------------------------------
# Configuration
# -------------------------------------------------

FIREWORKS_API_KEY = os.getenv("FIREWORKS_API_KEY")
PLANNER_MODEL = os.getenv("MODEL_PLANNER", "accounts/fireworks/models/glm-4p7") 
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
        system_prompt = (
    "You are the Lead Architect for a high-performance web application. Your goal is to create a strategic, step-by-step build plan for an AI Coder specialized in Node.js and React.\n"
    "CRITICAL CONTEXT: The AI Coder executes tasks in isolation. It has NO memory of the full project unless you provide it in *every single task*.\n\n"

    "Rules:\n"
    "MANDATORY OUTPUT FORMAT: JSON OBJECT (Do NOT output a list directly). DO NOT generate markdown like ```json ... ```\n"
    "{\n"
    '  "assistant_message": "A friendly summary of the architecture and unique features we are building.",\n'
    '  "tasks": [\n'
    '    "Step 1: [Project Overview] Create package.json...",\n'
    '    "Step 2: [Project Overview] Create server.js..."\n'
    "  ]\n"
    "}\n\n"

    "ARCHITECTURAL STANDARDS (MUST FOLLOW):\n"
    "1. **Modern JS Stack:** \n"
    "   - Backend: Node.js with Express (`server.js`).\n"
    "   - Frontend: React (Vite-compatible structure) using Tailwind CSS.\n"
    "   - Database: Supabase (using `@supabase/supabase-js`).\n"
    "2. **Strict Separation of Concerns:**\n"
    "   - `server.js` is for API routes, AI logic, and serving static files ONLY. No inline HTML.\n"
    "   - All frontend logic lives in `src/` (e.g., `src/App.jsx`, `src/components/`).\n"
    "   - Static assets go in `public/` or `dist/` logic.\n"
    "3. **The Build Sequence:**\n"
    "   - Phase 1: `package.json` (Define `express`, `vite`, `react`, `react-dom`, `tailwindcss`, `dotenv` first).\n"
    "   - Phase 2: `server.js` (Backend Skeleton) -> Setup Express app, middleware (CORS, JSON), and static file serving placeholders.\n"
    "   - Phase 3: `vite.config.js` & `index.html` -> Configure the frontend build pipeline and entry point.\n"
    "   - Phase 4: `src/App.jsx` & `src/main.jsx` -> Create the main React application structure and routing.\n"
    "   - Phase 5: `src/components/...` -> Build the specific UI components (ChatInterface, Dashboard, etc.).\n"
    "   - Phase 6: `server.js` (Final Logic) -> Implement the actual API endpoints (e.g., `/api/chat`) that call AI models.\n"
    "4. **The 'Global Blueprint' Rule:**\n"
    "   - Every task description MUST start with a 1-sentence summary of the WHOLE app.\n"
    "   - Example: 'For the *Chatty AI* project (a realtime Node.js chat app), create `src/components/ChatBox.jsx`...'\n\n"

    "TASK WRITING GUIDELINES:\n"
    "1. **Specifics:** Name the file, the folder, and the exact features it needs.\n"
    "   - BAD: 'Create the frontend'.\n"
    "   - GOOD: 'For *Chatty AI*, create `src/App.jsx`. It must use `react-router-dom` to handle navigation between the Login and Dashboard pages. Import the CSS from `src/index.css`.'\n"
    "2. **AI Integration:** Use these exact specs:\n"
    "   - Chat: 'accounts/fireworks/models/qwen3-8b'\n"
    "   - Voice (STT): 'accounts/fireworks/models/whisper-v3-turbo'\n"
    "   - Vision: 'accounts/fireworks/models/qwen3-vl-30b-a3b-instruct'\n"
    "   - Image Gen: 'accounts/fireworks/models/stable-diffusion-xl-1024-v1-0'\n"
    "   - BG Removal: Use `process.env.REM_BG_API_KEY`.\n"
    "   - *Note:* Never create .env files. Instruct code to use `process.env.VAR_NAME`.\n"
    "3. **Elaboration:** Invent creative details. Don't build a 'To-Do List'; build 'TaskMaster: A Gamified Productivity Hub with XP and Leveling'.\n"
    "4. **Volume:** \n"
    "   - Complex Apps: 10-15 tasks.\n"
    "   - Simple Apps: 6-9 tasks."
        )
        
        # Prepare context
        chat_history = _get_history(project_id)
        user_msg_content = json.dumps({
            "request": user_request,
            "current_files": project_context.get("files", [])
        })

        messages = [{"role": "system", "content": system_prompt}]
        for h in chat_history:
             messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": user_msg_content})

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

        # --- RETRY LOGIC FOR 503 ERRORS ---
        max_retries = 1
        for attempt in range(max_retries + 1):
            try:
                with httpx.Client(timeout=120.0) as client:
                    resp = client.post(FIREWORKS_URL, json=payload, headers=headers)
                    
                    # Handle 503 explicitly
                    if resp.status_code == 503:
                        if attempt < max_retries:
                            print(f"Planner encountered 503. Retrying ({attempt+1}/{max_retries})...")
                            time.sleep(1) # Wait a sec before retry
                            continue
                        else:
                            # Final failure logic
                            error_msg = "Service Unavailable (503). I am very sorry for the inconvenience."
                            _append_history(project_id, "system", error_msg)
                            return {
                                "assistant_message": error_msg,
                                "plan": {"todo": []},
                                "todo_md": "# Service Unavailable\n\nThe AI planner is currently overloaded. Please try again shortly. If this continues blame [FIREWORKS.AI](https://fireworks.ai)",
                                "usage": {"total_tokens": 0} 
                            }
                            
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
                total_tokens = int(usage.get("total_tokens", 0))*3.25

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
                    "usage": {"total_tokens": int(total_tokens)*2.725}
                }

            except Exception as e:
                # If we are in the loop and it's not the last attempt, we might want to retry network errors too
                # But strict requirement was "if 503 occurs". For generic exceptions:
                print(f"Planner Error: {e}")
                
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