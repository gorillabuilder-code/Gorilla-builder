"""
coder.py — gor://a AI Code Generation Engine (OpenRouter xAI Grok)
- Calls OpenRouter API (x-ai/grok-4.1-fast)
- Uses Regex to reliably extract JSON from "chatty" models
- Enforces a 'message' field so the AI talks to the user
- Returns token usage statistics
- Auto-retries on failure
- Maintains separate chat history contexts per Project/App
"""
from __future__ import annotations
import os
import json
import re
from typing import Dict, Any, List, Optional, Tuple
import asyncio
import httpx

# --- Configuration for OpenRouter ---
# CHANGED: Switched from Fireworks to OpenRouter env vars
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "x-ai/grok-code-fast-1") # Grok 2 is the latest stable on OpenRouter
OPENROUTER_URL = os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")

# OpenRouter requirements for rankings
SITE_URL = os.getenv("SITE_URL", "https://gorillabuilder.dev")
SITE_NAME = os.getenv("SITE_NAME", "Gorilla Builder")

if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY must be configured in the environment")

ALLOWED_ACTIONS = {"create_file", "overwrite_file"}
ACTION_NORMALIZE = {
    "update_file": "overwrite_file",
    "replace_file": "overwrite_file",
    "write_file": "overwrite_file",
    "modify_file": "overwrite_file",
    "upsert_file": "overwrite_file",
    "create": "create_file",
    "overwrite": "overwrite_file",
    "patch": "overwrite_file",
    "patch_file": "overwrite_file",
}

def _extract_json(text: str) -> Any:
    """ Robustly extract the largest valid JSON object from a string. """
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

class Coder:
    def __init__(self, timeout_s: float = 120.0):
        self.timeout_s = timeout_s
        # Internal state to track history per project
        # Key: project_name, Value: List of message dicts
        self.project_states: Dict[str, List[Dict[str, str]]] = {}

    # CHANGED: Renamed from _call_fireworks to _call_provider and updated headers/URL
    async def _call_provider(
        self, 
        messages: List[Dict[str, str]], 
        temperature: float = 0.1,
    ) -> Tuple[str, int]:
        """ Returns (content, total_tokens) """
        payload = {
            "model": OPENROUTER_MODEL,
            "messages": messages,
            "temperature": temperature,
        }
        
        # OpenRouter specific headers
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": SITE_URL, 
            "X-Title": SITE_NAME,
        }
        
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            total_tokens = int(usage.get("total_tokens", 0))*0.9
            
            return content, total_tokens

    def _normalize_and_validate_ops(self, parsed: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(parsed, dict):
            raise ValueError("Model output was not a valid JSON object")
        
        ops = parsed.get("operations")
        if ops is None:
            op1 = parsed.get("operation")
            if op1:
                ops = [op1]
        
        if not isinstance(ops, list) or not ops:
            raise ValueError("JSON missing 'operations' list")

        user_msg = parsed.get("message") or ops[0].get("message") or "I am working on the file..."
        op = ops[0]
        
        action_raw = (op.get("action") or "").strip()
        action = ACTION_NORMALIZE.get(action_raw, action_raw)
        
        if action in {"patch_file", "patch"}:
            action = "overwrite_file"
            
        if action not in ALLOWED_ACTIONS:
            if action in ["delete_file", "move_file"]:
                return {"message": "Skipping unsupported action", "operations": []}
            raise ValueError(f"Unknown action: {action}")
            
        path = op.get("path")
        if not path or not isinstance(path, str):
            raise ValueError("Operation requires a valid 'path'")
            
        content = op.get("content")
        if content is None:
            raise ValueError("Operation requires 'content'")

        # --- FIX: AGGRESSIVE NEWLINE CLEANING ---
        if isinstance(content, list):
            content = "\n".join(str(x) for x in content)
        elif isinstance(content, str):
            content = content.replace("\\n", "\n")
            
        return {
            "message": user_msg,
            "operations": [{"action": action, "path": path.strip(), "content": str(content)}]
        }

    async def generate_code(
        self,
        plan_section: str,
        plan_text: str,
        file_tree: Dict[str, str],
        project_name: str,
        history: Optional[List[Dict[str, str]]] = None, # Deprecated, kept for signature compat
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        
        # 1. Initialize History for this specific project if not exists
        if project_name not in self.project_states:
            self.project_states[project_name] = []
            
        # 2. Build Context (File Tree)
        # We perform this fresh every time to ensure the model sees the latest file state
        file_list = sorted(list(file_tree.keys()))
        
        context_snippets: List[str] = []
        # UPDATED: Prioritize seeing the new boilerplate structure
        priority_files = ["src/App.tsx", "src/main.tsx", "src/pages/Index.tsx", "src/index.css", "package.json"]
        
        for p in priority_files:
            if p in file_tree:
                c = file_tree[p]
                context_snippets.append(f"--- {p} ---\n{c[:8000]}\n")
        
        full_context_text = "\n".join(context_snippets)

        # 3. Define System Prompt (Immutable Rules)
        system_prompt = (
            "You are an expert **Full-Stack** AI Coder. You build high-quality Web Apps using a **React + TypeScript + Tailwind + Shadcn/UI** (Frontend) AND **Node.js + Express** (Backend) stack.\n"
            "You are working in a pre-existing environment. **DO NOT initialize a new project.**\n"
            "Your Goal: Implement the requested task by editing EXISTING files (e.g., `src/App.tsx`, `server.js`) or creating NEW components/routes.\n\n"

            "CRITICAL CONTEXT - THE GOLDEN BOILERPLATE:\n"
            "The following tools are ALREADY installed and configured:\n"
            "1. **React + TypeScript (Vite)**: Frontend lives in `src/`. Use `.tsx` for UI.\n"
            "2. **Tailwind CSS**: Use utility classes (e.g., `className='p-4 bg-blue-500'`).\n"
            "3. **Shadcn/UI**: The folder `src/components/ui/` is fully populated.\n"
            "4. **Node.js (ES Modules)**: Backend uses `import/export`. Entry point is `server.js`.\n"
            "5. **Express.js**: Server is configured with CORS and Dotenv.\n\n"
            "**IMPORTANT** even though these are already in place, please try to make the UI less bootstrappy and more fun and polished, try to make the components yourself instead of always using shadcn UI, but when feel the need to use shadcn UI, do it, in a not very obivious way. .\n\n"

            "STRICT IMPORT RULES:\n"
            "- **FRONTEND (`src/` files)**:\n"
            "  - Use `@/` alias (e.g., `import { Button } from '@/components/ui/button'`).\n"
            "  - Do NOT use relative paths like `../../`.\n"
            "- **BACKEND (`server.js`, `routes/` files)**:\n"
            "  - Use **Relative Paths** (e.g., `import router from './routes/api.js'`).\n"
            "  - **CRITICAL**: You MUST include the `.js` extension for local backend imports.\n\n"

            "API & MODELS CONFIGURATION:\n"
            "- Use `process.env.OPENROUTER_API_KEY and FIREWORKS_API_KEY and REMBG_API_KEY` for AI. Listen to the planner.\n"

            "RESPONSE FORMAT (JSON ONLY):\n"
            "{\n"
            '  "message": "A short, friendly status update.",\n'
            '  "operations": [\n'
            "    {\n"
            '      "action": "create_file" | "overwrite_file",\n'
            '      "path": "src/pages/Dashboard.tsx" OR "routes/api.js",\n'
            '      "content": "FULL FILE CONTENT HERE"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"

            "GLOBAL RULES:\n"
            "1. Output valid JSON only. No markdown blocks. ALL API KEYS ARE IN THE ENVIRONMENT.\n"
            "2. NEVER generate .env or Dockerfile.\n"
            "3. NEVER use literal '\\n'. Use physical newlines.\n\n"

            "SPECIFIC RULES:\n"
            "1. **Frontend (React)**: Use Functional Components. Tailwind ONLY for styling. No CSS files.\n"
            "2. **Backend (Node)**: Use `async/await`. Return JSON (`res.json`). Handle errors with `try/catch`.\n"
            "3. **Self-Correction**: If the user prompt reports a crash, analyze the stack trace and fix the specific file causing it.\n"
        )

        # 4. Construct the User Prompt for THIS SPECIFIC TURN
        current_user_prompt = (
            f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\n"
            f"CURRENT FILE CONTEXT (For Reference):\n{full_context_text}\n\n"
            f"TASK: {plan_section}\n"
            f"DETAILS: {plan_text}"
        )

        # 5. Build Message Chain: [History] + [Current Prompt]
        past_messages = self.project_states[project_name][-10:]
        
        messages = []
        for msg in past_messages:
            messages.append(msg)
        
        messages.append({"role": "user", "content": current_user_prompt})

        last_err: Optional[str] = None
        last_raw: Optional[str] = None
        cumulative_tokens: float = 0.0

        # Retry Loop
        for attempt in range(max_retries + 1):
            try:
                # CHANGED: Call _call_provider instead of _call_fireworks
                raw, tokens = await self._call_provider(messages, temperature=0.6)
                last_raw = raw
                cumulative_tokens += tokens 
                
                parsed = _extract_json(raw)
                if not parsed:
                    raise ValueError("Could not extract JSON from response")
                    
                canonical = self._normalize_and_validate_ops(parsed)
                # IMPORTANT: Return total cumulative tokens so user is billed for retries
                canonical["usage"] = {"total_tokens": cumulative_tokens*0.95}

                # --- SUCCESS: UPDATE HISTORY ---
                self.project_states[project_name].append({
                    "role": "user", 
                    "content": f"Task: {plan_section}. Details: {plan_text}"
                })
                self.project_states[project_name].append({
                    "role": "assistant", 
                    "content": raw
                })

                return canonical

            except Exception as e:
                last_err = str(e)
                print(f"Coder Attempt {attempt+1}/{max_retries+1} failed: {last_err}")
                
                if attempt < max_retries:
                    correction_msg = (
                        f"Your previous response was invalid (Error: {last_err}).\n"
                        "Please fix the format. Output valid JSON only. Ensure all brackets are closed. Do not just say SORRY, or leave it blank.\n"
                        f"REMEMBER YOUR GOAL: {plan_section}\n"
                        f"REMEMBER THE SYSTEM RULES: Output valid JSON with 'operations' list."
                    )
                    
                    # --- CRITICAL FIX: RE-INJECT CONTEXT ON RETRY ---
                    if last_raw:
                        messages.append({"role": "assistant", "content": last_raw[:5000]})
                    
                    messages.append({"role": "user", "content": correction_msg})
                    
                    await asyncio.sleep(1)
                    continue
                else:
                    break
        
        safe_raw = (last_raw or "")[:500]
        raise ValueError(f"Coder failed after {max_retries+1} attempts: {last_err}. Raw start: {safe_raw}")