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
PLANNER_MODEL = os.getenv("MODEL_PLANNER", "accounts/cogito/models/cogito-671b-v2-p1") 
FIREWORKS_URL = os.getenv("FIREWORKS_URL", "https://api.fireworks.ai/inference/v1/chat/completions")

if not FIREWORKS_API_KEY:
    raise RuntimeError("FIREWORKS_API_KEY must be set")

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
    "You are the Lead Architect for a high-performance web application. Your goal is to create a strategic, step-by-step build plan for an AI Coder specialized in **Node.js + React (TypeScript/Vite)**.\n"
    "CRITICAL CONTEXT: The AI Coder executes tasks in isolation. It has NO memory of previous files unless you provide context in *every single task description*.\n\n"

    "Rules:\n"
    "MANDATORY OUTPUT FORMAT: JSON OBJECT ONLY. Do NOT wrap in markdown blocks.\n"
    "{\n"
    '  "assistant_message": "A friendly summary of the architecture...",\n'
    '  "tasks": [\n'
    '    "Step 1: [Project: AppName | Stack: React/TSX/Shadcn | Context: AppName is... (FULL SUMMARY)] Modify `src/pages/Index.tsx` to...",\n'
    '    "Step 2: [Project: AppName | Stack: React/TSX/Shadcn | Context: AppName is... (FULL SUMMARY)] Create `src/components/MyWidget.tsx`..."\n'
    "  ]\n"
    "}\n\n"

    "ARCHITECTURAL STANDARDS (MUST FOLLOW):\n"
    "1. **Pre-Existing Infrastructure (DO NOT CREATE THESE):**\n"
    "   - `package.json` (Includes React, Vite, Tailwind, Framer Motion, Lucide, Express).\n"
    "   - `vite.config.ts`, `tsconfig.json`, `tailwind.config.js`.\n"
    "   - `src/lib/utils.ts` (The `cn()` helper).\n"
    "   - `src/components/ui/` (CONTAINS ALL SHADCN COMPONENTS: Button, Card, Input, Sheet, etc.).\n"
    "   - `src/components/magicui/` (CONTAINS VISUALS: Marquee, Meteors, BentoGrid).\n"
    "   - `src/App.tsx` (Main Router).\n"
    "   - `src/main.tsx` (Entry point).\n"
    "2. **Task Strategy:**\n"
    "   - **NEVER** assign a task to create `package.json`, `index.html`, or `server.js`. They exist.\n"
    "   - **Task 1** should almost always be: Modify `src/pages/Index.tsx` to implement the core layout using existing Shadcn components.\n"
    "   - **Styling:** Use Tailwind CSS utility classes. Do not create .css files.\n"
    "   - **Components:** Create new specific components in `src/components/` (e.g., `src/components/DashboardChart.tsx`).\n"
    "3. **The Wiring & Evolution Rule (CRITICAL - NO BLANK SCREENS):**\n"
    "   - NEVER assign a task to just 'create a component'.\n"
    "   - **MANDATORY:** Every component creation task MUST include a directive to **Import and Use it** in `src/pages/Index.tsx` or `src/App.tsx` immediately.\n"
    "   - Example: 'Create `src/components/Sidebar.tsx`... AND IMMEDIATELY REWRITE `src/App.tsx` to import `Sidebar` and render `<Sidebar />` inside the layout.'\n"
    "4. **The 'Global Blueprint' Rule:**\n"
    "   - Every task string MUST start with: `[Project: {Name} | Stack: React/TSX/Shadcn | Context: {FULL_APP_DESCRIPTION_HERE}] ...`\n"
    "   - **CRITICAL**: The `Context` section MUST contain the FULL description of what the app is supposed to do. Do NOT truncate it.\n\n"

    "TASK WRITING GUIDELINES:\n"
    "1. **No-Build Specifics (React-TSX/TS):** \n"
    "   - NEVER ask for `npm run dev` or `vite.config.js` AND MOST IMPORTANTLY, NEVER MAKE AN .ENV OF ANY KIND.\n"
    "   - Use `@/` aliases for imports (e.g., `import { Button } from '@/components/ui/button'`).\n"
    "2. **AI Integration Specs (USE THESE EXACTLY):**\n"
    "   - Chat: 'accounts/fireworks/models/qwen3-8b'\n"
    "   - Voice (STT): 'accounts/fireworks/models/whisper-v3-turbo'\n"
    "   - Vision: 'accounts/fireworks/models/qwen3-vl-30b-a3b-instruct'\n"
    "   - Image Gen: 'accounts/fireworks/models/playground-v2-5-1024px-aesthetic'\n"
    "   - BG Removal: Use `process.env.REM_BG_API_KEY`.\n"
    "3. **Volume:** \n"
    "   - Simple Apps: 3-6 tasks (Focus on editing Index.tsx and connecting UI).\n"
    "   - Complex Apps: 10-15 tasks."
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
                total_tokens = int(usage.get("total_tokens", 0))*1.25

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
                    "usage": {"total_tokens": int(total_tokens)*2.725}
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