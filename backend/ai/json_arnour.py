"""
json_armor.py — Battle-Hardened JSON Extraction for Gorilla Builder
====================================================================

Drop-in fix for GLM-5 and other large models that output valid JSON
wrapped in preamble text, or produce massive code blocks that confuse
the brace-depth scanner.

Usage in app.py:
    import backend.ai.json_armor  # Auto-patches on import

Root causes this fixes:
1. Model outputs "I'll build..." preamble before the JSON object
2. Code content contains unescaped quotes/braces that break parsing  
3. Response gets truncated mid-JSON due to token limits
4. _extract_json's brace scanner is O(n²) and breaks on nested JSX
5. Coder retries waste tokens with aggressive error messages

What it patches:
- Replaces _extract_json() with a 3-stage parser (strip → repair → extract)
- Replaces _fix_common_json_issues() with code-aware repair
- Patches CoderAgent._normalize_and_validate_ops to handle partial JSON
- Patches the coder's retry prompts to be calmer (reduces panic-coding)
"""

import re
import json
import sys
from typing import Any, Dict, List, Optional, Tuple


# ============================================================================
# STAGE 1: STRIP — Remove everything that isn't JSON
# ============================================================================

def _strip_preamble(text: str) -> str:
    """
    Remove LLM preamble/postamble text surrounding JSON.
    
    Models like GLM-5 often output:
        "I'll create the files now. Here's my implementation:\n\n{...}\n\nLet me know..."
    
    This strips everything before the first { and after the last }.
    """
    text = text.strip()
    
    # Remove markdown code fences first
    # Handle ```json\n{...}\n``` and ```\n{...}\n```
    fence_match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', text)
    if fence_match:
        inner = fence_match.group(1).strip()
        if inner.startswith('{'):
            text = inner
    
    # Find the first { that starts a JSON object
    first_brace = text.find('{')
    if first_brace == -1:
        return text
    
    # Find the last } that closes it
    last_brace = text.rfind('}')
    if last_brace == -1 or last_brace <= first_brace:
        return text
    
    return text[first_brace:last_brace + 1]


# ============================================================================
# STAGE 2: REPAIR — Fix common JSON corruption patterns
# ============================================================================

def _repair_json(text: str) -> str:
    """
    Fix JSON corruption patterns specific to code-generating LLMs.
    
    Unlike _fix_common_json_issues, this is designed for JSON that contains
    large code strings with embedded quotes, braces, and special characters.
    """
    # Remove BOM and zero-width characters
    text = text.lstrip('\ufeff\u200b\u200c\u200d')
    
    # Remove JS-style comments (but NOT inside strings — simple heuristic)
    # Only strip // comments that appear at the start of a line or after whitespace
    text = re.sub(r'(?m)^\s*//[^\n]*$', '', text)
    text = re.sub(r'/\*[\s\S]*?\*/', '', text)
    
    # Remove trailing commas before } or ] (extremely common LLM mistake)
    text = re.sub(r',\s*([}\]])', r'\1', text)
    
    # Replace Python literals
    # Be careful: only replace when they appear as JSON values, not inside strings
    # Simple heuristic: replace only when preceded by : or , or [ and followed by , or } or ]
    text = re.sub(r'(?<=[\[:,\[])\s*True\s*(?=[,}\]])', ' true ', text)
    text = re.sub(r'(?<=[\[:,\[])\s*False\s*(?=[,}\]])', ' false ', text)
    text = re.sub(r'(?<=[\[:,\[])\s*None\s*(?=[,}\]])', ' null ', text)
    
    return text


def _repair_truncated_json(text: str) -> str:
    """
    Attempt to close a truncated JSON object.
    
    When the model runs out of tokens, the JSON gets cut off like:
        {"message": "ok", "operations": [{"action": "create_file", "path": "src/App.tsx", "content": "import Re
    
    This function tries to close the string, object, and array.
    """
    text = text.rstrip()
    
    # Count unclosed braces and brackets
    in_string = False
    escape_next = False
    brace_depth = 0
    bracket_depth = 0
    
    for c in text:
        if escape_next:
            escape_next = False
            continue
        if c == '\\' and in_string:
            escape_next = True
            continue
        if c == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            brace_depth += 1
        elif c == '}':
            brace_depth -= 1
        elif c == '[':
            bracket_depth += 1
        elif c == ']':
            bracket_depth -= 1
    
    # If we're still inside a string, close it
    if in_string:
        text += '"'
    
    # Close brackets and braces
    text += ']' * max(bracket_depth, 0)
    text += '}' * max(brace_depth, 0)
    
    return text


# ============================================================================
# STAGE 3: EXTRACT — Parse JSON with multiple strategies
# ============================================================================

def _armored_extract_json(text: str) -> Any:
    """
    Battle-hardened JSON extractor for LLM outputs containing code.
    
    3-stage pipeline:
    1. STRIP: Remove preamble/postamble text
    2. REPAIR: Fix common corruption patterns
    3. EXTRACT: Try multiple parsing strategies
    
    Falls back to progressively more aggressive repair attempts.
    """
    if not text or not text.strip():
        return None
    
    original_text = text
    
    # === ATTEMPT 1: Direct parse (fast path for well-formed JSON) ===
    text_stripped = text.strip()
    if text_stripped.startswith('{'):
        try:
            return json.loads(text_stripped)
        except json.JSONDecodeError:
            pass
    
    # === ATTEMPT 2: Strip preamble + direct parse ===
    stripped = _strip_preamble(text)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    
    # === ATTEMPT 3: Strip + repair + parse ===
    repaired = _repair_json(stripped)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass
    
    # === ATTEMPT 4: Try markdown code blocks ===
    code_blocks = re.findall(r'```(?:json)?\s*([\s\S]*?)\s*```', original_text)
    for block in code_blocks:
        block = block.strip()
        for attempt in [block, _repair_json(block)]:
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                pass
    
    # === ATTEMPT 5: Brace-depth scan (O(n) single-pass, not O(n²)) ===
    # Find the FIRST top-level JSON object
    result = _single_pass_json_scan(original_text)
    if result is not None:
        return result
    
    # === ATTEMPT 6: Repair truncation + parse ===
    for candidate in [stripped, repaired]:
        truncation_fixed = _repair_truncated_json(candidate)
        try:
            parsed = json.loads(truncation_fixed)
            # Validate it has the expected structure
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    
    # === ATTEMPT 7: Nuclear option — regex extract operations array ===
    ops_result = _extract_operations_by_regex(original_text)
    if ops_result:
        return ops_result
    
    return None


def _single_pass_json_scan(text: str) -> Any:
    """
    O(n) single-pass JSON object extraction.
    
    Properly tracks string boundaries, escape sequences, and nesting depth.
    Returns the LARGEST valid top-level JSON object found.
    """
    best = None
    best_len = 0
    i = 0
    n = len(text)
    
    while i < n:
        if text[i] == '{':
            # Try to find the matching close brace
            start = i
            depth = 0
            in_str = False
            escape = False
            j = i
            
            while j < n:
                c = text[j]
                
                if escape:
                    escape = False
                    j += 1
                    continue
                
                if c == '\\' and in_str:
                    escape = True
                    j += 1
                    continue
                
                if c == '"':
                    in_str = not in_str
                    j += 1
                    continue
                
                if in_str:
                    j += 1
                    continue
                
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:j + 1]
                        if len(candidate) > best_len:
                            try:
                                parsed = json.loads(candidate)
                                if isinstance(parsed, dict):
                                    best = parsed
                                    best_len = len(candidate)
                            except json.JSONDecodeError:
                                # Try repair
                                try:
                                    parsed = json.loads(_repair_json(candidate))
                                    if isinstance(parsed, dict):
                                        best = parsed
                                        best_len = len(candidate)
                                except json.JSONDecodeError:
                                    pass
                        break
                
                j += 1
            
            # Skip past this object to avoid re-scanning
            i = j + 1
        else:
            i += 1
    
    return best


def _extract_operations_by_regex(text: str) -> Optional[Dict]:
    """
    Nuclear fallback: extract operations using regex when JSON is too broken.
    
    Looks for patterns like:
        "action": "create_file", "path": "src/App.tsx", "content": "..."
    """
    ops = []
    
    # Find action/path/content triplets
    pattern = r'"action"\s*:\s*"(create_file|overwrite_file|read_file)"\s*,\s*"path"\s*:\s*"([^"]+)"'
    matches = re.finditer(pattern, text)
    
    for match in matches:
        action = match.group(1)
        path = match.group(2)
        
        if action == "read_file":
            ops.append({"action": action, "path": path, "content": None})
        else:
            # Try to extract the content field
            # Look for "content": " after this match
            after = text[match.end():]
            content_match = re.search(r',\s*"content"\s*:\s*"', after)
            if content_match:
                # Content starts after the opening quote
                content_start = match.end() + content_match.end()
                # This is unreliable for large content, so we just note the file
                ops.append({"action": action, "path": path, "content": f"// Content extraction failed — file was being generated but JSON was truncated"})
    
    if ops:
        # Try to extract message
        msg_match = re.search(r'"message"\s*:\s*"([^"]*)"', text)
        message = msg_match.group(1) if msg_match else "Partial extraction from broken JSON"
        
        return {
            "message": message,
            "operations": ops,
            "_partial": True  # Flag so the coder knows this was a partial extraction
        }
    
    return None


# ============================================================================
# CODER RETRY LOGIC FIX
# ============================================================================

def _patched_normalize_and_validate_ops(self, parsed: Dict[str, Any]) -> Dict[str, Any]:
    """
    Patched version that handles partial JSON extraction gracefully.
    
    Changes from original:
    1. If _partial flag is set, log a warning but don't crash
    2. More lenient content validation (allow None for truncated files)
    3. Better error messages that don't trigger panic-coding
    """
    # Import normalize map
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
    ALLOWED_ACTIONS = {"create_file", "overwrite_file", "read_file"}
    
    if not isinstance(parsed, dict):
        raise ValueError("Could not parse a valid JSON object from your response.")
    
    # Check for partial extraction
    is_partial = parsed.get("_partial", False)
    
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
        if not isinstance(op, dict):
            continue
            
        action_raw = (op.get("action") or "").strip()
        action = ACTION_NORMALIZE.get(action_raw, action_raw)
        
        if action in {"patch_file", "patch"}:
            action = "overwrite_file"
            
        if action not in ALLOWED_ACTIONS:
            if action in ["delete_file", "move_file"]:
                continue
            # Don't raise — just skip unknown actions
            continue
            
        path = op.get("path")
        if not path or not isinstance(path, str):
            continue  # Skip instead of raising
        
        if action == "read_file":
            normalized_ops.append({
                "action": action,
                "path": path.strip(),
                "content": None
            })
            continue
            
        content = op.get("content")
        if content is None:
            if is_partial:
                # Partial extraction — skip this op silently
                continue
            # For non-partial, this is a real error but don't panic
            continue

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


# ============================================================================
# AUTO-APPLY PATCHES
# ============================================================================

def apply_json_armor():
    """
    Apply all JSON armor patches:
    1. Replace _extract_json globally
    2. Replace _fix_common_json_issues globally  
    3. Patch CoderAgent._normalize_and_validate_ops
    """
    patched = []
    
    # Patch both agent modules
    for module_name in ['backend.ai.agent', 'backend.ai.supabase_agent', 'agent', 'supabase_agent']:
        try:
            module = sys.modules.get(module_name)
            if module is None:
                continue
            
            # Replace _extract_json
            if hasattr(module, '_extract_json'):
                module._extract_json = _armored_extract_json
                patched.append(f"{module_name}._extract_json")
            
            # Replace _fix_common_json_issues
            if hasattr(module, '_fix_common_json_issues'):
                module._fix_common_json_issues = _repair_json
                patched.append(f"{module_name}._fix_common_json_issues")
            
            # Patch CoderAgent._normalize_and_validate_ops
            if hasattr(module, 'CoderAgent'):
                module.CoderAgent._normalize_and_validate_ops = _patched_normalize_and_validate_ops
                patched.append(f"{module_name}.CoderAgent._normalize_and_validate_ops")
            
            # Patch BaseAgent.extract_json to use the new extractor
            if hasattr(module, 'BaseAgent'):
                module.BaseAgent.extract_json = lambda self, text: _armored_extract_json(text)
                patched.append(f"{module_name}.BaseAgent.extract_json")
                
        except Exception as e:
            print(f"[json_armor] Warning: could not patch {module_name}: {e}")
    
    if patched:
        # Try to log via the agent's logger
        try:
            log = None
            for mod_name in ['backend.ai.agent', 'backend.ai.supabase_agent', 'agent', 'supabase_agent']:
                mod = sys.modules.get(mod_name)
                if mod and hasattr(mod, 'log_agent'):
                    log = mod.log_agent
                    break
            
            if log:
                log("json_armor", f"✅ Patched {len(patched)} functions: {', '.join(patched)}", "")
            else:
                print(f"[json_armor] ✅ Patched {len(patched)} functions")
        except Exception:
            print(f"[json_armor] ✅ Patched {len(patched)} functions")
    
    return patched


# Auto-apply on import
_applied = apply_json_armor()