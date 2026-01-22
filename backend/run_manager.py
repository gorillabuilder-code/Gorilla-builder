from __future__ import annotations

import os
import json
import asyncio
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import httpx
from dotenv import load_dotenv
from e2b import Sandbox

load_dotenv()

@dataclass
class RunInfo:
    project_id: str
    sandbox: Sandbox
    url: str  # Public HTTPS URL from E2B

class ProjectRunManager:
    """
    E2B Cloud Runner:
      - Runs projects in isolated cloud sandboxes.
      - Proxies traffic from your backend -> E2B -> User App.
    """

    def __init__(self):
        self._runs: Dict[str, RunInfo] = {}
        self.api_key = os.getenv("E2B_API_KEY")
        if not self.api_key:
            print("⚠️ WARNING: E2B_API_KEY not found. Previews will fail.")

    def is_running(self, project_id: str) -> Tuple[bool, Optional[int]]:
        """
        Checks if the sandbox is active.
        Returns (is_running, fake_port). Port is irrelevant in E2B but kept for compatibility.
        """
        info = self._runs.get(project_id)
        if not info:
            return False, None
        
        try:
            if not info.sandbox.is_running():
                self._runs.pop(project_id, None)
                return False, None
        except Exception:
            self._runs.pop(project_id, None)
            return False, None
            
        return True, 80 # Fake port

    async def stop(self, project_id: str) -> None:
        """Kills the sandbox to stop billing."""
        info = self._runs.get(project_id)
        if info:
            try:
                info.sandbox.close()
            except Exception as e:
                print(f"Error closing sandbox: {e}")
            self._runs.pop(project_id, None)

    def _determine_start_command(self, file_tree: Dict[str, str]) -> str:
        """Heuristic to find the correct start command."""
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
        """
        Starts a project in an E2B Sandbox.
        Raises RuntimeError if npm install or startup fails (caught by AI to fix).
        """
        # 1. Cleanup existing run to save money
        await self.stop(project_id)

        print(f"--> [E2B] Creating sandbox for {project_id}...")
        
        # ------------------------------------------------------------
        # FIX IS HERE: Changed 'template=' to 'id=' for new SDK compatibility
        # ------------------------------------------------------------
        try:
            # Try new syntax first
            sandbox = Sandbox(id="nodejs")
        except TypeError:
            # Fallback for older versions just in case (or positional)
            sandbox = Sandbox("nodejs")
        
        try:
            # 2. Write Files
            print(f"--> [E2B] Writing {len(file_tree)} files...")
            for path, content in file_tree.items():
                if not path or path.endswith("/"): continue
                dir_path = os.path.dirname(path)
                if dir_path and dir_path not in [".", ""]:
                    sandbox.filesystem.make_dir(dir_path)
                sandbox.filesystem.write(path, content)

            # 3. Install Dependencies
            if "package.json" in file_tree:
                print(f"--> [E2B] Running npm install...")
                install_proc = sandbox.commands.run("npm install", background=False)
                if install_proc.exit_code != 0:
                    err_log = (install_proc.stderr or "") + "\n" + (install_proc.stdout or "")
                    raise RuntimeError(f"App crashed during 'npm install':\n{err_log}")

            # 4. Start Server
            start_cmd = self._determine_start_command(file_tree)
            print(f"--> [E2B] Starting server with: {start_cmd}")
            
            # Start in background with PORT=3000
            sandbox.commands.run(f"export PORT=3000 && {start_cmd}", background=True)

            # 5. Health Check (Wait for port 3000)
            port_open = False
            for _ in range(10): # Wait up to 10s
                check = sandbox.commands.run("curl -s http://localhost:3000 > /dev/null", background=False)
                if check.exit_code == 0:
                    port_open = True
                    break
                await asyncio.sleep(1)
            
            if not port_open:
                raise RuntimeError(
                    f"Server started using '{start_cmd}' but port 3000 did not open after 10s.\n"
                    "Possible causes: Syntax error, missing module, or app crashed immediately."
                )

            # 6. Get Public URL
            host = sandbox.get_host(3000)
            public_url = f"https://{host}"
            print(f"--> [E2B] Live at {public_url}")
            
            info = RunInfo(project_id=project_id, sandbox=sandbox, url=public_url)
            self._runs[project_id] = info
            return info

        except Exception as e:
            # If startup fails, kill the sandbox so we don't leak money
            sandbox.close()
            raise e

    async def proxy(self, project_id: str, path: str, method: str, headers: dict, body: bytes, query: str) -> httpx.Response:
        """Proxies requests to the E2B Public URL."""
        info = self._runs.get(project_id)
        if not info or not info.sandbox.is_running():
            raise RuntimeError("CRASH_DETECTED: Server not running (Sandbox died).")

        target_url = f"{info.url}/{path.lstrip('/')}"
        if query: target_url += f"?{query}"

        fwd_headers = {k: v for k, v in headers.items() if k.lower() not in ("host", "content-length", "connection")}

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                return await client.request(method, target_url, headers=fwd_headers, content=body)
            except httpx.RequestError as e:
                raise RuntimeError(f"CRASH_DETECTED: Connection failed to E2B sandbox: {e}")