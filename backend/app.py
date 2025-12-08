"""
app.py — gor://a ASGI entrypoint (Final)

Serves:
- Auth
- Projects API
- Files API
- Agent Builder
- Export
- Preview relays
- Websocket progress stream
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .settings import get_settings
from .urls import ALL_ROUTERS
from .ws_progress import ws_router

settings = get_settings()

app = FastAPI(title="gor://a Backend", version="1.0.0")

# CORS for local frontend + preview iframe
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Attach all main REST routers
for r in ALL_ROUTERS:
    app.include_router(r)

# Websocket router
app.include_router(ws_router)

@app.get("/", include_in_schema=False)
def root():
    return {"service": "gor://a", "status": "running", "version": app.version}
