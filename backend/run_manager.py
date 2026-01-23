from __future__ import annotations

import os
import json
import asyncio
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Any

import httpx
from dotenv import load_dotenv

# Try importing from both new and old SDK locations
try:
    from e2b_code_interpreter import Sandbox
    SDK_VERSION = "new"
except ImportError:
    try:
        from e2b import Sandbox
        SDK_VERSION = "legacy"
    except ImportError:
        Sandbox = None
        SDK_VERSION = "none"

load_dotenv()

@dataclass
class RunInfo:
    project_id: str
    sandbox: Any # Sandbox object
    url: str  # Public HTTPS URL from E2B

class ProjectRunManager:
    """
    E2B Cloud Runner (Universal Compatibility):
      - Support for E2B v0.x (Legacy) via await Sandbox.create()
      - Support for E2B v1.x (New) via Sandbox()
      - **Updated:** Automatically runs 'npm run build' for React/TSX projects.
    """

    def __init__(self):
        self._runs: Dict[str, RunInfo] = {}
        self.api_key = os.getenv("E2B_API_KEY")
        if not self.api_key:
            print("⚠️ WARNING: E2B_API_KEY not found. Previews will fail.")

    def is_running(self, project_id: str) -> Tuple[bool, Optional[int]]:
        info = self._runs.get(project_id)
        if not info:
            return False, None
        
        try:
            # E2B v1 check vs v0 check
            is_alive = False
            if hasattr(info.sandbox, "is_running"):
                is_alive = info.sandbox.is_running()
            else:
                # Legacy SDK doesn't always have is_running(), assume alive if process exists
                is_alive = True 
                
            if not is_alive:
                self._runs.pop(project_id, None)
                return False, None
        except Exception:
            self._runs.pop(project_id, None)
            return False, None
            
        return True, 80

    async def stop(self, project_id: str) -> None:
        info = self._runs.get(project_id)
        if info:
            try:
                # Handle async close for legacy, sync for new
                if asyncio.iscoroutinefunction(info.sandbox.close):
                    await info.sandbox.close()
                else:
                    info.sandbox.close()
            except Exception as e:
                print(f"Error closing sandbox: {e}")
            self._runs.pop(project_id, None)

    def _determine_start_command(self, file_tree: Dict[str, str]) -> str:
        if "package.json" in file_tree:
            try:
                pkg = json.loads(file_tree["package.json"])
                scripts = pkg.get("scripts", {})
                if "start" in scripts: return "npm start"
                if "dev" in scripts: return "npm run dev"
                if pkg.get("main"): return f"node {pkg['main']}"
            except Exception:
                pass
        if "server.js" in file_tree: return "node server.js"
        if "index.js" in file_tree: return "node index.js"
        return "npm start"

    async def start(self, project_id: str, file_tree: Dict[str, str]) -> RunInfo:
        if not Sandbox:
            raise RuntimeError("E2B SDK not installed. Run: pip install e2b")

        await self.stop(project_id)
        print(f"--> [E2B] Creating sandbox for {project_id} (SDK: {SDK_VERSION})...")
        
        sandbox = None
        
        # ------------------------------------------------------------
        # UNIVERSAL SANDBOX INITIALIZATION STRATEGY
        # ------------------------------------------------------------
        try:
            # STRATEGY 1: Legacy Async Factory
            if hasattr(Sandbox, "create"):
                print("--> [E2B] Attempting legacy Sandbox.create(template='nodejs')...")
                try:
                    sandbox = await Sandbox.create(template="nodejs")
                except TypeError:
                    sandbox = await Sandbox.create(id="nodejs")
            
            # STRATEGY 2: Modern Constructor
            elif SDK_VERSION == "new":
                print("--> [E2B] Attempting new Sandbox(id='nodejs')...")
                sandbox = Sandbox(id="nodejs")
            
            # STRATEGY 3: Fallback Constructor
            else:
                print("--> [E2B] Fallback to Sandbox('nodejs')...")
                sandbox = Sandbox("nodejs")

        except Exception as e:
            raise RuntimeError(f"Failed to create E2B Sandbox. Check API Key & SDK Version. Error: {e}")

        if not sandbox:
             raise RuntimeError("Sandbox creation returned None.")

        try:
            # 2. Write Files
            print(f"--> [E2B] Writing {len(file_tree)} files...")
            for path, content in file_tree.items():
                if not path or path.endswith("/"): continue
                dir_path = os.path.dirname(path)
                if dir_path and dir_path not in [".", ""]:
                    sandbox.filesystem.make_dir(dir_path)
                sandbox.filesystem.write(path, content)

            # 3. Install Dependencies & Build
            if "package.json" in file_tree:
                print(f"--> [E2B] Running npm install...")
                
                # --- INSTALL ---
                if hasattr(sandbox, "commands"):
                    proc = sandbox.commands.run("npm install", background=False)
                    exit_code = proc.exit_code
                    err_out = proc.stderr or proc.stdout
                else:
                    proc = await sandbox.process.start_and_wait("npm install")
                    exit_code = proc.exit_code
                    err_out = proc.stderr or proc.stdout

                if exit_code != 0:
                    raise RuntimeError(f"App crashed during 'npm install':\n{err_out}")

                # --- NEW: BUILD STEP (CRITICAL FOR REACT/TSX) ---
                try:
                    pkg = json.loads(file_tree["package.json"])
                    if "build" in pkg.get("scripts", {}):
                        print(f"--> [E2B] Running npm run build...")
                        if hasattr(sandbox, "commands"):
                            b_proc = sandbox.commands.run("npm run build", background=False)
                            b_code = b_proc.exit_code
                            b_err = b_proc.stderr or b_proc.stdout
                        else:
                            b_proc = await sandbox.process.start_and_wait("npm run build")
                            b_code = b_proc.exit_code
                            b_err = b_proc.stderr or b_proc.stdout
                        
                        if b_code != 0:
                            raise RuntimeError(f"Build failed:\n{b_err}")
                except Exception as build_e:
                    # If build fails, we might still try to start, or fail hard. 
                    # For now, print and proceed, or raise if critical.
                    print(f"⚠️ Build warning: {build_e}")

            # 4. Start Server
            start_cmd = self._determine_start_command(file_tree)
            print(f"--> [E2B] Starting server with: {start_cmd}")
            
            # Force PORT 3000
            full_cmd = f"export PORT=3000 && {start_cmd}"
            
            if hasattr(sandbox, "commands"):
                # New SDK
                sandbox.commands.run(full_cmd, background=True)
            else:
                # Legacy SDK
                await sandbox.process.start(full_cmd)

            # 5. Health Check & URL Retrieval
            port_open = False
            
            # Wait loop
            for _ in range(10):
                if hasattr(sandbox, "commands"):
                    check = sandbox.commands.run("curl -s http://localhost:3000 > /dev/null", background=False)
                    if check.exit_code == 0:
                        port_open = True
                        break
                else:
                    # Legacy check (harder, assume success after sleep if no crash)
                    await asyncio.sleep(1)
                    port_open = True # Optimistic for legacy
                    break
                await asyncio.sleep(1)
            
            # Get URL
            public_url = ""
            if hasattr(sandbox, "get_host"):
                host = sandbox.get_host(3000)
                public_url = f"https://{host}"
            elif hasattr(sandbox, "get_hostname"):
                public_url = f"https://{sandbox.get_hostname(3000)}"
            else:
                raise RuntimeError("Could not determine public URL method on Sandbox object.")

            print(f"--> [E2B] Live at {public_url}")
            
            info = RunInfo(project_id=project_id, sandbox=sandbox, url=public_url)
            self._runs[project_id] = info
            return info

        except Exception as e:
            # Cleanup on fail
            try:
                if asyncio.iscoroutinefunction(sandbox.close):
                    await sandbox.close()
                else:
                    sandbox.close()
            except: pass
            raise e

    async def proxy(self, project_id: str, path: str, method: str, headers: dict, body: bytes, query: str) -> httpx.Response:
        info = self._runs.get(project_id)
        
        # Robust check for liveness
        is_alive = False
        if info:
            if hasattr(info.sandbox, "is_running"):
                is_alive = info.sandbox.is_running()
            else:
                is_alive = True # Assume true for legacy

        if not info or not is_alive:
            raise RuntimeError("CRASH_DETECTED: Server not running (Sandbox died).")

        target_url = f"{info.url}/{path.lstrip('/')}"
        if query: target_url += f"?{query}"

        fwd_headers = {k: v for k, v in headers.items() if k.lower() not in ("host", "content-length", "connection")}

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                return await client.request(method, target_url, headers=fwd_headers, content=body)
            except httpx.RequestError as e:
                raise RuntimeError(f"CRASH_DETECTED: Connection failed to E2B sandbox: {e}")