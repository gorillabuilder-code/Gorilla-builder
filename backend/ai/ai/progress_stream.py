"""
progress_stream.py — gor://a Simple Progress Feeder

Wraps monitor_events to provide standard events:
- start
- update
- error
- done
"""

from __future__ import annotations
import uuid
from .monitor_events import MonitorEvents


class ProgressStream:

    def __init__(self):
        self.monitor = MonitorEvents()

    def start(self, project_id: uuid.UUID, step: str):
        self.monitor.emit(project_id, "start", f"{step} started")

    def update(self, project_id: uuid.UUID, step: str, msg: str):
        self.monitor.emit(project_id, "progress", f"{step}: {msg}")

    def done(self, project_id: uuid.UUID, step: str):
        self.monitor.emit(project_id, "done", f"{step} completed")

    def error(self, project_id: uuid.UUID, step: str, msg: str):
        self.monitor.emit(project_id, "error", f"{step} failed: {msg}")
