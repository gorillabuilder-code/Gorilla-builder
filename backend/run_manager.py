# backend/run_manager.py
from __future__ import annotations

import os
import shutil
import socket
import asyncio
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import httpx


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
    """

    def __init__(self, base_dir: str = "/tmp/gor-a"):
        self.base_dir = base_dir
        self._runs: Dict[str, RunInfo] = {}
        os.makedirs(self.base_dir, exist_ok=True)

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
        """
        Runs a command and returns (exit_code, combined_output).
        """
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return 124, f"Timeout running: {' '.join(cmd)}"
        return int(proc.returncode or 0), (out_bytes or b"").decode("utf-8", errors="ignore")

    async def _ensure_venv_and_install(self, root: str) -> str:
        """
        If requirements.txt exists, create/use venv and install deps.
        Returns python executable path to use for uvicorn.
        """
        req_path = os.path.join(root, "requirements.txt")
        venv_dir = os.path.join(root, ".venv")

        # Always create venv if missing (cheap + predictable)
        if not os.path.isdir(venv_dir):
            code, out = await self._run_cmd(root, ["python", "-m", "venv", ".venv"], timeout_s=180)
            if code != 0:
                raise RuntimeError(f"Failed to create venv:\n{out}")

        python_bin = os.path.join(venv_dir, "bin", "python")
        pip_bin = [python_bin, "-m", "pip"]

        # Upgrade pip/wheel (helps many installs)
        code, out = await self._run_cmd(root, pip_bin + ["install", "--upgrade", "pip", "setuptools", "wheel"], timeout_s=240)
        if code != 0:
            # not fatal in all cases, but usually indicates broken venv
            raise RuntimeError(f"Failed to upgrade pip:\n{out}")

        if os.path.exists(req_path):
            code, out = await self._run_cmd(root, pip_bin + ["install", "-r", "requirements.txt"], timeout_s=600)
            if code != 0:
                raise RuntimeError(f"requirements.txt install failed:\n{out}")

        return python_bin

    async def start(self, project_id: str, file_tree: Dict[str, str]) -> RunInfo:
        # restart if already running
        await self.stop(project_id)

        root = os.path.join(self.base_dir, project_id)
        if os.path.isdir(root):
            shutil.rmtree(root, ignore_errors=True)
        os.makedirs(root, exist_ok=True)

        # write files
        for path, content in (file_tree or {}).items():
            if not path or path.endswith("/"):
                continue
            abs_path = os.path.join(root, path)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content or "")

        # require app.py
        entry = os.path.join(root, "app.py")
        if not os.path.exists(entry):
            raise RuntimeError("No app.py found in project root. Server preview requires app.py exporting `app`.")

        # Install requirements if provided
        python_bin = await self._ensure_venv_and_install(root)

        port = _pick_free_port()

        # run uvicorn in project venv
        proc = await asyncio.create_subprocess_exec(
            python_bin,
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        info = RunInfo(project_id=project_id, port=port, proc=proc, root_dir=root, python_bin=python_bin)
        self._runs[project_id] = info

        # quick warmup
        await asyncio.sleep(0.6)
        return info

    async def proxy(self, project_id: str, path: str, method: str, headers: dict, body: bytes, query: str) -> httpx.Response:
        running, port = self.is_running(project_id)
        if not running or not port:
            raise RuntimeError("Server not running. Click 'Run server' first.")

        url = f"http://127.0.0.1:{port}/{path.lstrip('/')}"
        if query:
            url += f"?{query}"

        fwd_headers = {}
        for k, v in headers.items():
            lk = k.lower()
            if lk in ("host", "content-length", "connection"):
                continue
            fwd_headers[k] = v

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.request(method, url, headers=fwd_headers, content=body)
            return resp
