"""
sandbox.py — gor://a Safe Execution & Code Testing Environment

Purpose:
- Evaluate generated backend python code in a *restricted* execution scope
- Prevent harmful code from running
- Provide error feedback to the agent
- NEVER execute system commands, imports, I/O, or network calls

This module DOES NOT:
- Run JS
- Execute CLI / subprocess
- Allow unrestricted imports
- Write to disk
"""

from __future__ import annotations

import ast
import traceback
from typing import Dict, Any


class UnsafeCodeError(Exception):
    pass


class Sandbox:

    SAFE_BUILTINS = {
        "range": range,
        "len": len,
        "print": print,
        "min": min,
        "max": max,
        "sum": sum,
        "abs": abs,
        "sorted": sorted,
        "enumerate": enumerate,
        "zip": zip,
        "map": map,
        "filter": filter,
    }

    BLOCKED_KEYWORDS = [
        "import",
        "exec",
        "eval",
        "open",
        "os.",
        "sys.",
        "__",
        "subprocess",
        "socket",
        "httpx",
        "requests",
        "shutil",
        "pathlib",
        "thread",
        "multiprocessing",
    ]

    def _check_security(self, code: str) -> None:
        """
        Reject code strings containing dangerous patterns.
        """
        lowered = code.lower()
        for word in self.BLOCKED_KEYWORDS:
            if word in lowered:
                raise UnsafeCodeError(f"Blocked keyword detected: {word}")

        try:
            ast.parse(code)
        except SyntaxError as e:
            raise UnsafeCodeError(f"Code failed syntax check: {e}")

    def try_execute(self, code: str, inputs: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Safely execute restricted python code.
        Useful for:
        - business logic functions
        - small API handlers
        - validation routines
        """

        self._check_security(code)

        local_env = {}
        global_env = self.SAFE_BUILTINS.copy()

        if inputs:
            for k, v in inputs.items():
                local_env[k] = v

        try:
            exec(code, global_env, local_env)
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "trace": traceback.format_exc(),
            }

        return {
            "success": True,
            "output": local_env,
        }

    def simulate_api_handler(
        self, code: str, request_body: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Tests generated backend handler code in isolation.
        The code should define a function: handle(request)
        """
        self._check_security(code)

        env = {"request": request_body}
        global_env = self.SAFE_BUILTINS.copy()

        try:
            exec(code, global_env, env)
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "trace": traceback.format_exc(),
            }

        if "handle" not in env or not callable(env["handle"]):
            return {
                "success": False,
                "error": "No valid handle(request) function found",
            }

        try:
            result = env["handle"](request_body)
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "trace": traceback.format_exc(),
            }

        return {
            "success": True,
            "result": result,
        }
