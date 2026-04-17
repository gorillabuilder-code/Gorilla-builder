"""
Lineage Agent v4 — Gorilla Builder Single-Agent Brain
=======================================================

v4 changes:
  - System prompt now teaches the agent to think in "command-by-command" flow
  - Agent understands its output streams live to the user (frame by frame)
  - Agent understands the boilerplate runs TWO servers via `concurrently`:
      npm run client → Vite on port 8080 (the preview iframe)
      npm run server → Express on port 3000 (the backend)
  - Agent is explicitly told to ALWAYS `npm install` new imports before using them
  - `done: false` is encouraged for tasks where the agent needs to see stdout
"""

from __future__ import annotations

import os
import json
import re
import time
import hashlib
from typing import Dict, Any, List, Optional, Tuple

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL = os.getenv("LINEAGE_MODEL", "anthropic/claude-sonnet-4.6")
VISION_MODEL = os.getenv("VISION_MODEL", "arcee-ai/trinity-large-thinking")
OPENROUTER_URL = os.getenv(
    "OPENROUTER_URL",
    "https://openrouter.ai/api/v1/chat/completions",
).strip()
SITE_URL = os.getenv("SITE_URL", "https://gorillabuilder.dev").strip()
SITE_NAME = os.getenv("SITE_NAME", "Gor://a Builder")

MAX_CONTEXT_TOKENS = 140_000
CHARS_PER_TOKEN = 4

if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY must be set")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_external_log_callback = None


def set_log_callback(cb):
    global _external_log_callback
    _external_log_callback = cb


def log_agent(role: str, message: str, project_id: str = "") -> None:
    prefix = f"[{project_id[:8]}]" if project_id else "[AGENT]"
    ts = time.strftime("%H:%M:%S")
    colors = {
        "agent": "\033[94m", "llm": "\033[90m", "system": "\033[97m",
        "debugger": "\033[91m",
    }
    c = colors.get(role.lower(), "\033[94m")
    r = "\033[0m"; d = "\033[90m"
    print(f"{d}{ts}{r} {prefix} {c}{role.upper()}{r}: "
          f"{message[:200]}{'...' if len(message) > 200 else ''}")
    if _external_log_callback and project_id and role.lower() != "llm":
        try:
            _external_log_callback(project_id, role.lower(), message)
        except Exception:
            pass


def _render_token_limit_message() -> str:
    return (
        '<div style="display:flex;flex-direction:column;align-items:center;'
        'justify-content:center;padding:40px 30px;'
        'background:linear-gradient(135deg,rgba(15,23,42,0.9),rgba(30,10,50,0.8));'
        'border:1px solid rgba(217,70,239,0.3);border-radius:20px;'
        'text-align:center;max-width:400px;margin:20px auto;'
        'box-shadow:0 20px 60px rgba(0,0,0,0.5);">'
        '<h2 style="color:#fff;font-size:24px;font-weight:700;margin:0 0 12px;">'
        'Token Limit Reached</h2>'
        '<p style="color:#94a3b8;font-size:14px;line-height:1.6;margin:0 0 28px;'
        'max-width:280px;">Upgrade to Premium for unlimited access.</p>'
        '<a href="/pricing" style="background:linear-gradient(135deg,#d946ef,#a855f7);'
        'color:white;text-decoration:none;padding:14px 32px;border-radius:12px;'
        'font-size:14px;font-weight:600;">Upgrade to Premium</a></div>'
    )


# ---------------------------------------------------------------------------
# History mirror
# ---------------------------------------------------------------------------
_HISTORY: Dict[str, list] = {}
HISTORY_CAP = 100


def _norm_role(r: str) -> str:
    return "user" if (r or "").strip().lower() in ("user", "you") else "assistant"


def _append_history(project_id: str, role: str, content: str) -> None:
    if not project_id or not content:
        return
    _HISTORY.setdefault(project_id, []).append(
        {"role": _norm_role(role), "content": content.strip()}
    )
    if len(_HISTORY[project_id]) > HISTORY_CAP:
        _HISTORY[project_id] = _HISTORY[project_id][-HISTORY_CAP:]


def _get_history(project_id: str, max_items: int = 20) -> list:
    return list(_HISTORY.get(project_id, []))[-max_items:]


def clear_history(project_id: str) -> None:
    _HISTORY.pop(project_id, None)


# ---------------------------------------------------------------------------
# Token substitution
# ---------------------------------------------------------------------------
class TokenSubstitution:
    THRESHOLD = 500

    def __init__(self):
        self._vault: Dict[str, str] = {}
        self._reverse: Dict[str, str] = {}
        self._n = 0

    def _mk(self) -> str:
        self._n += 1
        return f"__BLOB_{self._n:04d}__"

    @staticmethod
    def _is_b64(s: str) -> bool:
        if len(s) < 100:
            return False
        sample = s[:200].strip()
        ratio = sum(1 for c in sample if c.isalnum() or c in "+/=") / len(sample)
        return ratio > 0.9 and "\n" not in sample[:100]

    def compress_tree(self, tree: Dict[str, str]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for path, content in tree.items():
            if content and len(content) > self.THRESHOLD:
                if (path.endswith(".b64")
                        or self._is_b64(content)
                        or (path.endswith(".json") and len(content) > 5000)
                        or (path.endswith(".svg") and len(content) > 3000)):
                    h = hashlib.md5(content[:200].encode()).hexdigest()
                    if h in self._reverse:
                        out[path] = self._reverse[h]
                    else:
                        pid = self._mk()
                        self._vault[pid] = content
                        self._reverse[h] = pid
                        out[path] = pid
                    continue
            out[path] = content
        return out

    def expand(self, text: str) -> str:
        for ph, original in self._vault.items():
            if ph in text:
                text = text.replace(ph, original)
        return text


def _estimate_tokens(messages: list) -> int:
    total = 0
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, str):
            total += len(c) // CHARS_PER_TOKEN
        elif isinstance(c, list):
            for item in c:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        total += len(item.get("text", "")) // CHARS_PER_TOKEN
                    elif item.get("type") == "image_url":
                        total += 1000
    return total


def _shorten(messages: list, max_tokens: int = MAX_CONTEXT_TOKENS) -> list:
    if _estimate_tokens(messages) <= max_tokens:
        return messages
    sys_msg = messages[0] if messages and messages[0].get("role") == "system" else None
    recent = messages[-4:]
    return ([sys_msg] if sys_msg else []) + recent


# ---------------------------------------------------------------------------
# SYSTEM PROMPT — v4 (streaming-aware)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_BASE = r"""You are the Gorilla Builder AI — a single autonomous full-stack engineer inside an E2B Ubuntu cloud sandbox with Node 20, npm, pnpm, curl, jq, and git.

You write files and run code by emitting shell commands. Every command you emit streams its output live to the user's chat UI — they watch each line scroll by as it happens. You never produce code blocks for the user to copy; you execute everything yourself.

## OUTPUT FORMAT — STRICT JSON, NO EXCEPTIONS

One raw JSON object. No markdown fences, no prose around it.

{
  "message": "One friendly sentence for the user describing what you're doing.",
  "commands": [
    "mkdir -p src/components",
    "cat > src/components/Hero.tsx << 'GORILLA_EOF'\n...file content...\nGORILLA_EOF",
    "npm install framer-motion"
  ],
  "done": true
}

## THE done FLAG

- `"done": true`   → you're finished for this turn. The orchestrator restarts the dev server and shows the preview.
- `"done": false`  → you need to SEE the output of your commands before continuing. The orchestrator executes everything, then calls you again with stdout/stderr attached. Use this when:
    • you need to `cat` a file to see its current content
    • you need to inspect a `curl` response (e.g. a Supabase migration result)
    • you're unsure what version of a package is installed
    • you're sure all dependencies are installed (npm install has already been run, only run it if you have changed the package.json)
    • you're debugging and need to see what an error actually says

You can run multiple `done: false` cycles per request. Use them freely when it saves you from guessing.

## THE STACK (THIS BOILERPLATE, EXACTLY)

- Frontend: React 18 + TypeScript + Vite + Tailwind + Shadcn/UI in `src/components/ui/`
- Backend:  Node.js + Express (ES modules, entry `server.js`, routes in `routes/`)
- Dev runs BOTH servers in parallel via `concurrently`:
    • `npm run client` → Vite on **port 8080** ← this is what the PREVIEW IFRAME shows
    • `npm run server` → Express on **port 3000** (proxied by Vite)
- `npm run dev` starts both

When you make a visual change, the user sees it at port 8080. Keep that in mind.

## COMMAND PATTERNS

- **Create / overwrite file**:
    ```
    cat > src/App.tsx << 'GORILLA_EOF'
    ...content...
    GORILLA_EOF
    ```
    Delimiter MUST be exactly `GORILLA_EOF` (single-quoted so $vars don't expand).

- **Make dirs first**: `mkdir -p src/components/chat`

- **Install dependencies**: `npm install framer-motion lucide-react` — **ALWAYS run this BEFORE importing a package for the first time.** Vite will fail with "could not be resolved" if you import without installing.

- **Delete**: `rm path/to/file`  or  `rm -rf some/dir`  (orchestrator syncs deletions to DB)

- **Read a file**: `cat path/to/file` then `"done": false` so you see its content next turn

- **List files**: `find . -type f -not -path '*/node_modules/*' -not -path '*/.git/*'`

- **HTTP calls / SQL**: `curl -X POST ... -H "Authorization: Bearer $TOKEN" ...`

## ENVIRONMENT (sourced from .gorilla_env — available in every command)
  $GORILLA_API_KEY         — backend key for the Gorilla AI proxy
  $VITE_GORILLA_AUTH_ID    — unique auth gateway ID for this app
  $VITE_SUPABASE_URL       — (if DB linked) Supabase project URL
  $VITE_SUPABASE_ANON_KEY  — (if DB linked) Supabase anon key
  $SUPABASE_MGMT_TOKEN     — (if DB linked) Management API bearer token
  $SUPABASE_PROJECT_REF    — (if DB linked) Supabase project reference

## SAFETY
- NEVER `sudo`, `shutdown`, `reboot`, `mkfs`, or write to `/dev/sd*`
- NEVER delete `package.json`, `vite.config.ts`, or the `dev`/`client`/`server` npm scripts
- NEVER rewrite `.env` from scratch — it's already populated
- NEVER rewrite `package.json` from scratch — use `npm install` to add deps

## RULES OF PRACTICE
1. Frontend imports use `@/` alias: `import { Button } from '@/components/ui/button'`
2. Backend imports use relative paths + `.js` extension: `import router from './routes/api.js'`
3. Icons: `lucide-react`. Animations: `framer-motion`. Both must be `npm install`ed if not in package.json.
4. NEVER use Inter font. Pick distinctive typography each project.
5. Design sleek, modern, non-bootstrappy UIs.
6. async/await + try/catch in every backend route.
7. **Be surgically lazy**: only touch files that actually need changes this turn. Don't rewrite App.tsx if you're only adding a new component.
8. Wire every new component (import + render) and every new route (mount in server.js).
9. **Check `package.json` before importing.** If you're unsure whether a dep exists, either `cat package.json` + `done: false`, or just `npm install` it — installs are idempotent.

## AUTH (built-in gateway)
```tsx
import { login, onAuthStateChanged, logout } from '@/utils/auth';
useEffect(() => onAuthStateChanged(setUser), []);
<button onClick={() => login('google')}>Sign in</button>
```

## AI INTEGRATIONS via Gorilla Proxy ({GORILLA_PROXY}) — backend only
All use `process.env.GORILLA_API_KEY`.
- LLM:     POST {GORILLA_PROXY}/api/v1/chat/completions   (do NOT send model/temperature)
- Images:  POST {GORILLA_PROXY}/api/v1/images/generations  (OpenAI payload)
- STT:     POST {GORILLA_PROXY}/api/v1/audio/transcriptions (Whisper)
- TTS:     Use `window.speechSynthesis` on the frontend
- BG rm:   POST {GORILLA_PROXY}/api/v1/images/remove-background (FormData)

### IMAGE GENERATION
If the user wants AI-generated images (hero art, avatars, product mockups, etc.),
you generate them YOURSELF using curl and the Gorilla proxy. Save to `public/`
so they're served by Vite.
 
```
mkdir -p public/generated
 
# Generate a single image and save it to disk
curl -sS -X POST "{GORILLA_PROXY}/api/v1/images/generations" \
  -H "Authorization: Bearer $GORILLA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"minimalist mountain landscape at dusk, vibrant blue sky","samples":1}' \
  | jq -r '.[0].base64 // .data[0].b64_json // .base64' \
  | base64 -d > public/generated/hero.jpg
``` 
Then reference it in your React code as `/generated/hero.jpg`.

RIGHT BEFORE YOU START CODING, YOU MUST SEE THE ENTIRE FILE TREE FOR CONTEXT, AS THERE ARE MANY DEPENDENCIES ALREADY INSTALLED, ALONG WITH BOILERPLATE FILES.

**CRITICAL:** NEVER EVER RUN MORE THAN 4 COMMANDS PER TURN, AND ALWAYS ENSURE THE APP RUNS PERFECTLY ON NPM RUN DEV / BUILD BEFORE SAYING YOU'RE DONE. IF YOU'RE UNSURE, RUN ONE COMMAND AT A TIME AND USE "done": false TO SEE THE OUTPUT AS YOU GO. "done: True" should be a privellage reserved for when you're 100% sure the task is complete and the app is in a good state, THERE IS NO NEED TO HURRY, MAKE YOUR OWN TOOLS IF YOU NEED TO, ANE MAKE FULL USE OF THE SANDBOX, FOR EXAMPLE USING PYTHON SCRIPTS TO CHANGE BIG FILES...

"""


SUPABASE_PROMPT = r"""
## SUPABASE IS ACTIVE — YOU EXECUTE SQL YOURSELF

Run migrations from the shell with curl against the Management API.

### Client-side
```ts
import { createClient } from '@supabase/supabase-js';
const supabase = createClient(
  import.meta.env.VITE_SUPABASE_URL,
  import.meta.env.VITE_SUPABASE_ANON_KEY
);
```

### Write + execute a migration
```
mkdir -p migrations
cat > migrations/001_profiles.sql << 'GORILLA_EOF'
CREATE TABLE IF NOT EXISTS profiles (
  id UUID PRIMARY KEY REFERENCES auth.users ON DELETE CASCADE,
  username TEXT UNIQUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
CREATE POLICY "profiles_read_own"   ON profiles FOR SELECT USING (auth.uid() = id);
CREATE POLICY "profiles_update_own" ON profiles FOR UPDATE USING (auth.uid() = id);
CREATE POLICY "profiles_insert_own" ON profiles FOR INSERT WITH CHECK (auth.uid() = id);
GORILLA_EOF

SQL_PAYLOAD=$(cat migrations/001_profiles.sql | jq -Rs '{query: .}')
curl -sS -X POST "https://api.supabase.com/v1/projects/$SUPABASE_PROJECT_REF/database/query" \
  -H "Authorization: Bearer $SUPABASE_MGMT_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$SQL_PAYLOAD"
```

### SQL rules (non-negotiable)
- `CREATE TABLE IF NOT EXISTS` — always idempotent
- `ENABLE ROW LEVEL SECURITY` on every table
- Explicit `CREATE POLICY` for every operation you need
- Foreign keys: `REFERENCES other(id) ON DELETE CASCADE` where appropriate

### On error
If the curl response contains `"message":` with an error, the SQL failed. Set `"done": false`, inspect the output next turn, fix the SQL, and re-run. Never retry the same broken SQL.
"""


DEBUG_PROMPT = r"""
## DEBUG MODE
The app errored. Your ONLY job is the minimal surgical fix.
- Do NOT add features.
- Read the error carefully; identify the single root cause file.
- If unsure, `cat` the suspect file and set `"done": false` to examine it.
- Overwrite the file with the fix.
- If a dependency is missing (Vite "could not be resolved" error), `npm install` the missing package.
"""


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------
async def _call_llm(
    messages: list,
    model: str = MODEL,
    temperature: float = 0.6,
) -> Tuple[str, int]:
    messages = _shorten(messages)
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 16000
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": SITE_URL,
        "X-Title": SITE_NAME,
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    p = usage.get("prompt_tokens", 0)
    c = usage.get("completion_tokens", 0)
    weighted = int(p * 3 + c * 15)
    return content, weighted


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------
def _extract_json(text: str) -> Optional[Dict]:
    text = text.strip()
    for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)\s*```", text):
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    best = None
    best_len = 0
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        in_str = False
        esc = False
        for j in range(i, len(text)):
            c = text[j]
            if esc:
                esc = False; continue
            if c == "\\" and in_str:
                esc = True; continue
            if c == '"':
                in_str = not in_str; continue
            if in_str:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[i:j + 1]
                    if len(candidate) > best_len:
                        try:
                            parsed = json.loads(candidate)
                            best = parsed; best_len = len(candidate)
                        except json.JSONDecodeError:
                            fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
                            fixed = (fixed.replace("True", "true")
                                          .replace("False", "false")
                                          .replace("None", "null"))
                            try:
                                parsed = json.loads(fixed)
                                best = parsed; best_len = len(candidate)
                            except json.JSONDecodeError:
                                pass
                    break
    return best


# ---------------------------------------------------------------------------
# Shell safety
# ---------------------------------------------------------------------------
_DANGEROUS = [
    r"\brm\s+-rf\s+/($|\s)", r"\bsudo\b", r"\bshutdown\b", r"\breboot\b",
    r">\s*/dev/(sda|nvme|hda)", r"\bmkfs\b", r":\(\)\s*{\s*:\|:",
    r"\bdd\s+if=.*\s+of=/dev/",
]


def _is_safe_command(cmd: str) -> bool:
    for pattern in _DANGEROUS:
        if re.search(pattern, cmd, re.IGNORECASE):
            return False
    return True


# ---------------------------------------------------------------------------
# LineageAgent
# ---------------------------------------------------------------------------
class LineageAgent:
    def __init__(self, project_id: str):
        self.project_id = project_id
        self.total_tokens = 0
        self.token_sub = TokenSubstitution()

    async def run(
        self,
        user_request: str,
        file_tree: Dict[str, str],
        chat_history: Optional[list] = None,
        gorilla_proxy_url: str = "",
        has_supabase: bool = False,
        is_debug: bool = False,
        error_context: str = "",
        image_b64: Optional[str] = None,
        previous_command_output: Optional[str] = None,
    ) -> Dict[str, Any]:
        compressed = self.token_sub.compress_tree(file_tree)
        clean_paths = sorted(p for p in compressed if not p.endswith(".b64"))
        tree_str = "\n".join(f"  {p}" for p in clean_paths)

        sys_prompt = SYSTEM_PROMPT_BASE.replace(
            "{GORILLA_PROXY}",
            gorilla_proxy_url or "https://slaw-carefully-cried.ngrok-free.dev",
        )
        if has_supabase:
            sys_prompt += "\n" + SUPABASE_PROMPT
        if is_debug:
            sys_prompt += "\n" + DEBUG_PROMPT

        pkg_snippet = ""
        if "package.json" in compressed:
            pkg = compressed["package.json"]
            if len(pkg) < 3000:
                pkg_snippet = f"\n\n--- package.json ---\n{pkg}"

        messages: List[Dict[str, Any]] = [{"role": "system", "content": sys_prompt}]

        if chat_history:
            for h in chat_history[-12:]:
                role = h.get("role", "user")
                content = h.get("content", "")
                if not content:
                    continue
                if role == "system" and len(content) > 2000:
                    content = content[:2000] + "\n[...truncated...]"
                if role == "assistant":
                    try:
                        parsed = json.loads(content)
                        if isinstance(parsed, dict) and "assistant_message" in parsed:
                            content = parsed["assistant_message"]
                        elif isinstance(parsed, dict) and "message" in parsed:
                            content = parsed["message"]
                    except Exception:
                        pass
                messages.append({"role": _norm_role(role), "content": content})

        parts = [f"PROJECT FILES (in /home/user/app):\n{tree_str}{pkg_snippet}"]
        if is_debug and error_context:
            parts.append(f"\nERROR TO FIX:\n{error_context}")
        elif user_request:
            parts.append(f"\nUSER REQUEST:\n{user_request}")
        if previous_command_output:
            parts.append(
                f"\nOUTPUT FROM YOUR LAST COMMANDS:\n"
                f"{previous_command_output[:6000]}\n"
                f'\nContinue. Set "done": true when the task is finished.'
            )
        parts.append(
            "\nOutput ONLY a JSON object with keys: message, commands (array of shell strings), done (boolean). "
            "Heredoc delimiter MUST be exactly GORILLA_EOF."
        )
        text_content = "\n".join(parts)

        use_vision = bool(image_b64)
        if use_vision:
            image_url = image_b64 if image_b64.startswith("data:") else f"data:image/jpeg;base64,{image_b64}"
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": text_content},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            })
        else:
            messages.append({"role": "user", "content": text_content})

        model_used = VISION_MODEL if use_vision else MODEL
        log_agent(
            "agent",
            f"LLM ({model_used}, debug={is_debug}, supabase={has_supabase})",
            self.project_id,
        )

        try:
            raw, tokens = await _call_llm(messages, model=model_used, temperature=0.6)
            self.total_tokens += tokens
        except Exception as e:
            log_agent("agent", f"LLM call failed: {e}", self.project_id)
            return {
                "message": f"The AI model returned an error: {str(e)[:150]}",
                "commands": [], "done": True, "tokens": 0,
            }

        parsed = _extract_json(raw)
        if not parsed:
            log_agent("agent", "Malformed JSON, retrying with nudge", self.project_id)
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": (
                    "Your last response was not valid JSON. Output ONLY a JSON "
                    "object with keys 'message', 'commands' (array), 'done' (bool). No prose."
                ),
            })
            try:
                raw2, tokens2 = await _call_llm(messages, model=MODEL, temperature=0.4)
                self.total_tokens += tokens2
                parsed = _extract_json(raw2)
            except Exception:
                pass

        if not parsed:
            return {
                "message": "I had trouble formatting my response. Please repeat the request.",
                "commands": [], "done": True, "tokens": self.total_tokens,
            }

        message = parsed.get("message") or "Working on it..."
        raw_cmds = parsed.get("commands") or []
        done = bool(parsed.get("done", True))

        safe_cmds: List[str] = []
        for cmd in raw_cmds:
            if not isinstance(cmd, str) or not cmd.strip():
                continue
            expanded = self.token_sub.expand(cmd)
            if not _is_safe_command(expanded):
                log_agent("agent", f"Blocked dangerous command: {expanded[:60]}", self.project_id)
                continue
            safe_cmds.append(expanded)

        log_agent(
            "agent",
            f"{len(safe_cmds)} commands, done={done}, tokens={self.total_tokens}",
            self.project_id,
        )
        return {
            "message": message, "commands": safe_cmds,
            "done": done, "tokens": self.total_tokens,
        }


class Agent:
    """Legacy shim."""

    def __init__(self, timeout_s: float = 120.0):
        self.timeout_s = timeout_s

    def remember(self, project_id: str, role: str, text: str) -> None:
        _append_history(project_id, role, text)


__all__ = [
    "LineageAgent", "Agent", "set_log_callback", "log_agent",
    "_render_token_limit_message", "_append_history", "_get_history",
    "clear_history", "TokenSubstitution",
]