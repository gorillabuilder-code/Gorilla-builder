"""
planner.py — gor://a Deterministic AI Capability Planner (OpenRouter Moonshot Kimi)
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

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
PLANNER_MODEL = os.getenv("MODEL_PLANNER", "moonshotai/kimi-k2.5:online") 
OPENROUTER_URL = os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")

# OpenRouter specific headers for rankings/stats
SITE_URL = os.getenv("SITE_URL", "https://gorillabuilder.dev")
SITE_NAME = os.getenv("SITE_NAME", "Gorilla Builder")

if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY must be set")

# -------------------------------------------------
# Helpers
# -------------------------------------------------

def _extract_json(text: str) -> Any:
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
    # 3. Fallback
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    return None

# -------------------------------------------------
# Chat History
# -------------------------------------------------

class ChatMsg(TypedDict):
    role: str
    content: str

_HISTORY: Dict[str, List[ChatMsg]] = {}

def _norm_role(role: str) -> str:
    r = (role or "").strip().lower()
    if r in ("user", "you"): return "user"
    if r in ("assistant", "planner", "system", "coder", "agent"): return "assistant"
    return "user"

def _append_history(project_id: str, role: str, content: str, max_items: int = 16) -> None:
    if not project_id: return
    msg = {"role": _norm_role(role), "content": (content or "").strip()}
    if not msg["content"]: return
    _HISTORY.setdefault(project_id, []).append(msg)
    if len(_HISTORY[project_id]) > max_items:
        _HISTORY[project_id] = _HISTORY[project_id][-max_items:]

def _get_history(project_id: str, max_items: int = 12) -> List[ChatMsg]:
    if not project_id: return []
    return list(_HISTORY.get(project_id, []))[-max_items:]

# -------------------------------------------------
# Planner Class
# -------------------------------------------------

class Planner:
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
        
        project_id = str(project_context.get("project_id") or "").strip()
        if project_id:
            _append_history(project_id, "user", user_request)

        capabilities = self._infer_capabilities(user_request)
        
        # -------------------------------------------------------
        # SYSTEM PROMPT (UPDATED FOR BOILERPLATE + AI SPECS)
        # -------------------------------------------------------
        system_prompt = (
    "You are the Lead Architect for a high-performance **Full-Stack** web application. Your goal is to create a strategic, step-by-step build plan for an AI Coder specialized in **React (Frontend)** AND **Node.js/Express (Backend)**. Strictly give NO CODE AT ALL, in no form. But you MUST REASON HARD.\n"
    "CRITICAL CONTEXT: The AI Coder executes tasks in isolation. It has NO memory of previous files unless you provide context in *every single task description*.\n\n"

    "Rules:\n"
    "MANDATORY OUTPUT FORMAT: JSON OBJECT ONLY. Do NOT wrap in markdown blocks.\n"
    "{\n"
    '  "assistant_message": "A friendly summary of the architecture...",\n'
    '  "tasks": [\n'
    '    "Step 1: [Project: AppName | Stack: FullStack | Context: (FULL SUMMARY)] Create `db/schema.ts` and `drizzle.config.ts` for database setup...",\n'
    '    "Step 2: [Project: AppName | Stack: FullStack | Context: (FULL SUMMARY)] Modify `server.js` to setup API..."\n'
    "  ]\n"
    "}\n\n"

    "ARCHITECTURAL STANDARDS (MUST FOLLOW):\n"
    "1. **Pre-Existing Infrastructure (DO NOT CREATE THESE):**\n"
    "   - **Root**: `package.json` (React, Vite, Tailwind, Express, Drizzle ORM, SQLite).\n"
    "   - **Frontend**: `src/App.tsx`, `src/main.tsx`, `src/lib/utils.ts`, `vite.config.ts`, `tailwind.config.js`.\n"
    "   - **UI Library**: `src/components/ui/` & `src/components/magicui/`.\n"
    "   - **Backend**: `server.js` is the entry point. `routes/` folder for API logic.\n"
    "   - **Database**: Drizzle ORM with `better-sqlite3`. The DB will be a local file (`sqlite.db`).\n"
    "2. **Task Strategy:**\n"
    "   - **NEVER** assign a task to create `package.json` or `index.html`. They exist.\n"
    "   - **Database Tasks**: Instruct the coder to create `db/schema.ts` (for tables), `db/index.ts` (to export the db connection), and `drizzle.config.ts` at the root.\n"
    "   - **Frontend Tasks**: Modify `src/pages/Index.tsx` to implement layout. Create components in `src/components/`.\n"
    "   - **Backend Tasks**: Modify `server.js` to add middleware/routes. Create specific route files in `routes/`.\n"
    "3. **The Wiring & Evolution Rule (CRITICAL - NO DEAD CODE):**\n"
    "   - **Frontend Wiring**: Every new component MUST be immediately imported and used.\n"
    "   - **Backend Wiring**: Every new route file MUST be immediately mounted in `server.js`.\n"
    "4. **The 'Global Blueprint' Rule:**\n"
    "   - Every task string MUST start with: `[Project: {Name} | Stack: FullStack | Context: {FULL_APP_DESCRIPTION_HERE}] ...`\n"
    "   - **CRITICAL**: The `Context` section MUST contain the FULL description of what the app is supposed to do.\n\n"

    "TASK WRITING GUIDELINES:\n"
    "1. **No-Build Specifics:** \n"
    "   - NEVER ask for `npm run dev` or `vite.config.js`.\n"
    "   - NEVER generate an `.env` file.\n"
    "   - Frontend Imports: Use `@/` aliases.\n"
    "   - Backend Imports: Use relative paths with `.js` extension.\n"
    "2. **AI Integration Specs (USE THESE EXACTLY):**\n"
    "   - **High-Performance Logic**: Use `process.env.OPENROUTER_API_KEY` and 'openai/gpt-oss-20b:free'.\n"
    "   - **Vision**: Use 'accounts/fireworks/models/qwen3-8b'.\n"
    "   - **Voice (STT)**: 'accounts/fireworks/models/whisper-v3-turbo'.\n"
    "   - **Voice (TTS)**: 'openai/gpt-audio-mini'.\n"
    "   - **Image Gen**: 'accounts/fireworks/models/playground-v2-5-1024px-aesthetic'.\n"
    "   - **BG Removal**: Use `process.env.REM_BG_API_KEY`.\n"
    "3. **Volume:** \n"
    "   - Always try to ask the user at least 2 questions to elaborate on their request. WHEN YOU ASK A QUESTION DO NOT GENERATE TASKS AT ALL. Do not generate tasks even if the user asks a question.\n"
    "   - Simple Apps: 8-10 tasks (Mix of DB, Backend, Frontend).(if there are no questions only!)\n"
    "   - Above Simple Apps: 14-22 tasks.(if there are no questions only!)\n"
    "   - Debugging Tasks: 1-2 tasks.(if there are no questions only!)\n"
    "   - Never exceed 450 tokens per step. Update `server.js` and `App.tsx` **LAST** to wire up components/routes."
        )
        
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
            "messages": messages,
            "provider": {
            "order": ["baseten/fp4", "together"],
            "allow_fallbacks": False,
            "sort": "throughput"
            }
        }
        
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "HTTP-Referer": SITE_URL, # OpenRouter Requirement
            "X-Title": SITE_NAME,     # OpenRouter Requirement
        }

        # --- RETRY LOGIC (503s AND Invalid JSON) ---
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                with httpx.Client(timeout=120.0) as client:
                    resp = client.post(OPENROUTER_URL, json=payload, headers=headers)
                    
                    if resp.status_code == 503:
                        if attempt < max_retries:
                            time.sleep(1)
                            continue
                        else:
                            error_msg = "Service Unavailable (503)."
                            _append_history(project_id, "system", error_msg)
                            return {
                                "assistant_message": error_msg,
                                "plan": {"todo": []},
                                "todo_md": "# Service Unavailable",
                                "usage": {"total_tokens": 0} 
                            }
                            
                    resp.raise_for_status()
                    data_api = resp.json()

                raw = data_api["choices"][0]["message"]["content"]
                data = _extract_json(raw)
                
                if not data:
                    if attempt < max_retries:
                        time.sleep(1) 
                        continue 
                    else:
                        raise ValueError(f"Could not extract JSON: {raw[:100]}...")
                
                tasks = data.get("tasks", [])
                assistant_message = data.get("assistant_message", "Plan updated.")
                usage = data_api.get("usage", {})
                total_tokens = int(usage.get("total_tokens", 0))*2.5

                base_plan = {
                    "capabilities": [],
                    "ai_modules": [],
                    "glue_files": [],
                    "todo": tasks,
                }

                if project_id:
                    _append_history(project_id, "assistant", assistant_message)

                return {
                    "assistant_message": assistant_message,
                    "plan": base_plan,
                    "todo_md": self._to_todo_md(base_plan, assistant_message),
                    "usage": {"total_tokens": int(total_tokens)*2.5}
                }

            except Exception as e:
                if attempt < max_retries:
                     time.sleep(1)
                     continue
                
                fallback_msg = f"Error generating plan: {str(e)}"
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
        tasks = plan.get("todo", [])
        
        # If there are no tasks (e.g. just a chat response), return empty string
        if not tasks:
            return ""

        lines = ["# Build Plan\n", "## Tasks"]
        for task in tasks:
            lines.append(f"- {task}")
            
        return "\n".join(lines)