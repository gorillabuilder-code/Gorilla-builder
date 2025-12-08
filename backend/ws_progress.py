"""
ws_progress.py — gor://a WebSocket Progress Stream

Dashboard listens in real-time to:
- planner
- coder
- validator
- deploy
"""

from __future__ import annotations
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Dict, List
import uuid

ws_router = APIRouter(prefix="/ws", tags=["progress"])

connections: Dict[str, List[WebSocket]] = {}


async def broadcast(project_id: str, payload: dict):
    if project_id in connections:
        dead = []
        for ws in connections[project_id]:
            try:
                await ws.send_json(payload)
            except:
                dead.append(ws)
        for ws in dead:
            connections[project_id].remove(ws)


@ws_router.websocket("/progress/{project_id}")
async def ws_progress(websocket: WebSocket, project_id: str):
    await websocket.accept()

    if project_id not in connections:
        connections[project_id] = []
    connections[project_id].append(websocket)

    try:
        while True:
            # no receive needed, one-way push
            await websocket.receive_text()
    except WebSocketDisconnect:
        connections[project_id].remove(websocket)
