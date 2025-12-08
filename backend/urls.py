"""
urls.py — gor://a router registry (for reference)

You already mount routers in app.py; this file just exposes them neatly.
"""

from __future__ import annotations

from .auth import auth_router
from .upload_router import router as upload_router
from .views.projects_api import router as projects_router
from .views.files_api import router as files_router
from .views.agent_api import router as agent_router
from .views.export_api import router as export_router

ALL_ROUTERS = [
    auth_router,
    upload_router,
    projects_router,
    files_router,
    agent_router,
    export_router,
]
