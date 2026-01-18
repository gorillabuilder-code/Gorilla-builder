import os
import asyncio
from fastapi import Request, Response
from fastapi.responses import HTMLResponse
from backend.run_manager import ProjectRunManager
from supabase import Client

# HTML Snippet for the Badge
BADGE_HTML = """
<a href="https://gorillabuilder.dev" target="_blank" style="
    position: fixed;
    bottom: 20px;
    left: 20px;
    z-index: 9999;
    display: flex;
    align-items: center;
    gap: 8px;
    background: rgba(15, 21, 48, 0.9);
    border: 1px solid rgba(59, 108, 255, 0.3);
    padding: 8px 16px;
    border-radius: 9999px;
    color: #fff;
    text-decoration: none;
    font-family: sans-serif;
    font-size: 12px;
    font-weight: 500;
    box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    transition: transform 0.2s ease;
    backdrop-filter: blur(4px);
">
    <img src="logo.png" alt="Gorilla Logo" style="height: 16px; width: auto;">
    <span>Made with Gor://a</span>
</a>
<script>
document.querySelector('a[href="https://gorillabuilder.dev"]').addEventListener('mouseenter', e => {
    e.currentTarget.style.transform = 'translateY(-2px)';
});
document.querySelector('a[href="https://gorillabuilder.dev"]').addEventListener('mouseleave', e => {
    e.currentTarget.style.transform = 'translateY(0)';
});
</script>
"""

class Deployer:
    def __init__(self, run_manager: ProjectRunManager, supabase: Client):
        self.run_manager = run_manager
        self.supabase = supabase

    async def handle_request(self, request: Request, project_slug: str, path: str):
        """
        Handles traffic for /app/{project_slug}/{path}
        """
        # 1. Lookup Project by Slug (name-id) or ID
        try:
            # Try matching the new slug format first
            res = self.supabase.table("projects").select("id, owner_id, subdomain").eq("subdomain", project_slug).maybe_single().execute()
            project = res.data
            
            # Fallback: Try ID directly (for older links)
            if not project:
                res = self.supabase.table("projects").select("id, owner_id").eq("id", project_slug).maybe_single().execute()
                project = res.data
            
            if not project:
                return HTMLResponse("<h1>404 - App Not Found</h1>", status_code=404)
            
            project_id = project['id']
            owner_id = project['owner_id']

        except Exception as e:
            print(f"Deployer DB Error: {e}")
            return HTMLResponse("<h1>System Error</h1>", status_code=500)

        # 2. Check Owner Plan (For Badge Injection)
        show_badge = True
        try:
            user_res = self.supabase.table("users").select("plan").eq("id", owner_id).single().execute()
            if user_res.data and user_res.data.get("plan") == "premium":
                show_badge = False
        except:
            pass # Default to showing badge if check fails

        # 3. Auto-Boot (Cold Start)
        is_running, port = self.run_manager.is_running(project_id)
        if not is_running:
            try:
                # We need to import this function dynamically or pass it in to avoid circular imports
                # For now, assuming run_manager has access or we fetch files here
                files_res = self.supabase.table("files").select("path,content").eq("project_id", project_id).execute()
                file_tree = {r["path"]: (r.get("content") or "") for r in (files_res.data or [])}
                
                await self.run_manager.start(project_id, file_tree)
                await asyncio.sleep(1.5) # Give it a moment to bind port
            except Exception as e:
                return HTMLResponse(f"<h1>Boot Failed</h1><pre>{e}</pre>", status_code=500)

        # 4. Proxy Request
        body = await request.body()
        query_params = request.url.query
        
        try:
            # We strip the /app/project-slug prefix for the proxy
            # But usually the path argument comes in clean if using FastAPI path parameters
            
            resp = await self.run_manager.proxy(
                project_id=project_id,
                path=path,
                method=request.method,
                headers=dict(request.headers),
                body=body,
                query=query_params
            )

            # 5. Inject Badge (Only on HTML pages and if Free Tier)
            content = resp.content
            content_type = resp.headers.get("content-type", "")
            
            if show_badge and "text/html" in content_type and b"</body>" in content:
                # Inject before </body>
                content = content.replace(b"</body>", f"{BADGE_HTML}</body>".encode("utf-8"))
                # Update Content-Length
                resp.headers["content-length"] = str(len(content))

            # Exclude hop-by-hop headers
            excluded_headers = {"content-encoding", "content-length", "transfer-encoding", "connection"}
            headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded_headers}

            return Response(
                content=content,
                status_code=resp.status_code,
                headers=headers,
                media_type=content_type
            )

        except RuntimeError as e:
            return HTMLResponse(f"<h1>Application Error</h1><p>{e}</p>", status_code=502)