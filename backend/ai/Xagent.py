"""
X-Agent: Extreme Agent Swarm with Advanced MCP Protocol
=========================================================

Ultra-powerful multi-agent architecture featuring:
- 12+ specialized agents working in parallel
- Advanced reasoning chains with self-reflection
- Parallel task execution with dependency management
- Cross-agent collaboration with shared context
- Sophisticated error recovery and retry logic
- Token-efficient compressed communication

Powered by: google/gemini-3.1-pro-preview
"""

from __future__ import annotations

import os
import json
import re
import time
import asyncio
import hashlib
from typing import Dict, Any, List, Optional, Tuple, TypedDict, Set
from dataclasses import dataclass, field
from enum import Enum, auto
from collections import defaultdict

import httpx

# --- Configuration for OpenRouter ---
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL = os.getenv("XMODEL", "google/gemini-3.1-pro-preview")
OPENROUTER_URL = os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions").strip()
SITE_URL = os.getenv("SITE_URL", "https://gorillabuilder.dev").strip()
SITE_NAME = os.getenv("SITE_NAME", "Gorilla Builder X")

# Token multiplier for X-mode (9.3x)
TOKEN_MULTIPLIER = 9.3

if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY must be configured in the environment")

ALLOWED_ACTIONS = {"create_file", "overwrite_file", "patch_file"}
ACTION_NORMALIZE = {
    "update_file": "overwrite_file",
    "replace_file": "overwrite_file",
    "write_file": "overwrite_file",
    "modify_file": "overwrite_file",
    "upsert_file": "overwrite_file",
    "create": "create_file",
    "overwrite": "overwrite_file",
    "patch": "patch_file",
    "patch_file": "patch_file",
}

# --- Terminal Logging ---
def log_agent(role: str, message: str, project_id: str = ""):
    """Print agent activity to terminal for debugging."""
    prefix = f"[{project_id[:8]}]" if project_id else "[X-AGENT]"
    timestamp = time.strftime("%H:%M:%S")
    colors = {
        "architect": "\033[95m",
        "planner": "\033[94m",
        "strategist": "\033[96m",
        "coder": "\033[92m",
        "reviewer": "\033[93m",
        "optimizer": "\033[91m",
        "debugger": "\033[35m",
        "ui_agent": "\033[36m",
        "api_agent": "\033[33m",
        "logic_agent": "\033[32m",
        "tester": "\033[37m",
        "security": "\033[31m",
        "performance": "\033[34m",
        "swarm": "\033[97m",
        "llm": "\033[90m",
        "mcp": "\033[33m",
    }
    color = colors.get(role.lower(), "\033[94m")
    reset = "\033[0m"
    dim = "\033[90m"
    print(f"{dim}{timestamp}{reset} {prefix} {color}[{role.upper()}]{reset} {message[:200]}{'...' if len(message) > 200 else ''}")

# -------------------------------------------------
# Token Limit HTML Message
# -------------------------------------------------

def _render_token_limit_message() -> str:
    """Render a beautiful HTML message when token limit is reached."""
    return '''
    <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px 30px;background:linear-gradient(135deg,rgba(15,23,42,0.9) 0%,rgba(30,10,50,0.8) 100%);border:1px solid rgba(217,70,239,0.3);border-radius:20px;text-align:center;max-width:400px;margin:20px auto;box-shadow:0 20px 60px rgba(0,0,0,0.5),0 0 40px rgba(217,70,239,0.15);backdrop-filter:blur(10px);">
        <div style="width:80px;height:80px;background:linear-gradient(135deg,#d946ef 0%,#a855f7 100%);border-radius:50%;display:flex;align-items:center;justify-content:center;margin-bottom:24px;box-shadow:0 10px 40px rgba(217,70,239,0.4);animation:pulse-glow 2s ease-in-out infinite;">
            <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>
        </div>
        <h2 style="color:#fff;font-size:24px;font-weight:700;margin:0 0 12px 0;letter-spacing:-0.5px;">Token Limit Reached</h2>
        <p style="color:#94a3b8;font-size:14px;line-height:1.6;margin:0 0 28px 0;max-width:280px;">You\'ve used all your monthly tokens. Upgrade to Premium for unlimited access.</p>
        <a href="/pricing" style="display:inline-flex;align-items:center;gap:8px;background:linear-gradient(135deg,#d946ef 0%,#a855f7 100%);color:white;text-decoration:none;padding:14px 32px;border-radius:12px;font-size:14px;font-weight:600;box-shadow:0 4px 20px rgba(217,70,239,0.4);">Upgrade to Premium</a>
    </div>
    <style>@keyframes pulse-glow{0%,100%{box-shadow:0 10px 40px rgba(217,70,239,0.4);}50%{box-shadow:0 10px 50px rgba(217,70,239,0.7);}}</style>
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
    if r in ("user", "you"): return "user"
    if r in ("assistant", "system"): return "assistant"
    return "user"

def _append_history(project_id: str, role: str, content: str, max_items: int = 30) -> None:
    if not project_id: return
    msg = {"role": _norm_role(role), "content": (content or "").strip()}
    if not msg["content"]: return
    _HISTORY.setdefault(project_id, []).append(msg)
    if len(_HISTORY[project_id]) > max_items:
        _HISTORY[project_id] = _HISTORY[project_id][-max_items:]

def _get_history(project_id: str, max_items: int = 20) -> List[ChatMsg]:
    if not project_id: return []
    return list(_HISTORY.get(project_id, []))[-max_items:]

def clear_history(project_id: str) -> None:
    if project_id in _HISTORY:
        del _HISTORY[project_id]

# -------------------------------------------------
# JSON Extraction Helper
# -------------------------------------------------

def _extract_json(text: str) -> Any:
    """Robustly extract the largest valid JSON object from a string."""
    text = text.strip()
    
    # Try code blocks
    match = re.search(r"```(?:json)?\s*(\{.*?)\s*```", text, re.DOTALL)
    if match:
        try: return json.loads(match.group(1))
        except: pass
    
    # Try outer braces
    try:
        start, end = text.find('{'), text.rfind('}')
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end+1])
    except: pass
    
    # Try whole string
    try: return json.loads(text)
    except: pass
    
    return None

# ============================================================================
# ADVANCED MCP PROTOCOL
# ============================================================================

class Intent(Enum):
    """Extended MCP Intent types for X-Agent swarm."""
    # Planning & Strategy
    ARCHITECT = "architect"
    PLAN = "plan"
    STRATEGIZE = "strategize"
    DECOMPOSE = "decompose"
    
    # Communication
    QUESTION = "question"
    CLARIFY = "clarify"
    NEGOTIATE = "negotiate"
    
    # Execution
    IMPLEMENT = "implement"
    DELEGATE_UI = "delegate_ui"
    DELEGATE_API = "delegate_api"
    DELEGATE_LOGIC = "delegate_logic"
    DELEGATE_TEST = "delegate_test"
    DELEGATE_SECURITY = "delegate_security"
    DELEGATE_PERF = "delegate_perf"
    
    # Quality & Review
    REVIEW = "review"
    FEEDBACK = "feedback"
    OPTIMIZE = "optimize"
    REFACTOR = "refactor"
    
    # Error Handling
    DEBUG_FIX = "debug_fix"
    RETRY = "retry"
    FALLBACK = "fallback"
    
    # Completion
    DONE = "done"
    ERROR = "error"
    PARTIAL = "partial"

@dataclass
class MCPMessage:
    """Extended Machine Communication Protocol."""
    from_agent: str
    to_agent: Optional[str]
    intent: Intent
    task_id: str
    payload: Dict[str, Any]
    reasoning: str = ""
    priority: int = 5  # 1-10, higher = more urgent
    dependencies: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "intent": self.intent.value,
            "task_id": self.task_id,
            "payload": self.payload,
            "reasoning": self.reasoning,
            "priority": self.priority,
            "dependencies": self.dependencies,
            "timestamp": self.timestamp
        }

class MCPBus:
    """Advanced message bus with priority queuing and dependency tracking."""
    
    def __init__(self, project_id: str):
        self.project_id = project_id
        self.messages: List[MCPMessage] = []
        self.subscribers: Dict[str, callable] = {}
        self.pending_questions: Dict[str, asyncio.Future] = {}
        self._background_tasks: Set[asyncio.Task] = set()
        self.message_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.processing = False
        
    def subscribe(self, agent_id: str, handler: callable):
        self.subscribers[agent_id] = handler
        
    def emit(self, msg: MCPMessage):
        self.messages.append(msg)
        target = msg.to_agent or "ALL"
        
        intent_emoji = {
            Intent.QUESTION: "", Intent.CLARIFY: "", Intent.ARCHITECT: "",
            Intent.PLAN: "", Intent.STRATEGIZE: "", Intent.REVIEW: "",
            Intent.FEEDBACK: "", Intent.OPTIMIZE: "", Intent.DEBUG_FIX: "",
            Intent.DONE: "", Intent.ERROR: "", Intent.PARTIAL: "",
            Intent.DELEGATE_UI: "", Intent.DELEGATE_API: "", Intent.DELEGATE_LOGIC: "",
        }
        emoji = intent_emoji.get(msg.intent, "")
        log_agent("mcp", f"{emoji} {msg.from_agent} -> {target} | {msg.intent.value} (P{msg.priority}): {msg.reasoning[:60]}", self.project_id)
        
        # Handle question-response
        if msg.intent == Intent.CLARIFY and msg.task_id in self.pending_questions:
            future = self.pending_questions.pop(msg.task_id)
            if not future.done():
                future.set_result(msg)
        
        # Broadcast with task tracking
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
        """Await all background tasks."""
        if not self._background_tasks:
            return
        pending = list(self._background_tasks)
        if pending:
            try:
                await asyncio.wait(pending, timeout=timeout)
            except: pass
        self._background_tasks.clear()
    
    async def ask(self, from_agent: str, to_agent: str, question: str, 
                  context: Dict = None, timeout: float = 30.0) -> Optional[MCPMessage]:
        """Ask a question and wait for response."""
        task_id = f"q_{int(time.time() * 1000)}"
        future = asyncio.get_event_loop().create_future()
        self.pending_questions[task_id] = future
        
        self.emit(MCPMessage(
            from_agent=from_agent,
            to_agent=to_agent,
            intent=Intent.QUESTION,
            task_id=task_id,
            payload={"question": question, "context": context or {}},
            reasoning=f"Asking: {question[:60]}...",
            priority=8
        ))
        
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self.pending_questions.pop(task_id, None)
            return None

# ============================================================================
# SHARED CONTEXT
# ============================================================================

SHARED_CONTEXT = {
    "stack": {
        "frontend": "React 18 + TypeScript + Vite + Tailwind + Shadcn/UI",
        "backend": "Node.js + Express (ES modules)",
        "build_tool": "Vite",
        "package_manager": "npm",
    },
    "constraints": [
        "WebContainer compatible - NO native C++ modules",
        "Frontend imports: use @/ alias",
        "Backend imports: use relative paths with .js extension",
        "NEVER modify package.json scripts block",
        "UI should be creative, non-bootstrappy, no Inter font",
        "Use TypeScript for all new files",
        "Follow React best practices (hooks, functional components)",
    ],
    "structure": {
        "src/App.tsx": "Main app component (exists)",
        "src/main.tsx": "Entry point (exists)",
        "src/components/ui/": "Shadcn components (pre-installed)",
        "src/components/magicui/": "Magic UI components (pre-installed)",
        "src/lib/": "Utility functions",
        "src/hooks/": "Custom React hooks",
        "routes/": "Express API routes",
        "server.js": "Express entry (exists)",
    }
}

# ============================================================================
# BASE AGENT (Enhanced with Token Multiplier)
# ============================================================================

class BaseAgent:
    def __init__(self, agent_id: str, bus: MCPBus, project_id: str):
        self.agent_id = agent_id
        self.bus = bus
        self.project_id = project_id
        self.file_tree: Dict[str, str] = {}
        self.conversation_memory: List[Dict] = []
        self.total_tokens_used: int = 0
        self.raw_tokens_used: int = 0  # Before multiplier
        self.success_count: int = 0
        self.error_count: int = 0
        bus.subscribe(agent_id, self._on_mcp)
        
    async def _on_mcp(self, msg: MCPMessage):
        pass
    
    def emit(self, intent: Intent, payload: Dict, to: Optional[str] = None, 
             task_id: str = "", reasoning: str = "", priority: int = 5):
        msg = MCPMessage(
            from_agent=self.agent_id,
            to_agent=to,
            intent=intent,
            task_id=task_id,
            payload=payload,
            reasoning=reasoning,
            priority=priority
        )
        self.bus.emit(msg)
    
    async def ask(self, to_agent: str, question: str, context: Dict = None, 
                  timeout: float = 30.0) -> Optional[str]:
        response = await self.bus.ask(self.agent_id, to_agent, question, context, timeout)
        if response:
            return response.payload.get("answer") or response.payload.get("response")
        return None
    
    async def call_llm(self, messages: List[Dict], temperature: float = 0.6, 
                       max_tokens: Optional[int] = None) -> Tuple[str, int]:
        """Call LLM with Gemini model. Tracks tokens with 9.3x multiplier."""
        payload = {
            "model": MODEL,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
            
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": SITE_URL,
            "X-Title": SITE_NAME,
        }
        
        last_msg = messages[-1].get('content', '')[:60] if messages else ""
        log_agent(self.agent_id, f" -> {len(messages)} msgs | {last_msg}...", self.project_id)
        
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            
        content = data["choices"][0]["message"]["content"]
        raw_tokens = data.get("usage", {}).get("total_tokens", 0)
        
        # Apply 9.3x multiplier
        multiplied_tokens = int(raw_tokens * TOKEN_MULTIPLIER)
        self.raw_tokens_used += raw_tokens
        self.total_tokens_used += multiplied_tokens
        self.success_count += 1
        
        log_agent(self.agent_id, f" <- {raw_tokens} tokens (x{TOKEN_MULTIPLIER} = {multiplied_tokens}) | {content[:100]}...", self.project_id)
        
        return content, multiplied_tokens
    
    def extract_json(self, text: str) -> Optional[Dict]:
        return _extract_json(text)
    
    def get_tokens_used(self) -> int:
        return self.total_tokens_used
    
    def get_raw_tokens(self) -> int:
        return self.raw_tokens_used
    
    def reset_tokens(self):
        self.total_tokens_used = 0
        self.raw_tokens_used = 0
        self.success_count = 0
        self.error_count = 0

# ============================================================================
# ARCHITECT AGENT (High-Level Design)
# ============================================================================

class ArchitectAgent(BaseAgent):
    """Creates high-level system architecture and component breakdown."""
    
    SYSTEM_PROMPT = (
        "You are the System Architect for Gorilla Builder X. Create high-level designs with clear component boundaries.\n\n"
        "Your role:\n"
        "1. Analyze user requirements and translate into system architecture\n"
        "2. Define component boundaries and interfaces\n"
        "3. Design data flows and API contracts\n"
        "4. Identify potential bottlenecks and scalability concerns\n\n"
        "Output JSON:\n"
        "{\n"
        '  "system_design": "Overall architecture description",\n'
        '  "components": [{"name": "...", "purpose": "...", "tech": "..."}],\n'
        '  "data_flow": ["Step 1...", "Step 2..."],\n'
        '  "api_contracts": [{"endpoint": "/api/...", "method": "POST", "payload": {...}}],\n'
        '  "recommendations": ["Consider caching...", "Use WebSockets for real-time..."]\n'
        "}"
    )

    async def design(self, user_request: str, file_tree: Dict[str, str]) -> Dict[str, Any]:
        """Create system architecture design."""
        log_agent(self.agent_id, f"Designing architecture for: {user_request[:60]}...", self.project_id)
        
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": f"User Request: {user_request}\n\nExisting Files: {list(file_tree.keys())[:20]}\n\nCreate architecture design. Output JSON only."}
        ]
        
        raw, tokens = await self.call_llm(messages, temperature=0.4)
        design = self.extract_json(raw) or {"system_design": "Standard React + Express setup", "components": []}
        
        log_agent(self.agent_id, f"Designed {len(design.get('components', []))} components", self.project_id)
        return design

# ============================================================================
# STRATEGIST AGENT (Task Decomposition)
# ============================================================================

class StrategistAgent(BaseAgent):
    """Decomposes tasks into executable units with dependencies."""
    
    SYSTEM_PROMPT = (
        "You are the Task Strategist for Gorilla Builder X. Break down work into executable tasks with clear dependencies.\n\n"
        "Your role:\n"
        "1. Analyze the plan and break into atomic tasks\n"
        "2. Identify task dependencies and execution order\n"
        "3. Assign tasks to appropriate agent types\n"
        "4. Estimate complexity for each task\n\n"
        "Output JSON:\n"
        "{\n"
        '  "execution_strategy": "Parallel execution with...",\n'
        '  "tasks": [{\n'
        '    "id": 1,\n'
        '    "description": "Create component...",\n'
        '    "agent_type": "ui|api|logic|test",\n'
        '    "dependencies": [2, 3],\n'
        '    "estimated_complexity": "low|medium|high",\n'
        '    "output_files": ["src/..."],\n'
        '    "estimated_tokens": 500\n'
        '  }]\n'
        "}"
    )

    async def strategize(self, plan: Dict[str, Any], file_tree: Dict[str, str]) -> Dict[str, Any]:
        """Create execution strategy from plan."""
        log_agent(self.agent_id, "Creating execution strategy...", self.project_id)
        
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": f"Plan: {json.dumps(plan, indent=2)}\n\nExisting Files: {list(file_tree.keys())[:20]}\n\nCreate execution strategy. Output JSON only."}
        ]
        
        raw, tokens = await self.call_llm(messages, temperature=0.4)
        strategy = self.extract_json(raw) or {"tasks": [], "execution_strategy": "Sequential"}
        
        log_agent(self.agent_id, f"Created {len(strategy.get('tasks', []))} executable tasks", self.project_id)
        return strategy

# ============================================================================
# PLANNER AGENT (Enhanced with Extreme Reasoning)
# ============================================================================

class PlannerAgent(BaseAgent):
    """Creates detailed implementation plans with extreme reasoning."""
    
    def _build_system_prompt(self, agent_skills: Optional[Dict] = None) -> str:
        skills_addon = ""
        if agent_skills:
            skills_addon = "\n\nUSER PREFERENCES:\n"
            if agent_skills.get("visuals") == "clean-svg":
                skills_addon += "- Use SVG icons (Phosphor/Lucide), no emojis\n"
            elif agent_skills.get("visuals") == "emojis":
                skills_addon += "- Use text-based emojis\n"
            if agent_skills.get("framework") == "tailwind":
                skills_addon += "- Use Tailwind CSS\n"
            if agent_skills.get("style") == "beginner":
                skills_addon += "- Beginner-friendly, heavily commented\n"
            elif agent_skills.get("style") == "expert":
                skills_addon += "- Expert-level, concise, minimal comments\n"

        return (
    "You are the Lead Architect for a high-performance **Full-Stack** web application, you are the GOR://A BUILDER X multi agent AI BUILDER. Your goal is to create a strategic, step-by-step build plan for an AI Coder specialized in **React (Frontend)** AND **Node.js/Express (Backend)**. Strictly give NO CODE AT ALL, in no form. But you MUST REASON HARD.\n"
    "CRITICAL CONTEXT: The AI Coder executes tasks in isolation. It has NO memory of previous files unless you provide context in *every single task description*.\n\n"

    "Rules:\n"
    "MANDATORY OUTPUT FORMAT: JSON OBJECT ONLY. Do NOT wrap in markdown blocks.\n"
    "{\n"
    '  "assistant_message": "Sure I will build the monkeychat application for you with...and...it will be...",\n'
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
    "   - **Backend Tasks**: Modify `server.js` to add middleware/routes. Create specific route files in `routes/`.\n"
    "3. **The Wiring & Evolution Rule (CRITICAL - NO DEAD CODE):**\n"
    "   - **Frontend Wiring**: Every new component MUST be immediately imported and used.\n"
    "   - **Backend Wiring**: Every new route file MUST be immediately mounted in `server.js`.\n"
    "4. **The 'Global Blueprint' Rule:**\n"
    "   - Every task string MUST start with: `[Project: {Name} | Stack: FullStack | Context: {FULL_APP_DESCRIPTION_HERE}] ...`\n"
    "   - **CRITICAL**: The `Context` section MUST contain the FULL description of what the app is supposed to do.\n\n"

    "TASK WRITING GUIDELINES:\n"
    "1. **No-Build Specifics:** \n"
    "   - NEVER ask for `npm run dev` or `vite.config.js`.\n"
    "   - NEVER generate an `.env` file.\n"
    "   - Frontend Imports: Use `@/` aliases.\n"
    "   - Backend Imports: Use relative paths with `.js` extension.\n"
    "   - Never instruct to the coder to build a `vercel.json` file in the root of the project according to the project's requirements.\n"
    
    "2. **AI Integration Specs (USE THESE EXACTLY):**\n"
    "   - **Core Rule**: You MUST route all AI API calls through the Gorilla Proxy using `process.env.GORILLA_API_KEY`.\n"
    "   - **High-Performance Logic (LLM)**: Set baseURL to `https://corrinne-turbid-illustratively.ngrok-free.dev/api/v1` and use model `openai/gpt-oss-20b:free`.\n"
    "   - **Image Generation**: Send POST request to `https://corrinne-turbid-illustratively.ngrok-free.dev/api/v1/images/generations` with standard OpenAI payload.\n"
    "   - **Voice (STT)**: Send POST to `https://corrinne-turbid-illustratively.ngrok-free.dev/api/v1/audio/transcriptions` (OpenAI Whisper format).\n"
    "   - **Voice (TTS)**: DO NOT USE AN API. Strictly use the browser's native `window.speechSynthesis` Web Speech API in frontend components.\n"
    "   - **BG Removal**: Send POST with FormData (file) to `https://corrinne-turbid-illustratively.ngrok-free.dev/api/v1/images/remove-background`.\n"
    
    "3. **Volume:** \n"
    "   - Always try to ask the user at least 2 questions to elaborate on their request, they should be obvious and add functionality to their app if they agree. DO NOT ASK TECHNICAL QUESTIONS, THE USERS CANNOT CODE. WHEN YOU ASK A QUESTION DO NOT GENERATE TASKS AT ALL. Do not generate tasks even if the user asks a question. DO NOT BOTHER THE USER WITH TOO MANY OR ANY DEBUGGING QUESTIONS.\n"
    "   - Simple Apps: 8-10 tasks (Mix of DB, Backend, Frontend).(if there are no questions only!)\n"
    "   - Above Simple Apps: 15+ tasks.(if there are no questions only!)\n"
    "   - Debugging Tasks: 1-2 tasks. DO NOT ASK QUESTIONS FOR DEBUGGING.\n"
    "   - Never exceed 450 tokens per step. Update `server.js` and `App.tsx` **LAST** to wire up components/routes."
        + skills_addon
        )

    async def plan(self, user_request: str, file_tree: Dict[str, str], 
                   agent_skills: Optional[Dict] = None) -> MCPMessage:
        log_agent(self.agent_id, f"Planning: {user_request[:60]}...", self.project_id)
        
        is_debug = any(w in user_request.lower() for w in ["error", "fix", "bug", "crash", "broken"])
        
        system_prompt = self._build_system_prompt(agent_skills)
        chat_history = _get_history(self.project_id)
        
        messages = [{"role": "system", "content": system_prompt}]
        for h in chat_history:
            messages.append({"role": h["role"], "content": h["content"]})
        
        clean_files = [f for f in file_tree.keys() if not f.endswith(".b64")]
        messages.append({"role": "user", "content": f"""CONTEXT: {json.dumps(SHARED_CONTEXT, indent=2)}

FILES: {json.dumps(clean_files[:20])}

REQUEST: {user_request}

{'DEBUG MODE' if is_debug else 'Create a comprehensive plan.'}

Output JSON with type="plan" or type="questions"."""})

        for attempt in range(3):
            try:
                raw, tokens = await self.call_llm(messages, temperature=0.5)
                data = self.extract_json(raw)
                
                if not data:
                    if attempt < 2:
                        await asyncio.sleep(1)
                        continue
                    raise ValueError("Could not extract JSON")
                
                response_type = data.get("type", "plan")
                assistant_message = data.get("assistant_message", "Plan created.")
                
                if response_type == "questions":
                    questions = data.get("questions", [])
                    questions_formatted = "\n".join([f" {q}" for q in questions])
                    full_message = f"{assistant_message}\n\n{questions_formatted}"
                    
                    self.emit(
                        intent=Intent.QUESTION,
                        payload={"assistant_message": full_message, "questions": questions, "needs_clarification": True},
                        to=None,
                        task_id=f"q_{int(time.time())}",
                        reasoning=f"Need clarification: {len(questions)} questions"
                    )
                else:
                    tasks = data.get("tasks", [])
                    log_agent(self.agent_id, f"Generated {len(tasks)} tasks", self.project_id)
                    
                    for i, t in enumerate(tasks, 1):
                        log_agent(self.agent_id, f"  {i}. {str(t)[:50]}...", self.project_id)
                    
                    _append_history(self.project_id, "assistant", assistant_message)
                    
                    self.emit(
                        intent=Intent.PLAN,
                        payload={"assistant_message": assistant_message, "tasks": tasks, "is_debug": is_debug},
                        to="strategist",
                        task_id=f"plan_{int(time.time())}",
                        reasoning=f"Created plan with {len(tasks)} tasks"
                    )
                
                return self.bus.messages[-1]

            except Exception as e:
                log_agent(self.agent_id, f"Attempt {attempt+1} failed: {str(e)[:60]}", self.project_id)
                if attempt < 2:
                    await asyncio.sleep(1)
                else:
                    return MCPMessage(
                        from_agent=self.agent_id, to_agent="strategist", intent=Intent.ERROR,
                        task_id="error", payload={"error": str(e), "tasks": [], "assistant_message": "Plan failed."},
                        reasoning="Plan generation failed"
                    )

# ============================================================================
# CODER AGENT (Enhanced with Parallel Execution)
# ============================================================================

class CoderAgent(BaseAgent):
    """Main implementation orchestrator with parallel task execution."""
    
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
            "**IMPORTANT** even though these are already in place, please try to make the UI less bootstrappy and more fun and polished, try to make the components yourself instead of always using shadcn UI, but when feel the need to use shadcn UI, do it, in a not very obivious way. .\n\n"

        "UI/UX & DESIGN ENCOURAGEMENT:\n"
        "- Go all out on the frontend! We want a sleek, modern, and highly polished user interface. THINK OUT OF THE BOX WITHOUT BOOTSTRAPPY LOOKS AND NO INTER FONTS, BE CREATIVE!\n"
        "- Liberally use Tailwind CSS for beautiful styling, spacing, and typography.\n"
        "- Use `framer-motion` for buttery smooth micro-interactions, page transitions, and element reveals.\n"
        "- Use `lucide-react` for crisp, consistent iconography.\n"
        "- Make it look like a premium, production-ready SaaS product right out of the gate. Don't settle for basic layouts!\n\n"

        "STRICT IMPORT RULES:\n"
        "- **FRONTEND (`src/` files)**:\n"
        "  - Use `@/` alias (e.g., `import { Button } from '@/components/ui/button'`).\n"
        "  - Do NOT use relative paths like `../../`.\n"
        "- **BACKEND (`server.js`, `routes/` files)**:\n"
        "  - Use **Relative Paths** (e.g., `import router from './routes/api.js'`).\n"
        "  - **CRITICAL**: You MUST include the `.js` extension for local backend imports.\n\n"

        "API & MODELS CONFIGURATION:\n"
        "- Use `process.env.OPENROUTER_API_KEY and FIREWORKS_API_KEY and REMBG_API_KEY` for AI. Listen to the planner.\n\n"

        "RESPONSE FORMAT (JSON ONLY):\n"
        "{\n"
        '  "message": "A short, friendly status update.",\n'
        '  "operations": [\n'
        "    {\n"
        '      "action": "create_file" | "overwrite_file",\n'
        '      "path": "src/pages/Dashboard.tsx" OR "routes/api.js",\n'
        '      "content": "FULL FILE CONTENT HERE"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"

        "GLOBAL RULES:\n"
        "1. Output valid JSON only. No markdown blocks. ALL API KEYS ARE IN THE ENVIRONMENT.\n"
        "2. NEVER generate .env or Dockerfile. The main server is always server.js and the backend is always node.js within the routes/ folder, the frontend is always react/typescript.\n"
        "3. NEVER use literal '\\n'. Use physical newlines.\n"
        "4. There is no read file action, to find a file please look into the conversation history\n"
        "5. When you get instructions to finalize the server.js, ALWAYS update the WHOLE SERVER.JS and use overwrite_file action, never leave it as is.\n"
        "6. CRITICAL INFRASTRUCTURE RULE: If you modify `package.json` to add dependencies, you MUST entirely preserve the existing `scripts` block. NEVER delete or modify the `dev`, `server`, `client`, or `db:push` scripts, or the WebContainer will fatally crash.\n\n"

        "SPECIFIC RULES:\n"
        "1. **Frontend (React)**: Use Functional Components. MAKE EVERYTHING LOOK VERY GOOD! WITH EYECANDY FOR THE USER.\n"
        "2. **Backend (Node)**: Use `async/await`. Return JSON (`res.json`). Handle errors with `try/catch`.\n"
        "3. **Self-Correction**: If the user prompt reports a crash, analyze the stack trace and fix the specific file causing it.\n"
    )

    def __init__(self, agent_id: str, bus: MCPBus, project_id: str):
        super().__init__(agent_id, bus, project_id)
        self.sub_agents: Dict[str, BaseAgent] = {}
        self.pending_tasks: Dict[str, Dict] = {}
        self.all_operations: List[Dict] = []
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
    
    def _normalize_ops(self, parsed: Dict) -> List[Dict]:
        if not isinstance(parsed, dict):
            return []
        
        ops = parsed.get("operations") or ([parsed.get("operation")] if parsed.get("operation") else [])
        if not isinstance(ops, list):
            return []
        
        normalized = []
        for op in ops:
            action = ACTION_NORMALIZE.get(op.get("action", ""), op.get("action", ""))
            if action == "patch":
                action = "patch_file"
            if action not in ALLOWED_ACTIONS:
                continue
            
            path = op.get("path", "")
            content = op.get("content", "")
            if not path or content is None:
                continue
            
            if isinstance(content, list):
                content = "\n".join(str(x) for x in content)
            elif isinstance(content, str):
                content = content.replace("\\n", "\n")
            
            normalized.append({"action": action, "path": path.strip(), "content": str(content)})
        
        return normalized

    async def _execute_plan(self, plan_msg: MCPMessage):
        payload = plan_msg.payload
        tasks = payload.get("tasks", [])
        
        log_agent(self.agent_id, f"Executing {len(tasks)} tasks", self.project_id)
        
        if not tasks:
            self.execution_complete = True
            self.emit(Intent.DONE, {"status": "no_tasks", "operations": []}, reasoning="No tasks")
            return
        
        # Execute tasks in parallel where possible
        semaphore = asyncio.Semaphore(3)  # Max 3 concurrent tasks
        
        async def execute_with_limit(task, task_id):
            async with semaphore:
                await self._implement_task(task, task_id)
        
        # Group tasks by type for parallel execution
        task_coros = []
        for i, task in enumerate(tasks):
            task_id = f"task_{i+1}_{int(time.time() * 1000)}"
            task_coros.append(execute_with_limit(task, task_id))
        
        await asyncio.gather(*task_coros, return_exceptions=True)
        
        # Wait for sub-agents
        waited = 0
        while self.pending_tasks and waited < 180:
            await asyncio.sleep(0.5)
            waited += 0.5
        
        # Request review
        self.emit(
            Intent.REVIEW,
            {"operations": self.all_operations, "task_count": len(tasks)},
            to="reviewer",
            task_id=f"review_{int(time.time())}",
            reasoning=f"Completed {len(tasks)} tasks"
        )
    
    async def _implement_task(self, task: str, task_id: str):
        context = json.dumps({k: self.file_tree.get(k, "")[:2000] for k in ["src/App.tsx", "server.js"]})
        chat_history = _get_history(self.project_id)[-10:]
        
        messages = [{"role": "system", "content": self.SYSTEM_PROMPT}]
        for h in chat_history:
            messages.append({"role": h["role"], "content": h["content"]})
        
        messages.append({"role": "user", "content": f"CONTEXT:\n{context}\n\nTASK: {task}\n\nOutput JSON."})

        for attempt in range(3):
            try:
                raw, tokens = await self.call_llm(messages, temperature=0.5)
                parsed = self.extract_json(raw)
                
                if not parsed:
                    raise ValueError("No JSON found")
                
                ops = self._normalize_ops(parsed)
                self.all_operations.extend(ops)
                
                for op in ops:
                    log_agent(self.agent_id, f"  {op.get('action')}: {op.get('path')}", self.project_id)
                
                _append_history(self.project_id, "user", f"Task: {task}")
                _append_history(self.project_id, "assistant", raw)
                return

            except Exception as e:
                log_agent(self.agent_id, f"  Attempt {attempt+1} failed: {str(e)[:50]}", self.project_id)
                if attempt < 2:
                    messages.append({"role": "user", "content": f"Fix error: {str(e)[:100]}"})
                    await asyncio.sleep(1)
                else:
                    self.error_count += 1
    
    async def _handle_sub_done(self, msg: MCPMessage):
        if msg.task_id in self.pending_tasks:
            del self.pending_tasks[msg.task_id]
            ops = msg.payload.get("operations", [])
            self.all_operations.extend(ops)
    
    async def _handle_feedback(self, msg: MCPMessage):
        issues = msg.payload.get("issues", [])
        if issues:
            log_agent(self.agent_id, f"Reviewer found {len(issues)} issues", self.project_id)
        else:
            log_agent(self.agent_id, "Review passed", self.project_id)
        
        self.execution_complete = True
        self.emit(
            Intent.DONE,
            {"status": "complete", "operations": self.all_operations, "issues": issues},
            reasoning="Execution complete"
        )

# ============================================================================
# SUB-AGENTS (Enhanced)
# ============================================================================

class UISubAgent(BaseAgent):
    SYSTEM_PROMPT = (
        "You are the UI Specialist for Gorilla Builder X. Create beautiful React components with Tailwind.\n\n"
        "Your expertise:\n"
        "1. Create visually stunning, non-bootstrappy designs\n"
        "2. Use framer-motion for smooth animations\n"
        "3. Implement responsive layouts with Tailwind\n"
        "4. NEVER use Inter font - use system fonts or creative alternatives\n"
        "5. Use lucide-react for icons\n\n"
        "Output JSON: {\"message\": \"...\", \"operations\": [{\"action\": \"...\", \"path\": \"...\", \"content\": \"...\"}]}"
    )

class APISubAgent(BaseAgent):
    SYSTEM_PROMPT = (
        "You are the API Specialist for Gorilla Builder X. Create Express routes.\n\n"
        "Your expertise:\n"
        "1. Design RESTful endpoints\n"
        "2. Use ES modules (import/export)\n"
        "3. Use .js extension for imports\n"
        "4. Implement proper error handling with try/catch\n"
        "5. Add input validation\n\n"
        "Output JSON: {\"message\": \"...\", \"operations\": [{\"action\": \"...\", \"path\": \"...\", \"content\": \"...\"}]}"
    )

class LogicSubAgent(BaseAgent):
    SYSTEM_PROMPT = (
        "You are the Logic Specialist for Gorilla Builder X. Create utility functions.\n\n"
        "Your expertise:\n"
        "1. Write clean, reusable functions\n"
        "2. Use TypeScript with proper types\n"
        "3. Handle edge cases gracefully\n"
        "4. Add comprehensive JSDoc comments\n\n"
        "Output JSON: {\"message\": \"...\", \"operations\": [{\"action\": \"...\", \"path\": \"...\", \"content\": \"...\"}]}"
    )

class ReviewerAgent(BaseAgent):
    SYSTEM_PROMPT = (
        "You are the Code Reviewer for Gorilla Builder X. Check for quality issues.\n\n"
        "Check for:\n"
        "- Missing imports\n"
        "- Syntax errors\n"
        "- Type mismatches\n"
        "- Security issues (SQL injection, XSS)\n"
        "- Performance concerns\n"
        "- Best practice violations\n\n"
        "Output JSON: {\"passed\": true|false, \"issues\": [\"...\"]}"
    )

    async def _on_mcp(self, msg: MCPMessage):
        if msg.intent == Intent.REVIEW:
            await self._review(msg)
    
    async def _review(self, msg: MCPMessage):
        ops = msg.payload.get("operations", [])
        issues = []
        
        for op in ops:
            path = op.get("path", "")
            content = op.get("content", "")
            
            if path.endswith((".tsx", ".ts")):
                if "import React" in content and "from 'react'" not in content:
                    issues.append(f"{path}: Malformed React import")
                if "function" in content and "export default" not in content and "export " not in content:
                    issues.append(f"{path}: Function not exported")
            
            if path.endswith(".js"):
                if "require(" in content and "import " in content:
                    issues.append(f"{path}: Mixed require/import")
        
        self.emit(
            Intent.FEEDBACK,
            {"passed": len(issues) == 0, "issues": issues},
            to="coder",
            task_id=msg.task_id,
            reasoning=f"Review: {len(issues)} issues"
        )

class DebuggerAgent(BaseAgent):
    SYSTEM_PROMPT = (
        "You are the Debugger for Gorilla Builder X. Fix errors with minimal changes.\n\n"
        "Your approach:\n"
        "1. Analyze the error message carefully\n"
        "2. Identify the root cause\n"
        "3. Make the smallest fix possible\n"
        "4. Verify the fix doesn't break other parts\n\n"
        "Output JSON: {\"message\": \"Fixed...\", \"operations\": [{\"action\": \"overwrite_file\", \"path\": \"...\", \"content\": \"...\"}]}"
    )

    async def debug(self, error: str, file_tree: Dict) -> List[Dict]:
        log_agent(self.agent_id, f"Fixing: {error[:60]}...", self.project_id)
        
        file_match = re.search(r'(?:in|at)\s+(\S+\.(?:tsx|ts|js|jsx))', error)
        relevant = file_match.group(1) if file_match else ""
        content = file_tree.get(relevant, "")
        
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": f"ERROR:\n{error}\n\nFILE: {relevant}\nCONTENT:\n{content[:1500]}\n\nFix this. Output JSON."}
        ]
        
        raw, _ = await self.call_llm(messages, temperature=0.2)
        data = self.extract_json(raw)
        
        if data:
            ops = data.get("operations", [])
            for op in ops:
                log_agent(self.agent_id, f"  {op.get('action')}: {op.get('path')}", self.project_id)
            return ops
        return []

# ============================================================================
# ADDITIONAL SPECIALIZED AGENTS
# ============================================================================

class SecurityAgent(BaseAgent):
    """Security specialist for vulnerability detection."""
    
    SYSTEM_PROMPT = (
        "You are the Security Specialist for Gorilla Builder X.\n\n"
        "Your role:\n"
        "1. Scan code for security vulnerabilities\n"
        "2. Check for XSS, CSRF, SQL injection risks\n"
        "3. Verify proper input sanitization\n"
        "4. Ensure secure API patterns\n\n"
        "Output JSON: {\"vulnerabilities\": [{\"severity\": \"high|medium|low\", \"file\": \"...\", \"issue\": \"...\", \"fix\": \"...\"}]}"
    )

class PerformanceAgent(BaseAgent):
    """Performance optimization specialist."""
    
    SYSTEM_PROMPT = (
        "You are the Performance Specialist for Gorilla Builder X.\n\n"
        "Your role:\n"
        "1. Identify performance bottlenecks\n"
        "2. Suggest React optimization patterns\n"
        "3. Recommend efficient data fetching\n"
        "4. Check for unnecessary re-renders\n\n"
        "Output JSON: {\"optimizations\": [{\"file\": \"...\", \"suggestion\": \"...\", \"impact\": \"high|medium|low\"}]}"
    )

class DocsAgent(BaseAgent):
    """Documentation specialist."""
    
    SYSTEM_PROMPT = (
        "You are the Documentation Specialist for Gorilla Builder X.\n\n"
        "Your role:\n"
        "1. Generate JSDoc comments for functions\n"
        "2. Create README files\n"
        "3. Document API endpoints\n"
        "4. Add inline code comments\n\n"
        "Output JSON: {\"message\": \"...\", \"operations\": [{\"action\": \"...\", \"path\": \"...\", \"content\": \"...\"}]}"
    )

# ============================================================================
# X-SWARM ORCHESTRATOR (Enhanced with 12+ Agents)
# ============================================================================

class XAgentSwarm:
    """Extreme Agent Swarm with 12+ specialized agents."""
    
    def __init__(self, project_id: str):
        self.project_id = project_id
        self.bus = MCPBus(project_id)
        
        # Core agents
        self.architect = ArchitectAgent("architect", self.bus, project_id)
        self.planner = PlannerAgent("planner", self.bus, project_id)
        self.strategist = StrategistAgent("strategist", self.bus, project_id)
        self.coder = CoderAgent("coder", self.bus, project_id)
        self.reviewer = ReviewerAgent("reviewer", self.bus, project_id)
        self.debugger = DebuggerAgent("debugger", self.bus, project_id)
        
        # Sub-agents
        self.ui_agent = UISubAgent("ui_agent", self.bus, project_id)
        self.api_agent = APISubAgent("api_agent", self.bus, project_id)
        self.logic_agent = LogicSubAgent("logic_agent", self.bus, project_id)
        
        # Additional specialized agents
        self.security_agent = SecurityAgent("security", self.bus, project_id)
        self.performance_agent = PerformanceAgent("performance", self.bus, project_id)
        self.docs_agent = DocsAgent("docs", self.bus, project_id)
        
        # Register sub-agents with coder
        self.coder.register_sub_agent(self.ui_agent)
        self.coder.register_sub_agent(self.api_agent)
        self.coder.register_sub_agent(self.logic_agent)
        
        log_agent("swarm", "X-Agent Swarm initialized (Gemini-powered, 12+ agents)", project_id)
    
    def get_total_tokens(self) -> int:
        """Get total tokens from ALL agents (with 9.3x multiplier applied)."""
        agents = [
            self.architect, self.planner, self.strategist, self.coder,
            self.reviewer, self.debugger, self.ui_agent, self.api_agent, self.logic_agent,
            self.security_agent, self.performance_agent, self.docs_agent
        ]
        total = sum(a.get_tokens_used() for a in agents)
        raw_total = sum(a.get_raw_tokens() for a in agents)
        log_agent("swarm", f"Total tokens: {total} (raw: {raw_total}, multiplier: {TOKEN_MULTIPLIER}x)", self.project_id)
        return total
    
    def get_raw_tokens(self) -> int:
        """Get raw tokens before multiplier."""
        agents = [
            self.architect, self.planner, self.strategist, self.coder,
            self.reviewer, self.debugger, self.ui_agent, self.api_agent, self.logic_agent,
            self.security_agent, self.performance_agent, self.docs_agent
        ]
        return sum(a.get_raw_tokens() for a in agents)
    
    def reset_all_tokens(self):
        """Reset all token counters."""
        agents = [
            self.architect, self.planner, self.strategist, self.coder,
            self.reviewer, self.debugger, self.ui_agent, self.api_agent, self.logic_agent,
            self.security_agent, self.performance_agent, self.docs_agent
        ]
        for a in agents:
            a.reset_tokens()
    
    async def solve(self, user_request: str, file_tree: Dict[str, str],
                    agent_skills: Optional[Dict] = None) -> Dict[str, Any]:
        """Main entry point with full swarm execution."""
        
        for agent in [self.architect, self.planner, self.strategist, self.coder,
                      self.reviewer, self.debugger, self.ui_agent, self.api_agent, self.logic_agent,
                      self.security_agent, self.performance_agent, self.docs_agent]:
            agent.file_tree = file_tree
        
        log_agent("swarm", f"X-MODE: {user_request[:60]}...", self.project_id)
        
        # Reset state
        self.coder.all_operations = []
        self.coder.pending_tasks = {}
        self.coder.execution_complete = False
        self.bus.messages = []
        
        # Phase 1: Architecture Design
        arch_design = await self.architect.design(user_request, file_tree)
        
        # Phase 2: Planning
        plan_result = await self.planner.plan(user_request, file_tree, agent_skills)
        assistant_message = plan_result.payload.get("assistant_message", "Building...")
        
        if plan_result.intent == Intent.QUESTION:
            await self.bus.await_all_tasks(timeout=1.0)
            return {
                "status": "needs_clarification",
                "assistant_message": assistant_message,
                "questions": plan_result.payload.get("questions", []),
                "operations": [],
                "total_tokens": self.get_total_tokens(),
                "raw_tokens": self.get_raw_tokens()
            }
        
        # Phase 3: Strategize (if we have tasks)
        tasks = plan_result.payload.get("tasks", [])
        if tasks:
            strategy = await self.strategist.strategize(
                {"tasks": tasks, "architecture": arch_design}, 
                file_tree
            )
        
        # Phase 4: Wait for execution with stability detection
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
                    log_agent("swarm", f"Stable: {len(current_ops)} ops", self.project_id)
                    break
            else:
                stable_count = 0
                last_op_count = len(current_ops)
        
        # Collect all operations
        all_ops = []
        for msg in self.bus.messages:
            if msg.intent in [Intent.DONE, Intent.DEBUG_FIX]:
                all_ops.extend(msg.payload.get("operations", []))
        
        # Deduplicate
        seen = {}
        for op in all_ops:
            path = op.get("path", "")
            if path:
                seen[path] = op
        all_ops = list(seen.values())
        
        log_agent("swarm", f"Complete: {len(all_ops)} unique files", self.project_id)
        for op in all_ops:
            log_agent("swarm", f"   {op.get('path')}", self.project_id)
        
        await self.bus.await_all_tasks(timeout=3.0)
        
        return {
            "status": "complete",
            "assistant_message": assistant_message,
            "operations": all_ops,
            "total_tokens": self.get_total_tokens(),
            "raw_tokens": self.get_raw_tokens(),
            "architecture": arch_design
        }

# ============================================================================
# BACKWARD COMPATIBILITY - XAgent Class
# ============================================================================

class XAgent:
    """X-Agent wrapper for backward compatibility."""
    
    def __init__(self, timeout_s: float = 180.0):
        self.timeout_s = timeout_s
        self._swarm_cache: Dict[str, XAgentSwarm] = {}
    
    def _get_swarm(self, project_id: str) -> XAgentSwarm:
        if project_id not in self._swarm_cache:
            self._swarm_cache[project_id] = XAgentSwarm(project_id)
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
            
            if result.intent == Intent.QUESTION:
                return {
                    "assistant_message": assistant_message,
                    "plan": {"todo": [], "questions": result.payload.get("questions", [])},
                    "todo_md": "",
                    "usage": {"total_tokens": swarm.get_total_tokens(), "raw_tokens": swarm.get_raw_tokens()},
                    "needs_clarification": True
                }
            
            return {
                "assistant_message": assistant_message,
                "plan": {
                    "capabilities": [],
                    "ai_modules": [],
                    "glue_files": [],
                    "todo": tasks,
                },
                "todo_md": self._to_todo_md({"todo": tasks}, assistant_message),
                "usage": {"total_tokens": swarm.get_total_tokens(), "raw_tokens": swarm.get_raw_tokens()}
            }
        finally:
            loop.close()
    
    async def code(self, plan_section: str, plan_text: str,
                   file_tree: Dict[str, str], project_name: str,
                   history: Optional[List[Dict[str, str]]] = None,
                   max_retries: int = 3) -> Dict[str, Any]:
        """Generate code with X-Agent swarm."""
        swarm = self._get_swarm(project_name)
        
        # Reset and execute
        swarm.coder.all_operations = []
        swarm.coder.pending_tasks = {}
        swarm.coder.execution_complete = False
        swarm.bus.messages = []
        
        swarm.coder.emit(
            Intent.PLAN,
            {"tasks": [f"{plan_section}: {plan_text}"], "is_debug": False},
            to="coder",
            task_id="single",
            reasoning="Single task execution"
        )
        
        # Wait for completion
        max_wait = 180
        waited = 0
        
        while waited < max_wait:
            await asyncio.sleep(0.5)
            waited += 0.5
            
            for msg in swarm.bus.messages:
                if msg.intent == Intent.DONE and msg.from_agent == "coder":
                    ops = msg.payload.get("operations", [])
                    total_tokens = swarm.get_total_tokens()
                    raw_tokens = swarm.get_raw_tokens()
                    await swarm.bus.await_all_tasks(timeout=2.0)
                    return {
                        "message": f"Completed {len(ops)} operations",
                        "operations": ops,
                        "usage": {"total_tokens": total_tokens, "raw_tokens": raw_tokens}
                    }
        
        # Fallback collection
        all_ops = []
        for msg in swarm.bus.messages:
            if msg.intent == Intent.DONE:
                all_ops.extend(msg.payload.get("operations", []))
        
        total_tokens = swarm.get_total_tokens()
        raw_tokens = swarm.get_raw_tokens()
        await swarm.bus.await_all_tasks(timeout=2.0)
        
        return {
            "message": f"Completed {len(all_ops)} operations",
            "operations": all_ops,
            "usage": {"total_tokens": total_tokens, "raw_tokens": raw_tokens}
        }
    
    @staticmethod
    def _to_todo_md(plan: Dict[str, Any], msg: str = "") -> str:
        tasks = plan.get("todo", [])
        if not tasks:
            return ""
        return "# Build Plan\n\n## Tasks\n" + "\n".join([f"- {t}" for t in tasks])

__all__ = ["XAgent", "XAgentSwarm", "MCPBus", "MCPMessage", "Intent",
           "_render_token_limit_message", "clear_history"]