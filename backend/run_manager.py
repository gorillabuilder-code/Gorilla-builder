from __future__ import annotations

import os
import json
import asyncio
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Any

import httpx
from dotenv import load_dotenv

# --------------------------------------------------------------------------
# E2B SDK COMPATIBILITY LAYER
# --------------------------------------------------------------------------
SDK_VERSION = "none"
Sandbox = None

# 1. Try New SDK (v1.0+)
try:
    from e2b_code_interpreter import Sandbox
    SDK_VERSION = "new"
except ImportError:
    # 2. Try Legacy SDK
    try:
        from e2b import Sandbox
        SDK_VERSION = "legacy"
    except ImportError:
        pass

load_dotenv()

@dataclass
class RunInfo:
    project_id: str
    sandbox: Any 
    url: str 

class ProjectRunManager:
    """
    E2B Cloud Runner (Universal Compatibility + Vite Support)
    """

    def __init__(self):
        self._runs: Dict[str, RunInfo] = {}
        self.api_key = os.getenv("E2B_API_KEY")
        if not self.api_key:
            print("⚠️ WARNING: E2B_API_KEY not found. Previews will fail.")

    def is_running(self, project_id: str) -> Tuple[bool, Optional[int]]:
        info = self._runs.get(project_id)
        if not info: return False, None
        
        try:
            # Check liveness based on SDK version
            if hasattr(info.sandbox, "is_running"):
                # New SDK requires explicit method call
                if not info.sandbox.is_running():
                    self._runs.pop(project_id, None)
                    return False, None
            return True, 3000
        except Exception:
            self._runs.pop(project_id, None)
            return False, None

    async def stop(self, project_id: str) -> None:
        info = self._runs.get(project_id)
        if info:
            print(f"🛑 Stopping sandbox {project_id}...")
            try:
                # Handle both Async and Sync close methods safely
                if asyncio.iscoroutinefunction(info.sandbox.close):
                    await info.sandbox.close()
                else:
                    # Run sync close in thread to avoid blocking loop
                    await asyncio.to_thread(info.sandbox.close)
            except Exception as e:
                print(f"Error closing sandbox: {e}")
            self._runs.pop(project_id, None)

    def _determine_start_command(self, file_tree: Dict[str, str]) -> str:
        """
        Smart detection for Vite/React/Node scripts.
        """
        if "package.json" in file_tree:
            try:
                pkg = json.loads(file_tree["package.json"])
                scripts = pkg.get("scripts", {})
                
                # VITE / REACT SUPPORT
                # We must force host binding and port 3000 so E2B exposes it
                if "dev" in scripts: 
                    return "npm run dev -- --port 3000 --host"
                
                if "start" in scripts: return "npm start"
                if pkg.get("main"): return f"node {pkg['main']}"
            except: pass
            
        if "server.js" in file_tree: return "node server.js"
        if "index.js" in file_tree: return "node index.js"
        return "npm start"

    async def start(self, project_id: str, file_tree: Dict[str, str]) -> RunInfo:
        if not Sandbox:
            raise RuntimeError("E2B SDK not installed. Run: pip install e2b-code-interpreter")

        await self.stop(project_id)
        print(f"--> [E2B] Creating sandbox for {project_id} (SDK: {SDK_VERSION})...")
        
        sandbox = None
        
        # --- 1. ROBUST SANDBOX INITIALIZATION ---
        try:
            if SDK_VERSION == "new":
                # NEW SDK (v1.x): Synchronous Constructor
                # CRITICAL FIX: We run the class constructor in a thread.
                print(f"--> [E2B] Initializing new Sandbox()...")
                sandbox = await asyncio.to_thread(Sandbox)
            
            elif SDK_VERSION == "legacy":
                # LEGACY SDK: Async factory
                if hasattr(Sandbox, "create"):
                    print(f"--> [E2B] Initializing legacy Sandbox.create()...")
                    sandbox = await Sandbox.create(template="base")
                else:
                    sandbox = Sandbox(template="base")
            else:
                raise RuntimeError("Unknown SDK version state")

        except Exception as e:
            err_str = str(e)
            if "401" in err_str:
                raise RuntimeError("E2B API Key is invalid or missing.")
            raise RuntimeError(f"Failed to create E2B Sandbox: {err_str}")

        if not sandbox:
            raise RuntimeError("Sandbox creation returned None.")

        # --- 2. UPLOAD & RUN ---
        try:
            # Write Files
            print(f"--> [E2B] Writing {len(file_tree)} files...")
            
            # Detect filesystem (New vs Old SDK)
            fs = getattr(sandbox, "files", getattr(sandbox, "filesystem", None))
            if not fs: raise RuntimeError("Could not find filesystem on Sandbox object")

            for path, content in file_tree.items():
                if not path or path.endswith("/"): continue
                dir_path = os.path.dirname(path)
                if dir_path and dir_path not in [".", ""]:
                    try: fs.make_dir(dir_path)
                    except: pass
                fs.write(path, content)

            # --- HELPER: Unified Command Runner ---
            async def run_cmd(c, background=False):
                if hasattr(sandbox, "commands"): 
                    # NEW SDK (Sync methods) -> Wrap in thread
                    print(f"    [Exec] {c}")
                    if background:
                         await asyncio.to_thread(sandbox.commands.run, c, background=True)
                         return 0, ""
                    res = await asyncio.to_thread(sandbox.commands.run, c, background=False)
                    return res.exit_code, (res.stderr or res.stdout)
                else: 
                    # LEGACY SDK (Async methods) -> Await directly
                    print(f"    [Exec Legacy] {c}")
                    if background:
                        await sandbox.process.start(c)
                        return 0, ""
                    proc = await sandbox.process.start_and_wait(c)
                    return proc.exit_code, (proc.stderr or proc.stdout)

            # Install Dependencies
            if "package.json" in file_tree:
                print(f"--> [E2B] Running npm install...")
                code, err = await run_cmd("npm install")
                if code != 0:
                    print(f"⚠️ npm install warning: {err[:200]}")

                # Build (Best Effort)
                pkg = json.loads(file_tree["package.json"])
                if "build" in pkg.get("scripts", {}):
                    print(f"--> [E2B] Running npm run build...")
                    await run_cmd("npm run build")

            # Start Server
            start_cmd = self._determine_start_command(file_tree)
            print(f"--> [E2B] Starting server with: {start_cmd}")
            
            # Capture Logs
            full_cmd = f"export PORT=3000 && {start_cmd} > server.log 2> server_err.txt"
            
            # Start in background
            await run_cmd(full_cmd, background=True)

            # --- [FIXED] LENIENT HEALTH CHECK (NO -f FLAG) ---
            print(f"--> [E2B] Waiting for port 3000...")
            port_open = False
            for _ in range(20):
                # REMOVED "-f". We accept 404s as "Running".
                # If curl connects, exit code is 0 (Success).
                # If curl fails to connect (server down), exit code is non-zero.
                code, _ = await run_cmd("curl -s http://localhost:3000")
                if code == 0:
                    port_open = True
                    break
                await asyncio.sleep(1)
            
            if not port_open:
                print("--> [E2B] Server failed to start. Reading logs...")
                _, error_log = await run_cmd("cat server_err.txt")
                _, output_log = await run_cmd("cat server.log")
                
                full_log = (error_log or "") + "\n" + (output_log or "")
                
                # Raise the specific error so app.py can catch it
                raise RuntimeError(f"App crashed during startup: {full_log.strip()[:1000]}")

            # Get URL
            public_url = ""
            if hasattr(sandbox, "get_host"): 
                public_url = f"https://{sandbox.get_host(3000)}"
            elif hasattr(sandbox, "get_hostname"):
                public_url = f"https://{sandbox.get_hostname(3000)}"
            else:
                public_url = "https://placeholder-url-error.com"

            print(f"--> [E2B] Live at {public_url}")
            
            info = RunInfo(project_id=project_id, sandbox=sandbox, url=public_url)
            self._runs[project_id] = info
            return info

        except Exception as e:
            # Cleanup on fail
            try:
                if asyncio.iscoroutinefunction(sandbox.close): await sandbox.close()
                else: await asyncio.to_thread(sandbox.close)
            except: pass
            raise e

    async def proxy(self, project_id: str, path: str, method: str, headers: dict, body: bytes, query: str) -> httpx.Response:
        info = self._runs.get(project_id)
        if not info:
            raise RuntimeError("CRASH_DETECTED: Server not running.")

        target_url = f"{info.url}/{path.lstrip('/')}"
        if query: target_url += f"?{query}"

        fwd_headers = {k: v for k, v in headers.items() if k.lower() not in ("host", "content-length", "connection")}

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                return await client.request(method, target_url, headers=fwd_headers, content=body)
            except httpx.RequestError as e:
                raise RuntimeError(f"CRASH_DETECTED: Connection failed: {e}")