"""
Agent Swarm with Internal MCP Protocol
======================================

Multi-agent architecture where:
- Planner creates strategic plans
- Coder orchestrates implementation
- Sub-agents (UI/API/Logic) handle specialized tasks
- All communication via compressed MCP messages
- Terminal logging for development visibility
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
MODEL = os.getenv("MODEL", "inception/mercury-2")
OPENROUTER_URL = os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
SITE_URL = os.getenv("SITE_URL", "https://gorillabuilder.dev")
SITE_NAME = os.getenv("SITE_NAME", "Gorilla Builder")

if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY must be configured in the environment")

ALLOWED_ACTIONS = {"create_file", "overwrite_file"}
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
}

# --- Terminal Logging ---
def log_agent(role: str, message: str, project_id: str = ""):
    """Print agent activity to terminal for debugging."""
    prefix = f"[{project_id[:8]}]" if project_id else "[AGENT]"
    timestamp = time.strftime("%H:%M:%S")
    colors = {
        "planner": "\033[95m",
        "coder": "\033[94m",
        "ui_agent": "\033[96m",
        "api_agent": "\033[92m",
        "logic_agent": "\033[93m",
        "debugger": "\033[91m",
        "swarm": "\033[97m",
        "llm": "\033[90m",
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
    if r in ("assistant", "planner", "system", "coder", "agent"): 
        return "assistant"
    return "user"

def _append_history(project_id: str, role: str, content: str, max_items: int = 16) -> None:
    if not project_id: 
        return
    msg = {"role": _norm_role(role), "content": (content or "").strip()}
    if not msg["content"]: 
        return
    _HISTORY.setdefault(project_id, []).append(msg)
    if len(_HISTORY[project_id]) > max_items:
        _HISTORY[project_id] = _HISTORY[project_id][-max_items:]

def _get_history(project_id: str, max_items: int = 12) -> List[ChatMsg]:
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
# INTERNAL MCP PROTOCOL
# ============================================================================

class Intent(Enum):
    """MCP Intent types - the vocabulary of the swarm."""
    PLAN = "plan"
    IMPLEMENT = "implement"
    DELEGATE_UI = "delegate_ui"
    DELEGATE_API = "delegate_api"
    DELEGATE_LOGIC = "delegate_logic"
    DONE = "done"
    DEBUG_FIX = "debug_fix"

@dataclass
class MCPMessage:
    """Internal Machine Communication Protocol - compressed agent chat."""
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
        
    def subscribe(self, agent_id: str, handler: callable):
        self.subscribers[agent_id] = handler
        
    def emit(self, msg: MCPMessage):
        self.messages.append(msg)
        target = msg.to_agent or "ALL"
        log_agent(msg.from_agent, f"→ {target} | {msg.intent.value}: {msg.reasoning}", self.project_id)
        
        if msg.to_agent and msg.to_agent in self.subscribers:
            asyncio.create_task(self.subscribers[msg.to_agent](msg))
        elif msg.to_agent is None:
            for agent_id, handler in self.subscribers.items():
                if agent_id != msg.from_agent:
                    asyncio.create_task(handler(msg))

# ============================================================================
# SHARED CONTEXT
# ============================================================================

SHARED_CONTEXT = {
    "stack": {
        "frontend": "React 18 + TypeScript + Vite + Tailwind + Shadcn/UI",
        "backend": "Node.js + Express (ES modules)",
        "storage": "JSON-based (data/ folder)",
    },
    "constraints": [
        "WebContainer compatible - NO native C++ modules",
        "NO sqlite3/better-sqlite3 - use @libsql/client",
        "Frontend imports: use @/ alias",
        "Backend imports: use relative paths with .js extension",
        "NEVER modify package.json scripts block",
        "UI should be creative, non-bootstrappy, no Inter font",
    ],
    "structure": {
        "src/App.tsx": "Main app component (exists)",
        "src/main.tsx": "Entry point (exists)",
        "src/components/ui/": "Shadcn components (pre-installed)",
        "src/components/magicui/": "Magic UI components (pre-installed)",
        "routes/": "Express API routes",
        "data/": "JSON storage",
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
        bus.subscribe(agent_id, self._on_mcp)
        
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
    
    async def call_llm(self, messages: List[Dict], temperature: float = 0.6) -> Tuple[str, int]:
        payload = {
            "model": MODEL,
            "messages": messages,
            "temperature": temperature
        }
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": SITE_URL,
            "X-Title": SITE_NAME,
        }
        
        last_msg_preview = messages[-1].get('content', '')[:100] if messages else ""
        log_agent("llm", f"Sending request ({len(messages)} msgs) -> {last_msg_preview}...", self.project_id)
        
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            
        content = data["choices"][0]["message"]["content"]
        tokens = data.get("usage", {}).get("total_tokens", 0)
        
        log_agent("llm", f"Received response ({tokens} tokens) <- {content[:150]}...", self.project_id)
        
        return content, tokens
    
    def extract_json(self, text: str) -> Optional[Dict]:
        return _extract_json(text)

# ============================================================================
# PLANNER AGENT
# ============================================================================

class PlannerAgent(BaseAgent):
    """The Architect - creates plans, emits to Coder."""
    
    def _build_system_prompt(self, agent_skills: Optional[Dict] = None) -> str:
        skills_addon = ""
        if agent_skills and isinstance(agent_skills, dict):
            skills_addon = "\n\nUSER PREFERENCES (AGENT SKILLS - YOU MUST FOLLOW THESE):\n"
            if agent_skills.get("visuals") == "clean-svg":
                skills_addon += "- Visuals: Strictly use clean SVG icons (Phosphor/Lucide). Do NOT use emojis.\n"
            elif agent_skills.get("visuals") == "emojis":
                skills_addon += "- Visuals: Use native text-based emojis instead of SVG icons.\n"
            if agent_skills.get("framework") == "tailwind":
                skills_addon += "- Styling: Strictly use Tailwind CSS utility classes.\n"
            elif agent_skills.get("framework") == "vanilla-css":
                skills_addon += "- Styling: Use clean, standard Vanilla CSS.\n"
            if agent_skills.get("style") == "beginner":
                skills_addon += "- Code Style: Highly beginner-friendly, heavily commented, descriptive variable names.\n"
            elif agent_skills.get("style") == "expert":
                skills_addon += "- Code Style: Expert-level, highly concise, minimal comments, strict DRY principles.\n"
            if agent_skills.get("personality") == "professional":
                skills_addon += "- Communication: Professional, direct, formal, and strictly business.\n"
            elif agent_skills.get("personality") == "casual":
                skills_addon += "- Communication: Casual, friendly, conversational, use emojis in chat responses.\n"
            if agent_skills.get("rules"):
                skills_addon += f"- Golden Rules: {agent_skills.get('rules')}\n"

        return (
            "You are the Lead Architect for a high-performance **Full-Stack** web application. Your goal is to create a strategic, step-by-step build plan for an AI Coder specialized in **React (Frontend)** AND **Node.js/Express (Backend)**. Strictly give NO CODE AT ALL, in no form. But you MUST REASON HARD.\n"
            "CRITICAL CONTEXT: The AI Coder executes tasks in isolation. It has NO memory of previous files unless you provide context in *every single task description*.\n\n"

            "Rules:\n"
            "MANDATORY OUTPUT FORMAT: JSON OBJECT ONLY. Do NOT wrap in markdown blocks.\n"
            "{\n"
            '  "assistant_message": "A friendly summary of the architecture...",\n'
            '  "tasks": [\n'
            '    "Step 1: [Project: AppName | Stack: FullStack | Context: (FULL SUMMARY)] Create `db/schema.ts` for database setup...",\n'
            '    "Step 2: [Project: AppName | Stack: FullStack | Context: (FULL SUMMARY)] Modify `server.js` to setup API..."\n'
            "  ]\n"
            "}\n\n"

            "ARCHITECTURAL STANDARDS (MUST FOLLOW):\n"
            "1. **Pre-Existing Infrastructure (DO NOT CREATE THESE):**\n"
            "   - **Root**: `package.json` (React, Vite, Tailwind, Express, Drizzle ORM, SQLite).\n"
            "   - **Frontend**: `src/App.tsx`, `src/main.tsx`, `src/lib/utils.ts`, `vite.config.ts`, `tailwind.config.js`.\n"
            "   - **UI Library**: `src/components/ui/` & `src/components/magicui/`.\n"
            "   - **Backend**: `server.js` is the entry point. `routes/` folder for API logic.\n"
            "   - **Database**: Drizzle ORM with `better-sqlite3`. The DB will be a local file (`sqlite.db`).\n"
            "2. **Task Strategy:**\n"
            "   - **NEVER** assign a task to create `package.json` or `index.html`. They exist.\n"
            "   - **Database Tasks**: Instruct the coder to create `db/schema.ts` (for tables), `db/index.ts` (to export the db connection), and `drizzle.config.ts` at the root. ALL OF THESE FILES MUST BE CREATED.\n"
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
            
            "2. **AI Integration Specs (USE THESE EXACTLY):**\n"
            "   - **Core Rule**: You MUST route all AI API calls through the Gorilla Proxy using `process.env.GORILLA_API_KEY`.\n"
            "   - **High-Performance Logic (LLM)**: Set baseURL to `https://corrinne-turbid-illustratively.ngrok-free.dev/api/v1` and use model `openai/gpt-oss-20b:free`.\n"
            "   - **Image Generation**: Send POST request to `https://corrinne-turbid-illustratively.ngrok-free.dev/api/v1/images/generations` with standard OpenAI payload.\n"
            "   - **Voice (STT)**: Send POST to `https://corrinne-turbid-illustratively.ngrok-free.dev/api/v1/audio/transcriptions` (OpenAI format).\n"
            "   - **Voice (TTS)**: DO NOT USE AN API. Strictly use the browser's native `window.speechSynthesis` Web Speech API in frontend components.\n"
            "   - **BG Removal**: Send POST with FormData (file) to `https://corrinne-turbid-illustratively.ngrok-free.dev/api/v1/images/remove-background`.\n"
            
            "3. **Volume:** \n"
            "   - Always try to ask the user at least 2 questions to elaborate on their request, they should be obvious and add functionality to their app if they agree. DO NOT ASK TECHNICAL QUESTIONS, THE USERS CANNOT CODE. WHEN YOU ASK A QUESTION DO NOT GENERATE TASKS AT ALL. Do not generate tasks even if the user asks a question.\n"
            "   - Simple Apps: 8-10 tasks (Mix of DB, Backend, Frontend).(if there are no questions only!)\n"
            "   - Above Simple Apps: 15+ tasks.(if there are no questions only!)\n"
            "   - Debugging Tasks: 1-2 tasks.(if there are no questions only!)\n"
            "   - Never exceed 450 tokens per step. Update `server.js` and `App.tsx` **LAST** to wire up components/routes."
            + skills_addon
        )

    def _infer_capabilities(self, user_request: str) -> List[str]:
        """Heuristic-based capability detection for metadata."""
        text = (user_request or "").lower()
        caps = set()
        if "chat" in text: 
            caps.add("embeddings")
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
        caps.update(["progress_updates", "async_queue", "cost_tracking", "asset_storage"])
        return sorted(caps)

    async def plan(self, user_request: str, file_tree: Dict[str, str], 
                   agent_skills: Optional[Dict] = None) -> MCPMessage:
        log_agent("planner", f"Planning: {user_request[:60]}...", self.project_id)
        
        is_debug = any(word in user_request.lower() 
                      for word in ["error", "fix", "bug", "crash", "broken", "failed"])
        
        context_str = json.dumps(SHARED_CONTEXT, indent=2)
        clean_files = [f for f in file_tree.keys() if not f.endswith(".b64")]
        files_str = json.dumps(clean_files[:20])
        
        system_prompt = self._build_system_prompt(agent_skills)
        chat_history = _get_history(self.project_id)
        
        text_payload = json.dumps({
            "request": user_request,
            "current_files": clean_files
        })

        messages = [{"role": "system", "content": system_prompt}]
        for h in chat_history:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": text_payload})

        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                raw, tokens = await self.call_llm(messages, temperature=0.6)
                data = self.extract_json(raw)
                
                if not data:
                    if attempt < max_retries:
                        time.sleep(1)
                        continue
                    raise ValueError("Could not extract JSON from response")
                
                tasks = data.get("tasks", [])
                assistant_message = data.get("assistant_message", "Plan created.")
                
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
                    to="coder",
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
            to_agent="coder",
            intent=Intent.PLAN,
            task_id="error",
            payload={"error": error, "tasks": [], "assistant_message": "Plan generation failed."},
            reasoning="Plan generation failed"
        )

# ============================================================================
# CODER AGENT (Orchestrator)
# ============================================================================

class CoderAgent(BaseAgent):
    """Implementation Orchestrator - delegates or implements."""
    
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

        "CRITICAL ENVIRONMENT CONSTRAINTS (WebContainers):\n"
        "- You are writing code that will execute inside a browser-based WebContainer.\n"
        "- WebContainers DO NOT support native C++ Node modules.\n"
        "- NEVER use `better-sqlite3` or `sqlite3`. It will fatally crash the container.\n"
        "- ALWAYS use `@libsql/client` and `drizzle-orm/libsql` for local databases.\n"
        "- There is no external database server. You MUST configure both `drizzle.config.ts` and your DB connection instance to use a local file-based database with the exact connection URL: `file:local.db`.\n\n"

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
        
    def register_sub_agent(self, agent: BaseAgent):
        self.sub_agents[agent.agent_id] = agent
        
    async def _on_mcp(self, msg: MCPMessage):
        if msg.intent == Intent.PLAN:
            await self._execute_plan(msg)
        elif msg.intent == Intent.DONE:
            await self._handle_sub_done(msg)
    
    def _normalize_and_validate_ops(self, parsed: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and normalize coder operations."""
        if not isinstance(parsed, dict):
            raise ValueError("Model output was not a valid JSON object")
        
        ops = parsed.get("operations")
        if ops is None:
            op1 = parsed.get("operation")
            if op1:
                ops = [op1]
        
        if not isinstance(ops, list) or not ops:
            raise ValueError("JSON missing 'operations' list")

        user_msg = parsed.get("message") or ops[0].get("message") or "I am working on the file..."
        
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
            "operations": normalized_ops
        }

    def _build_context_snippets(self) -> str:
        """Build context from priority files."""
        snippets = []
        priority_files = ["src/App.tsx", "src/main.tsx", "src/pages/Index.tsx", "src/index.css", "package.json"]
        
        for p in priority_files:
            if p in self.file_tree:
                c = self.file_tree[p]
                snippets.append(f"--- {p} ---\n{c[:8000]}\n")
        
        return "\n".join(snippets)
    
    async def _execute_plan(self, plan_msg: MCPMessage):
        payload = plan_msg.payload
        tasks = payload.get("tasks", [])
        is_debug = payload.get("is_debug", False)
        
        log_agent("coder", f"Received plan: {len(tasks)} tasks (debug={is_debug})", self.project_id)
        
        if not tasks:
            self.emit(
                intent=Intent.DONE,
                payload={"status": "no_tasks", "message": "No tasks to execute", "operations": []},
                reasoning="Plan had no tasks"
            )
            return
        
        # Debug mode: implement directly
        if is_debug and len(tasks) <= 2:
            log_agent("coder", "Debug mode: implementing directly", self.project_id)
            await self._implement_debug_tasks(tasks)
            return
        
        # Normal flow: process each task
        for task in tasks:
            task_id = f"task_{int(time.time() * 1000)}"
            
            # Determine agent type from task description
            task_str = str(task).lower()
            if "ui" in task_str or "component" in task_str or "page" in task_str:
                agent_type = "ui"
            elif "api" in task_str or "route" in task_str or "endpoint" in task_str:
                agent_type = "api"
            else:
                agent_type = "logic"
            
            log_agent("coder", f"Processing: {str(task)[:40]}...", self.project_id)
            
            if agent_type in ["ui", "api"] and agent_type in self.sub_agents:
                await self._delegate_task(task, task_id, agent_type)
            else:
                await self._implement_task(task, task_id)
        
        # Wait for all sub-agents to complete
        waited = 0
        while self.pending_tasks and waited < 120:
            await asyncio.sleep(0.5)
            waited += 0.5
        
        log_agent("coder", f"All tasks complete: {len(self.all_operations)} total operations", self.project_id)
        
        self.emit(
            intent=Intent.DONE,
            payload={
                "status": "complete",
                "tasks_completed": len(tasks),
                "operations": self.all_operations
            },
            reasoning=f"Completed {len(tasks)} tasks with {len(self.all_operations)} file operations"
        )
    
    async def _delegate_task(self, task: str, task_id: str, agent_type: str):
        log_agent("coder", f"Delegating to {agent_type}_agent: {str(task)[:40]}...", self.project_id)
        
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
        log_agent("coder", f"Implementing: {str(task)[:40]}...", self.project_id)
        
        context = self._build_context_snippets()
        chat_history = _get_history(self.project_id)[-10:]
        
        messages = [{"role": "system", "content": self.SYSTEM_PROMPT}]
        for h in chat_history:
            messages.append({"role": h["role"], "content": h["content"]})
        
        messages.append({"role": "user", "content": f"""SYSTEM INSTRUCTIONS:
{self.SYSTEM_PROMPT}

CURRENT FILE CONTEXT (For Reference):
{context}

TASK: {task}

Output JSON with message and operations array."""})

        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                raw, tokens = await self.call_llm(messages, temperature=0.6)
                parsed = self.extract_json(raw)
                
                if not parsed:
                    raise ValueError("Could not extract JSON from response")
                    
                canonical = self._normalize_and_validate_ops(parsed)
                ops = canonical.get("operations", [])
                self.all_operations.extend(ops)
                
                for op in ops:
                    log_agent("coder", f"  → {op.get('action')}: {op.get('path')}", self.project_id)
                
                _append_history(self.project_id, "user", f"Task: {task}")
                _append_history(self.project_id, "assistant", raw)
                
                return

            except Exception as e:
                log_agent("coder", f"Attempt {attempt+1}/{max_retries+1} failed: {str(e)}", self.project_id)
                
                if attempt < max_retries:
                    correction_msg = (
                        f"Your previous response was invalid (Error: {str(e)}).\n"
                        "Please fix the format. Output valid JSON only.\n"
                        f"REMEMBER YOUR GOAL: {task}"
                    )
                    messages.append({"role": "user", "content": correction_msg})
                    await asyncio.sleep(1)
                else:
                    break
    
    async def _implement_debug_tasks(self, tasks: List[str]):
        for task in tasks:
            log_agent("coder", f"Debug fix: {str(task)[:40]}...", self.project_id)
            
            messages = [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": f"""DEBUG TASK - BE DIRECT:
{task}

Fix the issue with minimal changes. Output JSON with operations."""}
            ]
            
            raw, _ = await self.call_llm(messages, temperature=0.3)
            data = self.extract_json(raw)
            
            if data:
                try:
                    canonical = self._normalize_and_validate_ops(data)
                    ops = canonical.get("operations", [])
                    self.all_operations.extend(ops)
                    for op in ops:
                        log_agent("coder", f"  → {op.get('action')}: {op.get('path')}", self.project_id)
                except:
                    pass
        
        self.emit(
            intent=Intent.DONE,
            payload={"status": "debug_complete", "operations": self.all_operations},
            reasoning="Debug tasks completed"
        )
    
    async def _handle_sub_done(self, msg: MCPMessage):
        task_id = msg.task_id
        
        if task_id in self.pending_tasks:
            del self.pending_tasks[task_id]
            ops = msg.payload.get("operations", [])
            self.all_operations.extend(ops)
            log_agent("coder", f"Sub-agent completed task {task_id}: {len(ops)} operations", self.project_id)

# ============================================================================
# SUB-AGENTS
# ============================================================================

class UISubAgent(BaseAgent):
    """UI Specialist - React components and styling."""
    
    SYSTEM_PROMPT = (
        "You are the UI Specialist.\n\n"
        "YOUR JOB: Create beautiful, polished React components with Tailwind CSS.\n\n"
        "RULES:\n"
        "1. Use Shadcn/UI components from @/components/ui/ when appropriate\n"
        "2. Make designs creative and non-bootstrappy\n"
        "3. Use Tailwind for all styling\n"
        "4. Use framer-motion for smooth animations\n"
        "5. Use lucide-react for icons\n"
        "6. NEVER use Inter font - be creative with typography\n\n"
        "RESPONSE FORMAT (JSON ONLY):\n"
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
        
        log_agent("ui_agent", f"Creating UI: {str(task)[:40]}...", self.project_id)
        
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": f"""CONTEXT:
{context}

TASK:
{task}

Create a beautiful, polished component. Output JSON with operations."""}
        ]
        
        raw, _ = await self.call_llm(messages, temperature=0.7)
        data = self.extract_json(raw)
        
        if data:
            ops = data.get("operations", [])
            for op in ops:
                log_agent("ui_agent", f"  → {op.get('action')}: {op.get('path')}", self.project_id)
            
            self.emit(
                intent=Intent.DONE,
                payload={"operations": ops},
                to="coder",
                task_id=msg.task_id,
                reasoning=f"UI created for task"
            )

class APISubAgent(BaseAgent):
    """API Specialist - Express routes and backend."""
    
    SYSTEM_PROMPT = (
        "You are the API Specialist.\n\n"
        "YOUR JOB: Create Express routes and API endpoints.\n\n"
        "RULES:\n"
        "1. Use ES modules (import/export)\n"
        "2. Use relative paths with .js extension for imports\n"
        "3. Store data in JSON files (data/ folder) or use @libsql/client\n"
        "4. Use async/await for async operations\n"
        "5. Return proper JSON responses\n"
        "6. Handle errors with try/catch\n\n"
        "RESPONSE FORMAT (JSON ONLY):\n"
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
        
        log_agent("api_agent", f"Creating API: {str(task)[:40]}...", self.project_id)
        
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": f"""CONTEXT:
{context}

TASK:
{task}

Create the API route. Output JSON with operations."""}
        ]
        
        raw, _ = await self.call_llm(messages, temperature=0.5)
        data = self.extract_json(raw)
        
        if data:
            ops = data.get("operations", [])
            for op in ops:
                log_agent("api_agent", f"  → {op.get('action')}: {op.get('path')}", self.project_id)
            
            self.emit(
                intent=Intent.DONE,
                payload={"operations": ops},
                to="coder",
                task_id=msg.task_id,
                reasoning=f"API created for task"
            )

class LogicSubAgent(BaseAgent):
    """Logic Specialist - utilities and helpers."""
    
    SYSTEM_PROMPT = (
        "You are the Logic Specialist.\n\n"
        "YOUR JOB: Create utility functions, helpers, and business logic.\n\n"
        "RULES:\n"
        "1. Write clean, reusable functions\n"
        "2. Use TypeScript for type safety\n"
        "3. Add JSDoc comments for complex functions\n"
        "4. Handle edge cases\n\n"
        "RESPONSE FORMAT (JSON ONLY):\n"
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
        
        log_agent("logic_agent", f"Creating logic: {str(task)[:40]}...", self.project_id)
        
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": f"""CONTEXT:
{context}

TASK:
{task}

Create the utility/logic. Output JSON with operations."""}
        ]
        
        raw, _ = await self.call_llm(messages, temperature=0.5)
        data = self.extract_json(raw)
        
        if data:
            ops = data.get("operations", [])
            for op in ops:
                log_agent("logic_agent", f"  → {op.get('action')}: {op.get('path')}", self.project_id)
            
            self.emit(
                intent=Intent.DONE,
                payload={"operations": ops},
                to="coder",
                task_id=msg.task_id,
                reasoning=f"Logic created for task"
            )

# ============================================================================
# DEBUGGER AGENT
# ============================================================================

class DebuggerAgent(BaseAgent):
    """Debugger - direct fixes, no overthinking."""
    
    SYSTEM_PROMPT = (
        "You are the Debugger.\n\n"
        "YOUR JOB: Fix errors. Be DIRECT. No overthinking.\n\n"
        "RULES:\n"
        "1. Look at the error message\n"
        "2. Find the problematic file/line\n"
        "3. Fix it with MINIMAL changes\n"
        "4. Output the fix\n\n"
        "NO explaining. NO planning. Just fix.\n\n"
        "RESPONSE FORMAT (JSON ONLY):\n"
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
            {"role": "user", "content": f"""ERROR:
{error_message}

FILE: {relevant_file}
CONTENT:
{file_content[:1500] if file_content else '[file not found]'}

Fix this error. Output JSON with operations."""}
        ]
        
        raw, _ = await self.call_llm(messages, temperature=0.2)
        data = self.extract_json(raw)
        
        if data:
            ops = data.get("operations", [])
            for op in ops:
                log_agent("debugger", f"  → {op.get('action')}: {op.get('path')}", self.project_id)
            return ops
        
        return []

# ============================================================================
# SWARM ORCHESTRATOR
# ============================================================================

class AgentSwarm:
    """Main orchestrator - creates and manages all agents."""
    
    def __init__(self, project_id: str):
        self.project_id = project_id
        self.bus = MCPBus(project_id)
        
        self.planner = PlannerAgent("planner", self.bus, project_id)
        self.coder = CoderAgent("coder", self.bus, project_id)
        self.debugger = DebuggerAgent("debugger", self.bus, project_id)
        
        self.ui_agent = UISubAgent("ui_agent", self.bus, project_id)
        self.api_agent = APISubAgent("api_agent", self.bus, project_id)
        self.logic_agent = LogicSubAgent("logic_agent", self.bus, project_id)
        
        self.coder.register_sub_agent(self.ui_agent)
        self.coder.register_sub_agent(self.api_agent)
        self.coder.register_sub_agent(self.logic_agent)
        
        log_agent("swarm", "Agent swarm initialized", project_id)
    
    async def solve(self, user_request: str, file_tree: Dict[str, str], 
                    agent_skills: Optional[Dict] = None,
                    skip_planner: bool = False) -> Dict[str, Any]:
        """
        Main entry point.
        
        Returns:
        {
            "assistant_message": str,  # Only this goes to UI
            "operations": List[Dict],   # File operations
            "status": str
        }
        """
        
        for agent in [self.planner, self.coder, self.debugger, 
                      self.ui_agent, self.api_agent, self.logic_agent]:
            agent.file_tree = file_tree
        
        log_agent("swarm", f"Solving: {user_request[:60]}...", self.project_id)
        
        assistant_message = "Working on it..."
        
        if skip_planner:
            log_agent("swarm", "Direct mode - no planning", self.project_id)
            self.coder.emit(
                intent=Intent.PLAN,
                payload={
                    "assistant_message": "Applying fix...",
                    "tasks": [user_request],
                    "is_debug": True
                },
                to="coder",
                task_id="direct",
                reasoning="Direct task execution"
            )
            assistant_message = "Applying fix..."
        else:
            # Run planner
            plan_result = await self.planner.plan(user_request, file_tree, agent_skills)
            assistant_message = plan_result.payload.get("assistant_message", "Building your app...")
        
        # Wait for completion
        max_wait = 300
        waited = 0
        
        while waited < max_wait:
            await asyncio.sleep(0.5)
            waited += 0.5
            
            # Check for completion
            done_msgs = [m for m in self.bus.messages if m.intent == Intent.DONE]
            if done_msgs:
                last_done = done_msgs[-1]
                if last_done.from_agent == "coder" and not self.coder.pending_tasks:
                    log_agent("swarm", "Execution complete", self.project_id)
                    break
        
        # Collect all operations
        all_ops = []
        for msg in self.bus.messages:
            if msg.intent in [Intent.DONE, Intent.DEBUG_FIX]:
                all_ops.extend(msg.payload.get("operations", []))
        
        log_agent("swarm", f"Complete: {len(all_ops)} file operations", self.project_id)
        
        return {
            "status": "complete",
            "assistant_message": assistant_message,
            "operations": all_ops
        }
    
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
    """
    Unified Agent - backward-compatible wrapper for the AgentSwarm.
    Maintains the old interface while using the new MCP architecture internally.
    """
    
    def __init__(self, timeout_s: float = 120.0):
        self.timeout_s = timeout_s
        self._swarm_cache: Dict[str, AgentSwarm] = {}
    
    def _get_swarm(self, project_id: str) -> AgentSwarm:
        if project_id not in self._swarm_cache:
            self._swarm_cache[project_id] = AgentSwarm(project_id)
        return self._swarm_cache[project_id]
    
    def remember(self, project_id: str, role: str, text: str) -> None:
        """Add a message to the shared history."""
        _append_history(project_id, role, text)
    
    def plan(
        self,
        user_request: str,
        project_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Generate a build plan from the user request.
        Returns: {"assistant_message": str, "plan": dict, "todo_md": str, "usage": dict}
        """
        project_id = str(project_context.get("project_id") or "").strip()
        agent_skills = project_context.get("agent_skills")
        
        if project_id:
            _append_history(project_id, "user", user_request)
            log_agent("planner", f"Starting plan generation for: {user_request[:100]}...", project_id)

        file_tree = {f: "" for f in project_context.get("files", [])}
        
        # Run async plan in sync context
        loop = asyncio.new_event_loop()
        try:
            swarm = self._get_swarm(project_id)
            future = swarm.planner.plan(user_request, file_tree, agent_skills)
            result = loop.run_until_complete(future)
            
            tasks = result.payload.get("tasks", [])
            assistant_message = result.payload.get("assistant_message", "")
            tokens = result.payload.get("estimated_tokens", 0)
            capabilities = result.payload.get("capabilities", [])
            
            base_plan = {
                "capabilities": capabilities,
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
    
    async def code(
        self,
        plan_section: str,
        plan_text: str,
        file_tree: Dict[str, str],
        project_name: str,
        history: Optional[List[Dict[str, str]]] = None,
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """
        Generate code based on the plan.
        Returns: {"message": str, "operations": list, "usage": dict}
        """
        swarm = self._get_swarm(project_name)
        
        # Create a single-task plan
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
        
        # Wait and collect
        await asyncio.sleep(0.5)
        max_wait = 60
        waited = 0
        
        while waited < max_wait:
            await asyncio.sleep(0.5)
            waited += 0.5
            if not swarm.coder.pending_tasks:
                break
        
        # Collect operations
        operations = []
        for msg in swarm.bus.messages:
            if msg.intent == Intent.DONE:
                operations.extend(msg.payload.get("operations", []))
        
        return {
            "message": f"Completed {len(operations)} operations",
            "operations": operations,
            "usage": {"total_tokens": 0}
        }
    
    @staticmethod
    def _to_todo_md(plan: Dict[str, Any], msg: str = "") -> str:
        """Convert plan to markdown todo list."""
        tasks = plan.get("todo", [])
        
        if not tasks:
            return ""

        lines = ["# Build Plan\n", "## Tasks"]
        for task in tasks:
            lines.append(f"- {task}")
            
        return "\n".join(lines)

__all__ = ["Agent", "AgentSwarm", "MCPBus", "MCPMessage", "Intent", 
           "_render_token_limit_message", "clear_history"]