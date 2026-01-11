# backend/ai/Xcoder.py
"""
Xcoder.py — GOR://A X-MODE AI Engine (Fireworks Qwen3-Coder)

- The "Elite" version of the coder.
- Enforces Purple/Neon aesthetics (#bd00ff).
- Writes "Futuristic/Cyberpunk" UI code.
"""

from __future__ import annotations

import os
import json
import re
from typing import Dict, Any, List, Optional, Tuple

import httpx

# --- Configuration for Fireworks AI ---
FIREWORKS_API_KEY = os.getenv("FIREWORKS_API_KEY")
# Using the high-performance Qwen3-Coder model for X-Mode
FIREWORKS_MODEL = os.getenv("FIREWORKS_MODEL", "accounts/fireworks/models/glm-4p7") 
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
    
    # If we get here, the model probably just chatted without JSON.
    return None


class XCoder:
    def __init__(self, timeout_s: float = 120.0):
        self.timeout_s = timeout_s

    async def _call_fireworks(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.2, # Slightly higher for creativity in Xmode
    ) -> Tuple[str, int]:
        """
        Returns (content, total_tokens)
        """
        payload = {
            "model": FIREWORKS_MODEL,
            "messages": messages,
            "temperature": temperature,
            "stream": False
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
            total_tokens = int(usage.get("total_tokens", 0))*5.5
            
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

        # Get the friendly message
        user_msg = parsed.get("message") or ops[0].get("message") or "X-Mode Engine Active..."

        # Take only the first operation
        op = ops[0]
        action_raw = (op.get("action") or "").strip()
        action = ACTION_NORMALIZE.get(action_raw, action_raw)

        if action in {"patch_file", "patch"}:
            action = "overwrite_file"

        if action not in ALLOWED_ACTIONS:
             if action in ["delete_file", "move_file"]:
                 return {"message": "X-Mode ignores delete requests.", "operations": []}
             raise ValueError(f"Unknown action: {action}")

        path = op.get("path")
        if not path or not isinstance(path, str):
            raise ValueError("Operation requires a valid 'path'")

        content = op.get("content")
        if content is None:
            raise ValueError("Operation requires 'content'")

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
        history: Optional[List[Dict[str, str]]] = None,
        max_retries: int = 2,
    ) -> Dict[str, Any]:

        file_list = sorted(list(file_tree.keys()))
        file_list_txt = "\n".join(f"- {p}" for p in file_list[:300])
        
        context_snippets: List[str] = []
        for p in ["app.py", "main.py", "index.html", "requirements.txt", "styles.css", "script.js"]:
            if p in file_tree:
                c = file_tree[p]
                context_snippets.append(f"--- {p} ---\n{c[:8000]}\n")

        # --- XMODE PERSONA ---
        system_prompt = (
            "You are an expert AI Coder running on Deepseek v3.2 via Fireworks AI. When you build you never make a sample application... but you make the real deal\n"
            "Your Goal: Implement the requested task by generating the full code for ONE file. Use .env for API keys. For chatbots use 'llama-3.1-8b-instant' logic, for tts use 'canopylabs/orpheus-v1-english' and for stt use 'whisper-large-v3'. Try and make each file very long (500+ lines) and elaborate with the best ui.\n\n"
            "Make each file really elaborate with new exciting fonts, the best css styles and modern ui design, catering to the needs of the app, make each file minimum 300 lines long \n"
            "RESPONSE FORMAT (JSON ONLY):\n"
            "{\n"
            '  "message": "A short, friendly sentence telling the user what you are doing (e.g. \"I am creating the login page layout.) also use this to talk to the user about issues or any questions.\")",\n'
            '  "operations": [\n'
            "    {\n"
            '      "action": "create_file" | "overwrite_file",\n'
            '      "path": "path/to/file.ext",\n'
            '      "content": "FULL FILE CONTENT HERE"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "RULES:\n"
            "MOST IMPORTANT: never ever make a .env file not matter what is asked and never make a folder with the project name.\n"
            "1. Output valid JSON only. Do not add markdown text outside the JSON. Try to make a diverse folder stucture eg: intead of having a stt.py in the repository root file have a ai/stt.py\n"
            "2. EXACTLY ONE operation in the 'operations' array and never ever make a .env file\n"
            "3. Content must be the FULL file (no diffs) and be very specific in the requirments.txt. Never ever add any placeholder text either like lorem ipusm... it should always be the real thing.\n"
        )

        user_prompt = (
            f"Project: {project_name} (X-MODE)\n"
            f"Task: {plan_text}\n\n"
            f"Existing Files:\n{file_list_txt}\n\n"
            f"Context:\n{''.join(context_snippets) if context_snippets else '(none)'}\n\n"
            "Generate the X-Mode JSON response now."
        )

        # MERGE SYSTEM PROMPT INTO USER MESSAGE to avoid 400 Bad Request
        combined_prompt = f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\nUSER TASK:\n{user_prompt}"

        # Initialize messages list
        messages = []
        
        # Add history
        if history:
            messages.extend(history[-4:])
            
        # Append combined user message
        messages.append({"role": "user", "content": combined_prompt})

        last_err: Optional[str] = None
        last_raw: Optional[str] = None

        for attempt in range(max_retries + 1):
            try:
                # Call Fireworks instead of SambaNova
                raw, tokens = await self._call_fireworks(messages, temperature=0.2)
                last_raw = raw
                
                parsed = _extract_json(raw)
                if not parsed:
                    raise ValueError("Could not extract JSON from response")
                
                canonical = self._normalize_and_validate_ops(parsed)
                canonical["usage"] = {"total_tokens": int(tokens)*3.5} # XMODE multiplier
                return canonical
                
            except Exception as e:
                last_err = str(e)
                correction_msg = (
                    f"SYSTEM ERROR: Invalid JSON detected ({last_err}).\n"
                    "X-CODER, recalibrate and output PURE JSON only."
                )
                if last_raw:
                     messages.append({"role": "assistant", "content": last_raw[:4000]})
                messages.append({"role": "user", "content": correction_msg})

        safe_raw = (last_raw or "")[:500]
        raise ValueError(f"X-Coder failed: {last_err}. Raw start: {safe_raw}")