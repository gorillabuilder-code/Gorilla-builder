"""
Lineage Agent v10 — All 10 Improvements
==========================================

Architecture: mini-SWE-agent (ThoughtAction format, linear message history,
bash execution, raw OBSERVATION feedback).

Improvements over v9:
  #1  Prompt expander — cheap LLM call turns vague prompts into detailed specs
  #2  Planner — same model writes todo.md BEFORE any code
  #3  Auto-kill ports — pkill node/vite injected before first agent turn
  #4  Narration fix — full thought passed to UI, not truncated to 1 line
  #5  Per-file specs — planner writes what each file should contain
  #6  Linter-in-the-loop — tsc --noEmit after file writes, errors fed back
  #7  Template starters — (config-driven, not in this file — agent told about them)
  #8  Silent success — "command ran successfully" when stdout is empty
  #9  History compression — old observations collapsed to 1-line summaries
  #10 Reviewer — after GORILLA_DONE, one cheap call checks for obvious mistakes
"""

from __future__ import annotations

import os
import re
import time
import hashlib
from typing import Dict, Any, List, Optional, Tuple

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL = os.getenv("LINEAGE_MODEL", "xiaomi/mimo-v2.5")
PLANNER_MODEL = os.getenv("PLANNER_MODEL", "xiaomi/mimo-v2.5-pro")
VISION_MODEL = os.getenv("VISION_MODEL", "xiaomi/mimo-v2.5")
OPENROUTER_URL = os.getenv(
    "OPENROUTER_URL",
    "https://openrouter.ai/api/v1/chat/completions",
).strip()
SITE_URL = os.getenv("SITE_URL", "https://gorillabuilder.dev").strip()
SITE_NAME = os.getenv("SITE_NAME", "Gorilla Builder")

MAX_CONTEXT_TOKENS = 256_000
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
    c = {"agent": "\033[94m", "llm": "\033[90m", "system": "\033[97m",
         "debugger": "\033[91m"}.get(role.lower(), "\033[94m")
    print(f"\033[90m{ts}\033[0m {prefix} {c}{role.upper()}\033[0m: "
          f"{message[:300]}{'...' if len(message) > 300 else ''}")
    if _external_log_callback and project_id and role.lower() != "llm":
        try: _external_log_callback(project_id, role.lower(), message)
        except Exception: pass

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
# Legacy shims (kept for app.py compatibility)
# ---------------------------------------------------------------------------
_HISTORY: Dict[str, list] = {}
HISTORY_CAP = 100

def _norm_role(r: str) -> str:
    return "user" if (r or "").strip().lower() in ("user", "you") else "assistant"

def _append_history(project_id: str, role: str, content: str) -> None:
    if not project_id or not content: return
    _HISTORY.setdefault(project_id, []).append(
        {"role": _norm_role(role), "content": content.strip()})
    if len(_HISTORY[project_id]) > HISTORY_CAP:
        _HISTORY[project_id] = _HISTORY[project_id][-HISTORY_CAP:]

def _get_history(project_id: str, max_items: int = 20) -> list:
    return list(_HISTORY.get(project_id, []))[-max_items:]

def clear_history(project_id: str) -> None:
    _HISTORY.pop(project_id, None)

# ---------------------------------------------------------------------------
# Token substitution (for images/blobs in file tree)
# ---------------------------------------------------------------------------
class TokenSubstitution:
    THRESHOLD = 500
    def __init__(self):
        self._vault: Dict[str, str] = {}
        self._reverse: Dict[str, str] = {}
        self._n = 0
    def _mk(self) -> str:
        self._n += 1; return f"__BLOB_{self._n:04d}__"
    @staticmethod
    def _is_b64(s: str) -> bool:
        if len(s) < 100: return False
        sample = s[:200].strip()
        return (sum(1 for c in sample if c.isalnum() or c in "+/=") / len(sample)) > 0.9 and "\n" not in sample[:100]
    def compress_tree(self, tree: Dict[str, str]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for path, content in tree.items():
            if content and len(content) > self.THRESHOLD:
                if (path.endswith(".b64") or self._is_b64(content)
                        or (path.endswith(".json") and len(content) > 5000)
                        or (path.endswith(".svg") and len(content) > 3000)):
                    h = hashlib.md5(content[:200].encode()).hexdigest()
                    if h in self._reverse: out[path] = self._reverse[h]
                    else:
                        pid = self._mk(); self._vault[pid] = content; self._reverse[h] = pid; out[path] = pid
                    continue
            out[path] = content
        return out
    def expand(self, text: str) -> str:
        for ph, original in self._vault.items():
            if ph in text: text = text.replace(ph, original)
        return text

# ---------------------------------------------------------------------------
# Context management (#9 — history compression)
# ---------------------------------------------------------------------------
def _estimate_tokens(messages: list) -> int:
    total = 0
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, str): total += len(c) // CHARS_PER_TOKEN
        elif isinstance(c, list):
            for item in c:
                if isinstance(item, dict):
                    if item.get("type") == "text": total += len(item.get("text", "")) // CHARS_PER_TOKEN
                    elif item.get("type") == "image_url": total += 1000
    return total


def _compress_history(messages: list, max_tokens: int = MAX_CONTEXT_TOKENS) -> list:
    """
    #9 — History compression (from SWE-agent paper).
    Keep system prompt + first user message + last 5 full turns.
    Collapse earlier observations to one-line summaries.
    """
    if _estimate_tokens(messages) <= max_tokens:
        return messages

    sys_msg = messages[0] if messages and messages[0].get("role") == "system" else None
    first_user = messages[1] if len(messages) > 1 else None

    # Keep last 10 messages (5 turns) fully intact
    keep_full = 10
    recent = messages[-keep_full:]

    # Middle messages get compressed
    middle_start = 2 if first_user else 1
    middle_end = len(messages) - keep_full
    compressed_middle = []

    for m in messages[middle_start:middle_end]:
        role = m.get("role", "")
        content = m.get("content", "")
        if isinstance(content, list):
            # Vision message — drop the image, keep text summary
            text_parts = [p.get("text", "")[:100] for p in content if p.get("type") == "text"]
            content = " ".join(text_parts)
        if role == "user" and content.startswith("OBSERVATION:"):
            # Collapse observation to first line
            first_line = content.split("\n")[1] if "\n" in content else content
            compressed_middle.append({"role": "user", "content": f"OBSERVATION: {first_line[:120]}..."})
        elif role == "assistant":
            # Collapse assistant to just the discussion (no bash block)
            lines = content.split("```")[0].strip().split("\n")
            summary = lines[0][:120] if lines else "..."
            compressed_middle.append({"role": "assistant", "content": summary})
        else:
            compressed_middle.append({"role": role, "content": content[:200]})

    result = []
    if sys_msg: result.append(sys_msg)
    if first_user and first_user not in recent: result.append(first_user)
    result.extend(compressed_middle)
    result.extend(recent)

    # If still over budget, drop the compressed middle entirely
    if _estimate_tokens(result) > max_tokens:
        result = ([sys_msg] if sys_msg else [])
        if first_user: result.append(first_user)
        result.extend(recent)

    return result


# ═══════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = r"""You are Gor://a, an autonomous software engineer. You work inside an Ubuntu sandbox. You have full control: write files, run commands, start servers, debug, verify.

## How this works

You interact with the sandbox in a loop. Each step you provide:

1. DISCUSSION — your reasoning about what to do next (always include this and just speak naturally, never say Discussion: or working on it...)
2. A fenced bash code block with the command(s) to execute

Example response:

I need to check what's in App.tsx before making changes.

```bash
cat src/App.tsx
```

I will execute your command and show you the output as an OBSERVATION. Then you respond with your next step.

When finished and both ports 8080 and 3000 return HTTP 200, write GORILLA_DONE followed by a summary for the user. Do NOT write GORILLA_DONE until you have verified the ports.

## Environment

Ubuntu 22 / Node 20 / Python 3.11 / CWD: `/home/user/app`

Binaries: node, npm, npx, git, curl, jq, unzip, tar, find, grep, sed, awk, python3

npm packages already installed: react, react-dom, react-router-dom, vite, @vitejs/plugin-react, typescript, tailwindcss, postcss, autoprefixer, clsx, tailwind-merge, class-variance-authority, @radix-ui/*, lucide-react, @supabase/supabase-js, express, cors, body-parser, dotenv, concurrently

Dev server: `npm run dev` starts Vite on :8080 (frontend) + Express on :3000 (API) via concurrently.

Env vars sourced from `.gorilla_env`: $GORILLA_API_KEY, $VITE_GORILLA_AUTH_ID, $VITE_SUPABASE_URL, $VITE_SUPABASE_ANON_KEY, $SUPABASE_MGMT_TOKEN, $SUPABASE_PROJECT_REF

Layout: src/ (React), src/components/ui/ (shadcn), src/utils/auth.ts (auth gateway — import, don't rewrite), routes/ (Express), public/generated/ (AI images)

## Rules

1. npm install before importing anything not listed above
2. Read a file before editing it
3. Wire new components (import + render) and routes (import + app.use)
4. Frontend: `@/` alias. Backend: relative with `.js` ext
5. Never touch package.json directly, vite.config.ts, .env, src/utils/auth.ts
6. Start server: `cd /home/user/app && npm run dev > /tmp/dev.log 2>&1 &`
7. Verify: `sleep 3 && curl -so /dev/null -w '%{http_code}' http://localhost:8080 && curl -so /dev/null -w '%{http_code}' http://localhost:3000`
8. You MUST verify both :8080 and :3000 return 200 before writing GORILLA_DONE
9. If something fails, read /tmp/dev.log, fix the issue, restart, verify again
10. Create ONE file per step. Do NOT write multiple files in a single bash block.
11. NEVER say GORILLA_DONE before turn 5. You have not built enough yet!

## Auth gateway
```tsx
import { login, logout, onAuthStateChanged } from '@/utils/auth';
useEffect(() => onAuthStateChanged(setUser), []);
<button onClick={() => login('google')}>Sign in</button>
```

## AI proxy (backend, $GORILLA_API_KEY)
Base: {GORILLA_PROXY}
- LLM: POST {GORILLA_PROXY}/api/v1/chat/completions (don't send model)
- Images: POST {GORILLA_PROXY}/api/v1/images/generations (use this for the users app (don't send model)) if you want to generate image for users app do curl → save to public/generated/
- STT: POST {GORILLA_PROXY}/api/v1/audio/transcriptions
- BG removal: POST {GORILLA_PROXY}/api/v1/images/remove-background
- TTS: use window.speechSynthesis
"""

SUPABASE_ADDON = r"""
## Supabase is active
Client: `import { createClient } from '@supabase/supabase-js'; const supabase = createClient(import.meta.env.VITE_SUPABASE_URL, import.meta.env.VITE_SUPABASE_ANON_KEY);`
Migrations via bash:
```
cat > /tmp/migration.sql << 'SQL'
CREATE TABLE IF NOT EXISTS items (...);
ALTER TABLE items ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own" ON items USING (auth.uid() = user_id);
SQL
curl -sS -X POST "https://api.supabase.com/v1/projects/$SUPABASE_PROJECT_REF/database/query" \
  -H "Authorization: Bearer $SUPABASE_MGMT_TOKEN" -H "Content-Type: application/json" \
  -d "$(cat /tmp/migration.sql | jq -Rs '{query: .}')"
```
"""

DEBUG_ADDON = r"""
## Debug mode — fix the error, nothing else
Read the error. Find the file. Make the smallest fix. Restart server. Verify ports. GORILLA_DONE.
"""


# ═══════════════════════════════════════════════════════════════════════════
#  #1 — Prompt Expander
# ═══════════════════════════════════════════════════════════════════════════
 
EXPANDER_SYSTEM = """You are a product designer for Gorilla Builder — a platform that builds REAL working SaaS apps, not just landing pages.
 
The platform has these built-in capabilities that the developer can use:
- **Auth gateway** — Google & GitHub login, zero setup, returns {id, email, name, avatar}
- **AI proxy** — LLM chat completions, image generation, speech-to-text, background removal
- **Supabase** — Postgres database with row-level security, auth, realtime subscriptions
- **Image generation** — generate images via API, save to public/generated/, use in the app
- **Express backend** — full API server on :3000, can store data, proxy AI calls, handle webhooks
 
The user will give you a short app idea. Expand it into a detailed product spec (200-350 words) that a developer can build.
 
Think about what makes this a FUNCTIONAL APP, not a brochure:
- What does the user DO after landing? (create, browse, interact, generate, save, share)
- What data is stored? (user profiles, posts, items, generated content, preferences)
- What AI features make it special? (chat, image generation, content creation, analysis)
- Does it need auth? (if users save anything, YES)
 
Include:
- App name (creative, memorable)
- Color scheme (specific hex codes, dark mode first)
- Typography (Google Font — NEVER Inter)
- Pages: at least 3 (landing/home, main app/dashboard, settings/profile)
- For each page: what the user sees AND what they can DO
- Backend: what API routes are needed, what data is stored
- AI features: how the AI proxy is used (chat, image gen, content creation, etc.)
- Auth: whether login is needed and which providers
 
Examples of GOOD specs (functional SaaS):
- "AI recipe generator" → landing page, generate page (enter ingredients → AI creates recipe + generates food image), saved recipes dashboard, auth via Google
- "Mood journal" → landing, journal entry page (write + AI sentiment analysis + generates abstract art for mood), history dashboard with mood chart, Supabase for storage
- "Portfolio builder" → landing, editor page (add projects, AI writes descriptions, generates hero images), public portfolio view, auth + Supabase
 
Examples of BAD specs (brochure sites):
- Hero section, about section, features section, contact form, footer ← this is a template, not an app
 
Output ONLY the spec. No preamble."""
 
 
async def expand_prompt(short_prompt: str) -> str:
    """
    #1 — Turn "coffee shop site" into a 200-word detailed spec.
    Uses the same model — one cheap call, no extra cost.
    """
    if len(short_prompt) > 300:
        # Already detailed enough, skip expansion
        return short_prompt
 
    messages = [
        {"role": "system", "content": EXPANDER_SYSTEM},
        {"role": "user", "content": short_prompt},
    ]
    try:
        raw, _ = await _call_llm(messages, model=PLANNER_MODEL, temperature=0.8)
        expanded = raw.strip()
        if len(expanded) > len(short_prompt) * 2:
            log_agent("agent", f"Expanded prompt: {expanded[:150]}...")
            return expanded
        return short_prompt
    except Exception as e:
        log_agent("agent", f"Expander failed ({e}), using original prompt")
        return short_prompt
 
 
# ═══════════════════════════════════════════════════════════════════════════
#  #2 + #5 — Planner (todo.md with per-file specs)
# ═══════════════════════════════════════════════════════════════════════════
 
PLANNER_SYSTEM = """You are a project planner for Gorilla Builder — a platform that builds working SaaS apps with React + Express + Supabase.
 
## Platform capabilities (use these in your plans!)
 
**Auth gateway** — zero setup:
```tsx
import { login, logout, onAuthStateChanged } from '@/utils/auth';
// Providers: 'google' | 'github' — returns {id, email, name, avatar}
```
 
**AI proxy** (backend, uses $GORILLA_API_KEY):
- LLM chat: POST /api/v1/chat/completions (don't send model)
- Image gen: POST /api/v1/images/generations → save to public/generated/
- STT: POST /api/v1/audio/transcriptions
- BG removal: POST /api/v1/images/remove-background
 
**Image generation via curl** (use in bash steps):
```bash
curl -sS -X POST "$GORILLA_PROXY/api/v1/images/generations" \
  -H "Authorization: Bearer $GORILLA_API_KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"your prompt here","samples":1}' \
  | jq -r '.[0].base64 // .data[0].b64_json' | base64 -d > public/generated/image.jpg
```
 
**Supabase** (if linked): createClient, migrations via Management API, RLS policies
 
**Express backend**: routes/ folder, mounted in server.js, full API capability
 
## Your job
 
Given a spec, output a markdown checklist. Each item = ONE action (one file, one install, one migration, one image generation).
 
Think about the FULL STACK:
- Frontend pages (3+ pages minimum: landing, main app, settings/profile/dashboard)
- Backend API routes (if the app stores/processes data)
- Database tables (if Supabase is active)
- AI features (image generation, chat endpoints, content creation)
- Auth integration (if users save data)
- Generated images (hero images, placeholders, AI-generated content)
 
Format:
```
# Task: <short title>
 
- [ ] View existing files (App.tsx, server.js, index.css) to understand structure
- [ ] Generate hero image — curl AI proxy with prompt "dark moody coffee beans close-up"
- [ ] Create src/components/Navbar.tsx — fixed dark navbar, brand left, auth button right
- [ ] Create src/pages/Dashboard.tsx — user's saved items grid, create button, AI generate button
- [ ] Create routes/api.js — POST /api/generate (calls AI proxy), GET /api/items (reads Supabase)
- [ ] Update server.js — mount routes/api.js at /api
- [ ] Run Supabase migration — create items table with user_id FK, RLS policies
- [ ] Update src/App.tsx — add routes for all pages, wrap with auth check
- [ ] Update src/index.css — dark theme, custom fonts
- [ ] Install framer-motion
- [ ] Start dev server and verify both ports return 200
```
 
Rules:
- DO NOT COPY THE EXACT CHECKLIST THAT IS GIVEN EXPAND IT OR CHANGE IT ACCORDING TO THE TASK
- First item: "View existing files"
- Last item: "Start dev server and verify both ports return 200"
- ONE action per item. Never combine files.
- 8-20 items. Plan the WHOLE app, not just the landing page.
- Include image generation steps where the app needs visuals.
- Include backend routes if the app has any interactivity.
- Include Supabase migrations if data persistence is needed.
- Don't overspecify component internals — the developer knows React.
- Output ONLY the checklist."""
 
 
async def generate_plan(expanded_prompt: str, file_tree_summary: str) -> Optional[str]:
    """
    #2 + #5 — Generate a todo.md checklist with per-file specs.
    Uses the same model. Returns the raw markdown or None on failure.
    """
    messages = [
        {"role": "system", "content": PLANNER_SYSTEM},
        {"role": "user", "content": f"Existing files:\n{file_tree_summary}\n\nSpec:\n{expanded_prompt}"},
    ]
    try:
        raw, _ = await _call_llm(messages, model=PLANNER_MODEL, temperature=0.4)
        plan = raw.strip()
        # Validate it looks like a checklist
        if "- [ ]" in plan:
            log_agent("agent", f"Plan generated: {plan[:200]}...")
            return plan
        return None
    except Exception as e:
        log_agent("agent", f"Planner failed ({e}), agent will plan itself")
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  #10 — Reviewer
# ═══════════════════════════════════════════════════════════════════════════

REVIEWER_SYSTEM = """You are a code reviewer. The developer just finished building a web app. Review the file listing and recent build output for obvious mistakes.

Check for:
- Components created but not imported/rendered anywhere
- Missing npm installs (imports without corresponding package)
- Broken routes (Express routes not mounted)
- CSS files not imported
- TypeScript errors visible in the logs

If everything looks correct, respond with just: LGTM

If there are issues, respond with a numbered list of fixes needed (max 3), each one specific and actionable. No preamble."""


async def review_output(file_tree_summary: str, last_output: str) -> Optional[str]:
    """
    #10 — Quick review after agent says done. Returns fix instructions or None if LGTM.
    """
    messages = [
        {"role": "system", "content": REVIEWER_SYSTEM},
        {"role": "user", "content": f"Files:\n{file_tree_summary}\n\nRecent output:\n{last_output[:3000]}"},
    ]
    try:
        raw, _ = await _call_llm(messages, model=MODEL, temperature=0.2)
        review = raw.strip()
        if "LGTM" in review.upper():
            log_agent("agent", "Reviewer: LGTM")
            return None
        log_agent("agent", f"Reviewer found issues: {review[:200]}")
        return review
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  Response parser — ThoughtAction format
# ═══════════════════════════════════════════════════════════════════════════

def _parse_response(raw: str) -> Dict[str, Any]:
    """Parse discussion + ```bash``` + GORILLA_DONE."""
    thought = ""
    bash_block = ""
    done = False
    message = ""

    if not raw:
        return

    # Check for GORILLA_DONE
    if "GORILLA_DONE" in raw:
        done = True
        parts = raw.split("GORILLA_DONE", 1)
        thought = parts[0].strip()
        message = parts[1].strip() if len(parts) > 1 else ""
        m = re.search(r"```(?:bash|sh|shell)?\n(.*?)```", parts[0], re.DOTALL)
        if m:
            bash_block = m.group(1).strip()
            thought = parts[0][:m.start()].strip()
        if not message and thought:
            message = thought.split("\n")[0][:300]
        return {"thought": thought, "bash": bash_block, "done": done, "message": message or "Done."}

    # Extract fenced bash block
    m = re.search(r"```(?:bash|sh|shell)?\n(.*?)```", raw, re.DOTALL)
    if m:
        bash_block = m.group(1).strip()
        thought = raw[:m.start()].strip()
    else:
        thought = raw.strip()

    # #4 — Narration fix: pass FULL thought to UI, not just first line
    # Take up to first 3 sentences or 300 chars, whichever is shorter
    if thought:
        sentences = re.split(r'(?<=[.!?])\s+', thought)
        message = " ".join(sentences[:3])[:300]

    return {
        "thought": thought,
        "bash": bash_block,
        "done": done,
        "message": message or "Working on it.",
    }


# ═══════════════════════════════════════════════════════════════════════════
#  Shell safety
# ═══════════════════════════════════════════════════════════════════════════

_DANGEROUS = [
    r"\brm\s+-rf\s+/($|\s)", r"\bsudo\b", r"\bshutdown\b", r"\breboot\b",
    r">\s*/dev/(sda|nvme|hda)", r"\bmkfs\b", r":\(\)\s*{\s*:\|:",
    r"\bdd\s+if=.*\s+of=/dev/",
]

def _is_safe(cmd: str) -> bool:
    return not any(re.search(p, cmd, re.IGNORECASE) for p in _DANGEROUS)


# ═══════════════════════════════════════════════════════════════════════════
#  LLM call
# ═══════════════════════════════════════════════════════════════════════════

async def _call_llm(messages: list, model: str = MODEL, temperature: float = 0.6) -> Tuple[str, int]:
    messages = _compress_history(messages)
    payload = {
        "model": model, "messages": messages,
        "temperature": temperature, "max_tokens": 16000,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": SITE_URL, "X-Title": SITE_NAME,
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    content = data["choices"][0]["message"]["content"]
    u = data.get("usage", {})
    p = u.get("prompt_tokens", 0)
    c = u.get("completion_tokens", 0)
    # Weighted cost: frontier models cost more per token
    is_frontier = any(x in model for x in ["claude", "gpt-4", "gemini"])
    weight = (p * 0.6 + c * 2.4) if is_frontier else (p * 0.4 + c * 1.95)
    return content, int(weight)


# ═══════════════════════════════════════════════════════════════════════════
#  LineageAgent
# ═══════════════════════════════════════════════════════════════════════════

class LineageAgent:
    """
    mini-SWE-agent with planner pipeline.
    Linear message history. Persisted on SandboxSession.agent.
    """

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.total_tokens = 0
        self.token_sub = TokenSubstitution()
        self.messages: List[Dict[str, Any]] = []
        self._system_prompt_set = False
        self._plan_injected = False
        self._prompt_expanded = False

    def _ensure_system_prompt(self, gorilla_proxy_url: str, has_supabase: bool, is_debug: bool):
        if self._system_prompt_set:
            return
        prompt = SYSTEM_PROMPT.replace("{GORILLA_PROXY}", gorilla_proxy_url or "https://your-proxy.ngrok-free.dev")
        if has_supabase:
            prompt += "\n" + SUPABASE_ADDON
        if is_debug:
            prompt += "\n" + DEBUG_ADDON
        self.messages = [{"role": "system", "content": prompt}]
        self._system_prompt_set = True

    async def run(
        self, user_request: str, file_tree: Dict[str, str],
        chat_history: Optional[list] = None, gorilla_proxy_url: str = "",
        has_supabase: bool = False, is_debug: bool = False,
        error_context: str = "", image_b64: Optional[str] = None,
        previous_command_output: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_system_prompt(gorilla_proxy_url, has_supabase, is_debug)

        compressed = self.token_sub.compress_tree(file_tree)
        clean_paths = sorted(p for p in compressed if not p.endswith(".b64"))
        tree_str = "\n".join(f"  {p}" for p in clean_paths)

        pkg_snippet = ""
        if "package.json" in compressed:
            pkg = compressed["package.json"]
            if len(pkg) < 3000:
                pkg_snippet = f"\n\npackage.json:\n{pkg}"

        if previous_command_output:
            user_content = f"OBSERVATION:\n{previous_command_output[:8000]}"
            self.messages.append({"role": "user", "content": user_content})
        else:
            # ── First turn: expand prompt + generate plan ──────────
            effective_request = user_request

            # #1 — Prompt expander
            if not is_debug and not self._prompt_expanded and user_request:
                effective_request = await expand_prompt(user_request)
                self._prompt_expanded = True

            # #2 + #5 — Generate plan with per-file specs
            plan_text = ""
            if not is_debug and not self._plan_injected and file_tree:
                plan = await generate_plan(effective_request, tree_str)
                if plan:
                    plan_text = f"\n\nHere is your plan — follow it step by step, one file per bash block:\n{plan}"
                    self._plan_injected = True

            parts = [f"Project files in /home/user/app:\n{tree_str}{pkg_snippet}"]

            if is_debug and error_context:
                parts.append(f"\nError to fix:\n{error_context}")
            elif effective_request:
                parts.append(f"\nTask:\n{effective_request}{plan_text}")

            if chat_history:
                recent = chat_history[-6:]
                history_text = "\n".join(
                    f"{m.get('role','user').upper()}: {m.get('content','')[:200]}"
                    for m in recent if m.get('content')
                )
                if history_text:
                    parts.append(f"\nRecent conversation:\n{history_text}")

            user_content = "\n".join(parts)

            if image_b64:
                img_url = image_b64 if image_b64.startswith("data:") else f"data:image/jpeg;base64,{image_b64}"
                self.messages.append({"role": "user", "content": [
                    {"type": "text", "text": user_content},
                    {"type": "image_url", "image_url": {"url": img_url}},
                ]})
            else:
                self.messages.append({"role": "user", "content": user_content})

        # Call the LLM
        model = VISION_MODEL if (image_b64 and not previous_command_output) else MODEL
        log_agent("agent", f"v10 ({model}, turns={len(self.messages)//2})", self.project_id)

        try:
            raw, tokens = await _call_llm(self.messages, model=model, temperature=0.6)
            self.total_tokens += tokens
        except Exception as e:
            log_agent("agent", f"LLM error: {e}", self.project_id)
            return {"message": f"AI error: {str(e)[:150]}", "commands": [], "done": True, "tokens": 0}

        # Append raw assistant response to message thread
        self.messages.append({"role": "assistant", "content": raw})

        # Parse
        parsed = _parse_response(raw)

        if parsed["thought"]:
            log_agent("agent", f"THOUGHT: {parsed['thought'][:300]}", self.project_id)

        # Extract commands
        commands: List[str] = []
        if parsed["bash"]:
            bash_content = self.token_sub.expand(parsed["bash"])
            if _is_safe(bash_content):
                commands = [bash_content]
            else:
                log_agent("agent", "Blocked dangerous command", self.project_id)

        done = parsed["done"]

        log_agent(
            "agent",
            f"{'DONE' if done else f'{len(commands)} cmd block'}, tok={self.total_tokens}",
            self.project_id,
        )

        return {
            "message": parsed["message"],
            "commands": commands,
            "done": done,
            "tokens": self.total_tokens,
        }


# Legacy shim
class Agent:
    def __init__(self, timeout_s: float = 120.0): self.timeout_s = timeout_s
    def remember(self, project_id: str, role: str, text: str) -> None: _append_history(project_id, role, text)


__all__ = [
    "LineageAgent", "Agent", "set_log_callback", "log_agent",
    "_render_token_limit_message", "_append_history", "_get_history",
    "clear_history", "TokenSubstitution",
    "expand_prompt", "generate_plan", "review_output",
]