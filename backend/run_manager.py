from __future__ import annotations

import os
import shutil
import socket
import asyncio
import sys
import json
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import httpx
from dotenv import dotenv_values 

# --------------------------------------------------------------------------
# INJECTED SERVER SCRIPT (PYTHON ONLY)
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
    runtime_bin: str # python path or 'node'

class ProjectRunManager:
    """
    Hybrid Runner:
      - Detects Python (requirements.txt) vs Node.js (package.json)
      - Python: Installs venv, runs injected server_entry.py
      - Node: Runs npm install, runs npm start (or node entry)
      - Proxies traffic to localhost:PORT
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

    # --- PYTHON SETUP ---
    async def _setup_python(self, root: str) -> str:
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

        # 3. Core Deps
        for attempt in range(2):
            code, out = await self._run_cmd(root, pip_bin + ["install", "--upgrade", "pip", "uvicorn", "fastapi", "python-dotenv", "aiofiles", "jinja2", "multipart"], timeout_s=300)
            if code == 0: break
            
            if attempt == 0:
                print(f"⚠️ Venv issue. Recreating at {venv_dir}...")
                shutil.rmtree(venv_dir, ignore_errors=True)
                await self._run_cmd(root, [curr_python, "-m", "venv", ".venv"], timeout_s=180)
            else:
                raise RuntimeError(f"Failed to install core dependencies:\n{out}")

        # 4. User Reqs
        if os.path.exists(req_path):
            code, out = await self._run_cmd(root, pip_bin + ["install", "-r", "requirements.txt"], timeout_s=600)
            if code != 0: 
                print(f"⚠️ Warning: requirements.txt install had issues:\n{out}")

        # 5. Inject Runner
        server_entry_path = os.path.join(root, "server_entry.py")
        with open(server_entry_path, "w", encoding="utf-8") as f:
            f.write(SERVER_ENTRY_SCRIPT)

        return python_bin

    # --- NODE.JS SETUP ---
    async def _setup_node(self, root: str) -> list[str]:
        """Runs npm install and returns the start command."""
        pkg_path = os.path.join(root, "package.json")
        
        # 1. Install Dependencies
        if os.path.exists(pkg_path):
            print("--> Running npm install...")
            code, out = await self._run_cmd(root, ["npm", "install"], timeout_s=400)
            if code != 0:
                raise RuntimeError(f"npm install failed:\n{out}")
        
        # 2. Determine Start Command
        try:
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
            
        raise RuntimeError("Could not determine Node.js entry point (no 'start' script, server.js, or index.js).")

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

        # 2. Detect Runtime
        is_node = os.path.exists(os.path.join(root, "package.json"))
        
        start_cmd = []
        runtime_bin = ""

        # 3. Setup Environment
        if is_node:
            runtime_bin = "node"
            start_cmd = await self._setup_node(root)
        else:
            # Assume Python if no package.json
            if not os.path.exists(os.path.join(root, "app.py")):
                raise RuntimeError("Invalid project structure: No app.py (Python) or package.json (Node) found.")
            
            python_bin = await self._setup_python(root)
            runtime_bin = python_bin
            start_cmd = [python_bin, "server_entry.py"]

        port = _pick_free_port()

        # 4. Prepare Env Vars
        env_vars = os.environ.copy()
        # Remove sensitive keys from host
        sensitive_keys = [
            "FIREWORKS_API_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_URL",
            "RESEND_API_KEY", "AUTH_SECRET_KEY", "GROQ_API_KEY"
        ]
        for k in sensitive_keys: env_vars.pop(k, None)

        env_vars.update(self.injected_secrets)
        env_vars["PYTHONUNBUFFERED"] = "1"
        env_vars["PORT"] = str(port) # Both Uvicorn and standard Node apps listen on PORT

        # 5. Launch Process
        print(f"--> Launching {project_id} on port {port} using {start_cmd}...")
        proc = await asyncio.create_subprocess_exec(
            *start_cmd, 
            cwd=root, env=env_vars,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        info = RunInfo(project_id=project_id, port=port, proc=proc, root_dir=root, runtime_bin=runtime_bin)
        self._runs[project_id] = info

        # 6. Health Check
        startup_error = None
        for _ in range(60): 
            await asyncio.sleep(0.5)
            
            if proc.returncode is not None:
                # Died
                raw_err = await proc.stderr.read()
                raw_out = await proc.stdout.read()
                full_log = (raw_out.decode() + "\n" + raw_err.decode()).strip()
                
                # Check for common errors
                if "CRITICAL:" in full_log:
                    startup_error = full_log
                elif "Error:" in full_log:
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
            clean_err = startup_error
            if "CRITICAL:" in clean_err:
                try: clean_err = clean_err.split("="*40)[1].strip()
                except: pass
            raise RuntimeError(f"App crashed during startup:\n{clean_err}")
        
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