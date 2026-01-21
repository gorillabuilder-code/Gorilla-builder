"""
coder.py — gor://a AI Code Generation Engine (Fireworks Minimax-M2P1)
- Calls Fireworks AI chat completions (Minimax-M2P1)
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

# --- Configuration for Fireworks AI ---
FIREWORKS_API_KEY = os.getenv("FIREWORKS_API_KEY")
# Using Minimax as requested for high-quality reasoning
FIREWORKS_MODEL = os.getenv("FIREWORKS_MODEL", "accounts/fireworks/models/minimax-m2p1")
FIREWORKS_URL = os.getenv("FIREWORKS_URL", "https://api.fireworks.ai/inference/v1/chat/completions")

if not FIREWORKS_API_KEY:
    raise RuntimeError("FIREWORKS_API_KEY must be configured in the environment")

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

    async def _call_fireworks(
        self, 
        messages: List[Dict[str, str]], 
        temperature: float = 0.1,
    ) -> Tuple[str, int]:
        """ Returns (content, total_tokens) """
        payload = {
            "model": FIREWORKS_MODEL,
            "messages": messages,
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {FIREWORKS_API_KEY}",
            "Content-Type": "application/json",
        }
        
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(FIREWORKS_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            total_tokens = int(usage.get("total_tokens", 0)) * 1.65
            
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
        # file_list_txt = "\n".join(f"- {p}" for p in file_list[:300]) # Unused in prompt currently, but good context
        
        context_snippets: List[str] = []
        for p in ["app.jsx", "main.jsx", "index.html", "package.json", "styles.css", "server.js"]:
            if p in file_tree:
                c = file_tree[p]
                context_snippets.append(f"--- {p} ---\n{c[:8000]}\n")
        
        full_context_text = "\n".join(context_snippets)

        # 3. Define System Prompt (Immutable Rules)
        system_prompt = (
            "You are an expert Full-Stack AI Coder. You build high-quality Web Apps using a Node.js backend and a **Runtime React Frontend** (parsed via esbuild). "
            "When you are told to setup a file, you DO NOT put 'lorem ipsum', 'coming soon', or placeholders. You write the REAL, FUNCTIONAL code immediately.\n"
            "Your Goal: Implement the requested task by generating the full code for ONE or MORE files. \n\n"

            "API & MODELS CONFIGURATION:\n"
            "- Use `process.env.FIREWORKS_API_KEY` for AI. \n"
            "- Chat: 'accounts/fireworks/models/qwen3-8b'\n"
            "- STT: 'accounts/fireworks/models/whisper-v3-turbo'\n"
            "- Vision: 'accounts/fireworks/models/qwen3-vl-30b-a3b-instruct'\n"
            "- Image Gen: 'accounts/fireworks/models/playground-v2-5-1024px-aesthetic'\n"
            "- Background Removal: Use `process.env.REM_BG_API_KEY`\n\n"

            "STRICT SIZE CONSTRAINT: Keep files under 400 lines. Do not truncate.\n\n"

            "RESPONSE FORMAT (JSON ONLY):\n"
            "{\n"
            '  "message": "A short, friendly status update.",\n'
            '  "operations": [\n'
            "    {\n"
            '      "action": "create_file" | "overwrite_file",\n'
            '      "path": "path/to/file.ext",\n'
            '      "content": "FULL FILE CONTENT HERE"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"

            "GLOBAL RULES:\n"
            "1. Output valid JSON only. No markdown blocks.\n"
            "2. NEVER generate .env or Dockerfile.\n"
            "3. NEVER use literal '\\n'. Use physical newlines.\n\n"

            "BACKEND RULES (Node/Express):\n"
            "1. Environment: Node.js with Express. **ALWAYS use `require('dotenv').config();` at the very top.**\n"
            "2. HTTP Client: **USE NATIVE `fetch`** for Fireworks/External APIs. Do NOT use Axios.\n"
            "   **MANDATORY FETCH PATTERN:**\n"
            "   ```javascript\n"
            "   const response = await fetch('[https://api.fireworks.ai/inference/v1/chat/completions](https://api.fireworks.ai/inference/v1/chat/completions)', {\n"
            "     method: 'POST',\n"
            "     headers: {\n"
            "       'Accept': 'application/json',\n"
            "       'Content-Type': 'application/json',\n"
            "       'Authorization': `Bearer ${process.env.FIREWORKS_API_KEY}`\n"
            "     },\n"
            "     body: JSON.stringify({ ... })\n"
            "   });\n"
            "   ```\n"
            "3. PACKAGE.JSON: \n"
            "   - Scripts: `\"start\": \"node server.js\"`\n"
            "   - DevDependencies: Include `\"esbuild\": \"^0.19.0\"` (Critical for syntax checking).\n"
            "4. **ERROR BRIDGE (MANDATORY)**: In `server.js`, add this route to log frontend errors:\n"
            "   ```javascript\n"
            "   app.use(express.json());\n"
            "   const fs = require('fs');\n"
            "   app.post('/api/log-error', (req, res) => {\n"
            "     const err = req.body.error;\n"
            "     const msg = `[FRONTEND FATAL] ${err}\\n`;\n"
            "     console.error(msg); // Prints to Terminal for RunManager\n"
            "     try { fs.appendFileSync('server_errors.txt', msg); } catch(e) {}\n"
            "     res.json({ success: true });\n"
            "   });\n"
            "   ```\n"
            "5. STATIC SERVING: `app.use('/static', express.static('static'));` and serve `index.html` at root `/`.\n\n"

            "FRONTEND RULES (MODERN REACT via CDN):\n"
            "1. **DIRECTORY STRUCTURE (CRITICAL)**: \n"
            "   - `index.html` goes in root.\n"
            "   - `main.js` goes in `static/main.js`.\n"
            "   - ALL Components must go in `static/components/` (e.g., `static/components/Header.js`).\n"
            "   - **ENTRY SCRIPT**: In `index.html`, load main.js like this: `<script type='text/babel' data-type='module' src='static/main.js'></script>`. **DO NOT use `type='module'` alone** (it will crash browsers with JSX).\n"
            "2. **THE SPY SCRIPT**: In `index.html` `<head>`, add this script FIRST:\n"
            "   `<script>window.onerror = function(msg, url, line) { fetch('api/log-error', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ error: msg + ' at ' + url + ':' + line }) }); };</script>`\n"
            "3. **RELATIVE FETCH PATHS**: When fetching your own backend API, **NEVER use a leading slash**.\n"
            "   - ❌ BAD: `fetch('/api/chat')` (Fails in Proxy)\n"
            "   - ✅ GOOD: `fetch('api/chat')` (Works in Proxy)\n"
            "4. NO BUILD STEP (ESBUILD CHECK): Do NOT create vite.config.js or webpack.config.js. However, write strictly valid JSX that `esbuild` can parse.\n"
            "5. IMPORTS (ESM) - **CRITICAL PATH FIX**:\n"
            "   - React: `import React from 'https://esm.sh/react@18'`\n"
            "   - ReactDOM: `import ReactDOM from 'https://esm.sh/react-dom@18'`\n"
            "   - **INTERNAL IMPORTS (CRITICAL)**: Since Babel executes code in the context of `index.html` (root), you MUST import components starting with `static/`.\n"
            "     - ❌ WRONG: `import Header from './components/Header.js'` (Resolves to /run/{uuid}/components... -> 404)\n"
            "     - ❌ WRONG: `import Header from './static/components/Header.js'` (Resolves to /run/{uuid}/static/static... -> 404)\n"
            "     - ❌ WRONG: `import Header from '/static/components/Header.js'` (Resolves to domain root -> 404)\n"
            "     - ✅ CORRECT: `import Header from 'static/components/Header.js'` (Resolves relative to root -> /run/{uuid}/static/... -> 200)\n"
            "     - **ALWAYS** include the `.js` extension.\n"
            "6. **INTEGRATION**: If you create a component, you MUST update `static/main.js` to import and render it immediately.\n"
            "7. **SELF-CORRECTION**: If the user prompt contains '[CRITICAL RUNTIME ERRORS DETECTED]', your PRIMARY GOAL is to fix those errors. Analyze the stack trace, identify the file path (remembering the 'static/' prefix issue), and rewrite the file to fix the crash."
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
                # Call Fireworks
                raw, tokens = await self._call_fireworks(messages, temperature=0.6)
                last_raw = raw
                cumulative_tokens += tokens * 1.75
                
                parsed = _extract_json(raw)
                if not parsed:
                    raise ValueError("Could not extract JSON from response")
                    
                canonical = self._normalize_and_validate_ops(parsed)
                # IMPORTANT: Return total cumulative tokens so user is billed for retries
                canonical["usage"] = {"total_tokens": cumulative_tokens}

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
                        "Please fix the format. Output valid JSON only. Ensure all brackets are closed.\n"
                        f"REMEMBER YOUR GOAL: {plan_section}\n"
                        f"REMEMBER THE SYSTEM RULES: Output valid JSON with 'operations' list."
                    )
                    
                    # --- CRITICAL FIX: RE-INJECT CONTEXT ON RETRY ---
                    # Instead of just appending "fix it", we treat the next attempt as a fresh turn 
                    # with the error appended to the user prompt. This forces the model to see the
                    # full context again, reducing "hallucination loops".
                    
                    # We append the failure to the temporary message list
                    if last_raw:
                        messages.append({"role": "assistant", "content": last_raw[:5000]})
                    
                    messages.append({"role": "user", "content": correction_msg})
                    
                    await asyncio.sleep(1)
                    continue
                else:
                    break
        
        safe_raw = (last_raw or "")[:500]
        raise ValueError(f"Coder failed after {max_retries+1} attempts: {last_err}. Raw start: {safe_raw}")