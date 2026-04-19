"""
E2B Sandbox Manager — Final (matches Lineage Agent v9)
========================================================

Architecture:
  - Boot: tar.gz upload (v6 latency fix)
  - Tree read: batched find+xargs with sentinel (v6 latency fix)
  - Agent loop: mini-SWE-agent style — call run(), execute bash, feed
    raw OBSERVATION back, loop until GORILLA_DONE
  - Agent persisted on session across user messages
  - Sync: batched, runs once after agent finishes
  - No orchestrator error correction — agent handles everything
  - URL: uses SDK's get_host() method

The agent is fully autonomous. It runs npm run dev, checks ports, fixes
errors. The sandbox manager only does infrastructure: boot, execute,
sync, emit URL.
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

from backend.ai.lineage_agent import LineageAgent, log_agent, review_output

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
E2B_API_KEY = os.getenv("E2B_API_KEY", "")
SANDBOX_TEMPLATE = os.getenv("E2B_TEMPLATE", "base")
IDLE_TIMEOUT_S = 300
BILLING_TOKENS_PER_HOUR = 50_000
BILLING_TICK_S = 60
APP_DIR = "/home/user/app"
MAX_COMMANDS_PER_TURN = 40
MAX_TURNS_PER_REQUEST = 20
SYNC_MARKER = "/tmp/.gorilla_sync_marker"
FILE_READ_SENTINEL = "═══GORILLA_FILE_BOUNDARY_9f8c═══"
FILE_CONTENT_SENTINEL = "═══GORILLA_CONTENT_START_9f8c═══"

DEFAULT_PREVIEW_PORT = 8080
DEFAULT_SERVER_PORT = 3000

READY_PATTERNS = [
    r"ready in \d+\s*m?s",
    r"Local:\s+https?://",
    r"Server running",
    r"listening on",
]


# ---------------------------------------------------------------------------
# Command classifier — for UI activity cards
# ---------------------------------------------------------------------------
def classify_command(cmd: str) -> Dict[str, str]:
    c = cmd.strip()
    low = c.lower()
    m = re.match(r"cat\s+>>?\s+['\"]?([^\s'\"<]+)['\"]?\s+<<", c)
    if m:
        return {"verb": "edit", "target": m.group(1), "short": f"Edit {m.group(1)}"}
    if low.startswith("mkdir"):
        m = re.search(r"mkdir\s+(?:-p\s+)?['\"]?([^\s'\"]+)", c)
        return {"verb": "create", "target": m.group(1) if m else "", "short": f"Create dir {m.group(1) if m else ''}"}
    if low.startswith("rm"):
        m = re.search(r"rm\s+(?:-\S+\s+)*['\"]?([^\s'\"]+)", c)
        return {"verb": "delete", "target": m.group(1) if m else "", "short": f"Delete {m.group(1) if m else ''}"}
    if low.startswith("npm install") or low.startswith("npm i "):
        return {"verb": "install", "target": "", "short": "Install dependencies"}
    if low.startswith("npm run"):
        m = re.search(r"npm\s+run\s+(\S+)", c)
        return {"verb": "execute", "target": m.group(1) if m else "", "short": f"Run {m.group(1) if m else 'script'}"}
    if low.startswith("curl"):
        if "supabase.com" in c and "database/query" in c:
            return {"verb": "database", "target": "migration", "short": "Run SQL migration"}
        return {"verb": "fetch", "target": "", "short": "API call"}
    if low.startswith("cat ") or low.startswith("tail ") or low.startswith("head "):
        m = re.match(r"\S+\s+(?:-\S+\s+)*['\"]?([^\s'\"]+)", c)
        return {"verb": "read", "target": m.group(1) if m else "", "short": f"Read {m.group(1) if m else 'file'}"}
    if low.startswith("grep ") or low.startswith("find "):
        return {"verb": "scan", "target": "", "short": "Search files"}
    if low.startswith("python"):
        return {"verb": "execute", "target": "python", "short": "Run Python script"}
    if low.startswith("sed "):
        return {"verb": "edit", "target": "", "short": "Edit file"}
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
    agent: Optional[Any] = field(default=None, repr=False)


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
        if text:
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
        if text:
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
                    try: return int(m.group(1))
                    except ValueError: pass
        pkg = file_tree.get("package.json", "")
        m = re.search(r"--port[= ]+(\d+)", pkg)
        if m:
            try: return int(m.group(1))
            except ValueError: pass
        return DEFAULT_PREVIEW_PORT

    def _sandbox_url_for_port(self, sbx, port: int) -> str:
        try:
            host = sbx.get_host(port)
            return f"https://{host}"
        except Exception:
            return f"https://{sbx.sandbox_id}-{port}.e2b.dev"

    # -----------------------------------------------------------
    # Batched boot-time file upload via tar
    # -----------------------------------------------------------
    @staticmethod
    def _build_tar_base64(file_tree: Dict[str, str]) -> Tuple[str, int, Dict[str, str]]:
        buf = io.BytesIO()
        hashes: Dict[str, str] = {}
        count = 0
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for path, content in file_tree.items():
                if not path or content is None: continue
                if any(x in path for x in ["package-lock.json", "yarn.lock", "node_modules/", ".git/"]): continue
                data = content.encode("utf-8", errors="replace")
                if len(data) > 1_000_000: continue
                ti = tarfile.TarInfo(name=path)
                ti.size = len(data)
                ti.mtime = int(time.time())
                tf.addfile(ti, io.BytesIO(data))
                hashes[path] = hashlib.md5(data).hexdigest()
                count += 1
        raw = buf.getvalue()
        if len(raw) > 5 * 1024 * 1024: return "", 0, {}
        return base64.b64encode(raw).decode("ascii"), count, hashes

    def _upload_files_fast(self, sbx, file_tree: Dict[str, str]) -> Tuple[int, Dict[str, str]]:
        b64, count, hashes = self._build_tar_base64(file_tree)
        if not b64:
            return self._upload_files_slow(sbx, file_tree)
        try:
            sbx.commands.run("rm -f /tmp/bundle.b64 && touch /tmp/bundle.b64", timeout=5)
            for i in range(0, len(b64), 50000):
                chunk = b64[i:i+50000]
                meta = base64.b64encode(chunk.encode()).decode()
                sbx.commands.run(f"echo '{meta}' | base64 -d >> /tmp/bundle.b64", timeout=10)
            result = sbx.commands.run(
                f"mkdir -p {APP_DIR} && cat /tmp/bundle.b64 | base64 -d | tar -xzf - -C {APP_DIR} && rm -f /tmp/bundle.b64",
                timeout=60,
            )
            if result.exit_code != 0:
                return self._upload_files_slow(sbx, file_tree)
            return count, hashes
        except Exception as e:
            print(f"⚠️ Batched upload failed: {e}; falling back")
            return self._upload_files_slow(sbx, file_tree)

    @staticmethod
    def _upload_files_slow(sbx, file_tree: Dict[str, str]) -> Tuple[int, Dict[str, str]]:
        hashes: Dict[str, str] = {}
        count = 0
        dirs_created: Set[str] = set()
        for path, content in file_tree.items():
            if not path or content is None: continue
            if any(x in path for x in ["package-lock.json", "yarn.lock", "node_modules/", ".git/"]): continue
            full = f"{APP_DIR}/{path}"
            dirp = "/".join(full.split("/")[:-1])
            if dirp and dirp not in dirs_created:
                sbx.commands.run(f"mkdir -p '{dirp}'", timeout=5)
                dirs_created.add(dirp)
            safe = content.replace("GORILLA_EOF", "GORILLA__EOF")
            sbx.commands.run(f"cat > '{full}' << 'GORILLA_EOF'\n{safe}\nGORILLA_EOF", timeout=15)
            hashes[path] = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()
            count += 1
        return count, hashes

    # -----------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------
    async def ensure_running(self, project_id: str, env_vars: Dict[str, str], owner_id: str) -> SandboxSession:
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

    async def _do_boot(self, project_id, file_tree, env_vars, owner_id):
        if not Sandbox: raise RuntimeError("E2B SDK not installed")
        if not E2B_API_KEY: raise RuntimeError("E2B_API_KEY not set")

        self._emit_log(project_id, "sandbox", f"Booting sandbox ({SANDBOX_TEMPLATE})...")
        self._emit_status(project_id, "Booting Sandbox...")

        def _boot_sync():
            sbx = Sandbox(template=SANDBOX_TEMPLATE, api_key=E2B_API_KEY, timeout=3600)
            sbx.commands.run(f"mkdir -p {APP_DIR}", timeout=5)
            return sbx

        try:
            sbx = await asyncio.to_thread(_boot_sync)
        except Exception as e:
            self._emit_log(project_id, "sandbox", f"Boot failed: {e}")
            raise

        self._emit_log(project_id, "sandbox", "Uploading project files...")
        file_count, hashes = await asyncio.to_thread(self._upload_files_fast, sbx, file_tree)
        self._emit_log(project_id, "sandbox", f"Mounted {file_count} files")

        if env_vars:
            try:
                env_lines = "\n".join(f"{k}={v}" for k, v in env_vars.items() if v)
                shell_lines = "\n".join(f'export {k}="{v}"' for k, v in env_vars.items() if v)
                await asyncio.to_thread(
                    sbx.commands.run,
                    f"cat > {APP_DIR}/.env << 'GORILLA_EOF'\n{env_lines}\nGORILLA_EOF && "
                    f"cat > {APP_DIR}/.gorilla_env << 'GORILLA_EOF'\n{shell_lines}\nGORILLA_EOF",
                )
            except Exception as e:
                self._emit_log(project_id, "sandbox", f"env write warning: {e}")

        check = await asyncio.to_thread(
            sbx.commands.run, f"test -d {APP_DIR}/node_modules && echo EXISTS || echo MISSING",
        )
        deps_cached = "EXISTS" in (check.stdout or "")
        preview_port = self._detect_preview_port(file_tree)

        session = SandboxSession(
            project_id=project_id, sandbox=sbx, sandbox_id=sbx.sandbox_id,
            owner_id=owner_id, preview_port=preview_port,
            url=self._sandbox_url_for_port(sbx, preview_port),
            deps_installed=deps_cached, content_hashes=hashes,
        )
        self._sessions[project_id] = session

        if not deps_cached:
            self._emit_log(project_id, "sandbox", "Installing dependencies...")
            self._emit_status(project_id, "Installing Dependencies...")
            try:
                inst = await asyncio.to_thread(
                    sbx.commands.run,
                    f"cd {APP_DIR} && npm install --legacy-peer-deps --no-fund --no-audit --prefer-offline",
                    timeout=360,
                )
                if inst.exit_code == 0:
                    session.deps_installed = True
                    self._emit_log(project_id, "sandbox", "Dependencies installed")
                else:
                    self._emit_log(project_id, "sandbox", f"npm install warning: {(inst.stderr or '')[:200]}")
            except Exception as e:
                self._emit_log(project_id, "sandbox", f"npm install error: {e}")
        else:
            self._emit_log(project_id, "sandbox", "Dependencies cached")

        session._billing_task = asyncio.create_task(self._billing_loop(project_id))
        if not self._idle_monitor_task or self._idle_monitor_task.done():
            self._idle_monitor_task = asyncio.create_task(self._idle_monitor())

        self._emit_log(project_id, "sandbox", f"Sandbox ready: {session.url}")
        self._emit_status(project_id, "Agent started...)
        self._emit(project_id, {"type": "sandbox_url", "url": session.url})
        return session

    async def kill(self, project_id: str) -> None:
        session = self._sessions.pop(project_id, None)
        if not session:
            self._boot_locks.pop(project_id, None)
            self._turn_locks.pop(project_id, None)
            return
        now = time.time()
        elapsed = (now - session.last_bill_at) / 3600.0
        if elapsed > 0.01:
            prorated = int(BILLING_TOKENS_PER_HOUR * elapsed)
            if prorated > 0:
                try: self._add_tokens(session.owner_id, prorated)
                except Exception as e: print(f"⚠️ Billing error: {e}")
        if session._billing_task and not session._billing_task.done():
            session._billing_task.cancel()
        try: await asyncio.to_thread(session.sandbox.kill)
        except Exception as e: print(f"⚠️ Kill error: {e}")
        self._boot_locks.pop(project_id, None)
        self._turn_locks.pop(project_id, None)
        self._emit_status(project_id, "Sandbox Offline")

    def is_running(self, project_id: str) -> bool:
        return project_id in self._sessions

    def get_session(self, project_id: str) -> Optional[SandboxSession]:
        s = self._sessions.get(project_id)
        if s: s.last_activity = time.time()
        return s

    def get_preview_url(self, project_id: str) -> Optional[str]:
        s = self._sessions.get(project_id)
        return s.url if s else None

    # -----------------------------------------------------------
    # Agent turn — mini-SWE-agent loop
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
                project_id, user_request, user_id, env_vars,
                chat_history, gorilla_proxy_url, has_supabase,
                is_debug, error_context, image_b64, on_assistant_message,
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
            await asyncio.to_thread(session.sandbox.commands.run, f"touch {SYNC_MARKER}")
        except Exception:
            pass

        # #3 — Auto-kill ports before agent starts
        # Eliminates the "port in use" retry spiral
        try:
            await asyncio.to_thread(
                session.sandbox.commands.run,
                "pkill -f 'vite' 2>/dev/null; pkill -f 'npm run dev' 2>/dev/null; "
                "pkill -f 'node server' 2>/dev/null; sleep 1",
                timeout=10,
            )
        except Exception:
            pass

        # Persist agent across user messages
        if session.agent is None:
            session.agent = LineageAgent(project_id)
        agent = session.agent

        all_commands: List[str] = []
        final_message = ""
        total_tokens = 0
        turn_count = 0
        previous_output: Optional[str] = None
        last_raw_output = ""

        # Read file tree once
        tree = await self._read_tree_from_sandbox(project_id)

        for turn in range(MAX_TURNS_PER_REQUEST):
            turn_count = turn + 1
            log_agent("agent", f"Turn {turn_count}/{MAX_TURNS_PER_REQUEST}", project_id)

            result = await agent.run(
                user_request=user_request,
                file_tree=tree if turn == 0 else {},
                chat_history=chat_history if turn == 0 else None,
                gorilla_proxy_url=gorilla_proxy_url,
                has_supabase=has_supabase,
                is_debug=is_debug,
                error_context=error_context if turn == 0 else "",
                image_b64=image_b64 if turn == 0 else None,
                previous_command_output=previous_output,
            )
            total_tokens += result.get("tokens", 0)

            msg = result.get("message", "")
            if msg:
                final_message = msg
                self._emit_narration(project_id, msg)
                if on_assistant_message:
                    try: on_assistant_message(msg)
                    except Exception: pass

            commands = result.get("commands", [])

            if result.get("done", False) and not commands:
                break

            if not commands and not result.get("done", False):
                previous_output = (
                    "OBSERVATION:\n"
                    "No commands detected. Provide your next step in a "
                    "```bash``` code block, or write GORILLA_DONE if finished."
                )
                continue

            all_commands.extend(commands)
            cmd_results = await self._execute_commands_streaming(project_id, commands)

            # Build OBSERVATION
            output_parts = []
            for r in cmd_results:
                stdout = (r.get("stdout") or "").strip()
                stderr = (r.get("stderr") or "").strip()
                exit_code = r.get("exit_code", 0)
                if stdout: output_parts.append(stdout[:3000])
                if stderr: output_parts.append(f"STDERR: {stderr[:1500]}")
                if exit_code != 0: output_parts.append(f"[exit code: {exit_code}]")

            # #8 — Silent success confirmation
            raw_output = "\n".join(output_parts)[:6000] if output_parts else "Your command ran successfully and did not produce any output."
            last_raw_output = raw_output

            # #6 — Linter-in-the-loop: if the command wrote a .tsx/.ts file,
            # run tsc --noEmit on it and append lint errors to the observation
            for cmd in commands:
                # Detect cat > path.tsx heredoc writes
                m = re.search(r"cat\s+>\s+['\"]?(\S+\.tsx?)['\"]?\s+<<", cmd)
                if m:
                    lint_path = m.group(1)
                    try:
                        lint_result = await asyncio.to_thread(
                            session.sandbox.commands.run,
                            f"cd {APP_DIR} && npx tsc --noEmit {lint_path} 2>&1 | head -20",
                            timeout=15,
                        )
                        lint_out = (lint_result.stdout or "").strip()
                        if lint_out and ("error TS" in lint_out or "Error" in lint_out):
                            raw_output += f"\n\nLINT ERRORS in {lint_path}:\n{lint_out[:800]}"
                    except Exception:
                        pass

            previous_output = raw_output

            if result.get("done", False):
                break

        # #10 — Reviewer: after agent says done, one cheap call checks for mistakes
        if not is_debug and turn_count > 1:
            try:
                current_tree = await self._read_tree_from_sandbox(project_id)
                tree_summary = "\n".join(f"  {p}" for p in sorted(current_tree.keys()) if not p.endswith(".b64"))
                review_fixes = await review_output(tree_summary, last_raw_output)
                if review_fixes:
                    # Feed the review back to the agent for one more pass
                    self._emit_narration(project_id, "Reviewing output for issues...")
                    fix_result = await agent.run(
                        user_request=f"Code review found issues. Fix them:\n{review_fixes}",
                        file_tree={},
                        gorilla_proxy_url=gorilla_proxy_url,
                        has_supabase=has_supabase,
                        is_debug=True,
                        error_context=review_fixes,
                    )
                    total_tokens += fix_result.get("tokens", 0)
                    fix_cmds = fix_result.get("commands", [])
                    if fix_cmds:
                        all_commands.extend(fix_cmds)
                        await self._execute_commands_streaming(project_id, fix_cmds)
            except Exception as e:
                log_agent("agent", f"Reviewer error: {e}", project_id)

        # Sync to Supabase
        self._emit_status(project_id, "Syncing to database...")
        synced, deleted = await self._sync_once(project_id)
        self._emit_log(project_id, "sync", f"Synced {synced} changed, removed {deleted} deleted")

        url = session.url
        self._emit(project_id, {"type": "sandbox_url", "url": url})

        return {
            "ok": True, "commands": all_commands,
            "tokens": total_tokens,
            "final_message": final_message or "Done.",
            "turns": turn_count, "synced_files": synced,
            "deleted_files": deleted, "preview_url": url,
        }

    # -----------------------------------------------------------
    # Streaming executor
    # -----------------------------------------------------------
    async def _execute_commands_streaming(self, project_id, commands):
        session = self._sessions.get(project_id)
        if not session:
            return [{"command": "N/A", "stdout": "", "stderr": "Sandbox not running", "exit_code": -1}]

        session.last_activity = time.time()
        results: List[Dict[str, Any]] = []

        for cmd in commands[:MAX_COMMANDS_PER_TURN]:
            if not cmd or not cmd.strip(): continue

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
                if not line: return
                stdout_buf.append(line)
                clean = line.rstrip("\n")[:400]
                if clean.strip():
                    self._emit_activity_chunk(project_id, activity_id, "stdout", clean)

            def on_stderr(line: str):
                if not line: return
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
                "command": cmd, "stdout": "".join(stdout_buf),
                "stderr": "".join(stderr_buf), "exit_code": exit_code,
            })

        try:
            await asyncio.to_thread(session.sandbox.commands.run, "sync && true")
        except Exception: pass
        session.last_activity = time.time()
        return results

    @staticmethod
    def _run_command_with_streaming(sandbox, cmd, on_stdout, on_stderr):
        try:
            result = sandbox.commands.run(cmd, on_stdout=on_stdout, on_stderr=on_stderr, timeout=180)
            return getattr(result, "exit_code", 0)
        except TypeError: pass
        try:
            result = sandbox.commands.run(cmd, timeout=180)
            if result.stdout:
                for line in result.stdout.splitlines(keepends=True): on_stdout(line)
            if result.stderr:
                for line in result.stderr.splitlines(keepends=True): on_stderr(line)
            return getattr(result, "exit_code", 0)
        except Exception as e:
            on_stderr(str(e)); return -1

    # -----------------------------------------------------------
    # Batched tree read
    # -----------------------------------------------------------
    async def _read_tree_from_sandbox(self, project_id: str) -> Dict[str, str]:
        session = self._sessions.get(project_id)
        if not session: return {}

        dump_cmd = (
            f"find {APP_DIR} -type f "
            f"-not -path '*/node_modules/*' -not -path '*/.git/*' "
            f"-not -path '*/dist/*' -not -name 'package-lock.json' "
            f"-not -name '*.lock' -size -500k -print0 | "
            f'xargs -0 -I {{}} sh -c \''
            f'echo "{FILE_READ_SENTINEL}{{}}"; '
            f'echo "{FILE_CONTENT_SENTINEL}"; '
            f'cat "{{}}" 2>/dev/null; echo ""\''
        )

        try:
            result = await asyncio.to_thread(session.sandbox.commands.run, dump_cmd)
            output = result.stdout or ""
        except Exception:
            return await self._read_tree_slow(project_id)

        if not output.strip(): return {}

        tree: Dict[str, str] = {}
        for chunk in output.split(FILE_READ_SENTINEL)[1:]:
            if FILE_CONTENT_SENTINEL not in chunk: continue
            header, _, body = chunk.partition(FILE_CONTENT_SENTINEL)
            path = header.strip().replace(f"{APP_DIR}/", "", 1)
            if not path: continue
            content = body[:-1] if body.endswith("\n") else body
            if "\x00" in content[:1000]: continue
            tree[path] = content
        return tree

    async def _read_tree_slow(self, project_id: str) -> Dict[str, str]:
        session = self._sessions.get(project_id)
        if not session: return {}
        try:
            listing = await asyncio.to_thread(
                session.sandbox.commands.run,
                f"find {APP_DIR} -type f -not -path '*/node_modules/*' "
                f"-not -path '*/.git/*' -not -path '*/dist/*' "
                f"-not -name 'package-lock.json' -not -name '*.lock' -size -500k",
            )
        except Exception: return {}
        if not listing.stdout: return {}
        paths = [p.strip().replace(f"{APP_DIR}/", "", 1) for p in listing.stdout.strip().split("\n") if p.strip()]
        tree: Dict[str, str] = {}
        for rel in paths[:400]:
            try:
                r = await asyncio.to_thread(session.sandbox.commands.run, f"cat '{APP_DIR}/{rel}' 2>/dev/null")
                content = r.stdout or ""
                if "\x00" in content[:1000]: continue
                tree[rel] = content
            except Exception: continue
        return tree

    # -----------------------------------------------------------
    # Batched sync
    # -----------------------------------------------------------
    async def _sync_once(self, project_id: str) -> Tuple[int, int]:
        session = self._sessions.get(project_id)
        if not session: return (0, 0)
        try: await asyncio.to_thread(session.sandbox.commands.run, "sync")
        except Exception: pass

        changed_dump_cmd = (
            f"find {APP_DIR} -type f -newer {SYNC_MARKER} "
            f"-not -path '*/node_modules/*' -not -path '*/.git/*' "
            f"-not -path '*/dist/*' -not -name 'package-lock.json' "
            f"-not -name '*.lock' -size -500k -print0 | "
            f'xargs -0 -I {{}} sh -c \''
            f'echo "{FILE_READ_SENTINEL}{{}}"; '
            f'echo "{FILE_CONTENT_SENTINEL}"; '
            f'cat "{{}}" 2>/dev/null; echo ""\''
        )

        try:
            result = await asyncio.to_thread(session.sandbox.commands.run, changed_dump_cmd)
            dump = result.stdout or ""
        except Exception as e:
            self._emit_log(project_id, "sync", f"Sync failed: {e}")
            return (0, 0)

        changed_files: Dict[str, str] = {}
        if dump.strip():
            for chunk in dump.split(FILE_READ_SENTINEL)[1:]:
                if FILE_CONTENT_SENTINEL not in chunk: continue
                header, _, body = chunk.partition(FILE_CONTENT_SENTINEL)
                path = header.strip().replace(f"{APP_DIR}/", "", 1)
                if not path: continue
                content = body[:-1] if body.endswith("\n") else body
                if "\x00" in content[:1000]: continue
                if len(content) > 500_000: continue
                changed_files[path] = content

        rows: List[Dict[str, Any]] = []
        current_sandbox_paths: Set[str] = set(changed_files.keys())
        for rel, content in changed_files.items():
            h = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()
            if session.content_hashes.get(rel) == h: continue
            session.content_hashes[rel] = h
            rows.append({"project_id": project_id, "path": rel, "content": content})

        try:
            all_listing = await asyncio.to_thread(
                session.sandbox.commands.run,
                f"find {APP_DIR} -type f -not -path '*/node_modules/*' "
                f"-not -path '*/.git/*' -not -path '*/dist/*' "
                f"-not -name 'package-lock.json' -not -name '*.lock'",
            )
            if all_listing.stdout:
                for p in all_listing.stdout.strip().split("\n"):
                    p = p.strip()
                    if p: current_sandbox_paths.add(p.replace(f"{APP_DIR}/", "", 1))
        except Exception: pass

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
            to_delete = db_paths - current_sandbox_paths - {".env", ".gorilla_env"}
            for p in to_delete:
                try:
                    self._db_delete("files", {"project_id": project_id, "path": p})
                    self._emit_file_deleted(project_id, p)
                    session.content_hashes.pop(p, None)
                    deleted_count += 1
                except Exception: pass
        except Exception: pass

        try:
            await asyncio.to_thread(session.sandbox.commands.run, f"touch {SYNC_MARKER}")
        except Exception: pass

        return (len(rows), deleted_count)

    # -----------------------------------------------------------
    # Dev server (kept for /sandbox/start endpoint, NOT used in agent loop)
    # -----------------------------------------------------------
    async def start_dev_server(self, project_id: str) -> Tuple[Optional[str], Optional[str]]:
        session = self._sessions.get(project_id)
        if not session: return (None, None)
        session.last_activity = time.time()

        try:
            await asyncio.to_thread(session.sandbox.commands.run,
                "pkill -f vite || true; pkill -f 'npm run dev' || true; pkill -f 'node server' || true")
            await asyncio.sleep(1)
        except Exception: pass

        try:
            await asyncio.to_thread(session.sandbox.commands.run,
                f"cd {APP_DIR} && nohup npm run dev > /tmp/vite.log 2>&1 &")
        except Exception: pass

        bound = False
        for _ in range(15):
            await asyncio.sleep(1)
            try:
                check = await asyncio.to_thread(session.sandbox.commands.run,
                    f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{session.preview_port} || echo fail")
                if "200" in (check.stdout or "") or "304" in (check.stdout or ""):
                    bound = True; break
            except Exception: pass

        self._emit(project_id, {"type": "sandbox_url", "url": session.url})
        return (session.url, None)

    # -----------------------------------------------------------
    # File write/delete for editor /save bridge
    # -----------------------------------------------------------
    async def write_file(self, project_id: str, rel_path: str, content: str) -> bool:
        session = self._sessions.get(project_id)
        if not session: return False
        full = f"{APP_DIR}/{rel_path}"
        dirp = "/".join(full.split("/")[:-1])
        try:
            safe = content.replace("GORILLA_EOF", "GORILLA__EOF")
            await asyncio.to_thread(session.sandbox.commands.run,
                f"mkdir -p '{dirp}' && cat > '{full}' << 'GORILLA_EOF'\n{safe}\nGORILLA_EOF")
            session.content_hashes[rel_path] = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()
            session.last_activity = time.time()
            return True
        except Exception as e:
            print(f"⚠️ write_file failed: {e}"); return False

    async def delete_file(self, project_id: str, rel_path: str) -> bool:
        session = self._sessions.get(project_id)
        if not session: return False
        try:
            await asyncio.to_thread(session.sandbox.commands.run, f"rm -f '{APP_DIR}/{rel_path}'")
            session.content_hashes.pop(rel_path, None)
            session.last_activity = time.time()
            return True
        except Exception: return False

    # -----------------------------------------------------------
    # Billing + idle
    # -----------------------------------------------------------
    async def _billing_loop(self, project_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(BILLING_TICK_S)
                session = self._sessions.get(project_id)
                if not session: return
                now = time.time()
                elapsed = (now - session.last_bill_at) / 3600.0
                if elapsed <= 0: continue
                prorated = int(BILLING_TOKENS_PER_HOUR * elapsed)
                if prorated <= 0: continue
                try:
                    self._add_tokens(session.owner_id, prorated)
                    session.total_billed_tokens += prorated
                    session.last_bill_at = now
                except Exception as e:
                    print(f"⚠️ Billing error: {e}")
        except asyncio.CancelledError: pass

    async def _idle_monitor(self) -> None:
        while True:
            try:
                now = time.time()
                for pid in [p for p, s in list(self._sessions.items()) if (now - s.last_activity) > IDLE_TIMEOUT_S]:
                    print(f"💤 Idle kill: {pid}")
                    try: await self._sync_once(pid)
                    except Exception: pass
                    await self.kill(pid)
            except Exception as e:
                print(f"Idle monitor error: {e}")
            await asyncio.sleep(30)


sandbox_manager: Optional[E2BSandboxManager] = None