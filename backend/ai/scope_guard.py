"""
scope_guard.py — Drop-in Scope Guard for Gorilla Builder Coder Agent
=====================================================================

Import this module AFTER importing your agent module to patch the CoderAgent
with scope-aware task execution that prevents the coder from trying to build
the entire app in a single task.

Usage in app.py:
    from agent import Agent, AgentSwarm  # or from supabase_agent import ...
    import scope_guard  # Auto-patches CoderAgent on import

What it fixes:
1. Coder sees full app description in every task and tries to build everything
2. Chat history leaks the full plan JSON (all tasks visible to each sub-task)
3. Architecture map shows ALL files, overwhelming the coder
4. Error prompts trigger panic-coding (dumping entire app to "fix" an error)
5. Follow-up prompts after read_file don't remind of scope boundaries

How it works:
- Replaces CoderAgent._implement_task with a scoped version
- Adds _extract_task_files() and _build_scoped_context() helper methods
- Strips plan JSON from chat history before injecting into coder context
- Adds explicit SCOPE GUARD boundaries to every task prompt
"""

import re
import json
import time
import asyncio
from typing import Dict, Any, List, Optional

# Will be populated on first use
_original_implement_task = None
_patched = False


def _extract_task_files(task: str) -> List[str]:
    """Extract file paths mentioned in backticks or bare paths from a task description."""
    # Match paths in backticks like `src/App.tsx`, `server.js`, `routes/api.js`
    backtick_paths = re.findall(r'`([^`]*(?:\.(?:tsx|ts|js|jsx|css|json|sql|md))[^`]*)`', task)
    # Match bare paths like src/components/Header.tsx, routes/auth.js
    bare_paths = re.findall(r'(?:^|\s)((?:src/|routes/|migrations/|server\.js)[\w/.-]+)', task)
    all_paths = list(set(backtick_paths + bare_paths))
    return [p.strip() for p in all_paths if p.strip()]


def _build_scoped_context(coder_self, task: str) -> str:
    """
    Build a SCOPED context that highlights only files relevant to the current task.
    Shows the full architecture map but marks relevant files with arrows.
    """
    mentioned_files = _extract_task_files(task)
    
    clean_paths = [path for path in coder_self.file_tree.keys() if not path.endswith(".b64")]
    clean_paths.sort()
    
    if mentioned_files:
        tree_lines = []
        for path in clean_paths:
            if any(m in path for m in mentioned_files):
                tree_lines.append(f"- {path}  ← RELEVANT TO THIS TASK")
            else:
                tree_lines.append(f"- {path}")
        tree_structure = "\n".join(tree_lines)
    else:
        tree_structure = "\n".join([f"- {path}" for path in clean_paths])
    
    snippets = [f"PROJECT ARCHITECTURE MAP:\n{tree_structure}\n"]
    
    if "package.json" in coder_self.file_tree:
        snippets.append(f"--- package.json ---\n{coder_self.file_tree['package.json'][:2000]}\n")
    
    snippets.append(
        "\n⚠️ CRITICAL MCP INSTRUCTION ⚠️\n"
        "You ONLY see the PROJECT ARCHITECTURE MAP above. You do NOT see the actual file contents.\n"
        "If you need to edit an existing file, YOU MUST use `{\"action\": \"read_file\", \"path\": \"filename\"}` to read it FIRST.\n"
        "Do NOT guess or hallucinate existing code. Read it first, then overwrite."
    )
    
    return "\n".join(snippets)


def _get_history_import():
    """Lazy import of _get_history to avoid circular imports."""
    try:
        from agent import _get_history
        return _get_history
    except ImportError:
        pass
    try:
        from supabase_agent import _get_history
        return _get_history
    except ImportError:
        pass
    return lambda pid, n=16: []


def _get_skills_manager():
    """Lazy import of SkillsManager."""
    try:
        from agent import SkillsManager
        return SkillsManager
    except ImportError:
        pass
    try:
        from supabase_agent import SkillsManager
        return SkillsManager
    except ImportError:
        pass
    return None


def _get_log_agent():
    """Lazy import of log_agent."""
    try:
        from agent import log_agent
        return log_agent
    except ImportError:
        pass
    try:
        from supabase_agent import log_agent
        return log_agent
    except ImportError:
        pass
    # Fallback: just print
    def _fallback_log(role, msg, pid=""):
        print(f"[{role}] {msg[:200]}")
    return _fallback_log

_log_agent = None

def _get_cached_log():
    global _log_agent
    if _log_agent is None:
        _log_agent = _get_log_agent()
    return _log_agent


async def _scoped_implement_task(self, task: str, task_id: str):
    """
    Replacement for CoderAgent._implement_task with scope guards.
    
    Key changes from original:
    1. Uses _build_scoped_context instead of _build_context_snippets
    2. Strips plan JSON from chat history (only keeps assistant_message)
    3. Adds explicit SCOPE GUARD to the task prompt
    4. Extracts mentioned files and adds FILE FOCUS hints
    5. Truncates oversized history entries
    6. Softer error/follow-up prompts that don't trigger scope creep
    """
    log_agent = _get_cached_log()
    
    # Use SCOPED context
    context = _build_scoped_context(self, task)
    mentioned_files = _extract_task_files(task)
    
    _get_history = _get_history_import()
    chat_history = _get_history(self.project_id, 6)
    
    # Detect task type for skills injection
    task_type = "fullstack"
    t = task.lower()
    if any(x in t for x in ["component", "page", "ui", "frontend"]):
        task_type = "frontend"
    elif any(x in t for x in ["route", "api", "server", "backend"]):
        task_type = "backend"
    elif any(x in t for x in ["migration", ".sql", "schema", "database"]):
        task_type = "database"
    
    SkillsManager = _get_skills_manager()
    skills_context = ""
    if SkillsManager:
        skills_context = SkillsManager.get_relevant_skills(self.project_id, task_type)
    
    system_prompt = self.SYSTEM_PROMPT
    if skills_context:
        system_prompt += f"\n\n### ACTIVE SKILLS:\n{skills_context}"
    
    messages = [{"role": "system", "content": system_prompt}]
    
    # SCOPE FIX: Sanitize chat history — strip full plan JSON
    for h in chat_history:
        role = h["role"]
        content = h["content"]
        
        if role == "assistant":
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    content = parsed.get("assistant_message", "")
                    if not content:
                        continue
            except Exception:
                pass 
        
        # Truncate oversized history entries
        if len(content) > 3000:
            content = content[:1500] + "\n[...truncated...]\n" + content[-500:]
                
        messages.append({"role": role, "content": content})
    
    # SCOPE GUARD: Build deterministic task prompt
    file_hint = ""
    if mentioned_files:
        file_hint = f"\nFILES TO FOCUS ON: {', '.join(mentioned_files)}"
        file_hint += "\nOnly create/modify these files (and any imports they need). Do NOT create files outside this scope."
    
    messages.append({
        "role": "user", 
        "content": (
            f"CONTEXT:\n{context}\n\n"
            f"YOUR CURRENT TASK (do ONLY this):\n"
            f"{task}\n"
            f"{file_hint}\n\n"
            f"SCOPE RULES:\n"
            f"- Complete ONLY the task described above. Other tasks will be handled separately by other agents.\n"
            f"- If the task mentions specific files, focus on those files only.\n"
            f"- Do NOT build features or create files that are not part of this specific task.\n"
            f"- Use read_file first if you need to see existing code before modifying it.\n"
            f"- Output a JSON object with an 'operations' array."
        )
    })

    max_iterations = 20
    
    for iteration in range(max_iterations):
        max_retries = 3
        last_error = None
        read_ops = []
        
        for attempt in range(max_retries + 1):
            try:
                raw, tokens = await self.call_llm(messages, temperature=0.6)
                parsed = self.extract_json(raw)
                
                if not parsed:
                    raise ValueError("Could not extract JSON from response.")
                    
                canonical = self._normalize_and_validate_ops(parsed)
                ops = canonical.get("operations", [])
                
                if not ops:
                    raise ValueError("The operations array was empty. Please include at least one action for your current task only.")
                
                read_ops = [op for op in ops if op.get("action") == "read_file"]
                write_ops = [op for op in ops if op.get("action") != "read_file"]
                
                # Handle read-only responses (continue reading loop)
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
                    messages.append({
                        "role": "user", 
                        "content": f"Here are the requested file contents:\n\n{''.join(file_contents)}\n\nNow continue with YOUR CURRENT TASK ONLY. Output the write operations (create_file/overwrite_file) needed to complete this specific task. Do not add extra files beyond what the task requires."
                    })
                    
                    last_error = None
                    break
                
                reflection = canonical.get("reflection", "")
                
                # Update file_tree immediately (Amnesia Fix)
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
                    correction_msg = f"There was an issue: {str(e)[:100]}\nPlease fix ONLY this error and re-output the JSON for your current task. Do not add extra files."
                    messages.append({"role": "user", "content": correction_msg})
                    await asyncio.sleep(1)
                else:
                    break
        
        if last_error is None and read_ops:
            continue
            
        if last_error:
            self.task_results[task_id] = {"error": str(last_error), "success": False}
            return
    
    log_agent("coder", f"Max read iterations reached for task {task_id}", self.project_id)
    self.task_results[task_id] = {"error": "Max file read iterations exceeded", "success": False}


def apply_scope_guard():
    """
    Apply scope guard patches to CoderAgent in whatever module imported it.
    Call this AFTER importing your agent module.
    """
    global _patched
    if _patched:
        return
    
    patched_count = 0
    
    # Try patching agent.py's CoderAgent
    try:
        from agent import CoderAgent as AC
        AC._implement_task = _scoped_implement_task
        AC._extract_task_files = staticmethod(_extract_task_files)
        AC._build_scoped_context = _build_scoped_context
        patched_count += 1
    except ImportError:
        pass
    
    # Try patching supabase_agent.py's CoderAgent
    try:
        from supabase_agent import CoderAgent as SC
        # Avoid double-patching if both modules share the same class
        already_patched = (patched_count > 0 and SC._implement_task is _scoped_implement_task)
        if not already_patched:
            SC._implement_task = _scoped_implement_task
            SC._extract_task_files = staticmethod(_extract_task_files)
            SC._build_scoped_context = _build_scoped_context
            patched_count += 1
    except (ImportError, NameError):
        pass
    
    _patched = True
    
    if patched_count > 0:
        try:
            from agent import log_agent
        except ImportError:
            from supabase_agent import log_agent
        log_agent("scope_guard", f"✅ Scope guard applied to {patched_count} CoderAgent class(es)", "")


# Auto-apply on import
apply_scope_guard()