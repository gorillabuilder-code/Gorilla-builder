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
Sandbox = None
SDK_MODE = "unknown"

try:
    from e2b_code_interpreter import Sandbox
    SDK_MODE = "new"
except ImportError:
    try:
        from e2b import Sandbox
        SDK_MODE = "legacy"
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
    Singleton Manager for E2B Sandboxes.
    Keeps sandboxes ALIVE even if the app crashes, allowing hot-patching.
    """

    def __init__(self):
        self._runs: Dict[str, RunInfo] = {}
        self.api_key = os.getenv("E2B_API_KEY")

    def get_run_info(self, project_id: str) -> Optional[RunInfo]:
        return self._runs.get(project_id)
        
    def is_running(self, project_id: str) -> Tuple[bool, Optional[int]]:
        """
        Checks if a project has an active sandbox.
        Returns (True, 3000) if active, (False, None) if not.
        """
        if project_id in self._runs:
            return True, 3000
        return False, None

    async def run_command(self, project_id: str, cmd: str, background: bool = False) -> Tuple[int, str]:
        info = self._runs.get(project_id)
        if not info:
            raise RuntimeError(f"Sandbox not active for {project_id}")
        
        sb = info.sandbox
        print(f"    [Exec] {cmd}")
        
        try:
            if hasattr(sb, "commands"): # New SDK
                if background:
                    await asyncio.to_thread(sb.commands.run, cmd, background=True)
                    return 0, ""
                res = await asyncio.to_thread(sb.commands.run, cmd)
                return res.exit_code, (res.stderr or res.stdout)
            else: # Legacy SDK
                if background:
                    await sb.process.start(cmd)
                    return 0, ""
                proc = await sb.process.start_and_wait(cmd)
                return proc.exit_code, (proc.stderr or proc.stdout)
        except Exception as e:
            return 1, str(e)

    async def write_file(self, project_id: str, path: str, content: str):
        """Hot-patches a file into the running sandbox."""
        info = self._runs.get(project_id)
        if not info: 
            print(f"⚠️ Cannot hot-patch {project_id}: Sandbox dead.")
            return

        sb = info.sandbox
        fs = getattr(sb, "files", getattr(sb, "filesystem", None))
        
        if fs:
            try:
                d = os.path.dirname(path)
                if d and d not in [".", ""]:
                    try: fs.make_dir(d)
                    except: pass
                fs.write(path, content)
                print(f"✅ Hot-patched: {path}")
            except Exception as e:
                print(f"⚠️ Write failed for {path}: {e}")

    async def stop(self, project_id: str) -> None:
        info = self._runs.pop(project_id, None)
        if info:
            try:
                if asyncio.iscoroutinefunction(info.sandbox.close):
                    await info.sandbox.close()
                else:
                    await asyncio.to_thread(info.sandbox.close)
            except: pass

    def _determine_start_command(self, file_tree: Dict[str, str]) -> str:
        if "package.json" in file_tree:
            try:
                pkg = json.loads(file_tree["package.json"])
                scripts = pkg.get("scripts", {})
                if "dev" in scripts: return "npm run dev -- --port 3000 --host"
                if "start" in scripts: return "npm start"
                if pkg.get("main"): return f"node {pkg['main']}"
            except: pass
        if "server.js" in file_tree: return "node server.js"
        return "npm start"

    async def start(self, project_id: str, file_tree: Dict[str, str]) -> RunInfo:
        # 1. REUSE EXISTING
        if project_id in self._runs:
            return self._runs[project_id]

        if not Sandbox: raise RuntimeError("E2B SDK missing")

        print(f"--> [RunManager] Booting NEW sandbox for {project_id}...")
        sandbox = None

        try:
            # 2. CREATE VM
            if SDK_MODE == "new":
                sandbox = await asyncio.to_thread(Sandbox)
            else:
                try: sandbox = await Sandbox.create(template="base")
                except: sandbox = await Sandbox.create()

            # 3. SYNC FILES
            fs = getattr(sandbox, "files", getattr(sandbox, "filesystem", None))
            for path, content in file_tree.items():
                if not path or path.endswith("/"): continue
                d = os.path.dirname(path)
                if d: 
                    try: fs.make_dir(d)
                    except: pass
                fs.write(path, content)

            # 4. NPM INSTALL
            if "package.json" in file_tree:
                print(f"--> [RunManager] npm install...")
                try:
                    if hasattr(sandbox, "commands"):
                        await asyncio.to_thread(sandbox.commands.run, "npm install")
                    else:
                        await sandbox.process.start_and_wait("npm install")
                except Exception as e:
                    print(f"⚠️ npm install warning: {e}")

            # 5. START SERVER
            start_cmd = self._determine_start_command(file_tree)
            fw_key = os.getenv("FIREWORKS_API_KEY", "")
            full_cmd = f"export PORT=3000 && export FIREWORKS_API_KEY='{fw_key}' && {start_cmd} > server.log 2> server_err.txt"
            
            print(f"--> [RunManager] Starting process: {start_cmd}")
            
            if hasattr(sandbox, "commands"):
                await asyncio.to_thread(sandbox.commands.run, full_cmd, background=True)
            else:
                await sandbox.process.start(full_cmd)

            # 6. REGISTER NOW (Crucial for Hot Patching)
            # We register BEFORE health check. If health check fails, the sandbox is still "active" 
            # so we can fix it.
            info = RunInfo(project_id=project_id, sandbox=sandbox, url="http://localhost:3000")
            self._runs[project_id] = info

            # 7. HEALTH CHECK
            print(f"--> [RunManager] Waiting for port 3000...")
            is_up = False
            for _ in range(30):
                try:
                    # Robust check that doesn't crash on connection refused
                    if hasattr(sandbox, "commands"):
                        res = await asyncio.to_thread(sandbox.commands.run, "curl -s http://localhost:3000")
                        if res.exit_code == 0: is_up = True; break
                    else:
                        proc = await sandbox.process.start_and_wait("curl -s http://localhost:3000")
                        if proc.exit_code == 0: is_up = True; break
                except:
                    pass
                await asyncio.sleep(1)

            if not is_up:
                # Retrieve logs
                try:
                    log_cmd = "cat server_err.txt"
                    if hasattr(sandbox, "commands"):
                        res = await asyncio.to_thread(sandbox.commands.run, log_cmd)
                        logs = res.stderr or res.stdout
                    else:
                        proc = await sandbox.process.start_and_wait(log_cmd)
                        logs = proc.stderr or proc.stdout
                except: logs = "No logs available."
                
                # RAISE ERROR, BUT KEEP SANDBOX ALIVE (It's already registered)
                raise RuntimeError(f"App crashed during startup: {str(logs)[:1000]}")

            print(f"--> [RunManager] Sandbox READY for {project_id}")
            return info

        except Exception as e:
            # Only kill if we failed to even CREATE the sandbox or register it
            # If we registered it, we keep it alive for debugging/patching
            if project_id not in self._runs and sandbox:
                try: 
                    if asyncio.iscoroutinefunction(sandbox.close): await sandbox.close()
                    else: await asyncio.to_thread(sandbox.close)
                except: pass
            raise e

run_manager = ProjectRunManager()