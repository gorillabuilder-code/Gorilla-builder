"""
E2B Sandbox Manager v6 — Latency-Killed Edition
==================================================

What's new from v5:
  - BOOT: single tar upload instead of N×3 round-trips per file.
    30-file project boot goes from ~45s of I/O to ~3s.
  - TREE READ: one-shot concatenated dump via find+sentinel delimiter.
    Instead of N round-trips it's ONE round-trip and we parse client-side.
  - SYNC: same pattern for changed files — one-shot batched read.
  - Bigger E2B OOM guard rails: template swap via env, larger timeouts,
    optional --no-fund --no-audit flags on npm install to reduce memory
    spikes from npm's telemetry/lockfile fetching.
  - Safer fallbacks: if a batched operation fails, falls back to the
    old per-file method so nothing breaks.

All the v5 features stay: activity_start / activity_chunk / activity_end,
narration, vite error detection, port auto-detection, content-hash cache,
batch upserts, delete detection, billing, idle monitor, per-project locks.
"""

from __future__ import annotations

import os
import re
import io
import time
import base64
import tarfile
import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Callable, Set, Tuple

try:
    from e2b import Sandbox
except ImportError:
    Sandbox = None
    print("⚠️ e2b package not installed. Run: pip install e2b")

from backend.ai.lineage_agent import LineageAgent, log_agent

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
E2B_API_KEY = os.getenv("E2B_API_KEY", "")
# Swap template via env. `base` is 512MB and OOMs on npm install + agent.
# Use `node` (community template, 1GB+) or a custom higher-memory one.
SANDBOX_TEMPLATE = os.getenv("E2B_TEMPLATE", "base")
IDLE_TIMEOUT_S = 300
BILLING_TOKENS_PER_HOUR = 50_000
BILLING_TICK_S = 60
APP_DIR = "/home/user/app"
DEV_SERVER_WAIT_S = 15
MAX_COMMANDS_PER_TURN = 40
MAX_TURNS_PER_REQUEST = 100
SYNC_MARKER = "/tmp/.gorilla_sync_marker"
FILE_READ_SENTINEL = "═══GORILLA_FILE_BOUNDARY_9f8c═══"
FILE_CONTENT_SENTINEL = "═══GORILLA_CONTENT_START_9f8c═══"
BOOT_UPLOAD_TAR_MAX = 5 * 1024 * 1024     # 5 MB single upload cap

DEFAULT_PREVIEW_PORT = 8080
DEFAULT_SERVER_PORT = 3000

READY_PATTERNS = [
    r"ready in \d+\s*m?s",
    r"Local:\s+https?://",
    r"Server running",
    r"listening on",
]


# ---------------------------------------------------------------------------
# Command classifier (unchanged from v5)
# ---------------------------------------------------------------------------
def classify_command(cmd: str) -> Dict[str, str]:
    """Return {verb, target, short} for UI activity cards."""
    c = cmd.strip()
    low = c.lower()

    m = re.match(r"cat\s+>>?\s+['\"]?([^\s'\"<]+)['\"]?\s+<<", c)
    if m:
        path = m.group(1)
        return {"verb": "edit", "target": path, "short": f"Edit {path}"}

    m = re.match(r"(?:echo|printf)\s+.+?>>?\s+['\"]?([^\s'\"&|]+)['\"]?", c)
    if m:
        return {"verb": "edit", "target": m.group(1), "short": f"Edit {m.group(1)}"}

    if low.startswith("mkdir"):
        m = re.search(r"mkdir\s+(?:-p\s+)?['\"]?([^\s'\"]+)", c)
        target = m.group(1) if m else ""
        return {"verb": "create", "target": target, "short": f"Create dir {target}"}

    if low.startswith("rm"):
        m = re.search(r"rm\s+(?:-\S+\s+)*['\"]?([^\s'\"]+)", c)
        target = m.group(1) if m else ""
        return {"verb": "delete", "target": target, "short": f"Delete {target}"}

    if low.startswith("mv "):
        return {"verb": "edit", "target": "", "short": "Move files"}
    if low.startswith("cp "):
        return {"verb": "edit", "target": "", "short": "Copy files"}

    if low.startswith("npm install") or low.startswith("pnpm add") or low.startswith("yarn add"):
        pkgs = re.findall(r"\b([@a-z0-9][@a-z0-9/\-._]+)\b", c[len("npm install"):])
        pkgs = [p for p in pkgs if not p.startswith("-") and p not in {"install", "add"}]
        target = " ".join(pkgs[:3]) if pkgs else ""
        short = f"Install {target}" if target else "Install dependencies"
        return {"verb": "install", "target": target, "short": short}

    if "npm run" in low or "pnpm run" in low:
        m = re.search(r"(?:npm|pnpm)\s+run\s+(\S+)", c)
        script = m.group(1) if m else ""
        return {"verb": "execute", "target": script, "short": f"Run {script}"}

    if low.startswith("curl"):
        m = re.search(r"(?:https?://[^\s\"']+)", c)
        host = ""
        if m:
            host = m.group(0).split("/")[2] if len(m.group(0).split("/")) > 2 else m.group(0)
        if "supabase.com" in c and "database/query" in c:
            return {"verb": "database", "target": "migration", "short": "Run SQL migration"}
        return {"verb": "fetch", "target": host, "short": f"Generate image from {host}" if host else "API call"}

    if low.startswith("cat ") or low.startswith("tail ") or low.startswith("head "):
        m = re.match(r"\S+\s+(?:-\S+\s+)*['\"]?([^\s'\"]+)", c)
        target = m.group(1) if m else ""
        return {"verb": "read", "target": target, "short": f"Read {target}" if target else "Read file"}

    if low.startswith("find ") or low.startswith("ls "):
        return {"verb": "scan", "target": "", "short": "Scan filesystem"}

    if low.startswith("git "):
        m = re.search(r"git\s+(\S+)", c)
        action = m.group(1) if m else ""
        return {"verb": "git", "target": action, "short": f"git {action}"}

    first = c.split()[0] if c.split() else "run"
    return {"verb": "execute", "target": "", "short": f"Execute {first}"}


@dataclass
class SandboxSession:
    project_id: str
    sandbox: Any
    sandbox_id: str
    owner_id: str
    preview_port: int = DEFAULT_PREVIEW_PORT
    url: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    last_bill_at: float = field(default_factory=time.time)
    total_billed_tokens: int = 0
    deps_installed: bool = False
    content_hashes: Dict[str, str] = field(default_factory=dict)
    _billing_task: Optional[asyncio.Task] = field(default=None, repr=False)


class E2BSandboxManager:
    def __init__(
        self,
        db_upsert_fn: Callable,
        db_delete_fn: Callable,
        db_upsert_batch_fn: Optional[Callable],
        add_tokens_fn: Callable,
        emit_log_fn: Callable,
        emit_status_fn: Callable,
        emit_file_changed_fn: Callable,
        emit_file_deleted_fn: Callable,
        fetch_files_fn: Callable,
        list_db_paths_fn: Callable,
        progress_bus: Any = None,
    ):
        self._sessions: Dict[str, SandboxSession] = {}
        self._db_upsert = db_upsert_fn
        self._db_delete = db_delete_fn
        self._db_upsert_batch = db_upsert_batch_fn
        self._add_tokens = add_tokens_fn
        self._emit_log = emit_log_fn
        self._emit_status = emit_status_fn
        self._emit_file_changed = emit_file_changed_fn
        self._emit_file_deleted = emit_file_deleted_fn
        self._fetch_files = fetch_files_fn
        self._list_db_paths = list_db_paths_fn
        self._progress_bus = progress_bus
        self._idle_monitor_task: Optional[asyncio.Task] = None
        self._boot_locks: Dict[str, asyncio.Lock] = {}
        self._turn_locks: Dict[str, asyncio.Lock] = {}
        self._activity_counter: Dict[str, int] = {}

    # -----------------------------------------------------------
    # Emit helpers
    # -----------------------------------------------------------
    def _emit(self, project_id: str, event: Dict[str, Any]) -> None:
        if self._progress_bus:
            self._progress_bus.emit(project_id, event)

    def _next_activity_id(self, project_id: str) -> str:
        self._activity_counter[project_id] = self._activity_counter.get(project_id, 0) + 1
        return f"act_{self._activity_counter[project_id]}"

    def _emit_activity_start(self, project_id, activity_id, verb, target, short):
        self._emit(project_id, {
            "type": "activity_start", "id": activity_id,
            "verb": verb, "target": target, "short": short,
        })

    def _emit_activity_chunk(self, project_id, activity_id, stream, text):
        if not text:
            return
        self._emit(project_id, {
            "type": "activity_chunk", "id": activity_id,
            "stream": stream, "text": text,
        })

    def _emit_activity_end(self, project_id, activity_id, exit_code):
        self._emit(project_id, {
            "type": "activity_end", "id": activity_id,
            "exit_code": exit_code, "ok": exit_code == 0,
        })

    def _emit_narration(self, project_id, text):
        if not text:
            return
        self._emit(project_id, {"type": "narration", "text": text})

    # -----------------------------------------------------------
    # Port detection
    # -----------------------------------------------------------
    @staticmethod
    def _detect_preview_port(file_tree: Dict[str, str]) -> int:
        for path in ("vite.config.ts", "vite.config.js", "vite.config.mjs"):
            if path in file_tree and file_tree[path]:
                m = re.search(r"port\s*:\s*(\d+)", file_tree[path])
                if m:
                    try:
                        return int(m.group(1))
                    except ValueError:
                        pass
        pkg = file_tree.get("package.json", "")
        m = re.search(r"--port[= ]+(\d+)", pkg)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        return DEFAULT_PREVIEW_PORT

    def _sandbox_url_for_port(self, sandbox_id: str, port: int) -> str:
        return f"https://{sandbox_id}-{port}.e2b.dev"

    # -----------------------------------------------------------
    # LATENCY FIX 1: Batched boot-time file upload via tar+base64
    # -----------------------------------------------------------
    @staticmethod
    def _build_tar_base64(file_tree: Dict[str, str]) -> Tuple[str, int, Dict[str, str]]:
        """
        Pack all files into a tar.gz in memory, base64 it, and return
        (base64_str, file_count, content_hashes).
        Skips binary/lockfile paths. Returns "" if too large.
        """
        buf = io.BytesIO()
        hashes: Dict[str, str] = {}
        count = 0
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for path, content in file_tree.items():
                if not path or content is None:
                    continue
                if any(x in path for x in [
                    "package-lock.json", "yarn.lock", "node_modules/", ".git/",
                ]):
                    continue
                data = content.encode("utf-8", errors="replace")
                # Skip pre-encoded binary blobs to avoid bloating the tar
                if len(data) > 1_000_000:
                    continue
                ti = tarfile.TarInfo(name=path)
                ti.size = len(data)
                ti.mtime = int(time.time())
                tf.addfile(ti, io.BytesIO(data))
                hashes[path] = hashlib.md5(data).hexdigest()
                count += 1

        raw = buf.getvalue()
        if len(raw) > BOOT_UPLOAD_TAR_MAX:
            # Caller will fall back to per-file method
            return "", 0, {}
        b64 = base64.b64encode(raw).decode("ascii")
        return b64, count, hashes

    @staticmethod
    def _chunk_string(s: str, chunk_size: int = 60_000) -> List[str]:
        """Split a string into chunks to avoid hitting shell arg limits."""
        return [s[i:i + chunk_size] for i in range(0, len(s), chunk_size)]

    def _upload_files_fast(self, sbx, file_tree: Dict[str, str]) -> Tuple[int, Dict[str, str]]:
        """
        Upload all files in ONE batched operation.
        Returns (file_count, content_hashes). Falls back to per-file on failure.
        """
        b64, count, hashes = self._build_tar_base64(file_tree)
        if not b64:
            return self._upload_files_slow(sbx, file_tree)

        try:
            # Write base64 in chunks via append, then decode+extract in one go
            sbx.commands.run(f"rm -f /tmp/bundle.b64 && touch /tmp/bundle.b64", timeout=5)
            for chunk in self._chunk_string(b64, 50_000):
                # Append chunk safely using printf '%s' (no interpretation)
                # We have to base64-encode the chunk again to avoid shell quoting issues
                meta = base64.b64encode(chunk.encode()).decode()
                sbx.commands.run(
                    f"echo '{meta}' | base64 -d >> /tmp/bundle.b64",
                    timeout=10,
                )
            # Now decode the entire bundle and extract to APP_DIR
            result = sbx.commands.run(
                f"mkdir -p {APP_DIR} && "
                f"cat /tmp/bundle.b64 | base64 -d | tar -xzf - -C {APP_DIR} && "
                f"rm -f /tmp/bundle.b64",
                timeout=60,
            )
            if result.exit_code != 0:
                # Tar failed — fall back
                print(f"⚠️ Tar extract failed: {(result.stderr or '')[:200]}; falling back")
                return self._upload_files_slow(sbx, file_tree)
            return count, hashes
        except Exception as e:
            print(f"⚠️ Batched upload crashed: {e}; falling back to per-file")
            return self._upload_files_slow(sbx, file_tree)

    @staticmethod
    def _upload_files_slow(sbx, file_tree: Dict[str, str]) -> Tuple[int, Dict[str, str]]:
        """Old per-file upload. Only used as a fallback."""
        hashes: Dict[str, str] = {}
        count = 0
        dirs_created: Set[str] = set()
        for path, content in file_tree.items():
            if not path or content is None:
                continue
            if any(x in path for x in [
                "package-lock.json", "yarn.lock", "node_modules/", ".git/",
            ]):
                continue
            full = f"{APP_DIR}/{path}"
            dirp = "/".join(full.split("/")[:-1])
            # Cheap optimisation: only mkdir once per unique directory
            if dirp and dirp not in dirs_created:
                sbx.commands.run(f"mkdir -p '{dirp}'", timeout=5)
                dirs_created.add(dirp)
            safe = content.replace("GORILLA_EOF", "GORILLA__EOF")
            sbx.commands.run(
                f"cat > '{full}' << 'GORILLA_EOF'\n{safe}\nGORILLA_EOF",
                timeout=15,
            )
            hashes[path] = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()
            count += 1
        return count, hashes

    # -----------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------
    async def ensure_running(
        self, project_id: str, env_vars: Dict[str, str], owner_id: str,
    ) -> SandboxSession:
        if project_id in self._sessions:
            s = self._sessions[project_id]
            s.last_activity = time.time()
            return s
        self._boot_locks.setdefault(project_id, asyncio.Lock())
        async with self._boot_locks[project_id]:
            if project_id in self._sessions:
                s = self._sessions[project_id]
                s.last_activity = time.time()
                return s
            files = await self._fetch_files(project_id)
            return await self._do_boot(project_id, files, env_vars, owner_id)

    async def _do_boot(
        self, project_id: str, file_tree: Dict[str, str],
        env_vars: Dict[str, str], owner_id: str,
    ) -> SandboxSession:
        if not Sandbox:
            raise RuntimeError("E2B SDK not installed")
        if not E2B_API_KEY:
            raise RuntimeError("E2B_API_KEY not set")

        self._emit_log(project_id, "sandbox", f"Booting sandbox ({SANDBOX_TEMPLATE})...")
        self._emit_status(project_id, "Booting Sandbox...")

        # Run boot in a thread so we don't block the event loop
        def _boot_sync():
            sbx = Sandbox(template=SANDBOX_TEMPLATE, api_key=E2B_API_KEY, timeout=3600)
            sid = sbx.sandbox_id
            sbx.commands.run(f"mkdir -p {APP_DIR}", timeout=5)
            return sbx, sid

        try:
            sbx, sandbox_id = await asyncio.to_thread(_boot_sync)
        except Exception as e:
            self._emit_log(project_id, "sandbox", f"Boot failed: {e}")
            raise

        # LATENCY FIX 1: batched file upload
        self._emit_log(project_id, "sandbox", "Uploading project files...")
        file_count, hashes = await asyncio.to_thread(
            self._upload_files_fast, sbx, file_tree,
        )
        self._emit_log(project_id, "sandbox", f"Mounted {file_count} files")

        # Write env vars
        if env_vars:
            try:
                env_lines = "\n".join(f"{k}={v}" for k, v in env_vars.items() if v)
                shell_lines = "\n".join(
                    f'export {k}="{v}"' for k, v in env_vars.items() if v
                )
                await asyncio.to_thread(
                    sbx.commands.run,
                    f"cat > {APP_DIR}/.env << 'GORILLA_EOF'\n{env_lines}\nGORILLA_EOF && "
                    f"cat > {APP_DIR}/.gorilla_env << 'GORILLA_EOF'\n{shell_lines}\nGORILLA_EOF",
                )
            except Exception as e:
                self._emit_log(project_id, "sandbox", f"env write warning: {e}")

        # Check dep cache
        def _check_deps():
            return sbx.commands.run(
                f"test -d {APP_DIR}/node_modules && echo EXISTS || echo MISSING",
                timeout=5,
            )
        check = await asyncio.to_thread(_check_deps)
        deps_cached = "EXISTS" in (check.stdout or "")

        preview_port = self._detect_preview_port(file_tree)

        session = SandboxSession(
            project_id=project_id,
            sandbox=sbx,
            sandbox_id=sandbox_id,
            owner_id=owner_id,
            preview_port=preview_port,
            url=self._sandbox_url_for_port(sandbox_id, preview_port),
            deps_installed=deps_cached,
            content_hashes=hashes,
        )
        self._sessions[project_id] = session

        if not deps_cached:
            self._emit_log(project_id, "sandbox", "Installing dependencies (this can take 1-2 min)...")
            self._emit_status(project_id, "Installing Dependencies...")

            # --no-fund --no-audit reduce memory pressure and extra network calls
            # --prefer-offline speeds up if cache exists
            def _npm_install():
                return sbx.commands.run(
                    f"cd {APP_DIR} && npm install --legacy-peer-deps "
                    f"--no-fund --no-audit --prefer-offline",
                    timeout=360,
                )
            try:
                inst = await asyncio.to_thread(_npm_install)
                if inst.exit_code == 0:
                    session.deps_installed = True
                    self._emit_log(project_id, "sandbox", "Dependencies installed")
                else:
                    self._emit_log(
                        project_id, "sandbox",
                        f"npm install warning: {(inst.stderr or '')[:200]}",
                    )
            except Exception as e:
                self._emit_log(project_id, "sandbox", f"npm install error: {e}")
        else:
            self._emit_log(project_id, "sandbox", "Dependencies cached")

        session._billing_task = asyncio.create_task(self._billing_loop(project_id))
        if not self._idle_monitor_task or self._idle_monitor_task.done():
            self._idle_monitor_task = asyncio.create_task(self._idle_monitor())

        self._emit_log(
            project_id, "sandbox",
            f"Sandbox ready (preview port {preview_port}): {session.url}",
        )
        self._emit_status(project_id, "Sandbox Ready")
        self._emit(project_id, {"type": "sandbox_url", "url": session.url})
        return session

    async def kill(self, project_id: str) -> None:
        session = self._sessions.pop(project_id, None)
        if not session:
            self._boot_locks.pop(project_id, None)
            self._turn_locks.pop(project_id, None)
            return

        now = time.time()
        elapsed_hours = (now - session.last_bill_at) / 3600.0
        if elapsed_hours > 0.01:
            prorated = int(BILLING_TOKENS_PER_HOUR * elapsed_hours)
            if prorated > 0:
                try:
                    self._add_tokens(session.owner_id, prorated)
                    session.total_billed_tokens += prorated
                except Exception as e:
                    print(f"⚠️ Prorated billing error: {e}")

        if session._billing_task and not session._billing_task.done():
            session._billing_task.cancel()
        try:
            await asyncio.to_thread(session.sandbox.kill)
        except Exception as e:
            print(f"⚠️ Error killing sandbox {project_id}: {e}")
        self._boot_locks.pop(project_id, None)
        self._turn_locks.pop(project_id, None)
        self._emit_log(project_id, "sandbox", "Sandbox shut down")
        self._emit_status(project_id, "Sandbox Offline")

    def is_running(self, project_id: str) -> bool:
        return project_id in self._sessions

    def get_session(self, project_id: str) -> Optional[SandboxSession]:
        s = self._sessions.get(project_id)
        if s:
            s.last_activity = time.time()
        return s

    def get_preview_url(self, project_id: str) -> Optional[str]:
        s = self._sessions.get(project_id)
        return s.url if s else None

    # -----------------------------------------------------------
    # Main entry point
    # -----------------------------------------------------------
    async def run_agent_turn(
        self, project_id, user_request, user_id, env_vars,
        chat_history=None, gorilla_proxy_url="", has_supabase=False,
        is_debug=False, error_context="", image_b64=None,
        on_assistant_message=None,
    ) -> Dict[str, Any]:
        self._turn_locks.setdefault(project_id, asyncio.Lock())
        async with self._turn_locks[project_id]:
            return await self._do_run_agent_turn(
                project_id=project_id, user_request=user_request, user_id=user_id,
                env_vars=env_vars, chat_history=chat_history,
                gorilla_proxy_url=gorilla_proxy_url, has_supabase=has_supabase,
                is_debug=is_debug, error_context=error_context,
                image_b64=image_b64, on_assistant_message=on_assistant_message,
            )

    async def _do_run_agent_turn(
        self, project_id, user_request, user_id, env_vars,
        chat_history, gorilla_proxy_url, has_supabase, is_debug,
        error_context, image_b64, on_assistant_message,
    ) -> Dict[str, Any]:
        try:
            session = await self.ensure_running(project_id, env_vars, user_id)
        except Exception as e:
            self._emit_log(project_id, "system", f"Sandbox boot failed: {e}")
            self._emit_status(project_id, "Fatal Error")
            return {"ok": False, "error": str(e), "commands": [],
                    "tokens": 0, "final_message": "", "turns": 0}

        try:
            await asyncio.to_thread(
                session.sandbox.commands.run, f"touch {SYNC_MARKER}",
            )
        except Exception:
            pass

        agent = LineageAgent(project_id)
        all_commands: List[str] = []
        all_errors: List[Dict] = []
        final_message = ""
        previous_output: Optional[str] = None
        total_tokens = 0
        turn_count = 0

        for turn in range(MAX_TURNS_PER_REQUEST):
            turn_count = turn + 1
            log_agent("agent", f"Turn {turn_count}/{MAX_TURNS_PER_REQUEST}", project_id)

            # LATENCY FIX 2: batched tree read
            tree = await self._read_tree_from_sandbox(project_id)

            result = await agent.run(
                user_request=user_request, file_tree=tree,
                chat_history=chat_history, gorilla_proxy_url=gorilla_proxy_url,
                has_supabase=has_supabase, is_debug=is_debug,
                error_context=error_context,
                image_b64=image_b64 if turn == 0 else None,
                previous_command_output=previous_output,
            )
            total_tokens += result.get("tokens", 0)

            msg = result.get("message", "")
            if msg:
                final_message = msg
                self._emit_narration(project_id, msg)
                if on_assistant_message:
                    try:
                        on_assistant_message(msg)
                    except Exception:
                        pass

            commands = result.get("commands", [])
            if not commands:
                break

            all_commands.extend(commands)
            cmd_results = await self._execute_commands_streaming(project_id, commands)

            turn_errors = [
                {"command": r["command"], "stderr": r["stderr"], "exit_code": r["exit_code"]}
                for r in cmd_results
                if r.get("exit_code", 0) != 0 and r.get("stderr")
            ]
            all_errors.extend(turn_errors)

            if result.get("done", True):
                break

            parts = []
            for r in cmd_results[-6:]:
                cmd_short = r["command"][:100]
                out = (r["stdout"] or "")[:500]
                err = (r["stderr"] or "")[:300]
                p = f"$ {cmd_short}\n"
                if out:
                    p += f"stdout: {out}\n"
                if err:
                    p += f"stderr: {err}\n"
                p += f"exit: {r['exit_code']}"
                parts.append(p)
            previous_output = "\n---\n".join(parts)[:4000]

        # Error self-correction
        if all_errors and not is_debug:
            error_summary = "\n\n".join([
                f"Command: {e['command'][:120]}\nExit: {e['exit_code']}\nError: {e['stderr'][:300]}"
                for e in all_errors[:3]
            ])
            self._emit_log(project_id, "debugger", "Errors detected, self-correcting...")
            tree = await self._read_tree_from_sandbox(project_id)
            fix_result = await agent.run(
                user_request=f"Previous commands failed. Fix these errors:\n{error_summary}",
                file_tree=tree, chat_history=chat_history,
                gorilla_proxy_url=gorilla_proxy_url, has_supabase=has_supabase,
                is_debug=True, error_context=error_summary,
            )
            total_tokens += fix_result.get("tokens", 0)
            fix_msg = fix_result.get("message", "")
            if fix_msg:
                self._emit_narration(project_id, fix_msg)
            fix_cmds = fix_result.get("commands", [])
            if fix_cmds:
                all_commands.extend(fix_cmds)
                await self._execute_commands_streaming(project_id, fix_cmds)

        # Single end-of-turn sync
        self._emit_status(project_id, "Syncing to database...")
        synced, deleted = await self._sync_once(project_id)
        self._emit_log(
            project_id, "sync",
            f"Synced {synced} changed, removed {deleted} deleted",
        )

        # Dev server
        self._emit_status(project_id, "Starting dev server...")
        url, vite_error = await self.start_dev_server(project_id)

        if vite_error and not is_debug:
            self._emit_log(project_id, "debugger", "Dev server has issues, fixing...")
            tree = await self._read_tree_from_sandbox(project_id)
            fix_result = await agent.run(
                user_request=f"Dev server reports errors. Fix them:\n{vite_error}",
                file_tree=tree, chat_history=chat_history,
                gorilla_proxy_url=gorilla_proxy_url, has_supabase=has_supabase,
                is_debug=True, error_context=vite_error,
            )
            total_tokens += fix_result.get("tokens", 0)
            fix_msg = fix_result.get("message", "")
            if fix_msg:
                self._emit_narration(project_id, fix_msg)
            vite_fix_cmds = fix_result.get("commands", [])
            if vite_fix_cmds:
                all_commands.extend(vite_fix_cmds)
                await self._execute_commands_streaming(project_id, vite_fix_cmds)
                await self._sync_once(project_id)
                url, _ = await self.start_dev_server(project_id)

        return {
            "ok": True, "commands": all_commands, "errors": all_errors,
            "tokens": total_tokens,
            "final_message": final_message or "Working on it...",
            "turns": turn_count, "synced_files": synced,
            "deleted_files": deleted, "preview_url": url,
        }

    # -----------------------------------------------------------
    # Streaming executor (unchanged from v5)
    # -----------------------------------------------------------
    async def _execute_commands_streaming(self, project_id, commands):
        session = self._sessions.get(project_id)
        if not session:
            return [{"command": "N/A", "stdout": "",
                     "stderr": "Sandbox not running", "exit_code": -1}]

        session.last_activity = time.time()
        results: List[Dict[str, Any]] = []

        for cmd in commands[:MAX_COMMANDS_PER_TURN]:
            if not cmd or not cmd.strip():
                continue

            classification = classify_command(cmd)
            activity_id = self._next_activity_id(project_id)
            self._emit_activity_start(
                project_id, activity_id,
                classification["verb"], classification["target"],
                classification["short"],
            )

            if cmd.startswith("cd ") or cmd.startswith("/"):
                effective = cmd
            else:
                effective = f"cd {APP_DIR} && source .gorilla_env 2>/dev/null; {cmd}"

            stdout_buf: List[str] = []
            stderr_buf: List[str] = []

            def on_stdout(line: str):
                if not line:
                    return
                stdout_buf.append(line)
                clean = line.rstrip("\n")[:400]
                if clean.strip():
                    self._emit_activity_chunk(project_id, activity_id, "stdout", clean)

            def on_stderr(line: str):
                if not line:
                    return
                stderr_buf.append(line)
                clean = line.rstrip("\n")[:400]
                if clean.strip():
                    self._emit_activity_chunk(project_id, activity_id, "stderr", clean)

            try:
                exit_code = await asyncio.to_thread(
                    self._run_command_with_streaming,
                    session.sandbox, effective, on_stdout, on_stderr,
                )
            except Exception as e:
                err = str(e)[:200]
                stderr_buf.append(err)
                self._emit_activity_chunk(project_id, activity_id, "stderr", err)
                exit_code = -1

            self._emit_activity_end(project_id, activity_id, exit_code)

            results.append({
                "command": cmd,
                "stdout": "".join(stdout_buf),
                "stderr": "".join(stderr_buf),
                "exit_code": exit_code,
            })

        try:
            await asyncio.to_thread(session.sandbox.commands.run, "sync && true")
        except Exception:
            pass
        session.last_activity = time.time()
        return results

    @staticmethod
    def _run_command_with_streaming(sandbox, cmd: str, on_stdout, on_stderr) -> int:
        try:
            result = sandbox.commands.run(
                cmd, on_stdout=on_stdout, on_stderr=on_stderr, timeout=180,
            )
            return getattr(result, "exit_code", 0)
        except TypeError:
            pass
        try:
            result = sandbox.commands.run(cmd, timeout=180)
            if result.stdout:
                for line in result.stdout.splitlines(keepends=True):
                    on_stdout(line)
            if result.stderr:
                for line in result.stderr.splitlines(keepends=True):
                    on_stderr(line)
            return getattr(result, "exit_code", 0)
        except Exception as e:
            on_stderr(str(e))
            return -1

    # -----------------------------------------------------------
    # LATENCY FIX 2: batched tree read
    # -----------------------------------------------------------
    async def _read_tree_from_sandbox(self, project_id: str) -> Dict[str, str]:
        """
        Instead of N sequential `cat` round-trips, do ONE shell command that
        dumps every file with delimiters, then parse the combined output.
        """
        session = self._sessions.get(project_id)
        if not session:
            return {}

        # Single command: for each file, emit sentinel+path, then content
        dump_cmd = (
            f"find {APP_DIR} -type f "
            f"-not -path '*/node_modules/*' "
            f"-not -path '*/.git/*' "
            f"-not -path '*/dist/*' "
            f"-not -name 'package-lock.json' "
            f"-not -name '*.lock' "
            f"-size -500k -print0 | "
            # Use xargs to cat all files with sentinel headers
            f'xargs -0 -I {{}} sh -c \''
            f'echo "{FILE_READ_SENTINEL}{{}}"; '
            f'echo "{FILE_CONTENT_SENTINEL}"; '
            f'cat "{{}}" 2>/dev/null; '
            f'echo ""\''
        )

        try:
            result = await asyncio.to_thread(
                session.sandbox.commands.run, dump_cmd,
            )
            output = result.stdout or ""
        except Exception as e:
            print(f"⚠️ Batched tree read failed: {e}; falling back")
            return await self._read_tree_slow(project_id)

        if not output.strip():
            return {}

        # Parse: split on FILE_READ_SENTINEL, each chunk has path + content
        tree: Dict[str, str] = {}
        # First split gives us ["", "path1\nCONTENT_SENTINEL\ncontent1\n", "path2\n..."]
        chunks = output.split(FILE_READ_SENTINEL)
        for chunk in chunks[1:]:  # skip the empty prefix
            if FILE_CONTENT_SENTINEL not in chunk:
                continue
            header, _, body = chunk.partition(FILE_CONTENT_SENTINEL)
            path = header.strip().replace(f"{APP_DIR}/", "", 1)
            if not path:
                continue
            # Body ends with a trailing echo "" — strip it
            content = body
            if content.endswith("\n\n"):
                content = content[:-1]
            elif content.endswith("\n"):
                content = content[:-1]
            # Skip binary
            if "\x00" in content[:1000]:
                continue
            tree[path] = content

        return tree

    async def _read_tree_slow(self, project_id: str) -> Dict[str, str]:
        """Fallback: old one-cat-per-file. Only used if batched read fails."""
        session = self._sessions.get(project_id)
        if not session:
            return {}
        try:
            listing = await asyncio.to_thread(
                session.sandbox.commands.run,
                f"find {APP_DIR} -type f "
                f"-not -path '*/node_modules/*' -not -path '*/.git/*' "
                f"-not -path '*/dist/*' -not -name 'package-lock.json' "
                f"-not -name '*.lock' -size -500k",
            )
        except Exception:
            return {}
        if not listing.stdout:
            return {}
        paths = [
            p.strip().replace(f"{APP_DIR}/", "", 1)
            for p in listing.stdout.strip().split("\n")
            if p.strip()
        ]
        tree: Dict[str, str] = {}
        for rel in paths[:400]:
            full = f"{APP_DIR}/{rel}"
            try:
                r = await asyncio.to_thread(
                    session.sandbox.commands.run, f"cat '{full}' 2>/dev/null",
                )
                content = r.stdout or ""
                if "\x00" in content[:1000]:
                    continue
                tree[rel] = content
            except Exception:
                continue
        return tree

    # -----------------------------------------------------------
    # LATENCY FIX 3: batched sync
    # -----------------------------------------------------------
    async def _sync_once(self, project_id: str) -> Tuple[int, int]:
        session = self._sessions.get(project_id)
        if not session:
            return (0, 0)
        try:
            await asyncio.to_thread(session.sandbox.commands.run, "sync")
        except Exception:
            pass

        # One dump command for changed files only
        changed_dump_cmd = (
            f"find {APP_DIR} -type f -newer {SYNC_MARKER} "
            f"-not -path '*/node_modules/*' -not -path '*/.git/*' "
            f"-not -path '*/dist/*' -not -name 'package-lock.json' "
            f"-not -name '*.lock' -size -500k -print0 | "
            f'xargs -0 -I {{}} sh -c \''
            f'echo "{FILE_READ_SENTINEL}{{}}"; '
            f'echo "{FILE_CONTENT_SENTINEL}"; '
            f'cat "{{}}" 2>/dev/null; '
            f'echo ""\''
        )

        try:
            result = await asyncio.to_thread(
                session.sandbox.commands.run, changed_dump_cmd,
            )
            dump = result.stdout or ""
        except Exception as e:
            self._emit_log(project_id, "sync", f"Sync dump failed: {e}")
            return (0, 0)

        # Parse
        changed_files: Dict[str, str] = {}
        if dump.strip():
            for chunk in dump.split(FILE_READ_SENTINEL)[1:]:
                if FILE_CONTENT_SENTINEL not in chunk:
                    continue
                header, _, body = chunk.partition(FILE_CONTENT_SENTINEL)
                path = header.strip().replace(f"{APP_DIR}/", "", 1)
                if not path:
                    continue
                content = body
                if content.endswith("\n\n"):
                    content = content[:-1]
                elif content.endswith("\n"):
                    content = content[:-1]
                if "\x00" in content[:1000]:
                    continue
                if len(content) > 500_000:
                    continue
                changed_files[path] = content

        # Build upsert rows by hash comparison
        rows: List[Dict[str, Any]] = []
        current_sandbox_paths: Set[str] = set(changed_files.keys())
        for rel, content in changed_files.items():
            h = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()
            if session.content_hashes.get(rel) == h:
                continue
            session.content_hashes[rel] = h
            rows.append({"project_id": project_id, "path": rel, "content": content})

        # Also need the FULL file list to detect deletions
        try:
            all_listing = await asyncio.to_thread(
                session.sandbox.commands.run,
                f"find {APP_DIR} -type f "
                f"-not -path '*/node_modules/*' -not -path '*/.git/*' "
                f"-not -path '*/dist/*' -not -name 'package-lock.json' "
                f"-not -name '*.lock'",
            )
            if all_listing.stdout:
                for p in all_listing.stdout.strip().split("\n"):
                    p = p.strip()
                    if p:
                        current_sandbox_paths.add(p.replace(f"{APP_DIR}/", "", 1))
        except Exception:
            pass

        if rows:
            try:
                if self._db_upsert_batch:
                    self._db_upsert_batch("files", rows, on_conflict="project_id,path")
                else:
                    for row in rows:
                        self._db_upsert("files", row, on_conflict="project_id,path")
                for row in rows:
                    self._emit_file_changed(project_id, row["path"])
            except Exception as e:
                self._emit_log(project_id, "sync", f"Upsert error: {e}")

        deleted_count = 0
        try:
            db_paths = set(self._list_db_paths(project_id) or [])
            to_delete = db_paths - current_sandbox_paths
            safe_skip = {".env", ".gorilla_env"}
            to_delete -= safe_skip
            for p in to_delete:
                try:
                    self._db_delete("files", {"project_id": project_id, "path": p})
                    self._emit_file_deleted(project_id, p)
                    session.content_hashes.pop(p, None)
                    deleted_count += 1
                except Exception as e:
                    self._emit_log(project_id, "sync", f"Delete failed {p}: {e}")
        except Exception:
            pass

        # Reset sync marker for next pass
        try:
            await asyncio.to_thread(
                session.sandbox.commands.run, f"touch {SYNC_MARKER}",
            )
        except Exception:
            pass

        return (len(rows), deleted_count)

    # -----------------------------------------------------------
    # Dev server (mostly unchanged, just to_thread wrapped)
    # -----------------------------------------------------------
    async def start_dev_server(self, project_id: str) -> Tuple[Optional[str], Optional[str]]:
        session = self._sessions.get(project_id)
        if not session:
            return (None, None)
        session.last_activity = time.time()

        try:
            await asyncio.to_thread(
                session.sandbox.commands.run,
                "pkill -f vite || true; pkill -f 'npm run dev' || true; "
                "pkill -f 'node server' || true",
            )
            await asyncio.sleep(1)
        except Exception:
            pass

        self._emit_log(project_id, "sandbox", "Starting dev server...")
        try:
            await asyncio.to_thread(
                session.sandbox.commands.run,
                f"cd {APP_DIR} && nohup npm run dev > /tmp/vite.log 2>&1 &",
            )
        except Exception:
            pass

        bound = False
        last_log = ""
        for i in range(DEV_SERVER_WAIT_S):
            await asyncio.sleep(1)
            try:
                check = await asyncio.to_thread(
                    session.sandbox.commands.run,
                    f"curl -s -o /dev/null -w '%{{http_code}}' "
                    f"http://localhost:{session.preview_port} || echo fail",
                )
                code = (check.stdout or "").strip()
                if "200" in code or "304" in code:
                    bound = True
                    break
                log_peek = await asyncio.to_thread(
                    session.sandbox.commands.run, "tail -n 40 /tmp/vite.log",
                )
                last_log = log_peek.stdout or ""
                for pat in READY_PATTERNS:
                    if re.search(pat, last_log, re.IGNORECASE):
                        await asyncio.sleep(1)
                        check2 = await asyncio.to_thread(
                            session.sandbox.commands.run,
                            f"curl -s -o /dev/null -w '%{{http_code}}' "
                            f"http://localhost:{session.preview_port} || echo fail",
                        )
                        c2 = (check2.stdout or "").strip()
                        if "200" in c2 or "304" in c2:
                            bound = True
                        break
                if bound:
                    break
            except Exception:
                pass

        try:
            final_log = await asyncio.to_thread(
                session.sandbox.commands.run, "tail -n 60 /tmp/vite.log",
            )
            log_text = final_log.stdout or ""
        except Exception:
            log_text = last_log

        if bound:
            self._emit_log(project_id, "sandbox", f"Dev server live at {session.url}")
        else:
            self._emit_log(
                project_id, "debugger",
                f"Dev server not responding. Log tail:\n{log_text[:600]}",
            )

        vite_error = self._extract_vite_errors(log_text)
        self._emit(project_id, {"type": "sandbox_url", "url": session.url})
        return (session.url, vite_error)

    @staticmethod
    def _extract_vite_errors(log_text: str) -> Optional[str]:
        if not log_text:
            return None
        lines = log_text.splitlines()
        issues: List[str] = []
        for i, line in enumerate(lines):
            low = line.lower()
            if ("could not be resolved" in low
                    or "failed to resolve import" in low
                    or "cannot find module" in low):
                context = "\n".join(lines[i:i + 8]).strip()
                issues.append(context)
        for i, line in enumerate(lines):
            if re.search(r"(SyntaxError|Unexpected token|Unexpected end)", line):
                context = "\n".join(lines[max(0, i - 2):i + 6]).strip()
                issues.append(context)
        if not issues:
            return None
        return "\n\n---\n\n".join(issues[:3])[:3000]

    async def write_file(self, project_id: str, rel_path: str, content: str) -> bool:
        session = self._sessions.get(project_id)
        if not session:
            return False
        full = f"{APP_DIR}/{rel_path}"
        dirp = "/".join(full.split("/")[:-1])
        try:
            safe = content.replace("GORILLA_EOF", "GORILLA__EOF")
            await asyncio.to_thread(
                session.sandbox.commands.run,
                f"mkdir -p '{dirp}' && "
                f"cat > '{full}' << 'GORILLA_EOF'\n{safe}\nGORILLA_EOF",
            )
            session.content_hashes[rel_path] = hashlib.md5(
                content.encode("utf-8", errors="replace")
            ).hexdigest()
            session.last_activity = time.time()
            return True
        except Exception as e:
            print(f"⚠️ write_file failed: {e}")
            return False

    async def delete_file(self, project_id: str, rel_path: str) -> bool:
        session = self._sessions.get(project_id)
        if not session:
            return False
        try:
            await asyncio.to_thread(
                session.sandbox.commands.run, f"rm -f '{APP_DIR}/{rel_path}'",
            )
            session.content_hashes.pop(rel_path, None)
            session.last_activity = time.time()
            return True
        except Exception:
            return False

    async def _billing_loop(self, project_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(BILLING_TICK_S)
                session = self._sessions.get(project_id)
                if not session:
                    return
                now = time.time()
                elapsed_hours = (now - session.last_bill_at) / 3600.0
                if elapsed_hours <= 0:
                    continue
                prorated = int(BILLING_TOKENS_PER_HOUR * elapsed_hours)
                if prorated <= 0:
                    continue
                try:
                    self._add_tokens(session.owner_id, prorated)
                    session.total_billed_tokens += prorated
                    session.last_bill_at = now
                except Exception as e:
                    print(f"⚠️ Billing error for {project_id}: {e}")
        except asyncio.CancelledError:
            pass

    async def _idle_monitor(self) -> None:
        while True:
            try:
                now = time.time()
                to_kill = [
                    pid for pid, s in list(self._sessions.items())
                    if (now - s.last_activity) > IDLE_TIMEOUT_S
                ]
                for pid in to_kill:
                    print(f"💤 Idle kill: sandbox {pid}")
                    try:
                        await self._sync_once(pid)
                    except Exception:
                        pass
                    await self.kill(pid)
            except Exception as e:
                print(f"Idle monitor error: {e}")
            await asyncio.sleep(30)


sandbox_manager: Optional[E2BSandboxManager] = None