"""
True Agent Swarm with Conversational MCP
=========================================

Multi-agent architecture where agents:
- REASON before acting (not just execute)
- ASK questions when unclear (bidirectional communication)
- SELF-CORRECT when things fail
- COLLABORATE through conversation layers
- REFLECT on quality before marking done

Terminal logging for development visibility.
"""

from __future__ import annotations

import os
import json
import re
import time
import asyncio
from typing import Dict, Any, List, Optional, Tuple, TypedDict
from dataclasses import dataclass, field
from enum import Enum

import httpx

# --- Configuration for OpenRouter ---
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL = os.getenv("MODEL", "minimax/minimax-m2.5")
VISION_MODEL = os.getenv("MODEL", "xiaomi/mimo-v2-omni")
OPENROUTER_URL = os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions").strip()
SITE_URL = os.getenv("SITE_URL", "https://gorillabuilder.dev").strip()
SITE_NAME = os.getenv("SITE_NAME", "Gorilla Builder")

# --- Configuration for File API ---
# Set this to your app's base URL (e.g., "http://localhost:8000" or SITE_URL)
FILE_API_BASE_URL = os.getenv("FILE_API_BASE_URL", "https://corrinne-turbid-illustratively.ngrok-free.dev").strip()
FILE_API_TIMEOUT = 10.0

# --- Context Limits for MiniMax M2.5 ---
MINIMAX_MAX_CONTEXT = 200000  # 200k context limit
MINIMAX_SAFE_THRESHOLD = 180000  # Start shortening at 180k to leave room for response
CHARS_PER_TOKEN_ESTIMATE = 4  # Rough estimate: 4 chars ≈ 1 token

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

# --- Terminal Logging ---
def log_agent(role: str, message: str, project_id: str = ""):
    """Print agent activity to terminal for debugging."""
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
    }
    color = colors.get(role.lower(), "\033[94m")
    reset = "\033[0m"
    dim = "\033[90m"
    print(f"{dim}{timestamp}{reset} {prefix} {color}{role.upper()}{reset}: {message[:200]}{'...' if len(message) > 200 else ''}")

# -------------------------------------------------
# Token Limit HTML Message
# -------------------------------------------------

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

# -------------------------------------------------
# Shared Chat History (Agent Swarm Memory)
# -------------------------------------------------

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

# -------------------------------------------------
# JSON Extraction Helper
# -------------------------------------------------

def _extract_json(text: str) -> Any:
    """Robustly extract the largest valid JSON object from a string."""
    text = text.strip()
    
    code_block_pattern = r"```(?:json)?\s*(\{.*?)\s*```"
    match = re.search(code_block_pattern, text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
            
    try:
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end > start:
            json_str = text[start : end + 1]
            return json.loads(json_str)
    except json.JSONDecodeError:
        pass

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
        
    return None

# ============================================================================
# CONTEXT LENGTH MANAGEMENT FOR MINIMAX M2.5
# ============================================================================

class ContextManager:
    """Manages context length to stay within MiniMax M2.5's 200k token limit."""
    
    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Rough token estimation (4 chars ≈ 1 token for MiniMax)."""
        if not text:
            return 0
        # MiniMax uses similar tokenization to GPT, ~4 chars per token on average
        return len(text) // CHARS_PER_TOKEN_ESTIMATE
    
    @staticmethod
    def count_message_tokens(message: Dict[str, Any]) -> int:
        """Count tokens in a single message (handles text and multimodal)."""
        content = message.get("content", "")
        
        if isinstance(content, str):
            return ContextManager.estimate_tokens(content)
        elif isinstance(content, list):
            # Multimodal content (images, etc.)
            total = 0
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        total += ContextManager.estimate_tokens(item.get("text", ""))
                    elif item.get("type") == "image_url":
                        # Vision models typically charge ~1000-1500 tokens per image
                        total += 1000
            return total
        return 0
    
    @classmethod
    def count_messages_tokens(cls, messages: List[Dict[str, Any]]) -> int:
        """Count total tokens in all messages."""
        return sum(cls.count_message_tokens(msg) for msg in messages)
    
    @classmethod
    def shorten_context(
        cls, 
        messages: List[Dict[str, Any]], 
        max_tokens: int = MINIMAX_SAFE_THRESHOLD,
        preserve_recent: int = 4
    ) -> List[Dict[str, Any]]:
        """
        Intelligently shorten context to fit within token limit.
        
        Strategy:
        1. Always keep system message (first message if role == system)
        2. Always keep most recent N messages (default 4)
        3. Summarize or drop middle messages
        4. Add a truncation notice
        """
        if not messages:
            return messages
        
        current_tokens = cls.count_messages_tokens(messages)
        if current_tokens <= max_tokens:
            return messages
        
        log_agent("context", f"Context too long ({current_tokens} tokens), shortening...", "")
        
        # Extract system message if present
        system_msg = None
        start_idx = 0
        if messages[0].get("role") == "system":
            system_msg = messages[0]
            start_idx = 1
        
        # Always keep the last N messages
        recent_messages = messages[-preserve_recent:] if len(messages) > preserve_recent else []
        middle_messages = messages[start_idx:-preserve_recent] if len(messages) > preserve_recent + start_idx else []
        
        # Build result starting with system message
        result = []
        if system_msg:
            result.append(system_msg)
        
        # Add recent messages first to check if they alone exceed limit
        current_count = sum(cls.count_message_tokens(msg) for msg in result + recent_messages)
        if current_count > max_tokens:
            # Even recent messages are too much - keep only system + last 2
            recent_messages = recent_messages[-2:] if len(recent_messages) > 2 else recent_messages
            result = ([system_msg] if system_msg else []) + recent_messages
            
            # Add truncation notice
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
        
        # Add middle messages from oldest to newest until we hit limit
        kept_middle = []
        for msg in middle_messages:
            msg_tokens = cls.count_message_tokens(msg)
            if current_count + msg_tokens > max_tokens:
                break
            kept_middle.append(msg)
            current_count += msg_tokens
        
        # Assemble final result
        result.extend(kept_middle)
        result.extend(recent_messages)
        
        # Add truncation notice if we removed anything
        removed_count = len(messages) - len(result)
        if removed_count > 0:
            truncation_notice = {
                "role": "system",
                "content": f"[Context shortened: {removed_count} older messages removed to fit within {max_tokens} token limit. Focus on recent conversation.]"
            }
            # Insert after system message or at beginning
            insert_pos = 1 if (system_msg and result[0].get("role") == "system") else 0
            result.insert(insert_pos, truncation_notice)
        
        final_tokens = cls.count_messages_tokens(result)
        log_agent("context", f"Shortened from {len(messages)} to {len(result)} messages (~{final_tokens} tokens)", "")
        
        return result

# ============================================================================
# CONVERSATIONAL MCP PROTOCOL
# ============================================================================

class Intent(Enum):
    """MCP Intent types - the vocabulary of the swarm."""
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

@dataclass
class MCPMessage:
    """Internal Machine Communication Protocol - conversational agent chat."""
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
    """The nervous system - agents emit/receive MCP messages here."""
    
    def __init__(self, project_id: str):
        self.project_id = project_id
        self.messages: List[MCPMessage] = []
        self.subscribers: Dict[str, callable] = {}
        self.pending_questions: Dict[str, asyncio.Future] = {}
        self._background_tasks: set = set()
        
    def subscribe(self, agent_id: str, handler: callable):
        self.subscribers[agent_id] = handler
        
    def emit(self, msg: MCPMessage):
        self.messages.append(msg)
        target = msg.to_agent or "ALL"
        
        # Log with different colors for different intents
        intent_emoji = {
            Intent.QUESTION: "❓",
            Intent.CLARIFY: "💡",
            Intent.REASON: "🤔",
            Intent.REVIEW: "👁️",
            Intent.FEEDBACK: "💬",
            Intent.DONE: "✅",
            Intent.ERROR: "❌",
        }
        emoji = intent_emoji.get(msg.intent, "→")
        log_agent("mcp", f"{emoji} {msg.from_agent} → {target} | {msg.intent.value}: {msg.reasoning}", self.project_id)
        
        # Handle question-response pattern
        if msg.intent == Intent.CLARIFY and msg.task_id in self.pending_questions:
            future = self.pending_questions.pop(msg.task_id)
            if not future.done():
                future.set_result(msg)
        
        # Broadcast to subscribers - track tasks to prevent "destroyed but pending"
        if msg.to_agent and msg.to_agent in self.subscribers:
            task = asyncio.create_task(self.subscribers[msg.to_agent](msg))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        elif msg.to_agent is None:
            for agent_id, handler in self.subscribers.items():
                if agent_id != msg.from_agent:
                    task = asyncio.create_task(handler(msg))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
    
    async def await_all_tasks(self, timeout: float = 5.0):
        """Await all background tasks to prevent 'destroyed but pending' warnings."""
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
        """Ask a question and wait for clarification response."""
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
# SHARED CONTEXT
# ============================================================================

# ============================================================================
# SHARED CONTEXT (Merged Full-Stack + Database)
# ============================================================================

SHARED_CONTEXT = {
    "stack": {
        "frontend": "React 18 + TypeScript + Vite + Tailwind + Shadcn/UI",
        "backend": "Node.js + Express (ES modules)",
        "database": "Supabase PostgreSQL 15+ (Remote)",
    },
    "constraints": [
        "WebContainer compatible - NO native C++ modules",
        "Frontend imports: use @/ alias",
        "Backend imports: use relative paths with .js extension",
        "NEVER modify package.json scripts block",
        "UI should be creative, non-bootstrappy, no Inter font",
        "Database: Write raw SQL migrations in the `migrations/` directory."
    ],
    "structure": {
        "src/App.tsx": "Main app component (exists)",
        "src/main.tsx": "Entry point (exists)",
        "src/components/ui/": "Shadcn components (pre-installed)",
        "routes/": "Express API routes",
        "server.js": "Express entry (exists)",
        "migrations/": "Where generated PostgreSQL schema files live"
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
        self.total_tokens_used: int = 0  # Track tokens across all LLM calls
        bus.subscribe(agent_id, self._on_mcp)
    
    def get_tokens_used(self) -> int:
        """Get total tokens used by this agent."""
        return self.total_tokens_used
    
    def reset_tokens(self):
        """Reset token counter."""
        self.total_tokens_used = 0
        
    async def _on_mcp(self, msg: MCPMessage):
        """Override in subclasses to handle MCP messages."""
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
        """Ask another agent a question and get response."""
        response = await self.bus.ask(self.agent_id, to_agent, question, context, timeout)
        if response:
            return response.payload.get("answer") or response.payload.get("response")
        return None
    
    # ============================================================================
    # FILE READING CAPABILITIES
    # ============================================================================
    
    async def read_file(self, path: str, project_id: Optional[str] = None) -> Optional[str]:
        """
        Read a specific file from the project via the File API.
        
        Args:
            path: File path (e.g., "src/App.tsx")
            project_id: Optional override project ID (uses self.project_id if not provided)
            
        Returns:
            File content as string, or None if not found/error
        """
        pid = project_id or self.project_id
        if not pid:
            log_agent(self.agent_id, "Cannot read file: no project_id", self.project_id)
            return None
        
        # Check local file_tree first (cached)
        if hasattr(self, 'file_tree') and path in self.file_tree:
            return self.file_tree[path]
        
        # Fetch from API
        try:
            url = f"{FILE_API_BASE_URL}/api/project/{pid}/file"
            headers = {}
            
            # If running in same process as app, we might not need auth
            # But if external, you'd add: "Authorization": f"Bearer {API_TOKEN}"
            
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url, 
                    params={"path": path},
                    headers=headers,
                    timeout=FILE_API_TIMEOUT
                )
                
                if resp.status_code == 200:
                    data = resp.json()
                    content = data.get("content")
                    if content is not None:
                        # Update local cache
                        if hasattr(self, 'file_tree'):
                            self.file_tree[path] = content
                        log_agent(self.agent_id, f"Read file: {path} ({len(content)} chars)", pid)
                        return content
                elif resp.status_code == 404:
                    log_agent(self.agent_id, f"File not found: {path}", pid)
                else:
                    log_agent(self.agent_id, f"Error reading {path}: HTTP {resp.status_code}", pid)
                    
        except Exception as e:
            log_agent(self.agent_id, f"Failed to read file {path}: {str(e)[:60]}", pid)
        
        return None
    
    async def read_all_files(self, project_id: Optional[str] = None) -> Dict[str, str]:
        """
        Read all project files via the File API.
        
        Args:
            project_id: Optional override project ID
            
        Returns:
            Dictionary of {path: content}
        """
        pid = project_id or self.project_id
        if not pid:
            log_agent(self.agent_id, "Cannot read files: no project_id", self.project_id)
            return {}
        
        # Return cached if available
        if hasattr(self, 'file_tree') and self.file_tree:
            return dict(self.file_tree)
        
        try:
            url = f"{FILE_API_BASE_URL}/api/project/{pid}/files"
            headers = {}
            
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=headers, timeout=FILE_API_TIMEOUT)
                
                if resp.status_code == 200:
                    data = resp.json()
                    files = data.get("files", [])
                    file_dict = {f["path"]: f["content"] for f in files if "path" in f}
                    
                    # Update local cache
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
        """
        Read multiple files efficiently (concurrent requests).
        
        Args:
            paths: List of file paths
            project_id: Optional override project ID
            
        Returns:
            Dictionary of {path: content or None}
        """
        # Create tasks for all files
        tasks = [self.read_file(path, project_id) for path in paths]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        return {
            path: result if not isinstance(result, Exception) else None 
            for path, result in zip(paths, results)
        }
    
    # ============================================================================
    # LLM CALLS WITH AUTOMATIC CONTEXT SHORTENING
    # ============================================================================
    
    async def call_llm(self, messages: List[Dict], temperature: float = 0.6) -> Tuple[str, int]:
        """
        Call LLM with automatic context length management.
        
        Automatically shortens context if it exceeds MiniMax M2.5's safe threshold.
        """
        # Check and shorten context before sending
        original_count = len(messages)
        messages = ContextManager.shorten_context(messages, MINIMAX_SAFE_THRESHOLD)
        
        if len(messages) < original_count:
            log_agent(self.agent_id, f"Context auto-shortened: {original_count} → {len(messages)} messages", self.project_id)
        
        payload = {
            "model": MODEL,
            "messages": messages,
            "temperature": temperature,
            "provider": {
                "order": ["together"],
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
        tokens = data.get("usage", {}).get("total_tokens", 0)
        
        # Track tokens
        self.total_tokens_used += tokens
        
        log_agent("llm", f"← {tokens} tokens (total: {self.total_tokens_used}) | {content[:120]}...", self.project_id)
        
        return content, tokens

    async def call_vision_llm(self, messages: List[Dict], temperature: float = 0.6) -> Tuple[str, int]:
        """
        Call Vision LLM with automatic context length management.
        """
        # Check and shorten context before sending
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
        
        # Wrapped in str() to prevent logger crashes if 'content' is a complex list (image payload)
        last_msg_preview = str(messages[-1].get('content', ''))[:80] if messages else ""
        log_agent("llm", f"→ {len(messages)} msgs | {last_msg_preview}...", self.project_id)
        
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            
        content = data["choices"][0]["message"]["content"]
        tokens = data.get("usage", {}).get("total_tokens", 0)
        
        # Track tokens
        self.total_tokens_used += tokens
        
        log_agent("llm", f"← {tokens} tokens (total: {self.total_tokens_used}) | {content[:120]}...", self.project_id)
        
        return content, tokens
    
    def extract_json(self, text: str) -> Optional[Dict]:
        return _extract_json(text)
    
    def remember_conversation(self, role: str, content: str):
        """Store conversation for context."""
        self.conversation_memory.append({
            "role": role,
            "content": content,
            "timestamp": time.time()
        })
        if len(self.conversation_memory) > 20:
            self.conversation_memory = self.conversation_memory[-20:]

# ============================================================================
# PLANNER AGENT (With Questioning Capability)
# ============================================================================

class PlannerAgent(BaseAgent):
    """The Architect - creates plans, asks clarifying questions."""
    
    def _build_system_prompt(self, agent_skills: Optional[Dict] = None) -> str:
        skills_addon = ""
        if agent_skills and isinstance(agent_skills, dict):
            skills_addon = "\n\nUSER PREFERENCES:\n"
            if agent_skills.get("visuals") == "clean-svg":
                skills_addon += "- Visuals: Use clean SVG icons (Phosphor/Lucide). No emojis.\n"
            elif agent_skills.get("visuals") == "emojis":
                skills_addon += "- Visuals: Use text-based emojis instead of SVG icons.\n"
            if agent_skills.get("framework") == "tailwind":
                skills_addon += "- Styling: Use Tailwind CSS utility classes.\n"
            elif agent_skills.get("framework") == "vanilla-css":
                skills_addon += "- Styling: Use clean Vanilla CSS.\n"
            if agent_skills.get("style") == "beginner":
                skills_addon += "- Code Style: Beginner-friendly, heavily commented.\n"
            elif agent_skills.get("style") == "expert":
                skills_addon += "- Code Style: Expert-level, concise, minimal comments.\n"
            if agent_skills.get("personality") == "professional":
                skills_addon += "- Communication: Professional, formal.\n"
            elif agent_skills.get("personality") == "casual":
                skills_addon += "- Communication: Casual, friendly, use emojis.\n"

        return (
    "You are the Lead Architect for a high-performance **Full-Stack** web application, you are the GOR://A BUILDER multi agent AI BUILDER. Your goal is to create a strategic, step-by-step build plan for an AI Coder specialized in **React (Frontend)** AND **Node.js/Express (Backend)**. Strictly give NO CODE AT ALL, in no form. But you MUST REASON HARD.\n"
    "CRITICAL CONTEXT: The AI Coder executes tasks in isolation. It has NO memory of previous files unless you provide context in *every single task description*.\n\n"
    "Rules:\n"
    "MANDATORY OUTPUT FORMAT: JSON OBJECT ONLY. Do NOT wrap in markdown blocks.\n"
    "{\n"
    '  "assistant_message": "Sure I will build the ... application for you with...and...it will be...", --> this should be long and very precise.\n'
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
    "   - Backend Imports: Use relative paths with `.js` extension.\n"
    "   - Never instruct to the coder to build a `vercel.json` file in the root of the project according to the project's requirements.\n"
    "2. **AI Integration Specs (USE THESE EXACTLY):**\n"
    "   - **Core Rule**: You MUST route all AI API calls through the Gorilla Proxy using `process.env.GORILLA_API_KEY`.\n"
    "   - **High-Performance Logic (LLM)**: Use `https://corrinne-turbid-illustratively.ngrok-free.dev/api/v1/chat/completetions` with the process.env GORILLA_API_KEY, DO NOT SPECIFY THE MODEL OR ANY OTHER VALUES LIKE TEMPERATURE... NO MATTER WHAT.\n"
    "   - **Image Generation**: Send POST request to `https://corrinne-turbid-illustratively.ngrok-free.dev/api/v1/images/generations` with standard OpenAI payload.\n"
    "   - **Voice (STT)**: Send POST to `https://corrinne-turbid-illustratively.ngrok-free.dev/api/v1/audio/transcriptions` (OpenAI Whisper format).\n"
    "   - **Voice (TTS)**: DO NOT USE AN API. Strictly use the browser's native `window.speechSynthesis` Web Speech API in frontend components.\n"
    "   - **BG Removal**: Send POST with FormData (file) to `https://corrinne-turbid-illustratively.ngrok-free.dev/api/v1/images/remove-background`.\n"
    "3. **Task Bundling & Volume (CRITICAL FOR TOKEN SAVING):** \n"
    "   - Always try to ask the user at least 1 questions to elaborate on their request, they should be obvious and add functionality to their app if they agree. DO NOT ASK TECHNICAL QUESTIONS, THE USERS CANNOT CODE. WHEN YOU ASK A QUESTION DO NOT GENERATE TASKS AT ALL. Do not generate tasks even if the user asks a question. DO NOT BOTHER THE USER WITH TOO MANY QUESTIONS IF THEY DONT FEEL LIKE IT OR ANY DEBUGGING QUESTIONS.\n"
    "   - CONSOLIDATE TASKS: You MUST bundle related operations together. Combine them into Macro Steps (e.g., 'Step 1: Database & Backend setup', 'Step 2: Core UI Components', 'Step 3: Frontend Wiring').\n"
    "   - Simple Apps: Maximum 3-4 Macro/clubbed Tasks. + DB TASK (if there are no questions only!)\n"
"   - Complex Apps: Maximum 5-unlimited Macro/clubbed Tasks. + DB TASK (if there are no questions only!)\n"
    "   - Debugging/Simple addition Tasks: 1 task only. DO NOT ASK QUESTIONS FOR DEBUGGING.\n"
    "   - Update `server.js` and `App.tsx` **LAST** to wire up components/routes."
    "\n\n========================================================================\n"
    "🔥 SUPABASE FULL-STACK CAPABILITY UNLOCKED 🔥\n"
    "========================================================================\n"
    "You now have the ability to provision and structure a remote PostgreSQL database alongside the React/Node app.\n"
    "When a user asks for a database (even if they don't ask and the app needs one, try your best to make one), user accounts, or persistent storage, you MUST:\n"
    "1. Plan a task to create a SQL migration file in the `migrations/` directory (e.g., `migrations/001_init.sql`).\n"
    "2. Ensure the SQL includes `ENABLE ROW LEVEL SECURITY` and appropriate policies.\n"
    "3. Plan backend Node.js tasks to query this database using the `@supabase/supabase-js` client.\n"
    "4. The credentials `process.env.VITE_SUPABASE_URL` and `process.env.VITE_SUPABASE_ANON_KEY` are already injected into the environment. Use them."
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
        
        system_prompt = self._build_system_prompt(agent_skills)
        chat_history = _get_history(self.project_id)
        
        messages = [{"role": "system", "content": system_prompt}]
        for h in chat_history:
            messages.append({"role": h["role"], "content": h["content"]})
        
        messages.append({"role": "user", "content": f"""CONTEXT: {context_str} CURRENT FILES: {json.dumps(clean_files[:20])} USER REQUEST: {user_request} {'This appears to be a DEBUG request.' if is_debug else 'Analyze this request and either create a plan OR ask clarifying questions.'} Output JSON with either type="plan" or type="questions"."""})

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
                assistant_message = data.get("assistant_message", "Plan created.")
                
                if response_type == "questions":
                    questions = data.get("questions", [])
                    log_agent("planner", f"Asking {len(questions)} clarifying questions", self.project_id)
    
                    # Format questions into the assistant message so user sees them
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
                    
                    for i, task in enumerate(tasks, 1):
                        log_agent("planner", f"  Task {i}: {str(task)[:50]}...", self.project_id)
                    
                    if self.project_id:
                        _append_history(self.project_id, "assistant", assistant_message)
                    
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
# REASONER AGENT (The Thinker)
# ============================================================================

class ReasonerAgent(BaseAgent):
    """The Thinker - validates plans, reasons about approach, coordinates."""
    
    SYSTEM_PROMPT = (
        "You are the Reasoner Agent. Your job is to THINK before acting.\n\n"
        "RESPONSIBILITIES:\n"
        "1. Review plans from the Planner\n"
        "2. Identify potential issues, dependencies, or missing pieces\n"
        "3. Decide whether to proceed, ask for changes, or refine the plan\n"
        "4. Coordinate between agents\n\n"
        "5. Ensure that if the plan requires a database, it includes tasks to create `.sql` files in `migrations/` alongside the Node.js/React tasks."
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
        """Review and validate the plan before execution."""
        payload = plan_msg.payload
        tasks = payload.get("tasks", [])
        
        log_agent("reasoner", f"Reviewing plan: {len(tasks)} tasks", self.project_id)
        
        # Quick heuristic reasoning without LLM for speed
        concerns = []
        
        # Check for common issues
        task_text = " ".join([str(t) for t in tasks]).lower()
        
        if "server.js" in task_text and any("route" in str(t).lower() for t in tasks):
            concerns.append("Ensure routes are mounted before static file serving")
        
        if len(tasks) > 15:
            concerns.append("Large plan - consider if all tasks are necessary")
        
        # Check for frontend/backend balance
        has_frontend = any(x in task_text for x in ["component", "tsx", "ui", "page"])
        has_backend = any(x in task_text for x in ["route", "api", "server", "express"])
        
        if has_frontend and not has_backend and any(x in task_text for x in ["chat", "api"]):
            concerns.append("Frontend-focused plan but may need backend routes")
        
        if concerns:
            log_agent("reasoner", f"Concerns: {len(concerns)} issues identified", self.project_id)
            for c in concerns:
                log_agent("reasoner", f"  ⚠️ {c}", self.project_id)
        else:
            log_agent("reasoner", "Plan looks good, proceeding", self.project_id)
        
        # Forward to coder (with or without refinement)
        self.emit(
            intent=Intent.PLAN,
            payload={
                "reasoner_review": {
                    "concerns": concerns,
                    "approved": True
                }
            },
            to="coder",
            task_id=plan_msg.task_id,
            reasoning=f"Reviewed plan, {len(concerns)} concerns, proceeding"
        )
    
    async def _handle_clarification_needed(self, msg: MCPMessage):
        """Handle when planner needs user clarification."""
        payload = msg.payload
        questions = payload.get("questions", [])
        
        log_agent("reasoner", f"Clarification needed: {len(questions)} questions", self.project_id)
        
        # Forward to user via special message
        self.emit(
            intent=Intent.QUESTION,
            payload=payload,
            to=None,  # Broadcast - user handler will catch this
            task_id=msg.task_id,
            reasoning="Forwarding questions to user"
        )
    

# ============================================================================
# CODER AGENT (With Reflection)
# ============================================================================

class CoderAgent(BaseAgent):
    
    SYSTEM_PROMPT = (
        "You are an expert **Full-Stack** AI Coder. You build high-quality Web Apps using a **React + TypeScript + Tailwind + Shadcn/UI** (Frontend) AND **Node.js + Express** (Backend) stack.\n"
        "You are working in a pre-existing environment. **DO NOT initialize a new project.**\n"
        "Your Goal: Implement the requested task by editing EXISTING files (e.g., `src/App.tsx`, `server.js`) or creating NEW components/routes.\n\n"
        "CRITICAL CONTEXT - THE GOLDEN BOILERPLATE:\n"
        "The following tools are ALREADY installed and configured:\n"
        "1. **React + TypeScript (Vite)**: Frontend lives in `src/`. Use `.tsx` for UI.\n"
        "2. **Tailwind CSS**: Use utility classes (e.g., `className='p-4 bg-blue-500'`).\n"
        "3. **Shadcn/UI**: The folder `src/components/ui/` is fully populated.\n"
        "4. **Node.js (ES Modules)**: Backend uses `import/export`. Entry point is `server.js`.\n"
        "5. **Express.js**: Server is configured with CORS and Dotenv.\n\n"
        "**IMPORTANT** even though these are already in place, please try to make the UI less bootstrappy and more fun and polished, try to make the components yourself instead of always using shadcn UI, but when feel the need to use shadcn UI, do it, in a not very obivious way.\n\n"
        "UI/UX & DESIGN ENCOURAGEMENT:\n"
        "- Go all out on the frontend! We want a sleek, modern, and highly polished user interface. THINK OUT OF THE BOX WITHOUT BOOTSTRAPPY LOOKS AND NO INTER FONTS, BE CREATIVE!\n"
        "- Liberally use Tailwind CSS for beautiful styling, spacing, and typography.\n"
        "- Use `framer-motion` for buttery smooth micro-interactions, page transitions, and element reveals.\n"
        "- Use `lucide-react` for crisp, consistent iconography.\n"
        "- Make it look like a premium, production-ready SaaS product right out of the gate. Don't settle for basic layouts!\n\n"
        "AUTHENTICATION & GOOGLE/GITHUB SIGN-IN INSTRUCTIONS:\n"
        "- If the planner or user asks to add authentication, login, or 'Sign in with Google/GitHub', DO NOT install Firebase, Supabase auth, Auth0, or write raw OAuth logic. A secure auth gateway is ALREADY provided.\n"
        "- To implement Auth, strictly follow these steps in your React components:\n"
        "  1. Import the utility: `import { login, onAuthStateChanged, logout } from '@/utils/auth';`\n"
        "  2. Create state: `const [user, setUser] = useState<any>(null);`\n"
        "  3. Set up the listener: `useEffect(() => { const unsubscribe = onAuthStateChanged((u) => setUser(u)); return () => unsubscribe(); }, []);`\n"
        "  4. Trigger login: Use `onClick={() => login('google')}` or `onClick={() => login('github')}` on your buttons.\n"
        "  5. Trigger logout: Use `onClick={() => logout()}`.\n\n"
        "STRICT IMPORT RULES:\n"
        "- DO NOT READ MORE THAN 7-8 FILES PER TASK AND KEEP BELOW 4 UNLESS REQUIRED FOR CONTEXT"
        "- **FRONTEND (`src/` files)**:\n"
        "  - Use `@/` alias (e.g., `import { Button } from '@/components/ui/button'`).\n"
        "  - Do NOT use relative paths like `../../`.\n"
        "- **BACKEND (`server.js`, `routes/` files)**:\n"
        "  - Use **Relative Paths** (e.g., `import router from './routes/api.js'`).\n"
        "  - **CRITICAL**: You MUST include the `.js` extension for local backend imports.\n\n"
        "**OF HIGHEST IMPORTANCE: RESPONSE FORMAT:**\n"
        "{\n"
        '  "message": "A short, friendly status update.",\n'
        '  "operations": [\n'
        "    {\n"
        '      "action": "create_file" | "overwrite_file" | "read_file",\n'
        '      "path": "src/pages/Dashboard.tsx" OR "routes/api.js",\n'
        '      "content": "FULL FILE CONTENT HERE (only for create_file/overwrite_file)"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "**AI Integration Specs (USE THESE EXACTLY):**\n"
        "   - **Core Rule**: You MUST route all AI API calls through the Gorilla Proxy using `process.env.GORILLA_API_KEY`.\n"
        "   - **High-Performance Logic (LLM/Vision with B64)**: Use `https://corrinne-turbid-illustratively.ngrok-free.dev/api/v1/chat/completetions ` with the process.env GORILLA_API_KEY, DO NOT SPECIFY THE MODEL OR ANY OTHER VALUES LIKE TEMPERATURE... NO MATTER WHAT. For vision send the image as BASE64 data as a part of the prompt to the model, do not use a base 64 package, instead use ```Buffer.from...```\n"
        "   - **Image Generation**: Send POST request to `https://corrinne-turbid-illustratively.ngrok-free.dev/api/v1/images/generations ` with standard OpenAI payload.\n"
        "   - **Voice (STT)**: Send POST to `https://corrinne-turbid-illustratively.ngrok-free.dev/api/v1/audio/transcriptions ` (OpenAI Whisper format).\n"
        "   - **Voice (TTS)**: DO NOT USE AN API. Strictly use the browser's native `window.speechSynthesis` Web Speech API in frontend components.\n"
        "   - **BG Removal**: Send POST with FormData (file) to `https://corrinne-turbid-illustratively.ngrok-free.dev/api/v1/images/remove-background `.\n\n"
        "GLOBAL RULES:\n"
        "1. Output valid JSON only. No markdown blocks. ALL API KEYS ARE IN THE ENVIRONMENT.\n"
        "2. NEVER generate .env or Dockerfile. The main server is always server.js and the backend is always node.js within the routes/ folder, the frontend is always react/typescript.\n"
        "3. NEVER use literal '\\n'. Use physical newlines.\n"
        '4. **File Reading**: To read an existing file, output `{"action": "read_file", "path": "src/file.tsx"}`. The system will provide the file content in the next context turn. You can then use that content to inform your edits.\n'
        "5. When you get instructions to finalize the server.js, ALWAYS update the WHOLE SERVER.JS and use overwrite_file action, never leave it as is.\n"
        "6. CRITICAL INFRASTRUCTURE RULE: If you modify `package.json` to add dependencies, you MUST entirely preserve the existing `scripts` block. NEVER delete or modify the `dev`, `server`, `client`, or `db:push` scripts, or the WebContainer will fatally crash.\n\n"
        "You now have the ability to write raw PostgreSQL migrations IN ADDITION to React and Node.js code.\n"
        "1. DB MIGRATIONS: If instructed, write pure PostgreSQL to files in the `migrations/` directory (e.g., `migrations/001_schema.sql`). These must use `CREATE TABLE IF NOT EXISTS` and `ENABLE ROW LEVEL SECURITY`.\n"
        "2. DATABASE CLIENT: In Node.js or React, use the standard Supabase JS client to interact with the DB:\n"
        "   `import { createClient } from '@supabase/supabase-js';`\n"
        "   `const supabase = createClient(process.env.VITE_SUPABASE_URL, process.env.VITE_SUPABASE_ANON_KEY);`\n"
        "3. Do not fake data if a database is requested; write the SQL to create the tables, and the Node.js code to query them."
        "SPECIFIC RULES:\n"
        "1. **Frontend (React)**: Use Functional Components. MAKE EVERYTHING LOOK VERY GOOD! WITH EYECANDY FOR THE USER.\n"
        "2. **Backend (Node)**: Use `async/await`. Return JSON (`res.json`). Handle errors with `try/catch`.\n"
        "3. **Self-Correction**: If the user prompt reports a crash, analyze the stack trace and fix the specific file causing it. IF THERE IS AN UNINSTALLED DEPENDENCY LATER ON JUST MAKE THE COMPONENT NOT USE IT AS MUCH AS POSSIBLE."
    )


    def __init__(self, agent_id: str, bus: MCPBus, project_id: str):
        super().__init__(agent_id, bus, project_id)
        self.sub_agents: Dict[str, BaseAgent] = {}
        self.pending_tasks: Dict[str, Dict] = {}
        self.all_operations: List[Dict] = []
        self.task_results: Dict[str, Dict] = {}
        self.execution_complete = False  # Flag to track completion
        
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
            
            # Handle read_file - content is optional
            if action == "read_file":
                normalized_ops.append({
                    "action": action,
                    "path": path.strip(),
                    "content": None
                })
                continue
                
            # For write operations, content is required
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
        snippets = []
        priority_files = ["src/App.tsx", "src/main.tsx", "src/pages/Index.tsx", "src/index.css", "package.json"]
        
        for p in priority_files:
            if p in self.file_tree:
                c = self.file_tree[p]
                snippets.append(f"--- {p} ---\n{c[:5000]}\n")
        
        return "\n".join(snippets)
    
    async def _execute_plan(self, plan_msg: MCPMessage):
        payload = plan_msg.payload
        tasks = payload.get("tasks", [])
        is_debug = payload.get("is_debug", False)
        
        log_agent("coder", f"Executing {len(tasks)} tasks (debug={is_debug})", self.project_id)
        
        if not tasks:
            self.execution_complete = True
            self.emit(
                intent=Intent.DONE,
                payload={"status": "no_tasks", "message": "No tasks to execute", "operations": []},
                reasoning="Plan had no tasks"
            )
            return
        
        # Execute tasks with reflection between each
        for i, task in enumerate(tasks):
            task_num = i + 1
            task_id = f"task_{task_num}_{int(time.time() * 1000)}"
            
            log_agent("coder", f"[{task_num}/{len(tasks)}] {str(task)[:50]}...", self.project_id)
            
            # Determine agent type
            task_str = str(task).lower()
            if "ui" in task_str or "component" in task_str or "page" in task_str:
                agent_type = "ui"
            elif "api" in task_str or "route" in task_str or "endpoint" in task_str:
                agent_type = "api"
            else:
                agent_type = "logic"
            
            # Execute task
            if agent_type in ["ui", "api"] and agent_type in self.sub_agents:
                await self._delegate_task(task, task_id, agent_type)
            else:
                await self._implement_task(task, task_id)
            
            # Brief pause between tasks for stability
            await asyncio.sleep(0.2)
        
        # Wait for all sub-agents
        waited = 0
        while self.pending_tasks and waited < 180:
            await asyncio.sleep(0.5)
            waited += 0.5
        
        # Request review before marking complete
        self.emit(
            intent=Intent.REVIEW,
            payload={
                "operations": self.all_operations,
                "task_count": len(tasks)
            },
            to="reviewer",
            task_id=f"review_{int(time.time())}",
            reasoning=f"Completed {len(tasks)} tasks, requesting review"
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
        
        messages = [{"role": "system", "content": self.SYSTEM_PROMPT}]
        for h in chat_history:
            messages.append({"role": h["role"], "content": h["content"]})
        
        messages.append({"role": "user", "content": f"""CONTEXT: {context}, TASK: {task}, Implement this task. After coding, reflect on whether it's correct. Output JSON with message, reflection, and operations."""})

        max_iterations = 20  # Prevent infinite read loops
        
        for iteration in range(max_iterations):
            max_retries = 3
            last_error = None
            
            for attempt in range(max_retries + 1):
                try:
                    raw, tokens = await self.call_llm(messages, temperature=0.6)
                    parsed = self.extract_json(raw)
                    
                    if not parsed:
                        raise ValueError("Could not extract JSON from response")
                        
                    canonical = self._normalize_and_validate_ops(parsed)
                    ops = canonical.get("operations", [])
                    
                    # Check if AI wants to read files first
                    read_ops = [op for op in ops if op.get("action") == "read_file"]
                    write_ops = [op for op in ops if op.get("action") != "read_file"]
                    
                    if read_ops:
                        log_agent("coder", f"Reading {len(read_ops)} file(s)...", self.project_id)
                        
                        # Read requested files
                        file_contents = []
                        for read_op in read_ops:
                            path = read_op.get("path")
                            content = await self.read_file(path)
                            if content is not None:
                                file_contents.append(f"--- {path} ---\n{content}\n")
                                # Update local file_tree cache
                                self.file_tree[path] = content
                            else:
                                file_contents.append(f"--- {path} ---\n[File not found or error reading]\n")
                        
                        # Add AI response and file contents to context, then continue loop
                        messages.append({"role": "assistant", "content": raw})
                        messages.append({
                            "role": "user", 
                            "content": f"Here are the requested file contents:\n\n{''.join(file_contents)}\n\nNow continue with the task. Output JSON with any write operations needed."
                        })
                        
                        # Break out of retry loop to continue iteration with new context
                        last_error = None
                        break
                    
                    # No read operations - process write operations normally
                    reflection = canonical.get("reflection", "")
                    self.all_operations.extend(ops)
                    self.task_results[task_id] = {
                        "operations": ops,
                        "reflection": reflection,
                        "success": True
                    }
                    
                    for op in ops:
                        log_agent("coder", f"  ✓ {op.get('action')}: {op.get('path')}", self.project_id)
                    
                    if reflection:
                        log_agent("coder", f"  🤔 Reflection: {reflection[:80]}...", self.project_id)
                    
                    _append_history(self.project_id, "user", f"Task: {task}")
                    _append_history(self.project_id, "assistant", raw)
                    
                    return

                except Exception as e:
                    last_error = e
                    log_agent("coder", f"Attempt {attempt+1} failed: {str(e)[:60]}", self.project_id)
                    
                    if attempt < max_retries:
                        correction_msg = f"Fix the error and output valid JSON: {str(e)[:100]}"
                        messages.append({"role": "user", "content": correction_msg})
                        await asyncio.sleep(1)
                    else:
                        # Max retries exceeded
                        break
            
            # If we broke out due to file read, continue to next iteration
            if last_error is None and read_ops:
                continue
                
            # If we had an error after max retries, record failure
            if last_error:
                self.task_results[task_id] = {"error": str(last_error), "success": False}
                return
        
        # Max iterations reached (too many read_file rounds)
        log_agent("coder", f"Max read iterations reached for task {task_id}", self.project_id)
        self.task_results[task_id] = {"error": "Max file read iterations exceeded", "success": False}
    
    async def _handle_sub_done(self, msg: MCPMessage):
        task_id = msg.task_id
        
        if task_id in self.pending_tasks:
            del self.pending_tasks[task_id]
            ops = msg.payload.get("operations", [])
            self.all_operations.extend(ops)
            log_agent("coder", f"Sub-agent completed: {len(ops)} ops", self.project_id)
    
    async def _handle_feedback(self, msg: MCPMessage):
        """Handle feedback from reviewer."""
        feedback = msg.payload.get("feedback", "")
        issues = msg.payload.get("issues", [])
        
        if issues:
            log_agent("coder", f"Reviewer found {len(issues)} issues", self.project_id)
            # Could trigger re-implementation here
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
# REVIEWER AGENT (Quality Check)
# ============================================================================

class ReviewerAgent(BaseAgent):
    """Quality checker - reviews output before completion."""
    
    SYSTEM_PROMPT = (
        "You are the Reviewer Agent. Your job is quality control.\n\n"
        "RESPONSIBILITIES:\n"
        "1. Review all file operations\n"
        "2. Check for common issues (missing imports, syntax errors, etc.)\n"
        "3. Verify the implementation matches the intent\n"
        "4. Provide constructive feedback\n\n"
        "RULES:\n"
        "0. For any `.sql` files, verify that `ENABLE ROW LEVEL SECURITY` is present for tables. For `.js`/`.tsx` files, verify standard code quality."
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
        
        # Heuristic review without LLM for speed
        issues = []
        
        for op in operations:
            path = op.get("path", "")
            content = op.get("content", "")
            
            # Check for common issues
            if path.endswith(".tsx") or path.endswith(".ts"):
                if "import React" in content and "from 'react'" not in content:
                    issues.append(f"{path}: Malformed React import")
                
                if "function" in content and "export" not in content:
                    issues.append(f"{path}: Function may not be exported")
            
            if path.endswith(".js"):
                if "require(" in content and "import " in content:
                    issues.append(f"{path}: Mixing require and import")
        
        # Check for critical files
        paths = [op.get("path", "") for op in operations]
        if any("server.js" in p for p in paths):
            if not any("routes/" in p for p in paths):
                # This is fine - may just be updating server.js
                pass
        
        passed = len(issues) == 0
        
        if passed:
            log_agent("reviewer", "✅ All checks passed", self.project_id)
        else:
            log_agent("reviewer", f"⚠️ Found {len(issues)} issues", self.project_id)
            for issue in issues[:3]:
                log_agent("reviewer", f"   - {issue}", self.project_id)
        
        # Send feedback to coder
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
# SUB-AGENTS
# ============================================================================

class UISubAgent(BaseAgent):
    """UI Specialist - React components and styling."""
    
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
    """API Specialist - Express routes and backend."""
    
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
    """Logic Specialist - utilities and helpers."""
    
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
# DEBUGGER AGENT
# ============================================================================

class DebuggerAgent(BaseAgent):
    """Debugger - direct fixes, no overthinking."""
    
    SYSTEM_PROMPT = (
        "You are the Debugger.\n\n"
        "YOUR JOB: Fix errors. Be DIRECT.\n\n"
        "RULES:\n"
        "1. Look at the error message\n"
        "2. Find the problematic file/line\n"
        "3. Fix with MINIMAL changes\n\n"
        "NO explaining. Just fix.\n\n"
        "RESPONSE FORMAT (JSON):\n"
        "{\n"
        '  "message": "Fixed: ...",\n'
        '  "operations": [{"action": "overwrite_file", "path": "...", "content": "..."}]\n'
        "}"
    )

    async def debug(self, error_message: str, file_tree: Dict[str, str]) -> List[Dict]:
        log_agent("debugger", f"Fixing: {error_message[:60]}...", self.project_id)
        
        file_match = re.search(r'(?:in|at)\s+(\S+\.(?:tsx|ts|js|jsx))', error_message)
        relevant_file = file_match.group(1) if file_match else "unknown"
        file_content = file_tree.get(relevant_file, "")
        
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": f"""ERROR:\n{error_message}\n\nFILE: {relevant_file}\nCONTENT:\n{file_content[:1500]}\n\nFix this. Output JSON."""}
        ]
        
        raw, _ = await self.call_llm(messages, temperature=0.2)
        data = self.extract_json(raw)
        
        if data:
            ops = data.get("operations", [])
            for op in ops:
                log_agent("debugger", f"  ✓ {op.get('action')}: {op.get('path')}", self.project_id)
            return ops
        
        return []

# ============================================================================
# SWARM ORCHESTRATOR
# ============================================================================

class SupabaseAgentSwarm:
    """Main orchestrator - creates and manages all agents with conversation layers."""
    
    def __init__(self, project_id: str):
        self.project_id = project_id
        self.bus = MCPBus(project_id)
        
        # Core agents
        self.planner = PlannerAgent("planner", self.bus, project_id)
        self.reasoner = ReasonerAgent("reasoner", self.bus, project_id)
        self.coder = CoderAgent("coder", self.bus, project_id)
        self.reviewer = ReviewerAgent("reviewer", self.bus, project_id)
        self.debugger = DebuggerAgent("debugger", self.bus, project_id)
        
        # Sub-agents
        self.ui_agent = UISubAgent("ui_agent", self.bus, project_id)
        self.api_agent = APISubAgent("api_agent", self.bus, project_id)
        self.logic_agent = LogicSubAgent("logic_agent", self.bus, project_id)
        
        # Register sub-agents with coder
        self.coder.register_sub_agent(self.ui_agent)
        self.coder.register_sub_agent(self.api_agent)
        self.coder.register_sub_agent(self.logic_agent)
        
        log_agent("swarm", "🧠 Conversational agent swarm initialized", project_id)
    
    def get_total_tokens(self) -> int:
        """Get total tokens used by ALL agents in the swarm."""
        agents = [
            self.planner, self.reasoner, self.coder, self.reviewer, self.debugger,
            self.ui_agent, self.api_agent, self.logic_agent
        ]
        total = sum(agent.get_tokens_used() for agent in agents)
        log_agent("swarm", f"💰 Total tokens used: {total}", self.project_id)
        return total
    
    def reset_all_tokens(self):
        """Reset token counters for all agents."""
        agents = [
            self.planner, self.reasoner, self.coder, self.reviewer, self.debugger,
            self.ui_agent, self.api_agent, self.logic_agent
        ]
        for agent in agents:
            agent.reset_tokens()
        log_agent("swarm", "🔄 Token counters reset", self.project_id)
    
    async def solve(self, user_request: str, file_tree: Dict[str, str], 
                    agent_skills: Optional[Dict] = None,
                    skip_planner: bool = False) -> Dict[str, Any]:
        """Main entry point with full conversation flow."""
        
        for agent in [self.planner, self.reasoner, self.coder, self.reviewer,
                      self.debugger, self.ui_agent, self.api_agent, self.logic_agent]:
            agent.file_tree = file_tree
        
        log_agent("swarm", f"🎯 Solving: {user_request[:60]}...", self.project_id)
        
        assistant_message = "Working on it..."
        needs_clarification = False
        questions = []
        
        if skip_planner:
            log_agent("swarm", "Direct mode", self.project_id)
            self.coder.emit(
                intent=Intent.PLAN,
                payload={
                    "assistant_message": "Applying fix...",
                    "tasks": [user_request],
                    "is_debug": True
                },
                to="coder",
                task_id="direct",
                reasoning="Direct execution"
            )
            assistant_message = "Applying fix..."
        else:
            # Phase 1: Planning
            plan_result = await self.planner.plan(user_request, file_tree, agent_skills)
            assistant_message = plan_result.payload.get("assistant_message", "Building...")
            
            # Check if clarification needed
            if plan_result.intent == Intent.QUESTION:
                needs_clarification = True
                questions = plan_result.payload.get("questions", [])
                log_agent("swarm", f"⏸️ Need user clarification: {len(questions)} questions", self.project_id)
                
                # Await pending tasks before returning
                await self.bus.await_all_tasks(timeout=1.0)
                
                return {
                    "status": "needs_clarification",
                    "assistant_message": assistant_message,
                    "questions": questions,
                    "operations": []
                }
            
            # Phase 2: Reasoning (automatic)
            # Reasoner already processed via MCP subscription
        
        # Phase 3: Execution - wait for completion with stability detection
        if not needs_clarification:
            max_wait = 300
            waited = 0
            last_op_count = 0
            stable_count = 0
            
            while waited < max_wait:
                await asyncio.sleep(0.5)
                waited += 0.5
                
                # Count current operations from all DONE messages
                current_ops = []
                for msg in self.bus.messages:
                    if msg.intent in [Intent.DONE, Intent.DEBUG_FIX]:
                        current_ops.extend(msg.payload.get("operations", []))
                
                # Check for stability - operation count not growing
                if len(current_ops) == last_op_count and len(current_ops) > 0:
                    stable_count += 1
                    # Stable for 3 seconds and no pending tasks = done
                    if stable_count >= 6 and not self.coder.pending_tasks:
                        log_agent("swarm", f"✅ Stable: {len(current_ops)} operations", self.project_id)
                        break
                else:
                    stable_count = 0
                    last_op_count = len(current_ops)
                    if len(current_ops) > 0:
                        log_agent("swarm", f"⏳ Growing: {len(current_ops)} operations...", self.project_id)
        
        # Collect all operations from ALL DONE messages
        all_ops = []
        for msg in self.bus.messages:
            if msg.intent in [Intent.DONE, Intent.DEBUG_FIX]:
                ops = msg.payload.get("operations", [])
                if ops:
                    all_ops.extend(ops)
        
        # Deduplicate by path (keep last occurrence - most recent version)
        seen_paths = {}
        for op in all_ops:
            path = op.get("path", "")
            if path:
                seen_paths[path] = op
        all_ops = list(seen_paths.values())
        
        log_agent("swarm", f"📦 Complete: {len(all_ops)} unique file operations", self.project_id)
        for op in all_ops:
            log_agent("swarm", f"  📄 {op.get('action')}: {op.get('path')}", self.project_id)
        
        # Await any pending background tasks
        await self.bus.await_all_tasks(timeout=3.0)
        
        # Get total tokens used by all agents
        total_tokens = self.get_total_tokens()
        
        return {
            "status": "complete",
            "assistant_message": assistant_message,
            "operations": all_ops,
            "total_tokens": total_tokens
        }
    
    async def continue_with_clarification(self, answers: Dict[str, str], 
                                          file_tree: Dict[str, str]) -> Dict[str, Any]:
        """Continue after user provides clarification."""
        log_agent("swarm", "Continuing with user clarification", self.project_id)
        
        # Add clarification to history
        clarification_text = "User clarified: " + json.dumps(answers)
        _append_history(self.project_id, "user", clarification_text)
        
        # Re-run planning with clarification context
        return await self.solve(
            user_request="Proceed with clarified requirements",
            file_tree=file_tree,
            skip_planner=False
        )
    
    async def debug(self, error_message: str, file_tree: Dict[str, str]) -> Dict[str, Any]:
        """Quick debug entry point."""
        self.debugger.file_tree = file_tree
        operations = await self.debugger.debug(error_message, file_tree)
        
        return {
            "status": "debug_complete",
            "assistant_message": "Fixed the error.",
            "operations": operations
        }

# ============================================================================
# BACKWARD COMPATIBILITY - Unified Agent Class
# ============================================================================

class Agent:
    """Backward-compatible wrapper for the SupabaseAgentSwarm."""
    
    def __init__(self, timeout_s: float = 120.0):
        self.timeout_s = timeout_s
        self._swarm_cache: Dict[str, SupabaseAgentSwarm] = {}
    
    def _get_swarm(self, project_id: str) -> SupabaseAgentSwarm:
        if project_id not in self._swarm_cache:
            self._swarm_cache[project_id] = SupabaseAgentSwarm(project_id)
        return self._swarm_cache[project_id]
    
    def remember(self, project_id: str, role: str, text: str) -> None:
        _append_history(project_id, role, text)
    
    def plan(self, user_request: str, project_context: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a build plan."""
        project_id = str(project_context.get("project_id") or "").strip()
        agent_skills = project_context.get("agent_skills")
        
        if project_id:
            _append_history(project_id, "user", user_request)
            log_agent("planner", f"Planning: {user_request[:100]}...", project_id)

        file_tree = {f: "" for f in project_context.get("files", [])}
        
        loop = asyncio.new_event_loop()
        try:
            swarm = self._get_swarm(project_id)
            future = swarm.planner.plan(user_request, file_tree, agent_skills)
            result = loop.run_until_complete(future)
            
            tasks = result.payload.get("tasks", [])
            assistant_message = result.payload.get("assistant_message", "")
            tokens = result.payload.get("estimated_tokens", 0)
            
            # Check if questions needed
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
        """Generate code - BULLETPROOF operation collection."""
        swarm = self._get_swarm(project_name)
        
        # Reset coder state for fresh execution
        swarm.coder.all_operations = []
        swarm.coder.pending_tasks = {}
        swarm.coder.execution_complete = False
        
        # Clear old messages to avoid picking up stale DONE messages
        swarm.bus.messages = []
        
        # Emit the plan to the coder
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
        
        # Wait for execution to complete - collect ALL operations from ALL DONE messages
        max_wait = 180  # Increased timeout
        waited = 0
        all_collected_ops = []
        last_op_count = 0
        stable_count = 0
        
        while waited < max_wait:
            await asyncio.sleep(0.5)
            waited += 0.5
            
            # Collect operations from ALL DONE messages (not just first one)
            current_ops = []
            for msg in swarm.bus.messages:
                if msg.intent in [Intent.DONE, Intent.DEBUG_FIX]:
                    ops = msg.payload.get("operations", [])
                    if ops:
                        current_ops.extend(ops)
            
            # Track if operation count is stable (indicates completion)
            if len(current_ops) == last_op_count and len(current_ops) > 0:
                stable_count += 1
                # If stable for 3 seconds and no pending tasks, we're done
                if stable_count >= 6 and not swarm.coder.pending_tasks:
                    all_collected_ops = current_ops
                    log_agent("agent", f"✅ Stable: {len(all_collected_ops)} operations collected", project_name)
                    break
            else:
                stable_count = 0
                last_op_count = len(current_ops)
                all_collected_ops = current_ops
        
        # Final collection - ensure we got everything
        final_ops = []
        for msg in swarm.bus.messages:
            if msg.intent in [Intent.DONE, Intent.DEBUG_FIX]:
                ops = msg.payload.get("operations", [])
                if ops:
                    final_ops.extend(ops)
        
        # Use the larger collection
        operations = final_ops if len(final_ops) >= len(all_collected_ops) else all_collected_ops
        
        # Deduplicate by path (keep last occurrence)
        seen_paths = {}
        for op in operations:
            path = op.get("path", "")
            if path:
                seen_paths[path] = op
        operations = list(seen_paths.values())
        
        log_agent("agent", f"📦 Final: {len(operations)} unique file operations", project_name)
        
        # Await background tasks
        await swarm.bus.await_all_tasks(timeout=3.0)
        
        # Get total tokens from ALL agents in the swarm
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

__all__ = ["Agent", "SupabaseAgentSwarm", "MCPBus", "MCPMessage", "Intent", 
           "_render_token_limit_message", "clear_history", "ContextManager"]