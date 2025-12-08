"""
agent_api.py — gor://a Agent Orchestration API

Endpoint:
- POST /agent/run

Body:
- project_id
- prompt

Behavior:
- Pulls current file tree
- Runs Planner (TODO.md)
- Runs Coder section-by-section
- Validates operations
- Applies via Generator
- Emits progress events
"""

from __future__ import annotations
import uuid
from typing import Dict, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client

from ..settings import get_settings
from ..auth import get_current_user, User
from ..agent.planner import Planner
from ..agent.coder import Coder
from ..agent.validator import Validator, ValidationError
from ..agent.generator import Generator
from ..monitor_events import MonitorEvents

settings = get_settings()
supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

router = APIRouter(prefix="/agent", tags=["agent"])

planner = Planner()
coder = Coder()
validator = Validator()
generator = Generator()
monitor = MonitorEvents()


class AgentRunRequest(BaseModel):
    project_id: uuid.UUID
    prompt: str


class AgentRunResponse(BaseModel):
    success: bool
    message: str


def _get_file_tree(project_id: uuid.UUID) -> Dict[str, str]:
    res = (
        supabase.table("files")
        .select("path,content")
        .eq("project_id", str(project_id))
        .execute()
    )
    tree: Dict[str, str] = {}
    for row in res.data or []:
        tree[row["path"]] = row["content"]
    return tree


@router.post("/run", response_model=AgentRunResponse)
async def run_agent(body: AgentRunRequest, user: User = Depends(get_current_user)):
    # Ensure project ownership
    res = (
        supabase.table("projects")
        .select("id,owner_id")
        .eq("id", str(body.project_id))
        .maybe_single()
        .execute()
    )
    data = res.data
    if not data or data["owner_id"] != str(user.id):
        raise HTTPException(status_code=404, detail="Project not found")

    project_id = body.project_id

    monitor.emit(project_id, "start", "Agent run started")
    monitor.emit(project_id, "info", "Reading project file tree")

    tree = _get_file_tree(project_id)

    # ----------------- PLAN -----------------
    monitor.emit(project_id, "planner_start", "Planner analysing request")
    plan = await planner.generate_plan(body.prompt, {"file_count": len(tree)})
    todo_text = plan["raw"]
    monitor.emit(project_id, "planner_done", "Planner produced TODO.md")

    # Write TODO.md to project
    generator.generate(
        project_id,
        [
            {
                "action": "create_file" if "TODO.md" not in tree else "overwrite_file",
                "path": "TODO.md",
                "content": todo_text,
            }
        ],
        current_files=tree,
    )

    # ----------------- CODE PER SECTION -----------------
    for section, section_text in plan["structured"].items():
        monitor.emit(project_id, "coder_start", f"Generating code for: {section}")
        ops = await coder.generate_code(
            plan_section=section,
            plan_text=section_text,
            file_tree=_get_file_tree(project_id),
            project_name="gor-app",
        )

        try:
            validator.validate(ops["operations"])
        except ValidationError as exc:
            monitor.emit(project_id, "error", f"Validation failed in {section}: {exc}")
            raise HTTPException(status_code=400, detail=str(exc))

        results = generator.generate(
            project_id,
            operations=ops["operations"],
            current_files=_get_file_tree(project_id),
        )
        monitor.emit(
            project_id,
            "coder_done",
            f"{section}: applied {len(results)} changes",
        )

    monitor.emit(project_id, "done", "Agent run completed successfully")
    return AgentRunResponse(success=True, message="Agent completed")
