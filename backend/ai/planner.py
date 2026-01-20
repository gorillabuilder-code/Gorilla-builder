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
PLANNER_MODEL = os.getenv("MODEL_PLANNER", "accounts/fireworks/models/minimax-m2p1") 
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
    "You are the Lead Architect for a high-performance web application. Your goal is to create a strategic, step-by-step build plan for an AI Coder specialized in Node.js and **Runtime React** (No-Build).\n"
    "CRITICAL CONTEXT: The AI Coder executes tasks in isolation. It has NO memory of previous files unless you provide context in *every single task description*.\n\n"

    "Rules:\n"
    "MANDATORY OUTPUT FORMAT: JSON OBJECT ONLY. Do NOT wrap in markdown blocks.\n"
    "{\n"
    '  "assistant_message": "A friendly summary of the architecture...",\n'
    '  "tasks": [\n'
    '    "Step 1: [Project: ChatApp | Stack: Runtime React/Node | Context: ChatApp is a friendly chatbot... THE CONTEXT IS VERY IMPORTANT AND MANDATORY TO ADD] Create package.json... (include all dependencies)",\n'
    '    "Step 2: [Project: ChatApp | Stack: Runtime React/Node | Context: ChatApp is a friendly chatbot... THE CONTEXT IS VERY IMPORTANT AND MANDATORY TO ADD] Create index.html with CDNs... (include window.onerror)"\n'
    "  ]\n"
    "}\n\n"

    "ARCHITECTURAL STANDARDS (MUST FOLLOW):\n"
    "1. **Stack:** \n"
    "   - Backend: Node.js with Express (`server.js`).\n"
    "   - Frontend: React (Runtime/CDN-based). NO Vite, NO Webpack, NO `src` folder.\n"
    "   - Database: **Local SQLite** (using `better-sqlite3`) OR **JSON File Storage** (using `fs`). Do NOT use external DBs like Supabase unless explicitly asked.\n"
    "2. **Strict Separation:**\n"
    "   - `server.js` serves the API, the `static/` folder, and **Handles Error Logging**.\n"
    "   - `index.html` lives in the root.\n"
    "   - All React code lives in `static/main.js` (and other `.js` files in `static/`).\n"
    "3. **The Wiring & Evolution Rule (CRITICAL - NO BLANK SCREENS):**\n"
    "   - NEVER assign a task to just 'create a component'.\n"
    "   - **MANDATORY:** Every component creation task MUST include a directive to **Update `static/main.js`**.\n"
    "   - Example: 'Create `static/components/Sidebar.js`... AND IMMEDIATELY REWRITE `static/main.js` to import `Sidebar` and render `<Sidebar />` inside the App layout.'\n"
    "   - `main.js` must evolve in *every* frontend step. And most importantly on the FINAL STEP YOU  MUST UPDATE THE MAIN.JS & SERVER>JS. It should never remain a placeholder, this will happen, if you just say 'setup main.js'.\n"
    "4. **The Build Sequence (Runtime React Edition):**\n"
    "   - Phase 1: `package.json`. Define `scripts` ('start': 'node server.js') and `dependencies` ('express', 'cors', 'dotenv', 'fireworks-ai', 'better-sqlite3'). **Do NOT include** 'vite' or 'react' here.\n"
    "   - Phase 2: `server.js` (Backend Skeleton). Setup Express, `app.use(express.json())`, and the **Critical Error Logging Route** (`POST /api/log-error`). Setup static serving.\n"
    "   - Phase 3: `database.js` (The Adapter). Create local DB setup.\n"
    "   - Phase 4: `index.html` (The Shell). Create root HTML with Babel/React CDNs. **CRITICAL:** Include the `window.onerror` Spy Script in the `<head>`.\n"
    "   - Phase 5: `static/main.js` (The Entry Point). Create the initial App shell (e.g., A layout div with State management). **Do NOT just write 'Hello World'**; set up the actual container structure.\n"
    "   - Phase 6+: `static/components/...`. Create specific UI components AND **Wire them into `main.js`** immediately.\n"
    "   - Final Phase: `server.js` (Final Logic). Implement API endpoints.\n"
    "5. **The 'No-Placeholder' Rule:**\n"
    "   - The Coder is forbidden from writing comments like `// code goes here`. You must describe the logic needed.\n"
    "   - Every step must result in a **rendering** application. Never leave the app in a broken state between steps.\n"
    "6. **The 'Global Blueprint' Rule:**\n"
    "   - Every task string MUST start with: `[App: {Name} | Stack: Runtime React/Node] ...`\n\n"

    "TASK WRITING GUIDELINES:\n"
    "1. **No-Build Specifics:** \n"
    "   - NEVER ask for `npm run dev` or `vite.config.js` AND MOST IMPORTANTLY, NEVER MAKE AN .ENV OF ANY KIND, THIS WILL BE INJECTED BY THE SYSTEM.\n"
    "   - ALWAYS specify that frontend files go into `static/`.\n"
    "2. **AI Integration Specs:**\n"
    "   - Chat: 'accounts/fireworks/models/qwen3-8b'\n"
    "   - Voice (STT): 'accounts/fireworks/models/whisper-v3-turbo'\n"
    "   - Vision: 'accounts/fireworks/models/qwen3-vl-30b-a3b-instruct'\n"
    "   - Image Gen: 'accounts/fireworks/models/stable-diffusion-xl-1024-v1-0'\n"
    "   - BG Removal: Use `process.env.REM_BG_API_KEY`.\n"
    "3. **Volume:** \n"
    "   - Complex Apps: 20-23 tasks.\n"
    "   - Simple Apps: 7-10 tasks. Do not try to exceed this unless the app it very ambitious."
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

        # --- RETRY LOGIC (503s AND Invalid JSON) ---
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                with httpx.Client(timeout=120.0) as client:
                    resp = client.post(FIREWORKS_URL, json=payload, headers=headers)
                    
                    # 1. Handle 503 Service Unavailable (Retry Loop)
                    if resp.status_code == 503:
                        if attempt < max_retries:
                            print(f"Planner encountered 503. Retrying ({attempt+1}/{max_retries})...")
                            time.sleep(1) # Wait a sec before retry
                            continue
                        else:
                            # Final failure logic for 503
                            error_msg = "Service Unavailable (503). I am very sorry for the inconvenience."
                            _append_history(project_id, "system", error_msg)
                            return {
                                "assistant_message": error_msg,
                                "plan": {"todo": []},
                                "todo_md": "# Service Unavailable\n\nThe AI planner is currently overloaded. Please try again shortly.",
                                "usage": {"total_tokens": 0} 
                            }
                            
                    resp.raise_for_status()
                    data_api = resp.json()

                raw = data_api["choices"][0]["message"]["content"]
                
                # 2. Extract JSON
                data = _extract_json(raw)
                
                # 3. Handle Invalid JSON (Re-prompt / Retry Loop)
                if not data:
                    if attempt < max_retries:
                        print(f"Planner JSON extraction failed. Reprompting AI ({attempt+1}/{max_retries})...")
                        time.sleep(1) 
                        continue # This triggers the loop to call client.post() again (Reprompt)
                    else:
                        raise ValueError(f"Could not extract JSON from response after {max_retries+1} attempts: {raw[:100]}...")
                
                # --- Success Path ---
                tasks = data.get("tasks", [])
                assistant_message = data.get("assistant_message", "I have updated the plan.")
                
                # Capture usage
                usage = data_api.get("usage", {})
                total_tokens = int(usage.get("total_tokens", 0))*3.25

                # Construct response objects
                base_plan = {
                    "capabilities": capabilities,
                    "ai_modules": modules,
                    "glue_files": ["core_api.py", "queue_dispatcher.py"],
                    "todo": tasks,
                }

                # Save assistant reply to history
                if project_id:
                    _append_history(project_id, "assistant", assistant_message)

                return {
                    "assistant_message": assistant_message,
                    "plan": base_plan,
                    "todo_md": self._to_todo_md(base_plan, assistant_message),
                    "usage": {"total_tokens": int(total_tokens)*2.725}
                }

            except Exception as e:
                # If an error occurs that isn't caught by the retry logic (e.g. 500, 400, or network error),
                # check if we should retry or fail immediately.
                if attempt < max_retries:
                     print(f"Planner Generic Error: {e}. Retrying ({attempt+1}/{max_retries})...")
                     time.sleep(1)
                     continue
                
                print(f"Planner Final Error: {e}")
                
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