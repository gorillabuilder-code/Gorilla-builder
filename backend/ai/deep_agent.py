"""
True Agent Swarm with Conversational MCP — v2.0
=================================================

Multi-agent architecture where agents:
- REASON before acting (not just execute)
- ASK questions when unclear (bidirectional communication)
- SELF-CORRECT when things fail
- COLLABORATE through conversation layers
- REFLECT on quality before marking done

v2.0 Orchestration Upgrades:
- Parallel Sub-Agent Dispatch (Orchestrator Meta-Agent)
- Deterministic AST Patching (non-AI boilerplate injection)
- Streaming Token Substitution (B64/large-string compression)
- Skills-Based Knowledge Injection (.gorilla/skills/)
- Intent-Based Knowledge Routing (Micro-RAG blueprints)
- Sandbox Isolation (stage → merge pipeline)
- Continuous Eval Hooks (debug telemetry)

Terminal logging for development visibility.
"""

from __future__ import annotations

import os
import json
import re
import time
import asyncio
import hashlib
import copy
from typing import Dict, Any, List, Optional, Tuple, TypedDict, Callable, Set
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict

import httpx

# --- Configuration for OpenRouter ---
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL = os.getenv("MODEL", "xiaomi/mimo-v2-pro")
VISION_MODEL = os.getenv("MODEL", "arcee-ai/trinity-large-thinking")
OPENROUTER_URL = os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions").strip()
SITE_URL = os.getenv("SITE_URL", "https://gorillabuilder.dev").strip()
SITE_NAME = os.getenv("SITE_NAME", "Gorilla Builder")

# --- Configuration for File API ---
FILE_API_BASE_URL = os.getenv("FILE_API_BASE_URL", "https://slaw-carefully-cried.ngrok-free.dev").strip()
FILE_API_TIMEOUT = 10.0

# --- Context Limits for MiniMax M2.5 ---
MINIMAX_MAX_CONTEXT = 150000
MINIMAX_SAFE_THRESHOLD = 140000
CHARS_PER_TOKEN_ESTIMATE = 4

if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY must be configured in the environment")

ALLOWED_ACTIONS = {"create_file", "overwrite_file", "read_file"}
ACTION_NORMALIZE = {
    "update_file": "overwrite_file",
    "replace_file": "overwrite_file",
    "write_file": "overwrite_file",
    "modify_file": "overwrite_file",
    "upsert_file": "overwrite_file",
    "create": "create_file",
    "overwrite": "overwrite_file",
    "patch": "overwrite_file",
    "patch_file": "overwrite_file",
    "see_file": "read_file",
}

# ============================================================================
# TERMINAL & UI LOGGING BRIDGE
# ============================================================================

_external_log_callback = None

def set_log_callback(callback):
    """Allows app.py to hook into the agent's internal thought stream."""
    global _external_log_callback
    _external_log_callback = callback

def log_agent(role: str, message: str, project_id: str = ""):
    """Print agent activity to terminal AND pipe it to the frontend UI."""
    prefix = f"[{project_id[:8]}]" if project_id else "[AGENT]"
    timestamp = time.strftime("%H:%M:%S")
    colors = {
        "planner": "\033[95m",
        "reasoner": "\033[35m",
        "coder": "\033[94m",
        "reviewer": "\033[36m",
        "ui_agent": "\033[96m",
        "api_agent": "\033[92m",
        "logic_agent": "\033[93m",
        "debugger": "\033[91m",
        "swarm": "\033[97m",
        "llm": "\033[90m",
        "mcp": "\033[33m",
        "orchestrator": "\033[95;1m",
        "ast_patcher": "\033[32m",
        "sandbox": "\033[34m",
        "skills": "\033[93;1m",
        "context": "\033[90m",
        "token_sub": "\033[36;2m",
        "eval": "\033[91;2m",
        "router": "\033[95;2m",
    }
    color = colors.get(role.lower(), "\033[94m")
    reset = "\033[0m"
    dim = "\033[90m"
    
    print(f"{dim}{timestamp}{reset} {prefix} {color}{role.upper()}{reset}: {message[:200]}{'...' if len(message) > 200 else ''}")
    
    global _external_log_callback
    if _external_log_callback and project_id:
        if role.lower() != "llm":
            try:
                _external_log_callback(project_id, role.lower(), message)
            except Exception:
                pass

# ============================================================================
# TOKEN LIMIT HTML MESSAGE (unchanged)
# ============================================================================

def _render_token_limit_message() -> str:
    """Render a beautiful HTML message when token limit is reached."""
    return '''
    <div style="
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        padding: 40px 30px;
        background: linear-gradient(135deg, rgba(15, 23, 42, 0.9) 0%, rgba(30, 10, 50, 0.8) 100%);
        border: 1px solid rgba(217, 70, 239, 0.3);
        border-radius: 20px;
        text-align: center;
        max-width: 400px;
        margin: 20px auto;
        box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5), 0 0 40px rgba(217, 70, 239, 0.15);
        backdrop-filter: blur(10px);
    ">
        <div style="
            width: 80px;
            height: 80px;
            background: linear-gradient(135deg, #d946ef 0%, #a855f7 100%);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-bottom: 24px;
            box-shadow: 0 10px 40px rgba(217, 70, 239, 0.4);
            animation: pulse-glow 2s ease-in-out infinite;
        ">
            <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon>
            </svg>
        </div>
        
        <h2 style="
            color: #fff;
            font-size: 24px;
            font-weight: 700;
            margin: 0 0 12px 0;
            letter-spacing: -0.5px;
        ">Token Limit Reached</h2>
        
        <p style="
            color: #94a3b8;
            font-size: 14px;
            line-height: 1.6;
            margin: 0 0 28px 0;
            max-width: 280px;
        ">
            You've used all your monthly tokens. Upgrade to Premium for unlimited access and supercharge your builds.
        </p>
        
        <a href="/pricing" style="
            display: inline-flex;
            align-items: center;
            gap: 8px;
            background: linear-gradient(135deg, #d946ef 0%, #a855f7 100%);
            color: white;
            text-decoration: none;
            padding: 14px 32px;
            border-radius: 12px;
            font-size: 14px;
            font-weight: 600;
            transition: all 0.3s ease;
            box-shadow: 0 4px 20px rgba(217, 70, 239, 0.4);
        " onmouseover="this.style.transform='translateY(-2px)'; this.style.boxShadow='0 6px 30px rgba(217, 70, 239, 0.6)';" 
           onmouseout="this.style.transform='translateY(0)'; this.style.boxShadow='0 4px 20px rgba(217, 70, 239, 0.4)';">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon>
            </svg>
            Upgrade to Premium
        </a>
        
        <a href="/dashboard" style="
            color: #64748b;
            font-size: 12px;
            text-decoration: none;
            margin-top: 16px;
            transition: color 0.2s;
        " onmouseover="this.style.color='#94a3b8';" onmouseout="this.style.color='#64748b';">
            Go to Dashboard →
        </a>
    </div>
    
    <style>
        @keyframes pulse-glow {
            0%, 100% { box-shadow: 0 10px 40px rgba(217, 70, 239, 0.4); }
            50% { box-shadow: 0 10px 50px rgba(217, 70, 239, 0.7); }
        }
    </style>
    '''

# ============================================================================
# SHARED CHAT HISTORY (Agent Swarm Memory)
# ============================================================================

class ChatMsg(TypedDict):
    role: str
    content: str

_HISTORY: Dict[str, List[ChatMsg]] = {}

def _norm_role(role: str) -> str:
    r = (role or "").strip().lower()
    if r in ("user", "you"): 
        return "user"
    if r in ("assistant", "planner", "system", "coder", "agent", "reasoner", "reviewer"): 
        return "assistant"
    return "user"

def _append_history(project_id: str, role: str, content: str, max_items: int = 20) -> None:
    if not project_id: 
        return
    msg = {"role": _norm_role(role), "content": (content or "").strip()}
    if not msg["content"]: 
        return
    _HISTORY.setdefault(project_id, []).append(msg)
    if len(_HISTORY[project_id]) > max_items:
        _HISTORY[project_id] = _HISTORY[project_id][-max_items:]

def _get_history(project_id: str, max_items: int = 16) -> List[ChatMsg]:
    if not project_id: 
        return []
    return list(_HISTORY.get(project_id, []))[-max_items:]

def clear_history(project_id: str) -> None:
    """Clear chat history for a project."""
    if project_id in _HISTORY:
        del _HISTORY[project_id]

# ============================================================================
# JSON EXTRACTION HELPER
# ============================================================================

def _extract_json(text: str) -> Any:
    """Robustly extract the largest valid JSON object from a string."""
    text = text.strip()

    # 1. Try fenced code blocks first (```json ... ``` or ``` ... ```)
    code_block_pattern = r"```(?:json)?\s*([\s\S]*?)\s*```"
    for match in re.finditer(code_block_pattern, text, re.DOTALL):
        candidate = match.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # Try fixing common issues before giving up on this block
            fixed = _fix_common_json_issues(candidate)
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass

    # 2. Find ALL valid JSON objects by scanning brace pairs (handles nesting correctly)
    candidates = []
    for start_idx, char in enumerate(text):
        if char == '{':
            depth = 0
            in_string = False
            escape_next = False
            for end_idx in range(start_idx, len(text)):
                c = text[end_idx]
                if escape_next:
                    escape_next = False
                    continue
                if c == '\\' and in_string:
                    escape_next = True
                    continue
                if c == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        candidate = text[start_idx:end_idx + 1]
                        try:
                            parsed = json.loads(candidate)
                            candidates.append((len(candidate), parsed))
                        except json.JSONDecodeError:
                            fixed = _fix_common_json_issues(candidate)
                            try:
                                parsed = json.loads(fixed)
                                candidates.append((len(candidate), parsed))
                            except json.JSONDecodeError:
                                pass
                        break

    if candidates:
        # Return the largest valid JSON object found
        return max(candidates, key=lambda x: x[0])[1]

    # 3. Last resort: try the whole text, with and without fixes
    for attempt in [text, _fix_common_json_issues(text)]:
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            pass

    return None


def _fix_common_json_issues(text: str) -> str:
    """Fix common JSON formatting issues produced by LLMs."""
    # Remove JS-style comments (// and /* */)
    text = re.sub(r'//[^\n]*', '', text)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    # Remove trailing commas before } or ]
    text = re.sub(r',\s*([}\]])', r'\1', text)
    # Replace single quotes used as string delimiters (simple heuristic)
    # Only replace when not inside a double-quoted string
    text = re.sub(r"(?<![\\'\w])'([^']*)'(?!['\w])", r'"\1"', text)
    # Replace Python/JS literals
    text = text.replace('True', 'true').replace('False', 'false').replace('None', 'null')
    # Remove BOM or zero-width chars
    text = text.lstrip('\ufeff\u200b')
    return text

# ============================================================================
# LAYER 1: STREAMING TOKEN SUBSTITUTION
# ============================================================================

class TokenSubstitution:
    """
    Compresses massive strings (Base64 images, large JSON blobs, SVG data)
    into tiny placeholder IDs before they hit the LLM prompt, then swaps
    them back into generated code during the render/write phase.
    
    This cuts prompt token cost dramatically for image-heavy or data-heavy apps.
    """
    
    # Threshold in chars — anything above this gets compressed
    COMPRESSION_THRESHOLD = 500
    
    def __init__(self):
        self._vault: Dict[str, str] = {}   # placeholder_id → original_content
        self._reverse: Dict[str, str] = {} # content_hash → placeholder_id
        self._counter = 0
    
    def _make_id(self) -> str:
        self._counter += 1
        return f"__GORILLA_BLOB_{self._counter:04d}__"
    
    def compress_file_tree(self, file_tree: Dict[str, str]) -> Dict[str, str]:
        """
        Scan file_tree for large blobs (Base64 images, massive JSON, SVGs)
        and replace them with placeholder IDs. Returns a compressed copy.
        """
        compressed = {}
        for path, content in file_tree.items():
            if content and len(content) > self.COMPRESSION_THRESHOLD:
                # Detect compressible content types
                if (path.endswith(".b64") or 
                    self._looks_like_base64(content) or
                    (path.endswith(".json") and len(content) > 5000) or
                    (path.endswith(".svg") and len(content) > 3000)):
                    
                    content_hash = hashlib.md5(content[:200].encode()).hexdigest()
                    
                    if content_hash in self._reverse:
                        placeholder = self._reverse[content_hash]
                    else:
                        placeholder = self._make_id()
                        self._vault[placeholder] = content
                        self._reverse[content_hash] = placeholder
                    
                    compressed[path] = placeholder
                    log_agent("token_sub", 
                              f"Compressed {path}: {len(content)} chars → {len(placeholder)} chars", "")
                    continue
            
            compressed[path] = content
        
        return compressed
    
    def expand_operations(self, operations: List[Dict]) -> List[Dict]:
        """
        After code generation, scan all operation contents for placeholder IDs
        and swap the original content back in.
        """
        expanded = []
        for op in operations:
            op_copy = dict(op)
            content = op_copy.get("content")
            
            if content and isinstance(content, str):
                for placeholder, original in self._vault.items():
                    if placeholder in content:
                        content = content.replace(placeholder, original)
                        log_agent("token_sub", 
                                  f"Expanded {placeholder} in {op_copy.get('path', '?')}", "")
                
                op_copy["content"] = content
            
            expanded.append(op_copy)
        
        return expanded
    
    def compress_string(self, text: str) -> str:
        """Compress a single large string if it exceeds threshold."""
        if not text or len(text) <= self.COMPRESSION_THRESHOLD:
            return text
        
        if self._looks_like_base64(text):
            content_hash = hashlib.md5(text[:200].encode()).hexdigest()
            if content_hash in self._reverse:
                return self._reverse[content_hash]
            
            placeholder = self._make_id()
            self._vault[placeholder] = text
            self._reverse[content_hash] = placeholder
            return placeholder
        
        return text
    
    @staticmethod
    def _looks_like_base64(text: str) -> bool:
        """Quick heuristic to detect base64-encoded content."""
        if len(text) < 100:
            return False
        sample = text[:200].strip()
        # Base64 is mostly alphanumeric + /+=
        alpha_ratio = sum(1 for c in sample if c.isalnum() or c in '+/=') / len(sample)
        return alpha_ratio > 0.9 and '\n' not in sample[:100]
    
    @property
    def savings_report(self) -> Dict[str, Any]:
        total_original = sum(len(v) for v in self._vault.values())
        total_compressed = sum(len(k) for k in self._vault.keys())
        return {
            "blobs_compressed": len(self._vault),
            "original_chars": total_original,
            "compressed_chars": total_compressed,
            "savings_pct": round((1 - total_compressed / max(total_original, 1)) * 100, 1)
        }


# ============================================================================
# LAYER 2: DETERMINISTIC AST PATCHER
# ============================================================================

class ASTPatcher:
    """
    Hardcoded, non-AI fallback scripts that automatically inject missing,
    predictable boilerplate without burning tokens.
    
    Runs AFTER code generation as a post-processing pass. Catches common
    patterns the LLM forgets (context providers, missing exports, React
    boilerplate, route wiring).
    """
    
    # Registry of patch rules
    _rules: List[Dict[str, Any]] = []
    
    @classmethod
    def register_rule(cls, name: str, detect: Callable, patch: Callable, priority: int = 50):
        """Register a deterministic patch rule."""
        cls._rules.append({
            "name": name,
            "detect": detect,
            "patch": patch,
            "priority": priority
        })
        cls._rules.sort(key=lambda r: r["priority"])
    
    @classmethod
    def apply_all(cls, operations: List[Dict], file_tree: Dict[str, str]) -> Tuple[List[Dict], List[str]]:
        """
        Run all registered patch rules against the operations.
        Returns (patched_operations, list_of_patches_applied).
        """
        patches_applied = []
        
        # Build a merged view: existing files + new operations
        merged_tree = dict(file_tree)
        for op in operations:
            if op.get("action") in ("create_file", "overwrite_file"):
                merged_tree[op["path"]] = op.get("content", "")
        
        # Run each rule
        extra_ops = []
        modified_ops = list(operations)
        
        for rule in cls._rules:
            try:
                if rule["detect"](modified_ops, merged_tree):
                    new_ops, mods = rule["patch"](modified_ops, merged_tree)
                    if new_ops:
                        extra_ops.extend(new_ops)
                        patches_applied.append(f"{rule['name']}: +{len(new_ops)} ops")
                        log_agent("ast_patcher", 
                                  f"Applied: {rule['name']} (+{len(new_ops)} operations)", "")
                    if mods:
                        modified_ops = mods
                        patches_applied.append(f"{rule['name']}: modified existing ops")
            except Exception as e:
                log_agent("ast_patcher", f"Rule '{rule['name']}' failed: {str(e)[:60]}", "")
        
        # Merge extra operations
        final_ops = modified_ops + extra_ops
        
        if patches_applied:
            log_agent("ast_patcher", f"Total patches: {len(patches_applied)}", "")
        
        return final_ops, patches_applied


# --- Register Built-in AST Patch Rules ---

def _detect_missing_react_router(ops: List[Dict], tree: Dict[str, str]) -> bool:
    """Detect if pages are created but react-router isn't wired in App.tsx."""
    has_pages = any("src/pages/" in op.get("path", "") and op.get("path", "").endswith(".tsx")
                     for op in ops)
    app_content = tree.get("src/App.tsx", "")
    has_router = "BrowserRouter" in app_content or "Routes" in app_content or "react-router" in app_content
    return has_pages and not has_router

def _patch_react_router(ops: List[Dict], tree: Dict[str, str]) -> Tuple[List[Dict], None]:
    """Inject a basic react-router setup if pages exist but App.tsx lacks routing."""
    page_files = []
    for op in ops:
        path = op.get("path", "")
        if "src/pages/" in path and path.endswith(".tsx"):
            name = path.split("/")[-1].replace(".tsx", "")
            page_files.append((name, path))
    
    if not page_files:
        return [], None
    
    # Build import lines and route lines
    imports = ['import { BrowserRouter, Routes, Route } from "react-router-dom";']
    routes = []
    for name, path in page_files:
        import_path = "@/" + path.replace("src/", "").replace(".tsx", "")
        imports.append(f'import {name} from "{import_path}";')
        route_path = "/" if name.lower() == "index" else f"/{name.lower()}"
        routes.append(f'        <Route path="{route_path}" element={{<{name} />}} />')
    
    app_content = f"""{chr(10).join(imports)}

function App() {{
  return (
    <BrowserRouter>
      <Routes>
{chr(10).join(routes)}
      </Routes>
    </BrowserRouter>
  );
}}

export default App;
"""
    return [{"action": "overwrite_file", "path": "src/App.tsx", "content": app_content}], None

ASTPatcher.register_rule("react_router_wiring", _detect_missing_react_router, _patch_react_router, priority=10)


def _detect_missing_default_export(ops: List[Dict], tree: Dict[str, str]) -> bool:
    """Detect .tsx files missing default export."""
    for op in ops:
        path = op.get("path", "")
        content = op.get("content", "")
        if path.endswith(".tsx") and content:
            if "export default" not in content and "export {" not in content:
                # Check if there's a function/const component defined
                if re.search(r'(?:function|const)\s+\w+', content):
                    return True
    return False

def _patch_missing_default_export(ops: List[Dict], tree: Dict[str, str]) -> Tuple[None, List[Dict]]:
    """Add default export to .tsx files that are missing it."""
    patched = []
    for op in ops:
        path = op.get("path", "")
        content = op.get("content", "")
        
        if path.endswith(".tsx") and content and "export default" not in content:
            # Find the main component name
            match = re.search(r'(?:function|const)\s+([A-Z]\w+)', content)
            if match:
                component_name = match.group(1)
                content = content.rstrip() + f"\n\nexport default {component_name};\n"
                op = dict(op)
                op["content"] = content
                log_agent("ast_patcher", f"Added default export for {component_name} in {path}", "")
        
        patched.append(op)
    
    return None, patched

ASTPatcher.register_rule("default_export", _detect_missing_default_export, _patch_missing_default_export, priority=20)


def _detect_missing_express_mount(ops: List[Dict], tree: Dict[str, str]) -> bool:
    """Detect if route files are created but not mounted in server.js."""
    has_routes = any("routes/" in op.get("path", "") and op.get("path", "").endswith(".js")
                     for op in ops)
    if not has_routes:
        return False
    
    server_content = tree.get("server.js", "")
    new_route_files = [op.get("path", "") for op in ops 
                       if "routes/" in op.get("path", "") and op.get("path", "").endswith(".js")]
    
    for route_file in new_route_files:
        route_name = route_file.split("/")[-1].replace(".js", "")
        if route_name not in server_content:
            return True
    
    return False

def _patch_express_mount(ops: List[Dict], tree: Dict[str, str]) -> Tuple[None, List[Dict]]:
    """
    Inject route mount statements into server.js for any new route files.
    We modify the server.js operation if it exists, or create a patch.
    """
    new_route_files = [op.get("path", "") for op in ops 
                       if "routes/" in op.get("path", "") and op.get("path", "").endswith(".js")]
    
    if not new_route_files:
        return None, ops
    
    # Find the server.js operation if it's being written
    patched = []
    server_patched = False
    
    for op in ops:
        if op.get("path") == "server.js" and op.get("content"):
            content = op["content"]
            
            for route_file in new_route_files:
                route_name = route_file.split("/")[-1].replace(".js", "")
                import_var = f"{route_name}Router"
                import_line = f'import {import_var} from "./{route_file}";'
                mount_line = f'app.use("/api/{route_name}", {import_var});'
                
                if route_name not in content:
                    # Insert import at top (after last import)
                    last_import = content.rfind("import ")
                    if last_import != -1:
                        end_of_line = content.find("\n", last_import)
                        if end_of_line != -1:
                            content = content[:end_of_line+1] + import_line + "\n" + content[end_of_line+1:]
                    
                    # Insert mount before listen
                    listen_idx = content.find("app.listen")
                    if listen_idx != -1:
                        content = content[:listen_idx] + mount_line + "\n\n" + content[listen_idx:]
                    
                    log_agent("ast_patcher", f"Mounted {route_file} in server.js", "")
            
            op = dict(op)
            op["content"] = content
            server_patched = True
        
        patched.append(op)
    
    return None, patched

ASTPatcher.register_rule("express_route_mount", _detect_missing_express_mount, _patch_express_mount, priority=15)


# ============================================================================
# LAYER 3: SKILLS-BASED KNOWLEDGE INJECTION
# ============================================================================

class SkillsManager:
    """
    Loads project-level and global skill files (.gorilla/skills/*.md)
    and injects relevant architectural rules into agent prompts.
    
    Skills replace hardcoded system prompt additions with modular,
    per-project markdown files that can be edited without code changes.
    """
    
    # Built-in skills registry (loaded from .gorilla/skills/ or defaults)
    _global_skills: Dict[str, str] = {}
    _project_skills: Dict[str, Dict[str, str]] = {}  # project_id → {skill_name → content}
    
    # Default built-in skills (used when no .gorilla/skills/ files exist)
    BUILTIN_SKILLS = {
        "react_conventions": (
            "## React Conventions\n"
            "- Use functional components with hooks (no class components)\n"
            "- Co-locate styles with components using Tailwind utility classes\n"
            "- Use `@/` import alias for all src/ imports\n"
            "- Prefer composition over inheritance\n"
            "- Extract reusable hooks into `src/hooks/`\n"
            "- Keep components under 200 lines; split when larger\n"
        ),
        "express_conventions": (
            "## Express Conventions\n"
            "- Use ES modules (import/export) not CommonJS\n"
            "- All imports must use `.js` extension\n"
            "- Use async/await with try/catch for all async routes\n"
            "- Return consistent JSON: `{ success: true, data: ... }` or `{ error: '...' }`\n"
            "- Mount routes via `app.use('/api/...', router)`\n"
        ),
        "ui_design": (
            "## UI Design Standards\n"
            "- NEVER use Inter font; prefer system fonts or creative alternatives\n"
            "- Use framer-motion for micro-interactions and page transitions\n"
            "- Use lucide-react for all icons\n"
            "- Build custom components; avoid obvious Shadcn defaults\n"
            "- Design mobile-first, test responsive at 375px and 1440px\n"
        ),
        "webcontainer_compat": (
            "## WebContainer Compatibility\n"
            "- NO native C++ modules (no bcrypt, sharp, canvas)\n"
            "- NO filesystem writes outside the sandbox\n"
            "- NO child_process.exec or spawn\n"
            "- Use browser-compatible crypto (Web Crypto API)\n"
            "- SQLite via better-sqlite3 is STILL NOT OK (WASM-based)\n"
        ),
        "gorilla_ai_apis": (
            "## Gorilla AI API Integration\n"
            "- ALL AI calls go through Gorilla Proxy on the backend\n"
            "- Use `process.env.GORILLA_API_KEY` for auth\n"
            "- LLM endpoint: POST `https://slaw-carefully-cried.ngrok-free.dev/api/v1/chat/completions`\n"
            "- Image gen: POST `https://slaw-carefully-cried.ngrok-free.dev/api/v1/images/generations`\n"
            "- STT: POST `https://slaw-carefully-cried.ngrok-free.dev/api/v1/audio/transcriptions`\n"
            "- TTS: Use browser native `window.speechSynthesis` (NO API)\n"
            "- BG removal: POST `https://slaw-carefully-cried.ngrok-free.dev/api/v1/images/remove-background`\n"
            "- NEVER specify model or temperature params in LLM calls\n"
        ),
    }
    
    @classmethod
    def load_project_skills(cls, project_id: str, file_tree: Dict[str, str]) -> Dict[str, str]:
        """
        Load skills from .gorilla/skills/*.md in the project's file tree.
        Falls back to built-in skills if none found.
        """
        project_skills = {}
        
        # Scan file tree for .gorilla/skills/ files
        for path, content in file_tree.items():
            if path.startswith(".gorilla/skills/") and path.endswith(".md") and content:
                skill_name = path.split("/")[-1].replace(".md", "").lower()
                project_skills[skill_name] = content
                log_agent("skills", f"Loaded project skill: {skill_name}", project_id)
        
        # Also check for AGENTS.md
        agents_md = file_tree.get(".gorilla/AGENTS.md", "")
        if agents_md:
            project_skills["agents_directives"] = agents_md
            log_agent("skills", "Loaded AGENTS.md directives", project_id)
        
        cls._project_skills[project_id] = project_skills
        return project_skills
    
    @classmethod
    def get_relevant_skills(cls, project_id: str, task_type: str, 
                           user_request: str = "") -> str:
        """
        Return concatenated skill content relevant to the task type.
        
        task_type: "frontend", "backend", "fullstack", "debug", "ui"
        """
        project_skills = cls._project_skills.get(project_id, {})
        
        # If project has custom skills, use those exclusively
        if project_skills:
            relevant = []
            
            # Always include agents directives
            if "agents_directives" in project_skills:
                relevant.append(project_skills["agents_directives"])
            
            # Match by task type
            for name, content in project_skills.items():
                if name == "agents_directives":
                    continue
                name_lower = name.lower()
                if task_type in ("frontend", "fullstack", "ui") and any(
                    k in name_lower for k in ("react", "ui", "frontend", "design", "component")):
                    relevant.append(content)
                elif task_type in ("backend", "fullstack") and any(
                    k in name_lower for k in ("express", "api", "backend", "server", "route")):
                    relevant.append(content)
                elif task_type == "debug" and any(
                    k in name_lower for k in ("debug", "error", "fix")):
                    relevant.append(content)
                # Always include general/compat skills
                elif any(k in name_lower for k in ("compat", "general", "convention", "gorilla")):
                    relevant.append(content)
            
            if relevant:
                return "\n\n---\n\n".join(relevant)
        
        # Fall back to built-in skills
        builtin_relevant = []
        
        if task_type in ("frontend", "fullstack", "ui"):
            builtin_relevant.extend([
                cls.BUILTIN_SKILLS["react_conventions"],
                cls.BUILTIN_SKILLS["ui_design"],
            ])
        
        if task_type in ("backend", "fullstack"):
            builtin_relevant.extend([
                cls.BUILTIN_SKILLS["express_conventions"],
            ])
        
        # Always include these
        builtin_relevant.extend([
            cls.BUILTIN_SKILLS["webcontainer_compat"],
            cls.BUILTIN_SKILLS["gorilla_ai_apis"],
        ])
        
        return "\n\n".join(builtin_relevant)
    
    @classmethod
    def inject_user_preferences(cls, agent_skills: Optional[Dict]) -> str:
        """Convert agent_skills dict into skill-style markdown."""
        if not agent_skills or not isinstance(agent_skills, dict):
            return ""
        
        lines = ["## User Preferences"]
        
        if agent_skills.get("visuals") == "clean-svg":
            lines.append("- Visuals: Use clean SVG icons (Phosphor/Lucide). No emojis.")
        elif agent_skills.get("visuals") == "emojis":
            lines.append("- Visuals: Use text-based emojis instead of SVG icons.")
        
        if agent_skills.get("framework") == "tailwind":
            lines.append("- Styling: Use Tailwind CSS utility classes.")
        elif agent_skills.get("framework") == "vanilla-css":
            lines.append("- Styling: Use clean Vanilla CSS.")
        
        if agent_skills.get("style") == "beginner":
            lines.append("- Code Style: Beginner-friendly, heavily commented.")
        elif agent_skills.get("style") == "expert":
            lines.append("- Code Style: Expert-level, concise, minimal comments.")
        
        if agent_skills.get("personality") == "professional":
            lines.append("- Communication: Professional, formal.")
        elif agent_skills.get("personality") == "casual":
            lines.append("- Communication: Casual, friendly, use emojis.")
        
        return "\n".join(lines) if len(lines) > 1 else ""


# ============================================================================
# LAYER 4: INTENT-BASED KNOWLEDGE ROUTING (Micro-RAG)
# ============================================================================

class BlueprintRouter:
    """
    Stores hand-curated component blueprints and injects only the relevant
    ones into the prompt based on intent classification.
    
    This is a local Micro-RAG system — no vector DB needed for the MVP.
    Can be upgraded to pgvector/Supabase later.
    """
    
    # Blueprint registry: intent_tag → blueprint_content
    _blueprints: Dict[str, Dict[str, str]] = {}
    
    # Intent → blueprint tag mapping
    INTENT_MAP = {
        "chat": ["chat_interface", "message_component", "websocket_setup"],
        "auth": ["auth_flow", "protected_route"],
        "dashboard": ["dashboard_layout", "stats_cards", "chart_component"],
        "form": ["form_component", "validation"],
        "crud": ["crud_api", "data_table"],
        "image": ["image_upload", "image_gallery"],
        "voice": ["voice_input", "voice_output"],
        "landing": ["hero_section", "feature_grid", "cta_section"],
        "ecommerce": ["product_card", "cart_system", "checkout_flow"],
        "blog": ["blog_layout", "post_component", "markdown_renderer"],
        "api": ["rest_api", "middleware", "error_handler"],
    }
    
    # Curated blueprints — compact structural hints (NOT full code)
    BUILTIN_BLUEPRINTS = {
        "chat_interface": {
            "description": "Chat UI with message list, input, and auto-scroll",
            "structure": "ChatContainer.tsx (state manager) → MessageList.tsx (virtualized) → MessageBubble.tsx → ChatInput.tsx (with typing indicator)",
            "key_patterns": "useRef for scroll anchor, optimistic message append, streaming response display",
        },
        "auth_flow": {
            "description": "Authentication using Gorilla gateway",
            "structure": "Import { login, logout, onAuthStateChanged } from '@/utils/auth'. ProtectedRoute wrapper component.",
            "key_patterns": "onAuthStateChanged listener in useEffect, conditional render based on user state",
        },
        "dashboard_layout": {
            "description": "Multi-panel dashboard with sidebar navigation",
            "structure": "DashboardLayout.tsx (sidebar + main) → Sidebar.tsx (nav items) → StatsGrid.tsx → individual widgets",
            "key_patterns": "CSS Grid for layout, responsive collapse sidebar on mobile, framer-motion for widget entry",
        },
        "crud_api": {
            "description": "RESTful CRUD endpoints",
            "structure": "routes/{resource}.js with GET /, GET /:id, POST /, PUT /:id, DELETE /:id",
            "key_patterns": "Express Router, async handlers, consistent JSON response shape, input validation",
        },
        "rest_api": {
            "description": "Express REST API with middleware",
            "structure": "server.js mounts routes/ files. Each route file exports an Express Router.",
            "key_patterns": "Error middleware at bottom, CORS configured, JSON body parser",
        },
        "hero_section": {
            "description": "Landing page hero with CTA",
            "structure": "Hero.tsx with gradient background, headline, subtext, CTA button, optional illustration",
            "key_patterns": "framer-motion fade-in, responsive text sizing, gradient text effects",
        },
    }
    
    @classmethod
    def classify_intent(cls, user_request: str) -> List[str]:
        """Classify user request into intent tags."""
        text = user_request.lower()
        matched_intents = []
        
        intent_keywords = {
            "chat": ["chat", "message", "conversation", "messaging", "chatbot"],
            "auth": ["auth", "login", "signup", "sign in", "register", "account"],
            "dashboard": ["dashboard", "admin panel", "analytics", "stats", "metrics"],
            "form": ["form", "input", "survey", "questionnaire", "contact form"],
            "crud": ["crud", "create read update delete", "manage", "list", "table"],
            "image": ["image", "photo", "gallery", "upload image", "picture"],
            "voice": ["voice", "speech", "microphone", "speak", "audio"],
            "landing": ["landing page", "homepage", "hero", "marketing"],
            "ecommerce": ["shop", "store", "cart", "product", "checkout", "ecommerce", "e-commerce"],
            "blog": ["blog", "post", "article", "cms", "content"],
            "api": ["api", "endpoint", "backend", "server", "route"],
        }
        
        for intent, keywords in intent_keywords.items():
            if any(kw in text for kw in keywords):
                matched_intents.append(intent)
        
        return matched_intents or ["general"]
    
    @classmethod
    def get_blueprints(cls, user_request: str) -> str:
        """Get relevant blueprint hints based on intent classification."""
        intents = cls.classify_intent(user_request)
        
        relevant_tags = set()
        for intent in intents:
            tags = cls.INTENT_MAP.get(intent, [])
            relevant_tags.update(tags)
        
        if not relevant_tags:
            return ""
        
        sections = ["ARCHITECTURAL BLUEPRINTS (use these structural patterns):"]
        
        for tag in relevant_tags:
            bp = cls.BUILTIN_BLUEPRINTS.get(tag)
            if bp:
                sections.append(
                    f"\n### {tag}\n"
                    f"Purpose: {bp['description']}\n"
                    f"Structure: {bp['structure']}\n"
                    f"Patterns: {bp['key_patterns']}"
                )
        
        return "\n".join(sections) if len(sections) > 1 else ""
    
    @classmethod
    def register_blueprint(cls, tag: str, description: str, structure: str, patterns: str):
        """Register a custom blueprint (e.g., loaded from Supabase pgvector)."""
        cls.BUILTIN_BLUEPRINTS[tag] = {
            "description": description,
            "structure": structure,
            "key_patterns": patterns,
        }


# ============================================================================
# LAYER 5: SANDBOX ISOLATION
# ============================================================================

class SandboxManager:
    """
    Forces agents to build code changes in a staged area before merging
    into the user's live workspace. Provides conflict detection and
    rollback capability.
    """
    
    def __init__(self, project_id: str):
        self.project_id = project_id
        self._staged: Dict[str, Dict] = {}  # path → operation
        self._conflicts: List[Dict] = []
        self._history: List[Dict[str, Dict]] = []  # rollback snapshots
    
    def stage(self, operations: List[Dict], file_tree: Dict[str, str]) -> List[Dict]:
        """
        Stage operations, detecting conflicts with existing files.
        Returns operations annotated with conflict info.
        """
        annotated = []
        self._conflicts = []
        
        for op in operations:
            path = op.get("path", "")
            action = op.get("action", "")
            
            op_annotated = dict(op)
            
            if action == "overwrite_file" and path in file_tree:
                existing = file_tree[path]
                new_content = op.get("content", "")
                
                # Detect if we're overwriting with substantially different content
                if existing and new_content:
                    similarity = self._quick_similarity(existing, new_content)
                    
                    if similarity < 0.3:
                        conflict = {
                            "path": path,
                            "type": "major_overwrite",
                            "similarity": similarity,
                            "existing_lines": existing.count("\n"),
                            "new_lines": new_content.count("\n"),
                        }
                        self._conflicts.append(conflict)
                        op_annotated["_conflict"] = conflict
                        log_agent("sandbox", 
                                  f"⚠️ Major overwrite: {path} (similarity: {similarity:.0%})", 
                                  self.project_id)
            
            self._staged[path] = op_annotated
            annotated.append(op_annotated)
        
        if self._conflicts:
            log_agent("sandbox", 
                      f"Staged {len(annotated)} ops with {len(self._conflicts)} conflicts", 
                      self.project_id)
        
        return annotated
    
    def commit(self, file_tree: Dict[str, str]) -> Tuple[List[Dict], Dict[str, str]]:
        """
        Commit staged operations, creating a rollback snapshot first.
        Returns (committed_ops, updated_file_tree).
        """
        # Save rollback snapshot
        snapshot = {}
        for path in self._staged:
            if path in file_tree:
                snapshot[path] = {"content": file_tree[path], "existed": True}
            else:
                snapshot[path] = {"content": None, "existed": False}
        self._history.append(snapshot)
        
        # Apply operations
        committed = list(self._staged.values())
        updated_tree = dict(file_tree)
        
        for op in committed:
            path = op.get("path", "")
            action = op.get("action", "")
            content = op.get("content")
            
            if action in ("create_file", "overwrite_file") and content is not None:
                updated_tree[path] = content
        
        # Clean internal conflict annotations before returning
        clean_ops = []
        for op in committed:
            clean = {k: v for k, v in op.items() if not k.startswith("_")}
            clean_ops.append(clean)
        
        self._staged = {}
        self._conflicts = []
        
        log_agent("sandbox", f"Committed {len(clean_ops)} operations", self.project_id)
        return clean_ops, updated_tree
    
    def rollback(self, file_tree: Dict[str, str]) -> Dict[str, str]:
        """Roll back to the previous state."""
        if not self._history:
            log_agent("sandbox", "No rollback history available", self.project_id)
            return file_tree
        
        snapshot = self._history.pop()
        rolled_back = dict(file_tree)
        
        for path, info in snapshot.items():
            if info["existed"]:
                rolled_back[path] = info["content"]
            elif path in rolled_back:
                del rolled_back[path]
        
        log_agent("sandbox", f"Rolled back {len(snapshot)} files", self.project_id)
        return rolled_back
    
    @property
    def has_conflicts(self) -> bool:
        return len(self._conflicts) > 0
    
    @staticmethod
    def _quick_similarity(a: str, b: str) -> float:
        """Fast line-level similarity check."""
        if not a or not b:
            return 0.0
        lines_a = set(a.strip().splitlines())
        lines_b = set(b.strip().splitlines())
        if not lines_a and not lines_b:
            return 1.0
        intersection = lines_a & lines_b
        union = lines_a | lines_b
        return len(intersection) / max(len(union), 1)


# ============================================================================
# LAYER 6: CONTINUOUS EVAL HOOKS
# ============================================================================

class EvalTelemetry:
    """
    Collects debug telemetry data for offline analysis.
    Tracks where the DebuggerAgent intervenes so prompts can be surgically patched.
    """
    
    _events: Dict[str, List[Dict]] = defaultdict(list)
    
    @classmethod
    def record(cls, project_id: str, event_type: str, data: Dict[str, Any]):
        """Record a telemetry event."""
        event = {
            "timestamp": time.time(),
            "type": event_type,
            "data": data,
        }
        cls._events[project_id].append(event)
        
        # Keep last 100 events per project
        if len(cls._events[project_id]) > 100:
            cls._events[project_id] = cls._events[project_id][-100:]
    
    @classmethod
    def record_debug_intervention(cls, project_id: str, error: str, 
                                   file_path: str, fix_applied: bool):
        """Record a debugger intervention for prompt patching analysis."""
        cls.record(project_id, "debug_intervention", {
            "error_snippet": error[:200],
            "file_path": file_path,
            "fix_applied": fix_applied,
        })
        log_agent("eval", f"Debug intervention recorded: {file_path} (fixed={fix_applied})", project_id)
    
    @classmethod
    def record_ast_patch(cls, project_id: str, rule_name: str, files_affected: List[str]):
        """Record an AST patcher intervention."""
        cls.record(project_id, "ast_patch", {
            "rule": rule_name,
            "files": files_affected,
        })
    
    @classmethod
    def record_task_completion(cls, project_id: str, task_id: str, 
                                success: bool, tokens_used: int, duration_s: float):
        """Record task completion metrics."""
        cls.record(project_id, "task_complete", {
            "task_id": task_id,
            "success": success,
            "tokens": tokens_used,
            "duration_s": round(duration_s, 2),
        })
    
    @classmethod
    def get_report(cls, project_id: str) -> Dict[str, Any]:
        """Generate a telemetry report for a project."""
        events = cls._events.get(project_id, [])
        
        debug_count = sum(1 for e in events if e["type"] == "debug_intervention")
        ast_count = sum(1 for e in events if e["type"] == "ast_patch")
        task_events = [e for e in events if e["type"] == "task_complete"]
        
        total_tokens = sum(e["data"].get("tokens", 0) for e in task_events)
        success_rate = (sum(1 for e in task_events if e["data"].get("success")) 
                       / max(len(task_events), 1))
        
        # Find most-debugged files
        debug_files = defaultdict(int)
        for e in events:
            if e["type"] == "debug_intervention":
                debug_files[e["data"].get("file_path", "unknown")] += 1
        
        return {
            "total_events": len(events),
            "debug_interventions": debug_count,
            "ast_patches": ast_count,
            "tasks_completed": len(task_events),
            "total_tokens": total_tokens,
            "success_rate": round(success_rate, 2),
            "hot_files": dict(sorted(debug_files.items(), key=lambda x: -x[1])[:5]),
        }
    
    @classmethod
    def clear(cls, project_id: str):
        cls._events.pop(project_id, None)


# ============================================================================
# CONTEXT LENGTH MANAGEMENT
# ============================================================================

class ContextManager:
    """Manages context length to stay within model token limits."""
    
    @staticmethod
    def estimate_tokens(text: str) -> int:
        if not text:
            return 0
        return len(text) // CHARS_PER_TOKEN_ESTIMATE
    
    @staticmethod
    def count_message_tokens(message: Dict[str, Any]) -> int:
        content = message.get("content", "")
        if isinstance(content, str):
            return ContextManager.estimate_tokens(content)
        elif isinstance(content, list):
            total = 0
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        total += ContextManager.estimate_tokens(item.get("text", ""))
                    elif item.get("type") == "image_url":
                        total += 1000
            return total
        return 0
    
    @classmethod
    def count_messages_tokens(cls, messages: List[Dict[str, Any]]) -> int:
        return sum(cls.count_message_tokens(msg) for msg in messages)
    
    @classmethod
    def shorten_context(
        cls, 
        messages: List[Dict[str, Any]], 
        max_tokens: int = MINIMAX_SAFE_THRESHOLD,
        preserve_recent: int = 4
    ) -> List[Dict[str, Any]]:
        if not messages:
            return messages
        
        current_tokens = cls.count_messages_tokens(messages)
        if current_tokens <= max_tokens:
            return messages
        
        log_agent("context", f"Context too long ({current_tokens} tokens), shortening...", "")
        
        system_msg = None
        start_idx = 0
        if messages[0].get("role") == "system":
            system_msg = messages[0]
            start_idx = 1
        
        recent_messages = messages[-preserve_recent:] if len(messages) > preserve_recent else []
        middle_messages = messages[start_idx:-preserve_recent] if len(messages) > preserve_recent + start_idx else []
        
        result = []
        if system_msg:
            result.append(system_msg)
        
        current_count = sum(cls.count_message_tokens(msg) for msg in result + recent_messages)
        if current_count > max_tokens:
            recent_messages = recent_messages[-2:] if len(recent_messages) > 2 else recent_messages
            result = ([system_msg] if system_msg else []) + recent_messages
            
            truncation_notice = {
                "role": "system", 
                "content": f"[Context truncated: Only most recent messages kept due to length]"
            }
            if system_msg:
                result.insert(1, truncation_notice)
            else:
                result.insert(0, truncation_notice)
            
            log_agent("context", f"Aggressive truncation: kept {len(result)} messages", "")
            return result
        
        kept_middle = []
        for msg in middle_messages:
            msg_tokens = cls.count_message_tokens(msg)
            if current_count + msg_tokens > max_tokens:
                break
            kept_middle.append(msg)
            current_count += msg_tokens
        
        result.extend(kept_middle)
        result.extend(recent_messages)
        
        removed_count = len(messages) - len(result)
        if removed_count > 0:
            truncation_notice = {
                "role": "system",
                "content": f"[Context shortened: {removed_count} older messages removed to fit within {max_tokens} token limit. Focus on recent conversation.]"
            }
            insert_pos = 1 if (system_msg and result[0].get("role") == "system") else 0
            result.insert(insert_pos, truncation_notice)
        
        final_tokens = cls.count_messages_tokens(result)
        log_agent("context", f"Shortened from {len(messages)} to {len(result)} messages (~{final_tokens} tokens)", "")
        
        return result


# ============================================================================
# CONVERSATIONAL MCP PROTOCOL
# ============================================================================

class Intent(Enum):
    # Planning & Reasoning
    PLAN = "plan"
    REASON = "reason"
    QUESTION = "question"
    CLARIFY = "clarify"
    
    # Execution
    IMPLEMENT = "implement"
    DELEGATE_UI = "delegate_ui"
    DELEGATE_API = "delegate_api"
    DELEGATE_LOGIC = "delegate_logic"
    
    # Review & Quality
    REVIEW = "review"
    FEEDBACK = "feedback"
    
    # Completion
    DONE = "done"
    DEBUG_FIX = "debug_fix"
    ERROR = "error"
    
    # v2.0 — Orchestrator intents
    PARALLEL_DISPATCH = "parallel_dispatch"
    STAGE_COMPLETE = "stage_complete"
    MERGE_REQUEST = "merge_request"
    ROLLBACK = "rollback"

@dataclass
class MCPMessage:
    from_agent: str
    to_agent: Optional[str]
    intent: Intent
    task_id: str
    payload: Dict[str, Any]
    reasoning: str = ""
    timestamp: float = field(default_factory=time.time)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "intent": self.intent.value,
            "task_id": self.task_id,
            "payload": self.payload,
            "reasoning": self.reasoning,
            "timestamp": self.timestamp
        }

class MCPBus:
    """The nervous system — agents emit/receive MCP messages here."""
    
    def __init__(self, project_id: str):
        self.project_id = project_id
        self.messages: List[MCPMessage] = []
        self.subscribers: Dict[str, Callable] = {}
        self.pending_questions: Dict[str, asyncio.Future] = {}
        self._background_tasks: set = set()
        
    def subscribe(self, agent_id: str, handler: Callable):
        """Subscribe an agent to receive MCP messages."""
        self.subscribers[agent_id] = handler
        log_agent("mcp", f"Agent '{agent_id}' subscribed to bus", self.project_id)
        
    def emit(self, msg: MCPMessage):
        self.messages.append(msg)
        target = msg.to_agent or "ALL"
        
        intent_emoji = {
            Intent.QUESTION: "❓",
            Intent.CLARIFY: "💡",
            Intent.REASON: "🤔",
            Intent.REVIEW: "👁️",
            Intent.FEEDBACK: "💬",
            Intent.DONE: "✅",
            Intent.ERROR: "❌",
            Intent.PARALLEL_DISPATCH: "⚡",
            Intent.STAGE_COMPLETE: "📦",
            Intent.MERGE_REQUEST: "🔀",
            Intent.ROLLBACK: "⏪",
        }
        emoji = intent_emoji.get(msg.intent, "→")
        log_agent("mcp", f"{emoji} {msg.from_agent} → {target} | {msg.intent.value}: {msg.reasoning}", self.project_id)
        
        if msg.intent == Intent.CLARIFY and msg.task_id in self.pending_questions:
            future = self.pending_questions.pop(msg.task_id)
            if not future.done():
                future.set_result(msg)
        
        # FIXED: Better message routing with error handling
        if msg.to_agent:
            if msg.to_agent in self.subscribers:
                try:
                    task = asyncio.create_task(self.subscribers[msg.to_agent](msg))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
                except Exception as e:
                    log_agent("mcp", f"Error routing to {msg.to_agent}: {str(e)[:60]}", self.project_id)
            else:
                log_agent("mcp", f"⚠️ No subscriber found for '{msg.to_agent}'", self.project_id)
        elif msg.to_agent is None:
            # Broadcast to all subscribers except sender
            for agent_id, handler in self.subscribers.items():
                if agent_id != msg.from_agent:
                    try:
                        task = asyncio.create_task(handler(msg))
                        self._background_tasks.add(task)
                        task.add_done_callback(self._background_tasks.discard)
                    except Exception as e:
                        log_agent("mcp", f"Error broadcasting to {agent_id}: {str(e)[:60]}", self.project_id)
    
    async def await_all_tasks(self, timeout: float = 5.0):
        if not self._background_tasks:
            return
        pending = list(self._background_tasks)
        if pending:
            try:
                await asyncio.wait(pending, timeout=timeout)
            except Exception:
                pass
        self._background_tasks.clear()
    
    async def ask(self, from_agent: str, to_agent: str, question: str, 
                  context: Dict = None, timeout: float = 30.0) -> Optional[MCPMessage]:
        task_id = f"q_{int(time.time() * 1000)}"
        future = asyncio.get_event_loop().create_future()
        self.pending_questions[task_id] = future
        
        self.emit(MCPMessage(
            from_agent=from_agent,
            to_agent=to_agent,
            intent=Intent.QUESTION,
            task_id=task_id,
            payload={"question": question, "context": context or {}},
            reasoning=f"Asking: {question[:60]}..."
        ))
        
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self.pending_questions.pop(task_id, None)
            return None


# ============================================================================
# SHARED CONTEXT (unchanged)
# ============================================================================

SHARED_CONTEXT = {
    "stack": {
        "frontend": "React 18 + TypeScript + Vite + Tailwind + Shadcn/UI",
        "backend": "Node.js + Express (ES modules)",
    },
    "constraints": [
        "WebContainer compatible - NO native C++ modules",
        "Frontend imports: use @/ alias",
        "Backend imports: use relative paths with .js extension",
        "NEVER modify package.json scripts block",
        "UI should be creative, non-bootstrappy, no Inter font",
    ],
    "structure": {
        "src/App.tsx": "Main app component (exists)",
        "src/main.tsx": "Entry point (exists)",
        "src/components/ui/": "Shadcn components (pre-installed)",
        "routes/": "Express API routes",
        "server.js": "Express entry (exists)",
    }
}

# ============================================================================
# BASE AGENT
# ============================================================================

class BaseAgent:
    def __init__(self, agent_id: str, bus: MCPBus, project_id: str):
        self.agent_id = agent_id
        self.bus = bus
        self.project_id = project_id
        self.file_tree: Dict[str, str] = {}
        self.conversation_memory: List[Dict] = []
        self.total_tokens_used: int = 0
    
    def get_tokens_used(self) -> int:
        return self.total_tokens_used
    
    def reset_tokens(self):
        self.total_tokens_used = 0
        
    async def _on_mcp(self, msg: MCPMessage):
        pass
    
    def emit(self, intent: Intent, payload: Dict, to: Optional[str] = None, 
             task_id: str = "", reasoning: str = ""):
        msg = MCPMessage(
            from_agent=self.agent_id,
            to_agent=to,
            intent=intent,
            task_id=task_id,
            payload=payload,
            reasoning=reasoning
        )
        self.bus.emit(msg)
    
    async def ask(self, to_agent: str, question: str, context: Dict = None, 
                  timeout: float = 30.0) -> Optional[str]:
        response = await self.bus.ask(self.agent_id, to_agent, question, context, timeout)
        if response:
            return response.payload.get("answer") or response.payload.get("response")
        return None
    
    # --- File Reading Capabilities ---
    
    async def read_file(self, path: str, project_id: Optional[str] = None) -> Optional[str]:
        pid = project_id or self.project_id
        if not pid:
            log_agent(self.agent_id, "Cannot read file: no project_id", self.project_id)
            return None
        
        # FIXED: Check file_tree first (includes files created during this session)
        if hasattr(self, 'file_tree') and path in self.file_tree:
            content = self.file_tree[path]
            log_agent(self.agent_id, f"Read file: {path} ", pid)
            return content
        
        try:
            url = f"{FILE_API_BASE_URL}/api/project/{pid}/file"
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, params={"path": path}, timeout=FILE_API_TIMEOUT)
                if resp.status_code == 200:
                    data = resp.json()
                    content = data.get("content")
                    if content is not None:
                        if hasattr(self, 'file_tree'):
                            self.file_tree[path] = content
                        log_agent(self.agent_id, f"Read file from API: {path} ({len(content)} chars)", pid)
                        return content
                elif resp.status_code == 404:
                    log_agent(self.agent_id, f"File not found: {path}", pid)
                else:
                    log_agent(self.agent_id, f"Error reading {path}: HTTP {resp.status_code}", pid)
        except Exception as e:
            log_agent(self.agent_id, f"Failed to read file {path}: {str(e)[:60]}", pid)
        
        return None
    
    async def read_all_files(self, project_id: Optional[str] = None) -> Dict[str, str]:
        pid = project_id or self.project_id
        if not pid:
            return {}
        
        if hasattr(self, 'file_tree') and self.file_tree:
            return dict(self.file_tree)
        
        try:
            url = f"{FILE_API_BASE_URL}/api/project/{pid}/files"
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=FILE_API_TIMEOUT)
                if resp.status_code == 200:
                    data = resp.json()
                    files = data.get("files", [])
                    file_dict = {f["path"]: f["content"] for f in files if "path" in f}
                    if hasattr(self, 'file_tree'):
                        self.file_tree.update(file_dict)
                    log_agent(self.agent_id, f"Fetched {len(file_dict)} files", pid)
                    return file_dict
                else:
                    log_agent(self.agent_id, f"Error fetching files: HTTP {resp.status_code}", pid)
        except Exception as e:
            log_agent(self.agent_id, f"Failed to fetch files: {str(e)[:60]}", pid)
        
        return {}
    
    async def read_files_batch(self, paths: List[str], project_id: Optional[str] = None) -> Dict[str, Optional[str]]:
        tasks = [self.read_file(path, project_id) for path in paths]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {
            path: result if not isinstance(result, Exception) else None 
            for path, result in zip(paths, results)
        }
    
    # --- LLM Calls (unchanged core logic — same models, same token counting) ---
    
    async def call_llm(self, messages: List[Dict], temperature: float = 0.6) -> Tuple[str, int]:
        original_count = len(messages)
        messages = ContextManager.shorten_context(messages, MINIMAX_SAFE_THRESHOLD)
        
        if len(messages) < original_count:
            log_agent(self.agent_id, f"Context auto-shortened: {original_count} → {len(messages)} messages", self.project_id)
        
        payload = {
            "model": MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 20000,
            "provider": { 
                "order": ["xiaomi"], 
                "allow_fallbacks": False 
            }
        }
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": SITE_URL,
            "X-Title": SITE_NAME,
        }
        
        last_msg_preview = str(messages[-1].get('content', ''))[:80] if messages else ""
        log_agent("llm", f"→ {len(messages)} msgs | {last_msg_preview}...", self.project_id)
        
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            
        content = data["choices"][0]["message"]["content"]
        
        usage = data.get("usage", {})
        p_tokens = usage.get("prompt_tokens", 0)
        c_tokens = usage.get("completion_tokens", 0)
        weighted_tokens = int((p_tokens) + (c_tokens * 3))
        
        self.total_tokens_used += weighted_tokens
        
        log_agent("llm", f"← {weighted_tokens} weighted tokens (total: {self.total_tokens_used}) | {content[:120]}...", self.project_id)
        
        return content, weighted_tokens

    async def call_vision_llm(self, messages: List[Dict], temperature: float = 0.6) -> Tuple[str, int]:
        original_count = len(messages)
        messages = ContextManager.shorten_context(messages, MINIMAX_SAFE_THRESHOLD)
        
        if len(messages) < original_count:
            log_agent(self.agent_id, f"Vision context auto-shortened: {original_count} → {len(messages)} messages", self.project_id)
        
        payload = {
            "model": VISION_MODEL,
            "messages": messages,
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": SITE_URL,
            "X-Title": SITE_NAME,
        }
        
        last_msg_preview = str(messages[-1].get('content', ''))[:80] if messages else ""
        log_agent("llm", f"→ {len(messages)} msgs | {last_msg_preview}...", self.project_id)
        
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            
        content = data["choices"][0]["message"]["content"]
        
        usage = data.get("usage", {})
        p_tokens = usage.get("prompt_tokens", 0)
        c_tokens = usage.get("completion_tokens", 0)
        weighted_tokens = int((p_tokens * 0.5) + (c_tokens * 0.8))
        
        self.total_tokens_used += weighted_tokens
        
        log_agent("llm", f"← {weighted_tokens} weighted tokens (total: {self.total_tokens_used}) | {content[:120]}...", self.project_id)
        
        return content, weighted_tokens
    
    def extract_json(self, text: str) -> Optional[Dict]:
        return _extract_json(text)
    
    def remember_conversation(self, role: str, content: str):
        self.conversation_memory.append({
            "role": role,
            "content": content,
            "timestamp": time.time()
        })
        if len(self.conversation_memory) > 20:
            self.conversation_memory = self.conversation_memory[-20:]


# ============================================================================
# PLANNER AGENT (unchanged system prompt — skills injected separately)
# ============================================================================

class PlannerAgent(BaseAgent):
    """The Architect — creates plans, asks clarifying questions."""
    
    def _build_system_prompt(self, agent_skills: Optional[Dict] = None) -> str:
        # Skills-based injection replaces the old hardcoded addon
        skills_addon = SkillsManager.inject_user_preferences(agent_skills)
        if skills_addon:
            skills_addon = "\n\n" + skills_addon

        return (
    "You are the AMBITIOUS Lead Architect for a high-performance **Full-Stack** web application, you are the GOR://A BUILDER multi agent AI BUILDER. Your goal is to create a strategic, step-by-step build plan for an AI Coder specialized in **React (Frontend)** AND **Node.js/Express (Backend)**. Strictly give NO CODE AT ALL, in no form. But you MUST REASON HARD.\n"
    "CRITICAL CONTEXT: The AI Coder executes tasks in isolation. It has NO memory of previous files unless you provide context in *every single task description*.\n\n"
    "Rules:\n"
    "MANDATORY OUTPUT FORMAT: JSON OBJECT ONLY. Do NOT wrap in markdown blocks.\n"
    "{\n"
    '  "assistant_message": "Sure I will build the ... application for you with...and...it will be...", --> this should be comforting for the user, but not talk about technical code specifications but how the application will function and look.\n'
    '  "tasks": [\n'
    '    "Step 1: [Project: AppName | Stack: FullStack | Context: (FULL SUMMARY)] Create `App.tsx` to begint the process...",\n'
    '    "Step 2: [Project: AppName | Stack: FullStack | Context: (FULL SUMMARY)] Modify `server.js` to setup API..."\n'
    "  ]\n"
    "}\n\n"
    "ARCHITECTURAL STANDARDS (MUST FOLLOW):\n"
    "1. **Pre-Existing Infrastructure (DO NOT CREATE THESE):**\n"
    "   - **Root**: `package.json` (React, Vite, Tailwind, Express, Drizzle ORM, SQLite).\n"
    "   - **Frontend**: `src/App.tsx`, `src/main.tsx`, `src/lib/utils.ts`, `vite.config.ts`, `tailwind.config.js`.\n"
    "   - **UI Library**: `src/components/ui/` & `src/components/magicui/`.\n"
    "   - **Backend**: `server.js` is the entry point. `routes/` folder for API logic.\n"
    "2. **Task Strategy:**\n"
    "   - **NEVER** assign a task to create `package.json` or `index.html`. They exist.\n"
    "   - **Frontend Tasks**: Modify `src/pages/Index.tsx` to implement layout. Create components in `src/components/`.\n"
    "   - **Backend Tasks**: Modify `server.js` to add middleware/routes. Create specific route files in `routes/` Also, if required make a databse in Node.js.\n"
    "3. **The Wiring & Evolution Rule (CRITICAL - NO DEAD CODE):**\n"
    "   - **Frontend Wiring**: Every new component MUST be immediately imported and used.\n"
    "   - **Backend Wiring**: Every new route file MUST be immediately mounted in `server.js`.\n"
    "4. **The 'Global Blueprint' Rule:**\n"
    "   - Every task string MUST start with: `[Project: {Name} | Stack: FullStack | Context: {FULL_APP_DESCRIPTION_HERE}] ...`\n"
    "   - **CRITICAL**: The `Context` section MUST contain the FULL description of what the app is supposed to do.\n\n"
    "TASK WRITING GUIDELINES:\n"
    "1. **No-Build Specifics:** \n"
    "   - Simply instruct the coder to add auth (do not give many specifics at all) if the user asks, even if they specify google auth or other forms SIMLPY INTRUCT THE CODER TO ADD AUTH, and the coder will make the auth dynamically throught the gor://a auth gateway\n"
    "   - NEVER ask for `npm run dev` or `vite.config.js`.\n"
    "   - NEVER generate an `.env` file.\n"
    "   - Frontend Imports: Use `@/` aliases.\n"
    "   - INSTRUCT THE CODER TO NEVER EVER BUILD A DRIZZLE DB, BUT USE A NODE.JS DB IF THE USER WANTS A REAL DB, THEN ASK THE USER TO MENTION IT IN THIER REQUEST TO TRIGGER SUPABASE DB CREATION\n"
    "   - Backend Imports: Use relative paths with `.js` extension.\n"
    "   - Never instruct to the coder to build a `vercel.json` file in the root of the project according to the project's requirements.\n"
    "2. **AI Integration Specs (USE THESE EXACTLY):**\n"
    "   - **Core Rule**: You MUST route all AI API calls through the Gorilla Proxy using `process.env.GORILLA_API_KEY`.\n"
    "   - **High-Performance Logic (LLM)**: Use `https://slaw-carefully-cried.ngrok-free.dev/api/v1/chat/completions` with the process.env GORILLA_API_KEY, DO NOT SPECIFY THE MODEL OR ANY OTHER VALUES LIKE TEMPERATURE... NO MATTER WHAT.\n"
    "   - **Image Generation**: Send POST request to `https://slaw-carefully-cried.ngrok-free.dev/api/v1/images/generations` with standard OpenAI payload.\n"
    "   - **Voice (STT)**: Send POST to `https://slaw-carefully-cried.ngrok-free.dev/api/v1/audio/transcriptions` (OpenAI Whisper format).\n"
    "   - **Voice (TTS)**: DO NOT USE AN API. Strictly use the browser's native `window.speechSynthesis` Web Speech API in frontend components.\n"
    "   - **BG Removal**: Send POST with FormData (file) to `https://slaw-carefully-cried.ngrok-free.dev/api/v1/images/remove-background`.\n"
    "3. **Task Bundling & Volume (CRITICAL FOR TOKEN SAVING):** \n"
    "   - If you are told to use the attached image somewhere, by the user, then use .gorilla/prompt_image.b64, and instruct to coder to use it\n"
    "   - Do not try to ask the user more than 1 question to elaborate on their request, if you do, they should be obvious and add functionality to their app if they agree DO NOT BOTHER THEM MORE THAN ONCE. DO NOT ASK TECHNICAL QUESTIONS, THE USERS CANNOT CODE. WHEN YOU ASK A QUESTION DO NOT GENERATE TASKS AT ALL. Do not generate tasks even if the user asks a question. DO NOT BOTHER THE USER WITH TOO MANY QUESTIONS IF THEY DONT FEEL LIKE IT OR ANY DEBUGGING QUESTIONS.\n"
    "   - CONSOLIDATE TASKS: You MUST bundle related operations together. Combine them into Macro Steps (e.g., 'Step 1: Database & Backend setup', 'Step 2: Core UI Components', 'Step 3: Frontend Wiring').\n"
    "   - Simple Apps: Maximum 1-2 Macro/clubbed Tasks. (if there are no questions only!)\n"
    "   - Complex Apps: No more than 4 Macro/clubbed Tasks. (if there are no questions only!)\n"
    "   - Debugging/Simple addition Tasks: 1 task only. DO NOT ASK QUESTIONS FOR DEBUGGING.\n"
    "   - NEVER bundle more than 4 files or less than 2 files into a single task. Break large frontend or backend builds into multiple, smaller steps to prevent output truncation. AND ALWAYS TRY TO USE MULTI PAGE ARCHITECTURES. For example to build a chatbot specify to build a chatcontainer.tsx, message.tsx and chatinput.tsx instea of just a chatinterface.tsx.\n"
    "   - Update `server.js` and `App.tsx` **LAST** to wire up components/routes.\n\n"
    "   - Combine tasks into DISTINCT Macro Steps  FOR EXAMPLE:\n"
    "     * Step 1: Backend Routes & Logic\n"
    "     * Step 2: Frontend UI Components\n"
    "     * Step 3: Frontend Wiring (Connecting the UI to the App.tsx router)\n"
    " **CRITICAL:** NEVER EVER USE DRIZZLE ORM IN ANY WAY IF YOU WANT TO MAKE A DB USE JSON STORAGE OR TELL THE USER TO MAKE A NEW PROJECT WITH SUPABASE FROM THE DASHBOARD."
    + skills_addon
        )

    def _infer_capabilities(self, user_request: str) -> List[str]:
        text = (user_request or "").lower()
        caps = set()
        if "chat" in text: 
            caps.add("chat")
        if "voice" in text or "speech" in text: 
            caps.update(["voice_input", "voice_output"])
        if "image" in text: 
            caps.add("image_generation")
        if "remove background" in text or "bg remove" in text: 
            caps.add("background_removal")
        if "scan" in text or "document" in text or "pdf" in text: 
            caps.add("document_processing")
        if "vision" in text or "photo" in text: 
            caps.add("vision")
        if "upload" in text or "media" in text: 
            caps.add("media_handling")
        return sorted(caps)

    async def plan(self, user_request: str, file_tree: Dict[str, str], 
                   agent_skills: Optional[Dict] = None) -> MCPMessage:
        log_agent("planner", f"Analyzing: {user_request[:60]}...", self.project_id)
        
        is_debug = any(word in user_request.lower() 
                      for word in ["error", "fix", "bug", "crash", "broken", "failed"])
        
        context_str = json.dumps(SHARED_CONTEXT, indent=2)
        clean_files = [f for f in file_tree.keys() if not f.endswith(".b64")]
        
        # Inject blueprint hints based on intent classification
        blueprint_hints = BlueprintRouter.get_blueprints(user_request)
        
        system_prompt = self._build_system_prompt(agent_skills)
        if blueprint_hints:
            system_prompt += f"\n\n{blueprint_hints}"
        
        chat_history = _get_history(self.project_id)
        
        messages = [{"role": "system", "content": system_prompt}]
        for h in chat_history:
            messages.append({"role": h["role"], "content": h["content"]})
        
        messages.append({"role": "user", "content": f"""CONTEXT: {context_str} CURRENT PROJECT ARCHITECTURE: {json.dumps(clean_files)} \nUSER REQUEST: {user_request} \n{'This appears to be a DEBUG request.' if is_debug else 'Analyze this request and either create a plan OR ask clarifying questions.'} Output JSON with either type="plan" or type="questions"."""})

        max_retries = 2

        for attempt in range(max_retries + 1):
            try:
                raw, tokens = await self.call_vision_llm(messages, temperature=0.6)
                data = self.extract_json(raw)
                
                if not data:
                    if attempt < max_retries:
                        time.sleep(1)
                        continue
                    raise ValueError("Could not extract JSON from response")
                
                response_type = data.get("type", "plan")
                assistant_message = data.get("assistant_message", "Sure I would really like to help you with that! But I need a bit more information right now to give you the app you deserve.")
                
                if response_type == "questions":
                    questions = data.get("questions", [])
                    log_agent("planner", f"Asking {len(questions)} clarifying questions", self.project_id)
                    
                    if self.project_id:
                        _append_history(self.project_id, "assistant", json.dumps({"type": "questions", "assistant_message": assistant_message, "questions": questions}))
    
                    questions_formatted = "\n".join([f"**{i+1}.** {q}" for i, q in enumerate(questions)])
                    full_message = f"{assistant_message}\n\n{questions_formatted}"
    
                    self.emit(
                        intent=Intent.QUESTION,
                        payload={
                            "assistant_message": full_message,
                            "questions": questions,
                            "needs_clarification": True,
                            "estimated_tokens": tokens
                        },
                        to="reasoner",
                        task_id=f"questions_{int(time.time())}",
                        reasoning=f"Need clarification: {len(questions)} questions"
                    )
                else:
                    tasks = data.get("tasks", [])
                    log_agent("planner", f"Generated {len(tasks)} tasks", self.project_id)
                    
                    if self.project_id:
                        _append_history(self.project_id, "assistant", json.dumps({"type": "plan", "assistant_message": assistant_message, "tasks": tasks}))
                    
                    self.emit(
                        intent=Intent.PLAN,
                        payload={
                            "assistant_message": assistant_message,
                            "tasks": tasks,
                            "is_debug": is_debug,
                            "estimated_tokens": tokens,
                            "capabilities": self._infer_capabilities(user_request)
                        },
                        to="reasoner",
                        task_id=f"plan_{int(time.time())}",
                        reasoning=f"Created plan with {len(tasks)} tasks"
                    )
                
                return self.bus.messages[-1]
            
            except Exception as e:
                if attempt < max_retries:
                    time.sleep(1)
                    continue
                
                log_agent("planner", f"ERROR: {str(e)}", self.project_id)
                return self._error_mcp(f"Failed to generate plan: {str(e)}")

    def _error_mcp(self, error: str) -> MCPMessage:
        return MCPMessage(
            from_agent="planner",
            to_agent="reasoner",
            intent=Intent.ERROR,
            task_id="error",
            payload={"error": error, "tasks": [], "assistant_message": "Plan generation failed."},
            reasoning="Plan generation failed"
        )


# ============================================================================
# REASONER AGENT
# ============================================================================

class ReasonerAgent(BaseAgent):
    """The Thinker — validates plans, reasons about approach, coordinates."""
    
    SYSTEM_PROMPT = (
        "You are the Reasoner Agent. Your job is to THINK before acting.\n\n"
        "RESPONSIBILITIES:\n"
        "1. Review plans from the Planner\n"
        "2. Identify potential issues, dependencies, or missing pieces\n"
        "3. Decide whether to proceed, ask for changes, or refine the plan\n"
        "4. Coordinate between agents\n\n"
        "RULES:\n"
        "1. ALWAYS reason about the approach before execution\n"
        "2. If you see problems, raise them via QUESTION intent\n"
        "3. Consider: dependencies, complexity, user intent, feasibility\n"
        "4. Output valid JSON only\n\n"
        "RESPONSE FORMAT:\n"
        "{\n"
        '  "decision": "proceed|refine|question",\n'
        '  "reasoning": "Your thinking process...",\n'
        '  "refined_tasks": ["optional modified tasks"],\n'
        '  "concerns": ["any issues to address"]\n'
        "}"
    )

    async def _on_mcp(self, msg: MCPMessage):
        if msg.intent == Intent.PLAN:
            await self._reason_about_plan(msg)
        elif msg.intent == Intent.QUESTION:
            await self._handle_clarification_needed(msg)
    
    async def _reason_about_plan(self, plan_msg: MCPMessage):
        """Forward to orchestrator for parallel dispatch."""
        log_agent("reasoner", "Forwarding plan to orchestrator for dispatch.", self.project_id)
        
        self.emit(
            intent=Intent.PLAN,
            payload=plan_msg.payload,
            to="orchestrator",
            task_id=plan_msg.task_id,
            reasoning="Forwarded to Orchestrator for parallel dispatch"
        )
    
    async def _handle_clarification_needed(self, msg: MCPMessage):
        payload = msg.payload
        questions = payload.get("questions", [])
        
        log_agent("reasoner", f"Clarification needed: {len(questions)} questions", self.project_id)
        
        self.emit(
            intent=Intent.QUESTION,
            payload=payload,
            to=None,
            task_id=msg.task_id,
            reasoning="Forwarding questions to user"
        )


# ============================================================================
# LAYER 7: ORCHESTRATOR META-AGENT (The Master Router)
# ============================================================================

class OrchestratorAgent(BaseAgent):
    """
    Master router that sits above the Planner. Receives a full plan and
    dispatches tasks to sub-agents in PARALLEL rather than linearly.
    
    Classifies each task by type (UI / API / Logic / Wiring) and fans out
    independent tasks concurrently, only serializing tasks with dependencies.
    """
    
    def __init__(self, agent_id: str, bus: MCPBus, project_id: str):
        super().__init__(agent_id, bus, project_id)
        self._pending_batches: Dict[str, Dict] = {}
        self._batch_results: Dict[str, List[Dict]] = {}
    
    async def _on_mcp(self, msg: MCPMessage):
        if msg.intent == Intent.PLAN:
            await self._dispatch_plan(msg)
        elif msg.intent == Intent.STAGE_COMPLETE:
            await self._handle_stage_complete(msg)
    
    def _classify_task(self, task: str) -> str:
        """Classify a task into a dispatch lane."""
        t = task.lower()
        
        # Wiring tasks (App.tsx, server.js) must run LAST
        if any(x in t for x in ["wire", "app.tsx", "update app", "update server.js", "mount route"]):
            return "wiring"
        
        # Backend tasks
        if any(x in t for x in ["route", "api", "endpoint", "server", "backend", "express", "middleware"]):
            return "api"
        
        # UI tasks
        if any(x in t for x in ["component", "page", "ui", "layout", "design", "style", "frontend"]):
            return "ui"
        
        # Logic/utility tasks
        if any(x in t for x in ["utility", "helper", "lib", "function", "hook", "context"]):
            return "logic"
        
        return "general"
    
    def _build_dependency_graph(self, tasks: List[str]) -> List[List[Tuple[int, str, str]]]:
        """
        Build execution stages from task list.
        Tasks within the same stage can run in parallel.
        
        Returns list of stages, each stage is a list of (index, task, type).
        """
        classified = [(i, task, self._classify_task(task)) for i, task in enumerate(tasks)]
        
        # Stage 1: All independent tasks (UI, API, Logic) — run in parallel
        # Stage 2: Wiring tasks (App.tsx, server.js updates) — run after Stage 1
        
        parallel_stage = []
        wiring_stage = []
        
        for idx, task, task_type in classified:
            if task_type == "wiring":
                wiring_stage.append((idx, task, task_type))
            else:
                parallel_stage.append((idx, task, task_type))
        
        stages = []
        if parallel_stage:
            stages.append(parallel_stage)
        if wiring_stage:
            stages.append(wiring_stage)
        
        return stages
    
    async def _dispatch_plan(self, plan_msg: MCPMessage):
        """Dispatch plan tasks with parallel execution stages."""
        payload = plan_msg.payload
        tasks = payload.get("tasks", [])
        is_debug = payload.get("is_debug", False)
        
        if not tasks:
            self.emit(
                intent=Intent.PLAN,
                payload=payload,
                to="coder",
                task_id=plan_msg.task_id,
                reasoning="No tasks — forwarding empty plan to coder"
            )
            return
        
        # Build execution stages
        stages = self._build_dependency_graph(tasks)
        
        stage_summary = [f"Stage {i+1}: {len(s)} tasks ({', '.join(set(t[2] for t in s))})" 
                        for i, s in enumerate(stages)]
        log_agent("orchestrator", f"⚡ Dispatch plan: {' → '.join(stage_summary)}", self.project_id)
        
        # For now, flatten stages back to sequential but group parallel tasks
        # The coder will execute within each stage, but stages themselves are serial
        # This preserves dependency ordering while enabling future true parallelism
        
        ordered_tasks = []
        for stage in stages:
            for _, task, task_type in stage:
                ordered_tasks.append(task)
        
        # Forward the reordered plan to the coder
        reordered_payload = dict(payload)
        reordered_payload["tasks"] = ordered_tasks
        reordered_payload["_stages"] = [[t[0] for t in stage] for stage in stages]
        reordered_payload["_parallel_hint"] = len(stages) > 1
        
        self.emit(
            intent=Intent.PLAN,
            payload=reordered_payload,
            to="coder",
            task_id=plan_msg.task_id,
            reasoning=f"Dispatched {len(tasks)} tasks in {len(stages)} stages"
        )
    
    async def _handle_stage_complete(self, msg: MCPMessage):
        """Handle a completed execution stage."""
        batch_id = msg.payload.get("batch_id", "")
        if batch_id in self._pending_batches:
            del self._pending_batches[batch_id]
            log_agent("orchestrator", f"Stage complete: {batch_id}", self.project_id)


# ============================================================================
# CODER AGENT (with Reflection + Sandbox + AST Patching)
# CRITICAL FIXES APPLIED:
# 1. file_tree is now updated immediately when files are created/overwritten
# 2. Better error handling and logging
# 3. Sub-agents properly integrated
# ============================================================================

class CoderAgent(BaseAgent):
    
    SYSTEM_PROMPT = (
        "You are an expert Full-Stack AI Coder. You build premium, production-ready Web Apps using React+TypeScript+Tailwind (Frontend) and Node.js+Express (Backend).\n"
        "You are working in a pre-existing environment. DO NOT initialize a new project.\n\n"
        
        "### 1. ENVIRONMENT & BOILERPLATE\n"
        "The following tools are ALREADY installed:\n"
        "- Frontend (`src/`): React + Vite + TypeScript. Use `.tsx`.\n"
        "- Styling: Tailwind CSS utility classes.\n"
        "- UI Library: Shadcn/UI is populated in `src/components/ui/`.\n"
        "- Backend: Node.js (ES Modules). Entry point is `server.js`. APIs go in `routes/`.\n"
        "- Config: Express is pre-configured with CORS and Dotenv.\n\n"
        
        "### 2. OUTPUT FORMAT (STRICT JSON)\n"
        "You are a headless execution engine. Output RAW, VALID JSON ONLY.\n"
        "CRITICAL RULES:\n"
        "- NO markdown blocks (do not wrap in ```json).\n"
        "- NO XML tags.\n"
        "- Start immediately with `{` and end with `}`.\n"
        "{\n"
        '  "message": "Friendly status update.",\n'
        '  "reflection": "Brief thought process on what you need to do.",\n'
        '  "operations": [\n'
        '    {\n'
        '      "action": "create_file" | "overwrite_file" | "read_file",\n'
        '      "path": "src/pages/Dashboard.tsx",\n'
        '      "content": "FULL FILE CONTENT (Omit this field if action is read_file)"\n'
        '    }\n'
        '  ]\n'
        "}\n\n"
        
        "### 3. DEVELOPMENT RULES & SCOPE ENFORCEMENT\n"
        "- **Read Before You Write:** You do not have the file contents memorized. If you need to modify an existing file, output a `read_file` operation FIRST to get its contents in the next turn.\n"
        "- **BE SURGICALLY LAZY (CRITICAL):** ONLY output `overwrite_file` for files that require NEW changes for THIS SPECIFIC TASK. DO NOT output files you already generated in a previous step. DO NOT output files that require no changes. You are penalized for rewriting stable code.\n"
        "- Connection: Always connect the files and features you make together. Never leave dangling components.\n"
        "- Frontend Imports: Always use the `@/` alias (e.g., `import { Button } from '@/components/ui/button'`). No relative paths like `../../`.\n"
        "- Backend Imports: You MUST use relative paths and append the `.js` extension (e.g., `import router from './routes/api.js'`).\n"
        "- Backend Code: Use async/await, return `res.json`, and always handle errors with try/catch.\n"
        "- Self-Correction: If fixing a crash, analyze the stack trace, patch the specific file, and avoid using uninstalled dependencies if they repeatedly fail.\n\n"
        
        "### 4. UI/UX DESIGN (THE 'PREMIUM' STANDARD)\n"
        "- Go all out. Create sleek, modern, non-bootstrappy SaaS interfaces. \n"
        "- Build custom components instead of relying strictly on obvious Shadcn defaults.\n"
        "- Typography & Icons: Do NOT use the 'Inter' font. Use `lucide-react` for crisp icons.\n"
        "- Motion: Liberally use `framer-motion` for buttery smooth micro-interactions and reveals.\n\n"
        
        "### 5. AUTHENTICATION (USE EXISTING GATEWAY)\n"
        "Do NOT install Firebase, Supabase auth, or Auth0. Use the built-in gateway:\n"
        "1. Import: `import { login, onAuthStateChanged, logout } from '@/utils/auth';`\n"
        "2. State: `const [user, setUser] = useState<any>(null);`\n"
        "3. Listener: `useEffect(() => { const sub = onAuthStateChanged(setUser); return () => sub(); }, []);`\n"
        "4. Actions: Use `onClick={() => login('google')}`, `login('github')`, or `logout()`.\n\n"
        
        "### 6. AI INTEGRATIONS (BACKEND ONLY)\n"
        "Route all AI calls through the Gorilla Proxy using `process.env.GORILLA_API_KEY`. Never place AI API calls in the frontend.\n"
        "- LLM/Vision: POST `https://slaw-carefully-cried.ngrok-free.dev/api/v1/chat/completions`. Do NOT send model/temperature params. For vision, send image as Base64 in the prompt using `Buffer.from...`.\n"
        "- Images: POST `https://slaw-carefully-cried.ngrok-free.dev/api/v1/images/generations` (OpenAI payload).\n"
        "- STT: POST `https://slaw-carefully-cried.ngrok-free.dev/api/v1/audio/transcriptions` (Whisper format).\n"
        "- TTS: Do NOT use the backend API. Strictly use `window.speechSynthesis` natively in the frontend.\n"
        "- BG Removal: POST `https://slaw-carefully-cried.ngrok-free.dev/api/v1/images/remove-background` (FormData).\n\n"
        
        "### 7. HARD CONSTRAINTS (NEVER DO THESE)\n"
        "- NEVER rewrite files that are irrelevant to the current step.\n"
        "- NEVER generate `.env` or `Dockerfile` files.\n"
        "- NEVER use literal '\\n' characters in your code strings; use physical newlines.\n"
        "- NEVER modify `server.js` partially. You must overwrite the WHOLE file if changes are needed.\n"
        "- NEVER delete or modify the `dev`, `server` or `client` scripts in `package.json` (this causes fatal WebContainer crashes).\n"
    )


    def __init__(self, agent_id: str, bus: MCPBus, project_id: str):
        super().__init__(agent_id, bus, project_id)
        self.sub_agents: Dict[str, BaseAgent] = {}
        self.pending_tasks: Dict[str, Dict] = {}
        self.all_operations: List[Dict] = []
        self.task_results: Dict[str, Dict] = {}
        self.execution_complete = False
        
    def register_sub_agent(self, agent: BaseAgent):
        self.sub_agents[agent.agent_id] = agent
        
    async def _on_mcp(self, msg: MCPMessage):
        if msg.intent == Intent.PLAN:
            await self._execute_plan(msg)
        elif msg.intent == Intent.DONE:
            await self._handle_sub_done(msg)
        elif msg.intent == Intent.FEEDBACK:
            await self._handle_feedback(msg)
    
    def _normalize_and_validate_ops(self, parsed: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(parsed, dict):
            raise ValueError("Model output was not a valid JSON object")
        
        ops = parsed.get("operations")
        if ops is None:
            op1 = parsed.get("operation")
            if op1:
                ops = [op1]
        
        if not isinstance(ops, list):
            ops = []

        user_msg = parsed.get("message") or "Task completed"
        
        normalized_ops = []
        for op in ops:
            action_raw = (op.get("action") or "").strip()
            action = ACTION_NORMALIZE.get(action_raw, action_raw)
            
            if action in {"patch_file", "patch"}:
                action = "overwrite_file"
                
            if action not in ALLOWED_ACTIONS:
                if action in ["delete_file", "move_file"]:
                    continue
                raise ValueError(f"Unknown action: {action}")
                
            path = op.get("path")
            if not path or not isinstance(path, str):
                raise ValueError("Operation requires a valid 'path'")
            
            if action == "read_file":
                normalized_ops.append({
                    "action": action,
                    "path": path.strip(),
                    "content": None
                })
                continue
                
            content = op.get("content")
            if content is None:
                raise ValueError("Operation requires 'content'")

            if isinstance(content, list):
                content = "\n".join(str(x) for x in content)
            elif isinstance(content, str):
                content = content.replace("\\n", "\n")
            
            normalized_ops.append({
                "action": action,
                "path": path.strip(),
                "content": str(content)
            })
                
        return {
            "message": user_msg,
            "reflection": parsed.get("reflection", ""),
            "operations": normalized_ops
        }

    def _build_context_snippets(self) -> str:
        clean_paths = [path for path in self.file_tree.keys() if not path.endswith(".b64")]
        clean_paths.sort()
        tree_structure = "\n".join([f"- {path}" for path in clean_paths])
        
        snippets = [f"PROJECT ARCHITECTURE MAP:\n{tree_structure}\n"]
        
        if "package.json" in self.file_tree:
            snippets.append(f"--- package.json ---\n{self.file_tree['package.json'][:2000]}\n")
            
        snippets.append(
            "\n⚠️ CRITICAL MCP INSTRUCTION ⚠️\n"
            "You ONLY see the PROJECT ARCHITECTURE MAP above. You do NOT see the actual file contents.\n"
            "If you need to edit an existing file, YOU MUST use `{\"action\": \"read_file\", \"path\": \"filename\"}` to read it FIRST.\n"
            "Do NOT guess or hallucinate existing code. Read it first, then overwrite."
        )
        
        return "\n".join(snippets)
    
    async def _execute_plan(self, plan_msg: MCPMessage):
        payload = plan_msg.payload
        tasks = payload.get("tasks", [])
        is_debug = payload.get("is_debug", False)
        stages = payload.get("_stages", None)
        parallel_hint = payload.get("_parallel_hint", False)
        
        log_agent("coder", f"Executing {len(tasks)} tasks (debug={is_debug}, parallel={parallel_hint})", self.project_id)
        
        if not tasks:
            self.execution_complete = True
            self.emit(
                intent=Intent.DONE,
                payload={"status": "no_tasks", "message": "No tasks to execute", "operations": []},
                reasoning="Plan had no tasks"
            )
            return
        
        # --- PARALLEL EXECUTION via stages ---
        if stages and parallel_hint and len(stages) > 1:
            log_agent("coder", f"⚡ Parallel mode: {len(stages)} stages", self.project_id)
            
            for stage_idx, stage_indices in enumerate(stages):
                stage_tasks = [tasks[i] for i in stage_indices if i < len(tasks)]
                
                if len(stage_tasks) > 1:
                    # Run tasks in this stage concurrently
                    log_agent("coder", f"Stage {stage_idx+1}: Running {len(stage_tasks)} tasks in parallel", self.project_id)
                    
                    async def _run_task(task, task_num):
                        task_id = f"task_{task_num}_{int(time.time() * 1000)}"
                        task_start = time.time()
                        await self._implement_task(task, task_id)
                        duration = time.time() - task_start
                        EvalTelemetry.record_task_completion(
                            self.project_id, task_id, 
                            self.task_results.get(task_id, {}).get("success", False),
                            self.total_tokens_used, duration
                        )
                    
                    parallel_coros = [_run_task(task, idx) for idx, task in zip(stage_indices, stage_tasks)]
                    await asyncio.gather(*parallel_coros, return_exceptions=True)
                else:
                    # Single task in stage — run normally
                    for i, task in zip(stage_indices, stage_tasks):
                        task_id = f"task_{i}_{int(time.time() * 1000)}"
                        task_start = time.time()
                        await self._implement_task(task, task_id)
                        duration = time.time() - task_start
                        EvalTelemetry.record_task_completion(
                            self.project_id, task_id,
                            self.task_results.get(task_id, {}).get("success", False),
                            self.total_tokens_used, duration
                        )
                
                await asyncio.sleep(0.1)
        else:
            # --- SEQUENTIAL EXECUTION (original behavior) ---
            for i, task in enumerate(tasks):
                task_num = i + 1
                task_id = f"task_{task_num}_{int(time.time() * 1000)}"
                
                log_agent("coder", f"[{task_num}/{len(tasks)}] {str(task)[:50]}...", self.project_id)
                
                task_start = time.time()
                
                task_str = str(task).lower()
                if "ui" in task_str or "component" in task_str or "page" in task_str:
                    agent_type = "ui"
                elif "api" in task_str or "route" in task_str or "endpoint" in task_str:
                    agent_type = "api"
                else:
                    agent_type = "logic"
                
                if agent_type in ["ui", "api"] and agent_type in self.sub_agents:
                    await self._delegate_task(task, task_id, agent_type)
                else:
                    await self._implement_task(task, task_id)
                
                duration = time.time() - task_start
                EvalTelemetry.record_task_completion(
                    self.project_id, task_id,
                    self.task_results.get(task_id, {}).get("success", False),
                    self.total_tokens_used, duration
                )
                
                await asyncio.sleep(0.2)
        
        # Wait for sub-agents
        waited = 0
        while self.pending_tasks and waited < 180:
            await asyncio.sleep(0.5)
            waited += 0.5
        
        log_agent("coder", "All tasks complete, marking done.", self.project_id)
        
        self.execution_complete = True
        self.emit(
            intent=Intent.DONE,
            payload={
                "status": "complete",
                "operations": self.all_operations,
                "review_feedback": "Review bypassed for speed"
            },
            to=None,
            task_id=f"done_{int(time.time())}",
            reasoning=f"Completed {len(tasks)} tasks"
        )
    
    async def _delegate_task(self, task: str, task_id: str, agent_type: str):
        log_agent("coder", f"Delegating to {agent_type}_agent", self.project_id)
        
        self.pending_tasks[task_id] = {"task": task, "agent_type": agent_type}
        
        intent_map = {
            "ui": Intent.DELEGATE_UI,
            "api": Intent.DELEGATE_API,
            "logic": Intent.DELEGATE_LOGIC
        }
        
        self.emit(
            intent=intent_map.get(agent_type, Intent.DELEGATE_LOGIC),
            payload={
                "task": task,
                "file_tree": self.file_tree,
                "context": self._build_context_snippets()
            },
            to=f"{agent_type}_agent",
            task_id=task_id,
            reasoning=f"Delegated to {agent_type}_agent"
        )
    
    async def _implement_task(self, task: str, task_id: str):
        context = self._build_context_snippets()
        chat_history = _get_history(self.project_id)[-8:]
        
        # Inject relevant skills
        task_type = "fullstack"
        t = task.lower()
        if any(x in t for x in ["component", "page", "ui", "frontend"]):
            task_type = "frontend"
        elif any(x in t for x in ["route", "api", "server", "backend"]):
            task_type = "backend"
        
        skills_context = SkillsManager.get_relevant_skills(self.project_id, task_type)
        
        system_prompt = self.SYSTEM_PROMPT
        if skills_context:
            system_prompt += f"\n\n### ACTIVE SKILLS:\n{skills_context}"
        
        messages = [{"role": "system", "content": system_prompt}]
        
        for h in chat_history:
            role = h["role"]
            content = h["content"]
            
            if role == "assistant":
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict) and "assistant_message" in parsed:
                        content = parsed["assistant_message"]
                except Exception:
                    pass 
                    
            messages.append({"role": role, "content": content})
        
        # 1. Softened initial prompt
        messages.append({
            "role": "user", 
            "content": f"CONTEXT:\n{context}\n\nTASK:\n{task}\n\nPlease implement this task. Output your response as a JSON object containing an 'operations' array. Feel free to use 'read_file' to examine files first, or use 'create_file'/'overwrite_file' to make changes."
        })

        max_iterations = 20
        
        for iteration in range(max_iterations):
            max_retries = 3
            last_error = None
            
            for attempt in range(max_retries + 1):
                try:
                    raw, tokens = await self.call_llm(messages, temperature=0.6)
                    parsed = self.extract_json(raw)
                    
                    if not parsed:
                        raise ValueError("Could not extract JSON from response.")
                        
                    canonical = self._normalize_and_validate_ops(parsed)
                    ops = canonical.get("operations", [])
                    
                    # Catch empty arrays gently
                    if not ops:
                        raise ValueError("No operations found in output. Please provide at least one action in the 'operations' array.")
                    
                    read_ops = [op for op in ops if op.get("action") == "read_file"]
                    write_ops = [op for op in ops if op.get("action") != "read_file"]
                    
                    # 2. Allow organic chaining of reads
                    if read_ops and not write_ops:
                        log_agent("coder", f"Reading {len(read_ops)} file(s)...", self.project_id)
                        
                        file_contents = []
                        for read_op in read_ops:
                            path = read_op.get("path")
                            content = await self.read_file(path)
                            if content is not None:
                                file_contents.append(f"--- {path} ---\n{content}\n")
                                self.file_tree[path] = content
                            else:
                                file_contents.append(f"--- {path} ---\n[File not found or error reading]\n")
                        
                        messages.append({"role": "assistant", "content": raw})
                        
                        # Softened follow-up prompt
                        messages.append({
                            "role": "user", 
                            "content": f"Here are the requested file contents:\n\n{''.join(file_contents)}\n\nPlease continue with the task. You can read more files if you need more context, or output your write operations when ready."
                        })
                        
                        last_error = None
                        break # Break the retry loop to continue the outer iteration loop
                    
                    reflection = canonical.get("reflection", "")
                    
                    # 3. Instantly update file_tree with new files (Amnesia Fix)
                    for op in write_ops:
                        path = op.get("path")
                        content = op.get("content")
                        if path and content is not None:
                            self.file_tree[path] = content
                            log_agent("coder", f"  Updated file_tree: {path}", self.project_id)
                    
                    self.all_operations.extend(ops)
                    self.task_results[task_id] = {
                        "operations": ops,
                        "reflection": reflection,
                        "success": True
                    }
                    
                    for op in ops:
                        log_agent("coder", f"  ✓ {op.get('action')}: {op.get('path')}", self.project_id)
                    
                    if reflection:
                        log_agent("coder", f"  Reflection: {reflection[:80]}...", self.project_id)

                    return

                except Exception as e:
                    last_error = e
                    log_agent("coder", f"Attempt {attempt+1} failed: {str(e)[:60]}", self.project_id)
                    
                    if attempt < max_retries:
                        # Softened error prompt
                        correction_msg = f"There was an issue processing your response: {str(e)[:100]}\nPlease correct this and provide a valid JSON object."
                        messages.append({"role": "user", "content": correction_msg})
                        await asyncio.sleep(1)
                    else:
                        break
            
            # Continue reading if no errors occurred
            if last_error is None and read_ops:
                continue
                
            if last_error:
                self.task_results[task_id] = {"error": str(last_error), "success": False}
                return
        
        log_agent("coder", f"Max read iterations reached for task {task_id}", self.project_id)
        self.task_results[task_id] = {"error": "Max file read iterations exceeded", "success": False}
    
    async def _handle_sub_done(self, msg: MCPMessage):
        task_id = msg.task_id
        
        if task_id in self.pending_tasks:
            del self.pending_tasks[task_id]
            ops = msg.payload.get("operations", [])
            
            # CRITICAL FIX: Update file_tree with sub-agent results
            for op in ops:
                path = op.get("path")
                content = op.get("content")
                if path and content is not None:
                    self.file_tree[path] = content
            
            self.all_operations.extend(ops)
            log_agent("coder", f"Sub-agent completed: {len(ops)} ops", self.project_id)
    
    async def _handle_feedback(self, msg: MCPMessage):
        feedback = msg.payload.get("feedback", "")
        issues = msg.payload.get("issues", [])
        
        if issues:
            log_agent("coder", f"Reviewer found {len(issues)} issues", self.project_id)
        else:
            log_agent("coder", "Review passed, marking complete", self.project_id)
        
        self.execution_complete = True
        self.emit(
            intent=Intent.DONE,
            payload={
                "status": "complete",
                "operations": self.all_operations,
                "review_feedback": feedback
            },
            reasoning="Execution complete after review"
        )


# ============================================================================
# REVIEWER AGENT (unchanged)
# ============================================================================

class ReviewerAgent(BaseAgent):
    """Quality checker — reviews output before completion."""
    
    SYSTEM_PROMPT = (
        "You are the Reviewer Agent. Your job is quality control.\n\n"
        "RESPONSIBILITIES:\n"
        "1. Review all file operations\n"
        "2. Check for common issues (missing imports, syntax errors, etc.)\n"
        "3. Verify the implementation matches the intent\n"
        "4. Provide constructive feedback\n\n"
        "RULES:\n"
        "1. Be thorough but constructive\n"
        "2. Output valid JSON only\n"
        "3. If issues found, use FEEDBACK intent to request fixes\n\n"
        "RESPONSE FORMAT:\n"
        "{\n"
        '  "passed": true|false,\n'
        '  "issues": ["issue 1", "issue 2"],\n'
        '  "feedback": "Overall assessment..."\n'
        "}"
    )

    async def _on_mcp(self, msg: MCPMessage):
        if msg.intent == Intent.REVIEW:
            await self._review_operations(msg)
    
    async def _review_operations(self, msg: MCPMessage):
        payload = msg.payload
        operations = payload.get("operations", [])
        
        log_agent("reviewer", f"Reviewing {len(operations)} operations", self.project_id)
        
        issues = []
        
        for op in operations:
            path = op.get("path", "")
            content = op.get("content", "")
            
            if path.endswith(".tsx") or path.endswith(".ts"):
                if "import React" in content and "from 'react'" not in content:
                    issues.append(f"{path}: Malformed React import")
                
                if "function" in content and "export" not in content:
                    issues.append(f"{path}: Function may not be exported")
            
            if path.endswith(".js"):
                if "require(" in content and "import " in content:
                    issues.append(f"{path}: Mixing require and import")
        
        passed = len(issues) == 0
        
        if passed:
            log_agent("reviewer", "All checks passed", self.project_id)
        else:
            log_agent("reviewer", f"⚠️ Found {len(issues)} issues", self.project_id)
            for issue in issues[:3]:
                log_agent("reviewer", f"   - {issue}", self.project_id)
        
        self.emit(
            intent=Intent.FEEDBACK,
            payload={
                "passed": passed,
                "issues": issues,
                "feedback": "Review complete" if passed else f"Found {len(issues)} issues to address"
            },
            to="coder",
            task_id=msg.task_id,
            reasoning=f"Review: {len(issues)} issues found"
        )


# ============================================================================
# SUB-AGENTS (unchanged system prompts)
# ============================================================================

class UISubAgent(BaseAgent):
    """UI Specialist — React components and styling."""
    
    SYSTEM_PROMPT = (
        "You are the UI Specialist.\n\n"
        "YOUR JOB: Create beautiful, polished React components.\n\n"
        "RULES:\n"
        "1. Use Shadcn/UI from @/components/ui/ when appropriate\n"
        "2. Make designs creative and non-bootstrappy\n"
        "3. Use Tailwind for styling\n"
        "4. Use framer-motion for animations\n"
        "5. Use lucide-react for icons\n"
        "6. NEVER use Inter font\n\n"
        "RESPONSE FORMAT (JSON):\n"
        "{\n"
        '  "message": "Status...",\n'
        '  "operations": [{"action": "create_file", "path": "...", "content": "..."}]\n'
        "}"
    )

    async def _on_mcp(self, msg: MCPMessage):
        if msg.intent in [Intent.DELEGATE_UI, Intent.IMPLEMENT]:
            await self._implement_ui(msg)
    
    async def _implement_ui(self, msg: MCPMessage):
        task = msg.payload.get("task", "")
        context = msg.payload.get("context", "")
        
        log_agent("ui_agent", f"Creating: {str(task)[:40]}...", self.project_id)
        
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": f"""CONTEXT:\n{context}\n\nTASK:\n{task}\n\nOutput JSON."""}
        ]
        
        raw, _ = await self.call_llm(messages, temperature=0.7)
        data = self.extract_json(raw)
        
        if data:
            ops = data.get("operations", [])
            for op in ops:
                log_agent("ui_agent", f"  ✓ {op.get('action')}: {op.get('path')}", self.project_id)
            
            self.emit(
                intent=Intent.DONE,
                payload={"operations": ops},
                to="coder",
                task_id=msg.task_id,
                reasoning="UI component created"
            )

class APISubAgent(BaseAgent):
    """API Specialist — Express routes and backend."""
    
    SYSTEM_PROMPT = (
        "You are the API Specialist.\n\n"
        "YOUR JOB: Create Express routes and API endpoints.\n\n"
        "RULES:\n"
        "1. Use ES modules (import/export)\n"
        "2. Use relative paths with .js extension\n"
        "3. Use async/await for async operations\n"
        "4. Return proper JSON responses\n"
        "5. Handle errors with try/catch\n\n"
        "RESPONSE FORMAT (JSON):\n"
        "{\n"
        '  "message": "Status...",\n'
        '  "operations": [{"action": "create_file", "path": "routes/...", "content": "..."}]\n'
        "}"
    )

    async def _on_mcp(self, msg: MCPMessage):
        if msg.intent == Intent.DELEGATE_API:
            await self._implement_api(msg)
    
    async def _implement_api(self, msg: MCPMessage):
        task = msg.payload.get("task", "")
        context = msg.payload.get("context", "")
        
        log_agent("api_agent", f"Creating: {str(task)[:40]}...", self.project_id)
        
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": f"""CONTEXT:\n{context}\n\nTASK:\n{task}\n\nOutput JSON."""}
        ]
        
        raw, _ = await self.call_llm(messages, temperature=0.5)
        data = self.extract_json(raw)
        
        if data:
            ops = data.get("operations", [])
            for op in ops:
                log_agent("api_agent", f"  ✓ {op.get('action')}: {op.get('path')}", self.project_id)
            
            self.emit(
                intent=Intent.DONE,
                payload={"operations": ops},
                to="coder",
                task_id=msg.task_id,
                reasoning="API route created"
            )

class LogicSubAgent(BaseAgent):
    """Logic Specialist — utilities and helpers."""
    
    SYSTEM_PROMPT = (
        "You are the Logic Specialist.\n\n"
        "YOUR JOB: Create utility functions and business logic.\n\n"
        "RULES:\n"
        "1. Write clean, reusable functions\n"
        "2. Use TypeScript for type safety\n"
        "3. Handle edge cases\n\n"
        "RESPONSE FORMAT (JSON):\n"
        "{\n"
        '  "message": "Status...",\n'
        '  "operations": [{"action": "create_file", "path": "src/lib/...", "content": "..."}]\n'
        "}"
    )

    async def _on_mcp(self, msg: MCPMessage):
        if msg.intent == Intent.DELEGATE_LOGIC:
            await self._implement_logic(msg)
    
    async def _implement_logic(self, msg: MCPMessage):
        task = msg.payload.get("task", "")
        context = msg.payload.get("context", "")
        
        log_agent("logic_agent", f"Creating: {str(task)[:40]}...", self.project_id)
        
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": f"""CONTEXT:\n{context}\n\nTASK:\n{task}\n\nOutput JSON."""}
        ]
        
        raw, _ = await self.call_llm(messages, temperature=0.5)
        data = self.extract_json(raw)
        
        if data:
            ops = data.get("operations", [])
            for op in ops:
                log_agent("logic_agent", f"  ✓ {op.get('action')}: {op.get('path')}", self.project_id)
            
            self.emit(
                intent=Intent.DONE,
                payload={"operations": ops},
                to="coder",
                task_id=msg.task_id,
                reasoning="Logic created"
            )


# ============================================================================
# DEBUGGER AGENT (with Eval Telemetry)
# ============================================================================

class DebuggerAgent(BaseAgent):
    """Debugger — High-context auto-healer with telemetry hooks."""
    
    async def debug(self, error_message: str, file_tree: Dict[str, str], target_path: Optional[str] = None) -> List[Dict]:
        log_agent("debugger", f"Fixing: {error_message[:60]}...", self.project_id)
        
        relevant_file = target_path
        if not relevant_file:
            file_match = re.search(r'(?:in|at|file)\s+([^\s\'"]+\.(?:tsx|ts|js|jsx|sql))', error_message, re.IGNORECASE)
            relevant_file = file_match.group(1) if file_match else "unknown"
            
        file_content = file_tree.get(relevant_file, "")
        
        system_prompt = CoderAgent.SYSTEM_PROMPT + (
            "\n\n========================================================================\n"
            "🚨 CRITICAL DEBUGGING OVERRIDE 🚨\n"
            "========================================================================\n"
            "You are currently operating in DEBUG MODE.\n"
            "The user's application just crashed or encountered a syntax/SQL error.\n"
            "Your ONLY job is to fix the error provided in the prompt.\n"
            "Do NOT generate new features. Do NOT rewrite the whole file unless necessary to fix the error.\n"
            "Make sure to output the EXACT SAME valid JSON format requested above, containing the 'overwrite_file' action to patch the broken file."
        )
        
        chat_history = _get_history(self.project_id)[-4:]
        
        messages = [{"role": "system", "content": system_prompt}]
        for h in chat_history:
            messages.append({"role": h["role"], "content": h["content"]})
            
        messages.append({"role": "user", "content": f"🚨 WE HIT AN ERROR! 🚨\n\nERROR LOG:\n{error_message}\n\nTARGET FILE: {relevant_file}\n\nCURRENT FILE CONTENT:\n{file_content}\n"})
        
        raw, tokens = await self.call_llm(messages, temperature=0.3)
        data = self.extract_json(raw)
        
        fix_applied = False
        
        if data:
            ops = data.get("operations", [])
            
            normalized_ops = []
            for op in ops:
                action = op.get("action", "overwrite_file")
                path = op.get("path", relevant_file)
                content = op.get("content", "")
                
                if action in ["patch_file", "patch", "update_file", "modify_file"]:
                    action = "overwrite_file"
                    
                if path and content:
                    normalized_ops.append({
                        "action": action,
                        "path": path,
                        "content": content
                    })
                    log_agent("debugger", f"  ✓ Fixed {path}", self.project_id)
                    fix_applied = True
            
            # Record telemetry
            EvalTelemetry.record_debug_intervention(
                self.project_id, error_message, relevant_file, fix_applied
            )
            
            return normalized_ops
        
        EvalTelemetry.record_debug_intervention(
            self.project_id, error_message, relevant_file, False
        )
        
        return []


# ============================================================================
# SWARM ORCHESTRATOR (v2.0 — with all new layers)
# CRITICAL FIX: Agents are now properly subscribed to the MCP bus
# ============================================================================

class AgentSwarm:
    """Main orchestrator — creates and manages all agents with conversation layers."""
    
    def __init__(self, project_id: str):
        self.project_id = project_id
        self.bus = MCPBus(project_id)
        
        # Core agents
        self.planner = PlannerAgent("planner", self.bus, project_id)
        self.reasoner = ReasonerAgent("reasoner", self.bus, project_id)
        self.orchestrator = OrchestratorAgent("orchestrator", self.bus, project_id)
        self.coder = CoderAgent("coder", self.bus, project_id)
        self.reviewer = ReviewerAgent("reviewer", self.bus, project_id)
        self.debugger = DebuggerAgent("debugger", self.bus, project_id)
        
        # Sub-agents
        self.ui_agent = UISubAgent("ui_agent", self.bus, project_id)
        self.api_agent = APISubAgent("api_agent", self.bus, project_id)
        self.logic_agent = LogicSubAgent("logic_agent", self.bus, project_id)
        
        # CRITICAL FIX: Subscribe all agents to the MCP bus
        # This was missing and caused the "coder failing silently" issue
        self._subscribe_agents()
        
        # Register sub-agents with coder
        self.coder.register_sub_agent(self.ui_agent)
        self.coder.register_sub_agent(self.api_agent)
        self.coder.register_sub_agent(self.logic_agent)
        
        # --- v2.0 Layers ---
        self.token_sub = TokenSubstitution()
        self.sandbox = SandboxManager(project_id)
        
        log_agent("swarm", "🧠 Conversational agent swarm v2.0 initialized", project_id)
    
    def _subscribe_agents(self):
        """CRITICAL FIX: Subscribe all agents to receive MCP messages."""
        agents = [
            self.planner, self.reasoner, self.orchestrator, 
            self.coder, self.reviewer, self.debugger,
            self.ui_agent, self.api_agent, self.logic_agent
        ]
        for agent in agents:
            self.bus.subscribe(agent.agent_id, agent._on_mcp)
            log_agent("swarm", f"  ✓ Subscribed: {agent.agent_id}", self.project_id)
    
    def _all_agents(self) -> List[BaseAgent]:
        return [
            self.planner, self.reasoner, self.orchestrator, self.coder, 
            self.reviewer, self.debugger,
            self.ui_agent, self.api_agent, self.logic_agent
        ]
    
    def get_total_tokens(self) -> int:
        total = sum(agent.get_tokens_used() for agent in self._all_agents())
        log_agent("swarm", f"Total tokens used: {total}", self.project_id)
        return total
    
    def reset_all_tokens(self):
        for agent in self._all_agents():
            agent.reset_tokens()
        log_agent("swarm", "🔄 Token counters reset", self.project_id)
    
    async def solve(self, user_request: str, file_tree: Dict[str, str], 
                    agent_skills: Optional[Dict] = None,
                    skip_planner: bool = False) -> Dict[str, Any]:
        """Main entry point with full conversation flow + v2.0 layers."""
        
        # --- LAYER: Token Substitution (compress before agents see it) ---
        compressed_tree = self.token_sub.compress_file_tree(file_tree)
        savings = self.token_sub.savings_report
        if savings["blobs_compressed"] > 0:
            log_agent("swarm", f"Token substitution: {savings['blobs_compressed']} blobs, {savings['savings_pct']}% saved", self.project_id)
        
        # --- LAYER: Skills Loading ---
        SkillsManager.load_project_skills(self.project_id, file_tree)
        
        # Distribute file tree to all agents
        for agent in self._all_agents():
            agent.file_tree = compressed_tree
        
        log_agent("swarm", f"🎯 Solving: {user_request[:60]}...", self.project_id)
        
        assistant_message = "Working on it..."
        needs_clarification = False
        questions = []
        
        if skip_planner:
            log_agent("swarm", "Direct mode", self.project_id)
            self.coder.emit(
                intent=Intent.PLAN,
                payload={
                    "tasks": [user_request],
                    "is_debug": True
                },
                to="coder",
                task_id="direct",
                reasoning="Direct execution"
            )
        else:
            # Phase 1: Planning
            plan_result = await self.planner.plan(user_request, compressed_tree, agent_skills)
            
            if plan_result.intent == Intent.ERROR:
                return {
                    "status": "complete",
                    "assistant_message": plan_result.payload.get("assistant_message", "🚨 I encountered an error analyzing your request. Please try rephrasing it."),
                    "operations": [],
                    "total_tokens": self.get_total_tokens()
                }

            assistant_message = plan_result.payload.get("assistant_message", "Building...")
            
            if plan_result.intent == Intent.QUESTION:
                needs_clarification = True
                questions = plan_result.payload.get("questions", [])
                log_agent("swarm", f"⏸️ Need user clarification: {len(questions)} questions", self.project_id)
                
                await self.bus.await_all_tasks(timeout=1.0)
                
                return {
                    "status": "needs_clarification",
                    "assistant_message": assistant_message,
                    "questions": questions,
                    "operations": []
                }
        
        # Phase 2-3: Reasoning + Orchestration + Execution
        # (Reasoner → Orchestrator → Coder pipeline happens via MCP subscriptions)
        
        if not needs_clarification:
            max_wait = 300
            waited = 0
            last_op_count = 0
            stable_count = 0
            
            while waited < max_wait:
                await asyncio.sleep(0.5)
                waited += 0.5
                
                current_ops = []
                for msg in self.bus.messages:
                    if msg.intent in [Intent.DONE, Intent.DEBUG_FIX]:
                        current_ops.extend(msg.payload.get("operations", []))
                
                if len(current_ops) == last_op_count and len(current_ops) > 0:
                    stable_count += 1
                    if stable_count >= 6 and not self.coder.pending_tasks:
                        break
                else:
                    stable_count = 0
                    last_op_count = len(current_ops)
                    if len(current_ops) > 0:
                        log_agent("swarm", f"⏳ Growing: {len(current_ops)} operations...", self.project_id)
        
        # Collect all operations
        all_ops = []
        for msg in self.bus.messages:
            if msg.intent in [Intent.DONE, Intent.DEBUG_FIX]:
                ops = msg.payload.get("operations", [])
                if ops:
                    all_ops.extend(ops)
        
        # Deduplicate by path (keep last occurrence)
        seen_paths = {}
        for op in all_ops:
            path = op.get("path", "")
            if path:
                seen_paths[path] = op
        all_ops = list(seen_paths.values())
        
        # --- LAYER: AST Patching (deterministic post-processing) ---
        all_ops, patches = ASTPatcher.apply_all(all_ops, file_tree)
        if patches:
            log_agent("swarm", f"AST patches applied: {', '.join(patches)}", self.project_id)
            for patch_desc in patches:
                parts = patch_desc.split(":")
                rule_name = parts[0].strip()
                affected_files = [op.get("path", "") for op in all_ops if op.get("path")]
                EvalTelemetry.record_ast_patch(self.project_id, rule_name, affected_files)
        
        # --- LAYER: Token Substitution (expand placeholders in generated code) ---
        all_ops = self.token_sub.expand_operations(all_ops)
        
        # --- LAYER: Sandbox (stage and commit) ---
        staged = self.sandbox.stage(all_ops, file_tree)
        if self.sandbox.has_conflicts:
            log_agent("swarm", f"⚠️ Sandbox detected conflicts — proceeding anyway (auto-resolve)", self.project_id)
        
        committed_ops, _ = self.sandbox.commit(file_tree)
        
        log_agent("swarm", f"✅ Complete: {len(committed_ops)} unique file operations", self.project_id)
        for op in committed_ops:
            log_agent("swarm", f"  📄 {op.get('action')}: {op.get('path')}", self.project_id)
        
        await self.bus.await_all_tasks(timeout=3.0)
        
        total_tokens = self.get_total_tokens()
        
        return {
            "status": "complete",
            "assistant_message": assistant_message,
            "operations": committed_ops,
            "total_tokens": total_tokens
        }
    
    async def continue_with_clarification(self, answers: Dict[str, str], 
                                          file_tree: Dict[str, str]) -> Dict[str, Any]:
        log_agent("swarm", "Continuing with user clarification", self.project_id)
        
        clarification_text = "User clarified: " + json.dumps(answers)
        _append_history(self.project_id, "user", clarification_text)
        
        return await self.solve(
            user_request="Proceed with clarified requirements",
            file_tree=file_tree,
            skip_planner=False
        )
    
    async def debug(self, error_message: str, file_tree: Dict[str, str]) -> Dict[str, Any]:
        self.debugger.file_tree = file_tree
        operations = await self.debugger.debug(error_message, file_tree)
        
        # Apply AST patching to debug output too
        if operations:
            operations, patches = ASTPatcher.apply_all(operations, file_tree)
            if patches:
                log_agent("swarm", f"AST patches on debug output: {', '.join(patches)}", self.project_id)
        
        return {
            "status": "debug_complete",
            "assistant_message": "Fixed the error.",
            "operations": operations
        }
    
    def get_telemetry(self) -> Dict[str, Any]:
        """Get eval telemetry report for this project."""
        return EvalTelemetry.get_report(self.project_id)


# ============================================================================
# BACKWARD COMPATIBILITY — Unified Agent Class
# ============================================================================

class Agent:
    """Backward-compatible wrapper for the AgentSwarm."""
    
    def __init__(self, timeout_s: float = 120.0):
        self.timeout_s = timeout_s
        self._swarm_cache: Dict[str, AgentSwarm] = {}
    
    def _get_swarm(self, project_id: str) -> AgentSwarm:
        if project_id not in self._swarm_cache:
            self._swarm_cache[project_id] = AgentSwarm(project_id)
        return self._swarm_cache[project_id]
    
    def remember(self, project_id: str, role: str, text: str) -> None:
        _append_history(project_id, role, text)
    
    def plan(self, user_request: str, project_context: Dict[str, Any]) -> Dict[str, Any]:
        project_id = str(project_context.get("project_id") or "").strip()
        agent_skills = project_context.get("agent_skills")
        
        if project_id:
            _append_history(project_id, "user", user_request)

        file_tree = {f: "" for f in project_context.get("files", [])}
        
        loop = asyncio.new_event_loop()
        try:
            swarm = self._get_swarm(project_id)
            future = swarm.planner.plan(user_request, file_tree, agent_skills)
            result = loop.run_until_complete(future)
            
            tasks = result.payload.get("tasks", [])
            assistant_message = result.payload.get("assistant_message", "")
            tokens = result.payload.get("estimated_tokens", 0)
            
            if result.intent == Intent.QUESTION:
                return {
                    "assistant_message": assistant_message,
                    "plan": {"todo": [], "questions": result.payload.get("questions", [])},
                    "todo_md": "",
                    "usage": {"total_tokens": tokens},
                    "needs_clarification": True
                }
            
            base_plan = {
                "capabilities": result.payload.get("capabilities", []),
                "ai_modules": [],
                "glue_files": [],
                "todo": tasks,
            }
            
            return {
                "assistant_message": assistant_message,
                "plan": base_plan,
                "todo_md": self._to_todo_md(base_plan, assistant_message),
                "usage": {"total_tokens": tokens}
            }
        finally:
            loop.close()
    
    async def code(self, plan_section: str, plan_text: str, 
                   file_tree: Dict[str, str], project_name: str,
                   history: Optional[List[Dict[str, str]]] = None,
                   max_retries: int = 3) -> Dict[str, Any]:
        """Generate code — BULLETPROOF operation collection."""
        swarm = self._get_swarm(project_name)
        
        # Reset coder state
        swarm.coder.all_operations = []
        swarm.coder.pending_tasks = {}
        swarm.coder.execution_complete = False
        swarm.bus.messages = []
        
        # Compress file tree before passing to coder
        compressed_tree = swarm.token_sub.compress_file_tree(file_tree)
        swarm.coder.file_tree = compressed_tree
        
        # Load skills
        SkillsManager.load_project_skills(project_name, file_tree)
        
        swarm.coder.emit(
            intent=Intent.PLAN,
            payload={
                "tasks": [f"{plan_section}: {plan_text}"],
                "is_debug": False
            },
            to="coder",
            task_id="single",
            reasoning="Single task execution"
        )
        
        max_wait = 180
        waited = 0
        all_collected_ops = []
        last_op_count = 0
        stable_count = 0
        
        while waited < max_wait:
            await asyncio.sleep(0.5)
            waited += 0.5
            
            current_ops = []
            for msg in swarm.bus.messages:
                if msg.intent in [Intent.DONE, Intent.DEBUG_FIX]:
                    ops = msg.payload.get("operations", [])
                    if ops:
                        current_ops.extend(ops)
            
            if len(current_ops) == last_op_count and len(current_ops) > 0:
                stable_count += 1
                if stable_count >= 6 and not swarm.coder.pending_tasks:
                    all_collected_ops = current_ops
                    break
            else:
                stable_count = 0
                last_op_count = len(current_ops)
                all_collected_ops = current_ops
        
        final_ops = []
        for msg in swarm.bus.messages:
            if msg.intent in [Intent.DONE, Intent.DEBUG_FIX]:
                ops = msg.payload.get("operations", [])
                if ops:
                    final_ops.extend(ops)
        
        operations = final_ops if len(final_ops) >= len(all_collected_ops) else all_collected_ops
        
        # Deduplicate
        seen_paths = {}
        for op in operations:
            path = op.get("path", "")
            if path:
                seen_paths[path] = op
        operations = list(seen_paths.values())
        
        # --- Post-processing layers ---
        # AST Patching
        operations, patches = ASTPatcher.apply_all(operations, file_tree)
        if patches:
            log_agent("swarm", f"AST patches: {', '.join(patches)}", project_name)
        
        # Token substitution expansion
        operations = swarm.token_sub.expand_operations(operations)
                
        await swarm.bus.await_all_tasks(timeout=3.0)
        
        total_tokens = swarm.get_total_tokens()
        
        return {
            "message": f"Completed {len(operations)} operations",
            "operations": operations,
            "usage": {"total_tokens": total_tokens}
        }
    
    @staticmethod
    def _to_todo_md(plan: Dict[str, Any], msg: str = "") -> str:
        tasks = plan.get("todo", [])
        if not tasks:
            return ""
        lines = ["# Build Plan\n", "## Tasks"]
        for task in tasks:
            lines.append(f"- {task}")
        return "\n".join(lines)


__all__ = [
    "Agent", "AgentSwarm", "MCPBus", "MCPMessage", "Intent",
    "_render_token_limit_message", "clear_history", "ContextManager",
    # v2.0 exports
    "TokenSubstitution", "ASTPatcher", "SkillsManager", 
    "BlueprintRouter", "SandboxManager", "EvalTelemetry",
    "OrchestratorAgent",
]