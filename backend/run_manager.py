from __future__ import annotations

import os
import json
import socket
import asyncio
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import httpx
from dotenv import dotenv_values 

def _pick_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    _, port = s.getsockname()
    s.close()
    return int(port)

@dataclass
class RunInfo:
    project_id: str
    port: int
    proc: asyncio.subprocess.Process
    root_dir: str
    runtime_bin: str = "node"

class ProjectRunManager:
    """
    Node.js Only Runner:
      - Expects a package.json or standard JS entry point (server.js, index.js).
      - Runs `npm install` automatically.
      - Starts the application via `npm start` or `node [entry]`.
      - Proxies traffic to localhost:PORT.
    """

    def __init__(self, base_dir: str = "/tmp/gor-a"):
        self.base_dir = base_dir
        self._runs: Dict[str, RunInfo] = {}
        os.makedirs(self.base_dir, exist_ok=True)

        # Inject secrets from .env files if they exist
        self.injected_secrets = {
            **dotenv_values(".env.inject"),
            **dotenv_values("backend/.env.inject")
        }

    def is_running(self, project_id: str) -> Tuple[bool, Optional[int]]:
        info = self._runs.get(project_id)
        if not info:
            return False, None
        if info.proc.returncode is not None:
            self._runs.pop(project_id, None)
            return False, None
        return True, info.port

    async def stop(self, project_id: str) -> None:
        info = self._runs.get(project_id)
        if not info:
            return
        try:
            info.proc.terminate()
            await asyncio.wait_for(info.proc.wait(), timeout=3)
        except Exception:
            try:
                info.proc.kill()
            except Exception:
                pass
        self._runs.pop(project_id, None)

    async def _run_cmd(self, cwd: str, cmd: list[str], timeout_s: int = 300) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            return int(proc.returncode or 0), (out_bytes or b"").decode("utf-8", errors="ignore")
        except asyncio.TimeoutError:
            try: proc.kill()
            except: pass
            return 124, f"Timeout running: {' '.join(cmd)}"
        except Exception as e:
            return 1, str(e)

    # --- NODE.JS SETUP ---
    async def _setup_node(self, root: str) -> list[str]:
        """Runs npm install and returns the start command."""
        pkg_path = os.path.join(root, "package.json")
        
        # 1. Install Dependencies
        if os.path.exists(pkg_path):
            # optimization: check if node_modules exists to skip install? 
            # For now, we run it to ensure consistency, but standard npm is smart enough not to redo everything.
            print("--> Running npm install...")
            code, out = await self._run_cmd(root, ["npm", "install"], timeout_s=400)
            if code != 0:
                raise RuntimeError(f"npm install failed:\n{out}")
        
        # 2. Determine Start Command
        try:
            if os.path.exists(pkg_path):
                with open(pkg_path) as f:
                    pkg_data = json.load(f)
                
                scripts = pkg_data.get("scripts", {})
                
                if "start" in scripts:
                    return ["npm", "start"]
                elif "dev" in scripts:
                    return ["npm", "run", "dev"]
                elif pkg_data.get("main"):
                    return ["node", pkg_data["main"]]
        except Exception:
            pass
            
        # Fallback heuristics
        if os.path.exists(os.path.join(root, "server.js")):
            return ["node", "server.js"]
        if os.path.exists(os.path.join(root, "index.js")):
            return ["node", "index.js"]
        if os.path.exists(os.path.join(root, "app.js")):
            return ["node", "app.js"]
            
        # If we are here, we don't know how to run it.
        # Default to index.js and let it fail if missing, so the error bubbles up cleanly.
        return ["node", "index.js"]

    async def start(self, project_id: str, file_tree: Dict[str, str]) -> RunInfo:
        # OPTIMIZATION: If already running and healthy, do not restart.
        # This prevents "Live URL" visitors from killing the server if they arrive simultaneously.
        if project_id in self._runs:
            existing = self._runs[project_id]
            if existing.proc.returncode is None:
                return existing

        await self.stop(project_id)

        root = os.path.join(self.base_dir, project_id)
        os.makedirs(root, exist_ok=True)

        # 1. Write User Files
        for path, content in (file_tree or {}).items():
            if not path or path.endswith("/"): continue
            abs_path = os.path.join(root, path)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f: f.write(content or "")

        # 2. Setup Node environment
        start_cmd = await self._setup_node(root)
        port = _pick_free_port()

        # 3. Prepare Env Vars
        env_vars = os.environ.copy()
        
        # Remove sensitive keys from host
        sensitive_keys = [
            "FIREWORKS_API_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_URL",
            "RESEND_API_KEY", "AUTH_SECRET_KEY", "GROQ_API_KEY"
        ]
        for k in sensitive_keys: env_vars.pop(k, None)

        env_vars.update(self.injected_secrets)
        env_vars["PORT"] = str(port)

        # 4. Launch Process
        print(f"--> Launching {project_id} on port {port} using {start_cmd}...")
        proc = await asyncio.create_subprocess_exec(
            *start_cmd, 
            cwd=root, env=env_vars,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        info = RunInfo(project_id=project_id, port=port, proc=proc, root_dir=root)
        self._runs[project_id] = info

        # 5. Health Check
        startup_error = None
        for _ in range(60): 
            await asyncio.sleep(0.5)
            
            if proc.returncode is not None:
                # Died
                raw_err = await proc.stderr.read()
                raw_out = await proc.stdout.read()
                full_log = (raw_out.decode() + "\n" + raw_err.decode()).strip()
                
                # Simple error detection
                if "Error:" in full_log:
                    startup_error = full_log
                else:
                    startup_error = full_log or "Unknown crash (process exited)."
                break
            
            try:
                # Try to hit the root
                async with httpx.AsyncClient(timeout=1.0) as client:
                    await client.get(f"http://127.0.0.1:{port}/")
                return info
            except (httpx.ConnectError, httpx.ReadTimeout):
                continue
        
        if startup_error:
            raise RuntimeError(f"App crashed during startup:\n{startup_error}")
        
        await self.stop(project_id)
        raise RuntimeError("Server started but timed out (port not reachable). Did you listen on process.env.PORT?")

    async def proxy(self, project_id: str, path: str, method: str, headers: dict, body: bytes, query: str) -> httpx.Response:
        running, port = self.is_running(project_id)
        
        if not running:
            info = self._runs.get(project_id)
            err_msg = "Server not running."
            if info and info.proc.stderr:
                 try:
                    err_bytes = await info.proc.stderr.read()
                    if err_bytes: err_msg = err_bytes.decode()
                 except: pass
            raise RuntimeError(f"CRASH_DETECTED: {err_msg}")

        url = f"http://127.0.0.1:{port}/{path.lstrip('/')}"
        if query: url += f"?{query}"

        fwd_headers = {}
        for k, v in headers.items():
            lk = k.lower()
            if lk in ("host", "content-length", "connection"): continue
            fwd_headers[k] = v

        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.request(method, url, headers=fwd_headers, content=body)
                return resp
            except (httpx.ConnectError, httpx.ReadTimeout):
                raise RuntimeError("CRASH_DETECTED: Connection refused during request (Runtime Crash).")