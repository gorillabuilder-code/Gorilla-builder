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

# --------------------------------------------------------------------------
# INJECTED SERVER SCRIPT
# This script handles imports safely and catches SyntaxErrors (truncation)
# --------------------------------------------------------------------------
SERVER_ENTRY_SCRIPT = """
import sys
import os
import traceback

# Ensure the project directory is in python path
sys.path.insert(0, os.getcwd())

def run():
    try:
        print("--> Attempting to import app.py...")
        # 1. Attempt Import (This catches SyntaxErrors from AI truncation)
        from app import app
        import uvicorn
        
        # 2. Start Server
        port = int(os.environ.get("PORT", 8000))
        print(f"--> Starting Uvicorn on port {port}...")
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
        
    except SyntaxError as e:
        print("\\n" + "="*40)
        print("CRITICAL: APP.PY SYNTAX ERROR (LIKELY TRUNCATION)")
        print(f"File: {e.filename}, Line: {e.lineno}")
        print(f"Error: {e.msg}")
        print("="*40 + "\\n")
        sys.exit(1)
        
    except ImportError as e:
        print("\\n" + "="*40)
        print(f"CRITICAL: IMPORT ERROR ({e})")
        print("Did the AI forget to install a library?")
        print("="*40 + "\\n")
        sys.exit(1)
        
    except Exception:
        print("\\n" + "="*40)
        print("CRITICAL: RUNTIME CRASH")
        traceback.print_exc()
        print("="*40 + "\\n")
        sys.exit(1)

if __name__ == "__main__":
    run()
"""

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
      - INJECTS `server_entry.py` to handle startup safely
      - runs `python server_entry.py` instead of `python -m uvicorn`
      - proxies via FastAPI endpoint (in app.py)
    """

    def __init__(self, base_dir: str = "/tmp/gor-a"):
        self.base_dir = base_dir
        self._runs: Dict[str, RunInfo] = {}
        os.makedirs(self.base_dir, exist_ok=True)

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

    async def _ensure_venv_and_install(self, root: str) -> str:
        req_path = os.path.join(root, "requirements.txt")
        venv_dir = os.path.join(root, ".venv")
        curr_python = sys.executable

        # 1. Create Venv
        if not os.path.isdir(venv_dir):
            code, out = await self._run_cmd(root, [curr_python, "-m", "venv", ".venv"], timeout_s=180)
            if code != 0: raise RuntimeError(f"Failed to create venv:\n{out}")

        # 2. Find Python Binary
        if os.name == "nt":
            python_bin = os.path.join(venv_dir, "Scripts", "python.exe")
        else:
            python_bin = os.path.join(venv_dir, "bin", "python")
        
        if not os.path.exists(python_bin):
             python_bin = os.path.join(venv_dir, "bin", "python3")

        pip_bin = [python_bin, "-m", "pip"]

        # 3. Core Deps (Uvicorn/FastAPI/Dotenv)
        # We try twice. If it fails, we nuke the venv.
        for attempt in range(2):
            # Note: We install standard uvicorn, not the module version logic
            code, out = await self._run_cmd(root, pip_bin + ["install", "--upgrade", "pip", "uvicorn", "fastapi", "python-dotenv", "aiofiles", "jinja2", "multipart"], timeout_s=300)
            
            if code == 0:
                break
            
            if attempt == 0:
                print(f"⚠️ Venv issue. Recreating at {venv_dir}...")
                shutil.rmtree(venv_dir, ignore_errors=True)
                await self._run_cmd(root, [curr_python, "-m", "venv", ".venv"], timeout_s=180)
            else:
                raise RuntimeError(f"Failed to install core dependencies:\n{out}")

        # 4. User Reqs
        if os.path.exists(req_path):
            code, out = await self._run_cmd(root, pip_bin + ["install", "-r", "requirements.txt"], timeout_s=600)
            # We log but don't crash on user reqs failure (sometimes they ask for bad versions)
            if code != 0: 
                print(f"⚠️ Warning: requirements.txt install had issues:\n{out}")

        return python_bin

    async def start(self, project_id: str, file_tree: Dict[str, str]) -> RunInfo:
        await self.stop(project_id)

        root = os.path.join(self.base_dir, project_id)
        os.makedirs(root, exist_ok=True)

        # 1. Write User Files
        for path, content in (file_tree or {}).items():
            if not path or path.endswith("/"): continue
            abs_path = os.path.join(root, path)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f: f.write(content or "")

        # 2. Verify app.py
        entry = os.path.join(root, "app.py")
        if not os.path.exists(entry):
            raise RuntimeError("No app.py found.")

        # 3. Inject SERVER_ENTRY.PY (Method 2)
        server_entry_path = os.path.join(root, "server_entry.py")
        with open(server_entry_path, "w", encoding="utf-8") as f:
            f.write(SERVER_ENTRY_SCRIPT)

        # 4. Setup Venv
        python_bin = await self._ensure_venv_and_install(root)
        port = _pick_free_port()

        # 5. Env Vars
        env_vars = os.environ.copy()
        
        sensitive_keys = [
            "FIREWORKS_API_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_URL",
            "RESEND_API_KEY", "AUTH_SECRET_KEY", "GROQ_API_KEY",
            "GOOGLE_CLIENT_SECRET", "GOOGLE_CLIENT_ID", "FIREWORKS_URL"
        ]
        for k in sensitive_keys:
            env_vars.pop(k, None)

        env_vars.update(self.injected_secrets)
        env_vars["PYTHONUNBUFFERED"] = "1"
        env_vars["PORT"] = str(port)

        # 6. RUN THE INJECTED SCRIPT (Not -m uvicorn)
        proc = await asyncio.create_subprocess_exec(
            python_bin, "server_entry.py",  # <--- MAGIC FIX
            cwd=root, env=env_vars,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        info = RunInfo(project_id=project_id, port=port, proc=proc, root_dir=root, python_bin=python_bin)
        self._runs[project_id] = info

        # 7. Health Check
        startup_error = None
        for _ in range(60): 
            await asyncio.sleep(0.5)
            
            if proc.returncode is not None:
                # Process died. Read stderr to see why.
                raw_err = await proc.stderr.read()
                raw_out = await proc.stdout.read() # Sometimes error is in stdout due to our script
                
                full_log = (raw_out.decode() + "\n" + raw_err.decode()).strip()
                
                # Check for our custom markers
                if "CRITICAL: APP.PY SYNTAX ERROR" in full_log:
                    startup_error = full_log
                elif "frozen runpy" in full_log:
                     startup_error = "System Error: Python environment corrupted. Please retry."
                else:
                    startup_error = full_log or "Unknown crash."
                break
            
            try:
                async with httpx.AsyncClient(timeout=1.0) as client:
                    await client.get(f"http://127.0.0.1:{port}/")
                return info
            except (httpx.ConnectError, httpx.ReadTimeout):
                continue
        
        if startup_error:
            # Clean up the error message for the user
            clean_err = startup_error
            if "CRITICAL:" in clean_err:
                # Extract just the critical part
                try:
                    clean_err = clean_err.split("="*40)[1].strip()
                except:
                    pass
            raise RuntimeError(f"App crashed during startup:\n{clean_err}")
        
        await self.stop(project_id)
        raise RuntimeError("Server started but timed out (port not reachable).")

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