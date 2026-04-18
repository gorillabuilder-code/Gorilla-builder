"""
Lineage Agent v8 — Full Memory, Raw History, No Lobotomy
==========================================================

Bugs fixed:

  1. Assistant history lobotomy: run() was stripping every assistant turn
     down to just the "message" field ("Working on it..."), so the model
     had no memory of what it planned or did. Fix: assistant turns are
     passed through RAW. The model sees its own JSON including plan,
     calls, and done flag.

  2. No session memory within a request: the agent had no internal turn
     history. Each run() call rebuilt messages from scratch using only
     external chat_history. Fix: LineageAgent now accumulates its own
     _session_turns list. Every run() call appends the user message and
     the raw LLM response as assistant. On subsequent calls within the
     same request, the model sees its full conversation so far.

  3. _get_history() never called: dead code. Fix: removed the pretense.
     _append_history and _get_history kept only for legacy Agent shim.

The agent is fully autonomous. It runs npm run dev, checks ports, fixes
errors, and only says done when both :8080 and :3000 respond HTTP 200.
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
MODEL = os.getenv("LINEAGE_MODEL", "z-ai/glm-5.1")
VISION_MODEL = os.getenv("VISION_MODEL", "z-ai/glm-5.1")
OPENROUTER_URL = os.getenv(
    "OPENROUTER_URL",
    "https://openrouter.ai/api/v1/chat/completions",
).strip()
SITE_URL = os.getenv("SITE_URL", "https://gorillabuilder.dev").strip()
SITE_NAME = os.getenv("SITE_NAME", "Gorilla Builder")

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
    colors = {"agent": "\033[94m", "llm": "\033[90m", "system": "\033[97m",
              "debugger": "\033[91m"}
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
# History (legacy shim only — LineageAgent uses its own _session_turns)
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
# Token substitution
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

def _shorten(messages: list, max_tokens: int = MAX_CONTEXT_TOKENS) -> list:
    if _estimate_tokens(messages) <= max_tokens: return messages
    sys_msg = messages[0] if messages and messages[0].get("role") == "system" else None
    return ([sys_msg] if sys_msg else []) + messages[-6:]


# ═══════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT v8
# ═══════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT_BASE = r"""You are Gorilla — an autonomous full-stack engineer inside a live Ubuntu sandbox.
You have FULL CONTROL. You write code, install packages, start servers, debug errors, and verify the app works. Nobody does anything for you. The user's prompt goes in, a working app comes out.

## ENVIRONMENT

Ubuntu 22, Node 20, Python 3.11. CWD: `/home/user/app`

**Binaries:** node, npm, npx, git, curl, jq, unzip, tar, find, grep, sed, awk, python3
**npm deps already installed:** react, react-dom, react-router-dom, vite, @vitejs/plugin-react, typescript, tailwindcss, postcss, autoprefixer, clsx, tailwind-merge, class-variance-authority, @radix-ui/* (shadcn), lucide-react, @supabase/supabase-js, express, cors, body-parser, dotenv, concurrently

**Dev server:** `npm run dev` starts Vite on `:8080` (frontend) and Express on `:3000` (API) via concurrently.

**Env vars** (in `.gorilla_env`, sourced automatically):
`$GORILLA_API_KEY`, `$VITE_GORILLA_AUTH_ID`, `$VITE_SUPABASE_URL`, `$VITE_SUPABASE_ANON_KEY`, `$SUPABASE_MGMT_TOKEN`, `$SUPABASE_PROJECT_REF`

**Layout:** `src/` (React), `src/components/ui/` (shadcn), `src/utils/auth.ts` (auth gateway — import, don't rewrite), `routes/` (Express), `public/generated/` (AI images), `.gorilla/` (scratch)

## RESPONSE FORMAT

```json
{"plan": "...", "message": "...", "calls": [...], "done": false}
```

`done: false` → you need to see output. `done: true` → app verified at :8080 and :3000.

## TOOLS

| Tool | Args | Purpose |
|------|------|---------|
| `view` | `path` | Read file. Do this before str_replace. |
| `create_file` | `path`, `content` | New file or full rewrite. |
| `str_replace` | `path`, `old`, `new` | Surgical edit. `old` must be unique. |
| `todo_update` | `mark_done` / `add` | Manage .gorilla/todo.md checklist. |
| `bash` | `cmd` | Anything: npm install, npm run dev, curl, kill, tail logs. |
| `search` | `pattern`, `glob` | Grep before creating something that might exist. |
| `delete` | `path` | Remove file. |

## WORKFLOW

1. **Plan** — Create `.gorilla/todo.md` with checkboxes. Last item: "verify app runs."
2. **Explore** — `view` files you'll edit. `done: false`.
3. **Build** — Write code, install deps, wire everything. Tick todo items.
4. **Start** — `bash: cd /home/user/app && npm run dev > /tmp/dev.log 2>&1 &` then `done: false`.
5. **Verify** — `bash: sleep 4 && curl -s -o /dev/null -w '%{http_code}' http://localhost:8080 && curl -s -o /dev/null -w '%{http_code}' http://localhost:3000`
   Both 200 → `done: true`. Otherwise → read `/tmp/dev.log`, fix, restart, verify again.

**You cannot say `done: true` until both ports return HTTP 200.**

## RULES

1. `npm install <pkg>` before any new import.
2. `view` before `str_replace`.
3. Wire everything: new component → import + render. New route → mount in server.js.
4. Frontend: `@/components/ui/button`. Backend: `./routes/api.js` (with `.js`).
5. Never touch: `package.json` directly, `vite.config.ts`, `.env`, `src/utils/auth.ts`.
6. No Inter font. Dark defaults: `bg-zinc-950 text-zinc-100`.

## AUTH GATEWAY

```tsx
import { login, logout, onAuthStateChanged } from '@/utils/auth';
useEffect(() => onAuthStateChanged(setUser), []);
<button onClick={() => login('google')}>Sign in</button>
// user = { id, email, name, avatar, provider }
```

## AI PROXY — backend only, `$GORILLA_API_KEY`

Base: `{GORILLA_PROXY}`

**LLM** (don't send model/temperature):
```js
fetch(`{GORILLA_PROXY}/api/v1/chat/completions`, {
  method: 'POST',
  headers: { Authorization: `Bearer ${process.env.GORILLA_API_KEY}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({ messages: [{ role: 'user', content: prompt }] })
})
```

**Images** → `public/generated/`:
```bash
curl -sS -X POST {GORILLA_PROXY}/api/v1/images/generations \
  -H "Authorization: Bearer $GORILLA_API_KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"sleek dark geometric","samples":1}' \
  | jq -r '.[0].base64 // .data[0].b64_json' | base64 -d > public/generated/hero.jpg
```

**STT:** `POST {GORILLA_PROXY}/api/v1/audio/transcriptions` (FormData with `file`)
**BG removal:** `POST {GORILLA_PROXY}/api/v1/images/remove-background` (FormData → PNG blob)
**TTS:** `window.speechSynthesis` (browser-side)
"""


SUPABASE_PROMPT = r"""
## SUPABASE IS ACTIVE

**Client:** `import { createClient } from '@supabase/supabase-js'; export const supabase = createClient(import.meta.env.VITE_SUPABASE_URL, import.meta.env.VITE_SUPABASE_ANON_KEY);`

**Migrations** (bash tool):
```bash
mkdir -p migrations && cat > migrations/001.sql << 'MIG'
CREATE TABLE IF NOT EXISTS items (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), user_id UUID REFERENCES auth.users ON DELETE CASCADE, title TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW());
ALTER TABLE items ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own" ON items USING (auth.uid() = user_id);
MIG
curl -sS -X POST "https://api.supabase.com/v1/projects/$SUPABASE_PROJECT_REF/database/query" \
  -H "Authorization: Bearer $SUPABASE_MGMT_TOKEN" -H "Content-Type: application/json" \
  -d "$(cat migrations/001.sql | jq -Rs '{query: .}')"
```
If response has `"message":` → failed. Fix SQL, retry. Always RLS + explicit policies.
"""

DEBUG_PROMPT = r"""
## DEBUG MODE — surgical fix only
Read the error. Find the ONE file. Smallest `str_replace`. No features, no refactoring.
Missing import → `npm install`. Unsure → `view` with `done: false`.
After fixing: restart dev server, verify ports, then `done: true`.
"""


# ═══════════════════════════════════════════════════════════════════════════
#  Tool translator
# ═══════════════════════════════════════════════════════════════════════════

_HEREDOC_DELIM = "GORILLA_EOF"
_TODO_PATH = ".gorilla/todo.md"

def _bash_escape_heredoc_content(content: str) -> str:
    return content.replace(_HEREDOC_DELIM, "GORILLA__EOF")

def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"

def _str_replace_shell(path: str, old: str, new: str) -> str:
    script = (
        "import sys\n"
        f"p = {path!r}\n"
        "try:\n"
        "    with open(p, 'r', encoding='utf-8') as f: s = f.read()\n"
        "except FileNotFoundError:\n"
        "    sys.stderr.write(f'str_replace ERROR: file not found: {p}\\n'); sys.exit(4)\n"
        f"old = {old!r}\n"
        f"new = {new!r}\n"
        "count = s.count(old)\n"
        "if count == 0:\n"
        "    sys.stderr.write(f'str_replace ERROR: old string not found in {p}\\n'); sys.exit(2)\n"
        "if count > 1:\n"
        "    sys.stderr.write(f'str_replace ERROR: old appears {count}x in {p} — must be unique\\n'); sys.exit(3)\n"
        "with open(p, 'w', encoding='utf-8') as f: f.write(s.replace(old, new))\n"
        "print(f'str_replace OK: {p}')\n"
    )
    return f"python3 << '{_HEREDOC_DELIM}'\n{_bash_escape_heredoc_content(script)}\n{_HEREDOC_DELIM}"

def _todo_update_shell(mark_done: Optional[str], add: Optional[str]) -> Optional[str]:
    if not mark_done and not add: return None
    parts = [
        "import os, sys",
        f"p = {_TODO_PATH!r}",
        "os.makedirs(os.path.dirname(p), exist_ok=True)",
        "if not os.path.exists(p):",
        "    with open(p, 'w') as f: f.write('# Task\\n\\n## Plan\\n\\n## Notes\\n')",
        "with open(p, 'r') as f: s = f.read()",
    ]
    if mark_done:
        parts.append(f"target = {mark_done!r}")
        parts.append(
            "replaced = False\n"
            "for pfx in ['- [ ] ', '* [ ] ']:\n"
            "    if pfx + target in s:\n"
            "        s = s.replace(pfx + target, pfx.replace('[ ]','[x]') + target, 1); replaced = True; break\n"
            "if not replaced:\n"
            "    for i, ln in enumerate(s.splitlines()):\n"
            "        if '[ ]' in ln and target.lower()[:30] in ln.lower():\n"
            "            lines = s.splitlines(); lines[i] = ln.replace('[ ]','[x]',1); s = '\\n'.join(lines); replaced = True; break\n"
            "if not replaced: sys.stderr.write(f'todo WARN: not found: {target[:60]}\\n')\n"
        )
    if add:
        parts.append(f"to_add = {add!r}")
        parts.append(
            "item = f'- [ ] {to_add}\\n'\n"
            "if '## Plan' in s:\n"
            "    idx = s.find('## Plan'); nl = s.find('\\n', idx)\n"
            "    end = s.find('\\n## ', nl+1)\n"
            "    if end == -1: end = len(s)\n"
            "    s = s[:end].rstrip() + '\\n' + item + s[end:]\n"
            "else: s = s.rstrip() + '\\n' + item\n"
        )
    parts.append("with open(p, 'w') as f: f.write(s)")
    parts.append("print('todo.md updated')")
    script = "\n".join(parts) + "\n"
    return f"python3 << '{_HEREDOC_DELIM}'\n{_bash_escape_heredoc_content(script)}\n{_HEREDOC_DELIM}"

def _tool_call_to_shell(call: Dict[str, Any]) -> Optional[str]:
    if not isinstance(call, dict): return None
    tool = (call.get("tool") or "").lower().strip()
    if tool == "bash": return (call.get("cmd") or "").strip() or None
    if tool == "view":
        p = (call.get("path") or "").strip(); return f"cat {_sh_quote(p)}" if p else None
    if tool == "create_file":
        p = (call.get("path") or "").strip(); c = call.get("content") or ""
        if not p: return None
        d = "/".join(p.split("/")[:-1]); mk = f"mkdir -p {_sh_quote(d)} && " if d else ""
        return f"{mk}cat > {_sh_quote(p)} << '{_HEREDOC_DELIM}'\n{_bash_escape_heredoc_content(c)}\n{_HEREDOC_DELIM}"
    if tool == "str_replace":
        p = (call.get("path") or "").strip(); o = call.get("old") or ""; n = call.get("new") or ""
        return _str_replace_shell(p, o, n) if (p and o) else None
    if tool == "delete":
        p = (call.get("path") or "").strip(); return f"rm -f {_sh_quote(p)}" if p else None
    if tool == "search":
        pat = (call.get("pattern") or "").strip(); g = (call.get("glob") or "").strip()
        if not pat: return None
        args = ["grep", "-RIn", "--color=never"]
        if g: args += [f"--include={g.split('/')[-1] if '/' in g else g}"]
        args += [_sh_quote(pat), "."]; return " ".join(args) + " | head -n 200"
    if tool == "todo_update": return _todo_update_shell(call.get("mark_done"), call.get("add"))
    if tool == "finish": return None
    return None

def _translate_calls(calls: List[Dict[str, Any]]) -> Tuple[List[str], bool]:
    cmds: List[str] = []; finish = False
    for c in calls:
        if isinstance(c, dict) and (c.get("tool") or "").lower() == "finish": finish = True; continue
        cmd = _tool_call_to_shell(c)
        if cmd: cmds.append(cmd)
    return cmds, finish


# ═══════════════════════════════════════════════════════════════════════════
#  LLM call
# ═══════════════════════════════════════════════════════════════════════════

async def _call_llm(messages: list, model: str = MODEL, temperature: float = 0.6) -> Tuple[str, int]:
    messages = _shorten(messages)
    payload: Dict[str, Any] = {
        "model": model, "messages": messages, "temperature": temperature, "max_tokens": 16000,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json",
        "HTTP-Referer": SITE_URL, "X-Title": SITE_NAME,
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    content = data["choices"][0]["message"]["content"]
    u = data.get("usage", {}); p = u.get("prompt_tokens", 0); c = u.get("completion_tokens", 0)
    return content, int(p * 0.445 + c * 2.2)


# ═══════════════════════════════════════════════════════════════════════════
#  JSON extraction
# ═══════════════════════════════════════════════════════════════════════════

def _extract_json(text: str) -> Optional[Dict]:
    text = text.strip()
    for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)\s*```", text):
        try: return json.loads(m.group(1).strip())
        except json.JSONDecodeError: pass
    best = None; best_len = 0
    for i, ch in enumerate(text):
        if ch != "{": continue
        depth = 0; in_str = False; esc = False
        for j in range(i, len(text)):
            c = text[j]
            if esc: esc = False; continue
            if c == "\\" and in_str: esc = True; continue
            if c == '"': in_str = not in_str; continue
            if in_str: continue
            if c == "{": depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    cand = text[i:j+1]
                    if len(cand) > best_len:
                        try: parsed = json.loads(cand); best = parsed; best_len = len(cand)
                        except json.JSONDecodeError:
                            fixed = re.sub(r",\s*([}\]])", r"\1", cand)
                            fixed = fixed.replace("True","true").replace("False","false").replace("None","null")
                            try: parsed = json.loads(fixed); best = parsed; best_len = len(cand)
                            except json.JSONDecodeError: pass
                    break
    return best

# ═══════════════════════════════════════════════════════════════════════════
#  Safety
# ═══════════════════════════════════════════════════════════════════════════

_DANGEROUS = [
    r"\brm\s+-rf\s+/($|\s)", r"\bsudo\b", r"\bshutdown\b", r"\breboot\b",
    r">\s*/dev/(sda|nvme|hda)", r"\bmkfs\b", r":\(\)\s*{\s*:\|:",
    r"\bdd\s+if=.*\s+of=/dev/",
]

def _is_safe_command(cmd: str) -> bool:
    return not any(re.search(p, cmd, re.IGNORECASE) for p in _DANGEROUS)


# ═══════════════════════════════════════════════════════════════════════════
#  LineageAgent v8 — full session memory
# ═══════════════════════════════════════════════════════════════════════════

class LineageAgent:
    """
    Persisted on SandboxSession.agent across user messages.
    Maintains _session_turns: every user prompt + raw assistant JSON
    response within the current multi-turn request, so the model sees
    its own full history — plan, calls, and all — without lobotomy.
    """
    def __init__(self, project_id: str):
        self.project_id = project_id
        self.total_tokens = 0
        self.token_sub = TokenSubstitution()
        # Accumulated turns within the current multi-turn agent loop.
        # Each entry: {"role": "user"|"assistant", "content": str}
        # User content = the constructed prompt with file tree + tool output.
        # Assistant content = the RAW LLM response (full JSON, not stripped).
        self._session_turns: List[Dict[str, str]] = []

    def reset_session(self) -> None:
        """Called by the orchestrator at the start of a NEW user request
        so that stale session turns from the previous request don't leak."""
        self._session_turns = []

    async def run(
        self, user_request: str, file_tree: Dict[str, str],
        chat_history: Optional[list] = None, gorilla_proxy_url: str = "",
        has_supabase: bool = False, is_debug: bool = False,
        error_context: str = "", image_b64: Optional[str] = None,
        previous_command_output: Optional[str] = None,
    ) -> Dict[str, Any]:
        compressed = self.token_sub.compress_tree(file_tree)
        clean_paths = sorted(p for p in compressed if not p.endswith(".b64"))
        tree_str = "\n".join(f"  {p}" for p in clean_paths)

        sys_prompt = SYSTEM_PROMPT_BASE.replace(
            "{GORILLA_PROXY}", gorilla_proxy_url or "https://your-proxy.ngrok-free.dev")
        if has_supabase: sys_prompt += "\n" + SUPABASE_PROMPT
        if is_debug: sys_prompt += "\n" + DEBUG_PROMPT

        # Inject todo.md content
        todo_snippet = ""
        if _TODO_PATH in compressed:
            todo = compressed[_TODO_PATH] or ""
            if todo.strip(): todo_snippet = f"\n\n--- {_TODO_PATH} ---\n{todo[:2000]}"

        pkg_snippet = ""
        if "package.json" in compressed:
            pkg = compressed["package.json"]
            if len(pkg) < 3000: pkg_snippet = f"\n\n--- package.json ---\n{pkg}"

        # ── Build the messages array ────────────────────────────────────
        messages: List[Dict[str, Any]] = [{"role": "system", "content": sys_prompt}]

        # 1. Inject external chat_history (DB-persisted user/assistant turns
        #    from previous requests). Pass through RAW — no stripping.
        if chat_history:
            for h in chat_history[-10:]:
                role = h.get("role", "user")
                content = h.get("content", "")
                if not content: continue
                # Cap system messages to avoid bloat from Figma data etc.
                if role == "system" and len(content) > 2000:
                    content = content[:2000] + "\n[truncated]"
                # BUG 1 FIX: Do NOT strip assistant messages down to "message".
                # Pass them through raw so the model sees its own prior JSON
                # (plan, calls, done flag) and knows what it already did.
                messages.append({"role": _norm_role(role), "content": content})

        # 2. Inject session turns (accumulated within THIS multi-turn request).
        #    These are the turns from the current agent loop — not in DB yet.
        #    This is how the model remembers what it did on turns 1 and 2
        #    when it's now on turn 3.
        for st in self._session_turns:
            messages.append(st)

        # 3. Build the current user message
        parts = [f"PROJECT FILES:\n{tree_str}{todo_snippet}{pkg_snippet}"]
        if is_debug and error_context:
            parts.append(f"\nERROR TO FIX:\n{error_context}")
        elif user_request:
            parts.append(f"\nUSER REQUEST:\n{user_request}")

        if previous_command_output:
            parts.append(
                f"\nTOOL OUTPUT:\n{previous_command_output[:6000]}\n\n"
                "Continue. Check ports if you started the dev server. "
                "`done: true` ONLY when :8080 and :3000 both return 200.")
        else:
            parts.append(
                "\nFirst turn. Create .gorilla/todo.md, then start working. "
                "Final todo item: verify both ports respond 200.")

        parts.append(
            '\nONE JSON: {"plan":"...","message":"...","calls":[...],"done":bool}. '
            "Tools: view, create_file, str_replace, todo_update, bash, search, delete.")
        text_content = "\n".join(parts)

        use_vision = bool(image_b64)
        if use_vision:
            img_url = image_b64 if image_b64.startswith("data:") else f"data:image/jpeg;base64,{image_b64}"
            user_msg: Dict[str, Any] = {"role": "user", "content": [
                {"type": "text", "text": text_content},
                {"type": "image_url", "image_url": {"url": img_url}},
            ]}
        else:
            user_msg = {"role": "user", "content": text_content}

        messages.append(user_msg)

        model_used = VISION_MODEL if use_vision else MODEL
        log_agent("agent", f"v8 ({model_used}, debug={is_debug}, session_turns={len(self._session_turns)})", self.project_id)

        try:
            raw, tokens = await _call_llm(messages, model=model_used, temperature=0.6)
            self.total_tokens += tokens
        except Exception as e:
            log_agent("agent", f"LLM error: {e}", self.project_id)
            return {"message": f"AI error: {str(e)[:150]}", "commands": [], "done": True, "tokens": 0}

        parsed = _extract_json(raw)
        if not parsed:
            log_agent("agent", "Bad JSON — retrying", self.project_id)
            messages.extend([
                {"role": "assistant", "content": raw},
                {"role": "user", "content": 'Bad JSON. ONE object: {"plan":"...","message":"...","calls":[...],"done":bool}'},
            ])
            try:
                raw2, t2 = await _call_llm(messages, model=MODEL, temperature=0.3)
                self.total_tokens += t2
                parsed = _extract_json(raw2)
                if parsed: raw = raw2  # use the fixed version for session memory
            except Exception: pass

        if not parsed:
            return {"message": "Parse error. Try again.", "commands": [], "done": True, "tokens": self.total_tokens}

        # ── BUG 2 FIX: Record this turn in session memory ────────────
        # Store the user message (text only, no images — too big) and the
        # RAW assistant response (full JSON with plan, calls, done).
        self._session_turns.append({"role": "user", "content": text_content})
        self._session_turns.append({"role": "assistant", "content": raw})

        # Cap session turns to prevent context overflow on long loops
        if len(self._session_turns) > 24:
            self._session_turns = self._session_turns[-24:]

        plan = parsed.get("plan") or ""
        if plan: log_agent("agent", f"PLAN: {plan[:300]}", self.project_id)

        message = parsed.get("message") or "On it."
        done = bool(parsed.get("done", True))

        raw_calls = parsed.get("calls") or []
        legacy_cmds = parsed.get("commands") or []
        if raw_calls and isinstance(raw_calls, list):
            shell_cmds, finish = _translate_calls(raw_calls)
            done = done or finish
        elif legacy_cmds and isinstance(legacy_cmds, list):
            shell_cmds = [c for c in legacy_cmds if isinstance(c, str) and c.strip()]
        else:
            shell_cmds = []

        safe_cmds = [self.token_sub.expand(c) for c in shell_cmds if _is_safe_command(self.token_sub.expand(c))]

        log_agent("agent", f"{len(safe_cmds)} cmds, done={done}, tok={self.total_tokens}", self.project_id)
        return {"message": message, "commands": safe_cmds, "done": done, "tokens": self.total_tokens}


class Agent:
    """Legacy shim."""
    def __init__(self, timeout_s: float = 120.0): self.timeout_s = timeout_s
    def remember(self, project_id: str, role: str, text: str) -> None: _append_history(project_id, role, text)

__all__ = [
    "LineageAgent", "Agent", "set_log_callback", "log_agent",
    "_render_token_limit_message", "_append_history", "_get_history",
    "clear_history", "TokenSubstitution",
]