# backend/run_manager.py
from __future__ import annotations

import os
import shutil
import socket
import asyncio
import sys
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
    python_bin: str

class ProjectRunManager:
    """
    DEV-only runner:
      - dumps project files to /tmp/gor-a/<project_id>/
      - installs requirements.txt into a project venv if present
      - runs uvicorn for app.py if present (expects app:app)
      - proxies via FastAPI endpoint (in app.py)
      - [SECURE] Injects secrets from .env.inject at runtime
      - [SECURE] Scrubs backend keys from user process
      - [ROBUST] Checks for startup crashes and captures errors
    """

    def __init__(self, base_dir: str = "/tmp/gor-a"):
        self.base_dir = base_dir
        self._runs: Dict[str, RunInfo] = {}
        os.makedirs(self.base_dir, exist_ok=True)

        # 1. Load Secret Injection File
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
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            try: proc.kill()
            except: pass
            return 124, f"Timeout running: {' '.join(cmd)}"
        return int(proc.returncode or 0), (out_bytes or b"").decode("utf-8", errors="ignore")

    async def _ensure_venv_and_install(self, root: str) -> str:
        req_path = os.path.join(root, "requirements.txt")
        venv_dir = os.path.join(root, ".venv")

        if not os.path.isdir(venv_dir):
            code, out = await self._run_cmd(root, ["python", "-m", "venv", ".venv"], timeout_s=180)
            if code != 0: raise RuntimeError(f"Failed to create venv:\n{out}")

        python_bin = os.path.join(venv_dir, "bin", "python")
        pip_bin = [python_bin, "-m", "pip"]

        # FIX: Force install uvicorn and fastapi to prevent startup crashes if missing from reqs
        # Also install python-dotenv just in case user code relies on it
        code, out = await self._run_cmd(root, pip_bin + ["install", "--upgrade", "pip", "setuptools", "wheel", "uvicorn", "fastapi", "python-dotenv"], timeout_s=240)
        if code != 0: raise RuntimeError(f"Failed to upgrade pip/uvicorn:\n{out}")

        if os.path.exists(req_path):
            code, out = await self._run_cmd(root, pip_bin + ["install", "-r", "requirements.txt"], timeout_s=600)
            if code != 0: raise RuntimeError(f"requirements.txt install failed:\n{out}")

        return python_bin

    async def start(self, project_id: str, file_tree: Dict[str, str]) -> RunInfo:
        await self.stop(project_id)

        root = os.path.join(self.base_dir, project_id)
        if os.path.isdir(root): shutil.rmtree(root, ignore_errors=True)
        os.makedirs(root, exist_ok=True)

        for path, content in (file_tree or {}).items():
            if not path or path.endswith("/"): continue
            abs_path = os.path.join(root, path)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f: f.write(content or "")

        entry = os.path.join(root, "app.py")
        if not os.path.exists(entry):
            raise RuntimeError("No app.py found. Server preview requires app.py exporting `app`.")

        python_bin = await self._ensure_venv_and_install(root)
        port = _pick_free_port()

        # --- [SECURE ENV PREPARATION] ---
        env_vars = os.environ.copy()
        
        # Blocklist Backend Keys
        sensitive_keys = [
            "FIREWORKS_API_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_URL",
            "RESEND_API_KEY", "AUTH_SECRET_KEY", "GROQ_API_KEY",
            "GOOGLE_CLIENT_SECRET", "GOOGLE_CLIENT_ID", "FIREWORKS_URL"
        ]
        for k in sensitive_keys:
            env_vars.pop(k, None)

        # Inject Secrets
        env_vars.update(self.injected_secrets)
        
        # Config
        env_vars["PYTHONUNBUFFERED"] = "1"
        env_vars["PORT"] = str(port)

        # Start Uvicorn with Stderr Capture
        proc = await asyncio.create_subprocess_exec(
            python_bin, "-m", "uvicorn", "app:app",
            "--host", "127.0.0.1", "--port", str(port),
            cwd=root, env=env_vars,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, # Capture errors for Auto-Fix
        )

        info = RunInfo(project_id=project_id, port=port, proc=proc, root_dir=root, python_bin=python_bin)
        self._runs[project_id] = info

        # --- [STARTUP HEALTH CHECK] ---
        # Increased timeout to 30s (60 * 0.5) to allow for slow builds/installs
        startup_error = None
        for _ in range(60): 
            await asyncio.sleep(0.5)
            
            # Check if process died
            if proc.returncode is not None:
                raw_err = await proc.stderr.read()
                startup_error = raw_err.decode() if raw_err else "Unknown crash on startup"
                break
            
            # Check if port is listening
            try:
                async with httpx.AsyncClient(timeout=1.0) as client:
                    await client.get(f"http://127.0.0.1:{port}/")
                # If we get here (even if 404/500), the server IS running
                return info
            except (httpx.ConnectError, httpx.ReadTimeout):
                continue # Keep waiting
        
        # If loop finishes or process died
        if startup_error:
            raise RuntimeError(f"App crashed during startup:\n{startup_error}")
        
        # If we timed out but process is still "running" (zombie state)
        await self.stop(project_id)
        raise RuntimeError("Server started but timed out (port not reachable).")

    async def proxy(self, project_id: str, path: str, method: str, headers: dict, body: bytes, query: str) -> httpx.Response:
        running, port = self.is_running(project_id)
        
        # If not running, assume crash and try to read log
        if not running:
            info = self._runs.get(project_id)
            err_msg = "Server not running."
            if info and info.proc.stderr:
                 try:
                    err_bytes = await info.proc.stderr.read()
                    if err_bytes: err_msg = err_bytes.decode()
                 except: pass
            
            # Throw specific error for Auto-Fix to catch
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
                # Connection dropped mid-request -> Crash likely
                raise RuntimeError("CRASH_DETECTED: Connection refused during request (Runtime Crash).")