from __future__ import annotations

import os
import json
import asyncio
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Any

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
      - Runs projects in isolated cloud sandboxes (Firecracker micro-VMs).
      - Relays build/runtime errors back to the Python backend for AI Auto-Fixing.
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
        """Kills the sandbox."""
        info = self._runs.get(project_id)
        if info:
            try:
                info.sandbox.close()
            except Exception as e:
                print(f"Error closing sandbox: {e}")
            self._runs.pop(project_id, None)

    def _determine_start_command(self, file_tree: Dict[str, str]) -> str:
        """Heuristic to find the correct start command from the file tree."""
        # 1. Check package.json for "start" script
        if "package.json" in file_tree:
            try:
                pkg = json.loads(file_tree["package.json"])
                scripts = pkg.get("scripts", {})
                if "start" in scripts:
                    return "npm start"
                if "dev" in scripts:
                    return "npm run dev"
                if pkg.get("main"):
                    return f"node {pkg['main']}"
            except Exception:
                pass
        
        # 2. Check for common entry points
        if "server.js" in file_tree:
            return "node server.js"
        if "index.js" in file_tree:
            return "node index.js"
        if "app.js" in file_tree:
            return "node app.js"
            
        # 3. Default fallback
        return "npm start"

    async def start(self, project_id: str, file_tree: Dict[str, str]) -> RunInfo:
        """
        Starts a project in an E2B Sandbox.
        Raises RuntimeError if npm install or startup fails (caught by AI to fix).
        """
        # 1. Cleanup existing run
        await self.stop(project_id)

        print(f"--> [E2B] Creating sandbox for {project_id}...")
        
        # 2. Create Sandbox (Node.js environment)
        # We use the standard 'Nodejs' template
        sandbox = Sandbox(template="nodejs")
        
        try:
            # 3. Write Files
            # E2B accepts file writes. We need to ensure directories exist.
            print(f"--> [E2B] Writing {len(file_tree)} files...")
            
            for path, content in file_tree.items():
                if not path or path.endswith("/"): continue
                
                # Ensure directory exists
                dir_path = os.path.dirname(path)
                if dir_path and dir_path not in [".", ""]:
                    sandbox.filesystem.make_dir(dir_path)
                
                # Write file
                sandbox.filesystem.write(path, content)

            # 4. Install Dependencies
            # Only run if package.json exists
            if "package.json" in file_tree:
                print(f"--> [E2B] Running npm install...")
                install_proc = sandbox.commands.run("npm install", background=False)
                
                if install_proc.exit_code != 0:
                    # CAPTURE ERROR FOR AI
                    err_log = (install_proc.stderr or "") + "\n" + (install_proc.stdout or "")
                    raise RuntimeError(f"App crashed during 'npm install':\n{err_log}")

            # 5. Start Server
            start_cmd = self._determine_start_command(file_tree)
            print(f"--> [E2B] Starting server with: {start_cmd}")
            
            # Start in background. We force PORT=3000 convention.
            sandbox.commands.run(f"export PORT=3000 && {start_cmd}", background=True)

            # 6. Health Check (Wait for port 3000)
            # Give it up to 10 seconds to bind the port
            port_open = False
            error_log = ""
            
            for i in range(10):
                # Try to curl localhost inside the VM
                check = sandbox.commands.run("curl -s http://localhost:3000 > /dev/null", background=False)
                if check.exit_code == 0:
                    port_open = True
                    break
                await asyncio.sleep(1)
            
            if not port_open:
                # It failed to start. Try to grab logs?
                # (E2B doesn't persist background logs easily in v1 SDK without streaming)
                # We assume a crash.
                raise RuntimeError(
                    f"Server started using '{start_cmd}' but port 3000 did not open after 10s.\n"
                    "Possible causes: Syntax error, missing module, or app crashed immediately.\n"
                    "Check imports and package.json."
                )

            # 7. Get Public URL
            # E2B provides a hostname that tunnels to the sandbox port 3000
            host = sandbox.get_host(3000)
            public_url = f"https://{host}"
            
            print(f"--> [E2B] Live at {public_url}")
            
            info = RunInfo(
                project_id=project_id,
                sandbox=sandbox,
                url=public_url
            )
            self._runs[project_id] = info
            return info

        except Exception as e:
            # If startup fails, kill the sandbox so we don't leak money
            sandbox.close()
            raise e

    async def proxy(self, project_id: str, path: str, method: str, headers: dict, body: bytes, query: str) -> httpx.Response:
        """
        Proxies requests from your backend -> E2B Public URL.
        This keeps the frontend URL structure /run/{project_id}/... consistent.
        """
        info = self._runs.get(project_id)
        
        if not info or not info.sandbox.is_running():
            raise RuntimeError("CRASH_DETECTED: Server not running (Sandbox died).")

        # Construct target URL
        target_url = f"{info.url}/{path.lstrip('/')}"
        if query:
            target_url += f"?{query}"

        # Clean headers (Host header usually breaks proxies)
        fwd_headers = {k: v for k, v in headers.items() if k.lower() not in ("host", "content-length", "connection")}

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.request(method, target_url, headers=fwd_headers, content=body)
                return resp
            except httpx.RequestError as e:
                raise RuntimeError(f"CRASH_DETECTED: Connection failed to E2B sandbox: {e}")