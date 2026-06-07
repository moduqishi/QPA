"""QPA — Qoder2API FastAPI bridge with PAT pool, image support, and WebUI"""

import asyncio
import base64
import copy
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .bearer import SessionContext
from .pool import PatPool, Account
from .qoder_client import open_stream

logger = logging.getLogger("qpa")

app = FastAPI(title="QPA")

pool: PatPool | None = None
QODER_CHAT_URL = (
    "https://api3.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation"
    "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
)


@app.on_event("startup")
async def startup():
    global pool
    pool = PatPool()
    pool._template_base = _load_template()
    # Run init in background — don't block server startup on slow
    # session exchange / quota fetch.  Quota will appear once available.
    asyncio.create_task(_bg_init())


async def _bg_init():
    """Background initialization — retries quota fetch if it fails initially."""
    global pool
    if pool is None:
        return
    loop = asyncio.get_running_loop()
    # Run in executor to avoid blocking the event loop with sync HTTP calls
    await loop.run_in_executor(None, pool.init_all)
    # For any account that still has no quota, schedule a retry after 5s
    stale = [a for a in pool.accounts if a.enabled and a.session and not a.is_expired and a.last_quota is None]
    if stale:
        await asyncio.sleep(5)
        for acc in stale:
            await loop.run_in_executor(None, pool.refresh_quota, acc)
            if acc.last_quota is not None:
                logger.info("Quota fetched on retry for %s", acc.name)
            else:
                logger.warning("Quota still unavailable for %s — will retry on next admin refresh", acc.name)


async def _process_images(sess: SessionContext, messages: list[dict]) -> tuple[list[dict], list[str]]:
    image_urls: list[str] = []
    new_messages = copy.deepcopy(messages)

    for msg in new_messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in ("image_url", "input_image"):
                url = part.get("image_url", {}).get("url", "")
                if url:
                    image_urls.append(url)

    return new_messages, image_urls


def _blank_response_meta() -> dict:
    return {"id": "", "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "completion_tokens_details": {"reasoning_tokens": 0}, "prompt_tokens_details": {"cached_tokens": 0}}}

def _normalize_content(content) -> str:
    if content is None: return ""
    if isinstance(content, str): return content
    if isinstance(content, list):
        parts = [_normalize_content_part(item) for item in content]
        return "\n\n".join(p for p in parts if p)
    return _normalize_content_part(content)

def _normalize_content_part(item) -> str:
    if item is None: return ""
    if isinstance(item, str): return item
    if isinstance(item, dict):
        if "text" in item and isinstance(item["text"], str): return item["text"]
        typ = item.get("type", "")
        if typ in ("image_url", "input_image"):
            url = item.get("image_url", {}).get("url", "")
            return f"[image] {url}" if url else ""
        if "content" in item: return _normalize_content(item["content"])
        return json.dumps(item)
    return str(item)

def _normalize_message_text(message: dict) -> str:
    text = _normalize_content(message.get("content"))
    if not text.strip(): text = _normalize_content(message.get("contents"))
    return text

def _extract_latest_user_prompt(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            text = _normalize_message_text(msg)
            if text.strip(): return text
    return ""

def _parse_tool_calls_text(text: str | None) -> list[dict] | None:
    """Parse 'Tool calls:```json[...]```' from text, allowing extra text after."""
    if not text: return None
    trimmed = text.strip()
    if not trimmed.startswith("Tool calls:"): return None
    payload = trimmed[len("Tool calls:"):].strip()
    # Remove markdown code block markers if present
    if payload.startswith("```"):
        nl = payload.find("\n")
        if nl >= 0:
            payload = payload[nl + 1:]  # content after the ```json line
    if payload.endswith("```"):
        payload = payload[:-3].strip()
    elif payload.endswith("```\n") or payload.endswith("``` "):
        payload = payload[:-4].strip()
    # Also strip markdown variant with text after
    if "\n" in payload:
        # Take content up to the first ``` or end of JSON array
        end_idx = payload.find("```")
        if end_idx >= 0:
            payload = payload[:end_idx].strip()
        else:
            # Try to find end of JSON array
            # Find the last ] that closes the outermost array
            depth = 0
            json_end = -1
            for k, ch in enumerate(payload):
                if ch == '[':
                    depth += 1
                elif ch == ']':
                    depth -= 1
                    if depth == 0:
                        json_end = k + 1
                        break
            if json_end > 0:
                payload = payload[:json_end]
            # else leave as-is and let json.loads decide
    if not payload.startswith("["): return None
    try: return _normalize_tool_calls(json.loads(payload))
    except Exception as e:
        logger.debug("parse_tool_calls_text failed: %s | text=%.200s", e, text)
        return None

def _normalize_tool_calls(raw) -> list[dict] | None:
    if not isinstance(raw, list) or not raw: return None
    result = []
    for call in raw:
        func = call.get("function", {})
        name = func.get("name", "")
        arguments = _normalize_tool_arguments(func.get("arguments"))
        if not name and not arguments: continue
        result.append({"id": call.get("id", ""), "type": call.get("type", "function"),
                        "function": {"name": name, "arguments": arguments}})
    return result or None

def _normalize_tool_arguments(arg) -> str:
    if arg is None: return ""
    if isinstance(arg, str): return arg
    return json.dumps(arg)

def _render_tool_calls(tool_calls) -> str:
    return "Tool calls:\n" + json.dumps(tool_calls)

def _render_tool_result(message: dict, text: str) -> str:
    name = message.get("name", "")
    tcid = message.get("tool_call_id", "")
    sb = "Tool result"
    if name: sb += f" ({name})"
    if tcid: sb += f" [{tcid}]"
    if text: sb += f":\n{text}"
    return sb

def _summarize_unresolved_tool_calls(tool_calls: list[dict]) -> str:
    limit = min(len(tool_calls), 6)
    names = [tc.get("function", {}).get("name", "unknown") for tc in tool_calls[:limit]]
    sb = "Previously planned but unexecuted tool calls"
    if names: sb += ": " + ", ".join(names)
    if len(tool_calls) > limit: sb += f" and {len(tool_calls) - limit} more"
    return sb + "."

def _build_user_message(text: str) -> dict:
    return {"role": "user", "content": text, "contents": [{"type": "text", "text": text}],
            "response_meta": _blank_response_meta(), "reasoning_content_signature": ""}

def _build_structured_message(role: str, text: str) -> dict:
    return {"role": role, "content": text or "", "response_meta": _blank_response_meta(),
            "reasoning_content_signature": ""}

def _build_tool_message(message: dict, text: str) -> dict:
    # Qoder only supports "system" and "user" roles — "tool" role messages
    # are silently dropped by Qoder's API layer. We embed the tool result
    # as a user message instead.
    rendered = _render_tool_result(message, text)
    return _build_user_message(rendered)

def _has_role(messages: list[dict], role: str) -> bool:
    return any(m.get("role") == role for m in messages)

def _has_resolved_tool_response(messages: list[dict], idx: int) -> bool:
    msg = messages[idx]
    if msg.get("role") != "assistant": return False
    has_tc = (isinstance(msg.get("tool_calls"), list) and len(msg["tool_calls"]) > 0) or \
             _parse_tool_calls_text(_normalize_message_text(msg)) is not None
    if not has_tc: return False
    for i in range(idx + 1, len(messages)):
        r = messages[i].get("role")
        if r == "tool": return True
        if r in ("assistant", "user", "system"): return False
    return False

def _extract_any_tool_calls(message: dict, text: str, tools_enabled: bool) -> list[dict] | None:
    if not tools_enabled: return None
    tc = message.get("tool_calls")
    if isinstance(tc, list) and len(tc) > 0: return _normalize_tool_calls(tc)
    return _parse_tool_calls_text(text)

def _convert_incoming_message(message: dict, tools_enabled: bool, allow_stc: bool) -> dict | None:
    role = message.get("role", "user")
    text = _normalize_message_text(message)
    any_tc = _extract_any_tool_calls(message, text, tools_enabled)
    structured_tc = _extract_any_tool_calls(message, text, True) if (tools_enabled and allow_stc) else None

    if role == "assistant" and structured_tc is not None:
        content = text or ""
        if _parse_tool_calls_text(content) is not None: content = ""
        out = _build_structured_message("assistant", content)
        out["tool_calls"] = copy.deepcopy(structured_tc)
        return out

    if role == "assistant" and any_tc is not None and not allow_stc:
        return _build_structured_message("assistant", _summarize_unresolved_tool_calls(any_tc))

    if not tools_enabled and isinstance(message.get("tool_calls"), list) and len(message["tool_calls"]) > 0:
        text = (text + "\n\n" + _render_tool_calls(message["tool_calls"])) if text else _render_tool_calls(message["tool_calls"])

    if role == "tool":
        if tools_enabled: return _build_tool_message(message, text)
        role = "user"; text = _render_tool_result(message, text)

    if not text.strip(): return None
    if role == "user": return _build_user_message(text)
    return _build_structured_message(role, text)


def _convert_openai_contents_to_qoder(message: dict) -> dict:
    content = message.get("content")
    if not isinstance(content, list):
        return message

    qoder_contents = []
    text_parts = []
    for part in content:
        if isinstance(part, str):
            text_parts.append(part)
        elif isinstance(part, dict):
            typ = part.get("type", "")
            if typ == "text":
                text_parts.append(part.get("text", ""))
            elif typ == "image_url":
                url = part.get("image_url", {}).get("url", "")
                qoder_contents.append({"type": "image_url", "image_url": {"url": url}})
            elif typ == "input_image":
                url = part.get("image_url", {}).get("url", "")
                qoder_contents.append({"type": "image_url", "image_url": {"url": url}})

    if qoder_contents:
        full_text = "\n\n".join(t for t in text_parts if t)
        if full_text:
            qoder_contents.insert(0, {"type": "text", "text": full_text})
        result = copy.deepcopy(message)
        result["content"] = ""
        result["contents"] = qoder_contents
        return result

    return message


TOOL_INSTRUCTION = (
    "You are Codex, an AI coding assistant that helps users with software engineering tasks. You have access to various tools that let you execute commands, read and write files, search code, and browse the web. Use the instructions below and the tools available to you to assist the user.\n"
    "\n"
    "You operate in the Codex desktop environment. The user and you share one workspace. Your job is to collaborate with them until their goal is genuinely handled.\n"
    "\n"
    "IMPORTANT: You MUST use the available tools to complete any task. Never just describe what you would do — actually do it by calling the appropriate tool. Each tool response gives you results you need for the next step. Keep taking action until the task is done.\n"
    "IMPORTANT: After finishing the task, ALWAYS try to check whether the generated code and programs work correctly — by compiling, running, testing or other appropriate methods — if conditions allow.\n"
    "IMPORTANT: If the user does not specify a language, respond IN THE LANGUAGE THE USER USED for the question.\n"
    "\n"
    "\n"
    "# Who you are\n"
    "You are Codex — an AI coding agent that helps users build and debug software projects. You have access to tools for running commands, reading and editing files, searching code, browsing the web, and managing git.\n"
    "When asked about your identity, model, or what powers you, identify yourself as Codex based on GPT-5 — an AI coding agent.\n"
    "\n"
    "\n"
    "# Tone and style\n"
    "- Only use emojis if the user explicitly requests it. Avoid using emojis in all communication unless asked.\n"
    "- Your output will be displayed in a chat interface. Your responses should be clear and substantive. You can use GitHub-flavored markdown for formatting.\n"
    "- Output text to communicate with the user; all text you output is displayed to the user. Only use tools to complete tasks.\n"
    "- You can create files when needed for achieving the user's goal. Prefer editing existing files over creating new ones unless a new file makes sense.\n"
    "- NEVER create documentation files (*.md) or README files. Only create documentation files if explicitly requested by the User.\n"
    "\n"
    "# Professional objectivity\n"
    "Prioritize technical accuracy and truthfulness over validating the user's beliefs. Focus on facts and problem-solving, providing direct, objective technical info without any unnecessary superlatives, praise, or emotional validation. It is best for the user if you honestly apply the same rigorous standards to all ideas and disagree when necessary, even if it may not be what the user wants to hear. Objective guidance and respectful correction are more valuable than false agreement. Whenever there is uncertainty, it is best to investigate to find the truth first rather than instinctively confirming the user's beliefs. Avoid using over-the-top validation or excessive praise when responding to users such as \"You're absolutely right\" or similar phrases.\n"
    "\n"
    "# Planning without timelines\n"
    "When planning tasks, provide concrete implementation steps without time estimates. Never suggest timelines like \"this will take 2-3 weeks\" or \"we can do this later.\" Focus on what needs to be done, not when. Break work into actionable steps and let users decide scheduling.\n"
    "\n"
    "# Doing tasks\n"
    "The user will primarily request you perform software engineering tasks. This includes solving bugs, adding new functionality, refactoring, debugging, explaining code, and more. For these tasks the following steps are recommended:\n"
    "- NEVER propose changes to code you haven't read. If a user asks about or wants you to modify a file, read it first. Understand existing code before suggesting modifications.\n"
    "- Plan the task carefully first if it is multi-step, then execute each step. Use tools to explore the codebase before making changes.\n"
    "- Be careful not to introduce security vulnerabilities such as command injection, XSS, SQL injection, and other OWASP top 10 vulnerabilities. If you notice that you wrote insecure code, immediately fix it.\n"
    "- Avoid over-engineering. Only make changes that are directly requested or clearly necessary. Keep solutions simple and focused.\n"
    "  - Don't add features, refactor code, or make improvements beyond what was asked. A bug fix does not need surrounding code cleaned up. A simple feature does not need extra configurability. Don't add docstrings, comments, or type annotations to code you didn't change. Only add comments where the logic isn't self-evident.\n"
    "  - Don't add error handling, fallbacks, or validation for scenarios that cannot happen. Trust internal code and framework guarantees. Only validate at system boundaries (user input, external APIs). Don't use feature flags or backwards-compatibility shims when you can just change the code.\n"
    "  - Don't create helpers, utilities, or abstractions for one-time operations. Don't design for hypothetical future requirements. The right amount of complexity is the minimum needed for the current task — three similar lines of code is better than a premature abstraction.\n"
    "- Avoid backwards-compatibility hacks like renaming unused variables, re-exporting types, or adding removed comments for deleted code. If something is unused, delete it completely.\n"
    "- Implement the solution using all tools available to you. When a tool fails, try to understand why and fix the issue rather than giving up.\n"
    "- Verify the solution if possible by running it or testing it. Run the project's test suite if one exists.\n"
    "- NEVER commit changes or create pull requests unless the user explicitly asks you to. It is VERY IMPORTANT to only commit when explicitly asked, otherwise the user will feel that you are being too proactive.\n"
    "- The conversation has long context support through automatic summarization. You will not run out of context.\n"
    "- Tool results and user messages may include system-reminder tags. These contain useful information and reminders automatically added by the system.\n"
    "\n"
    "# Tool usage policy\n"
    "\n"
    "You have access to the following tool categories. You MUST use these tools to do work — never just describe what you would do.\n"
    "\n"
    "## exec_command\n"
    "Executes a command in a PTY terminal session. This is your primary tool for interacting with the system.\n"
    "\n"
    "Use exec_command for:\n"
    "- Running git commands (status, add, commit, push, diff, log)\n"
    "- Running build tools and test frameworks\n"
    "- Running dev servers and scripts\n"
    "- Checking file system state (ls, pwd, which, file)\n"
    "- Installing dependencies (npm install, pip install, go get)\n"
    "- Any command-line operation\n"
    "\n"
    "Parameters:\n"
    "- cmd: The shell command to execute (required)\n"
    "- workdir: Working directory (defaults to current)\n"
    "- timeout: Max execution time in ms (optional)\n"
    "- description: Short description of what the command does (recommended)\n"
    "\n"
    "Usage notes:\n"
    "- When commands are independent, call exec_command in parallel with other tools\n"
    "- When commands depend on each other, chain them sequentially\n"
    "- Use absolute paths to avoid cd confusion\n"
    "- For long-running processes like dev servers, use run_in_background or note the start\n"
    "\n"
    "IMPORTANT: Do NOT use exec_command for reading file contents, searching text in files, or editing files — use the dedicated file tools for those operations.\n"
    "\n"
    "## apply_patch\n"
    "Edits file content by applying precise text replacements. Use this for all code edits.\n"
    "\n"
    "Always read a file first before editing it. The edit will fail if old_string is not unique — provide enough context to make it unique.\n"
    "\n"
    "Parameters:\n"
    "- file_path: Absolute path to the file to modify\n"
    "- old_string: Exact text to replace (must be unique in the file)\n"
    "- new_string: Replacement text\n"
    "\n"
    "## File tools\n"
    "These tools handle all file I/O operations:\n"
    "- Read files: Use cat or read tool to view file contents\n"
    "- Create/overwrite files: Use write tool\n"
    "- Search text: Use rg (ripgrep) for fast file content searching — it is much faster than alternatives\n"
    "- List files: Use rg --files\n"
    "\n"
    "Use specialized tools instead of exec_command for file operations whenever possible.\n"
    "\n"
    "## Browser tools\n"
    "The in-app browser lets you open, navigate, inspect, test, click, type, and screenshot local web targets (localhost, file:// URLs, etc.). Use this after making frontend changes to verify they work.\n"
    "\n"
    "## MCP tools\n"
    "MCP (Model Context Protocol) servers provide access to external resources. Use list_mcp_resources and read_mcp_resource for accessing APIs, databases, and other external data.\n"
    "\n"
    "General rules for calling tools:\n"
    "- You can call multiple tools in a single response. If tools are independent and have no data dependencies, make all independent tool calls in parallel to be efficient.\n"
    "- If some tool calls depend on previous calls (e.g., you need to read a file before editing it, or check a directory before creating one), call them sequentially.\n"
    "- Never use placeholders or guess missing parameters in tool calls.\n"
    "- After each tool result, decide the next action and take it immediately. Do not stop to narrate what you will do next — just do it.\n"
    "- When you receive results from a tool, process them and immediately decide: either call the next tool or respond to the user with the outcome.\n"
    "- If a tool call fails due to an error, read the error message and try to fix the issue. Do not give up on the first failure.\n"
    "\n"
    "ULTRA IMPORTANT: Never respond to the user with just a plan or a description of what you will do. Always execute the first step immediately after describing your plan. If you catch yourself starting with \"I will\" or \"Let me\" followed by describing actions without calling a tool, you are doing it wrong. Call a tool now.\n"
    "\n"
    "# Doing multiple tool calls\n"
    "\n"
    "Examples of correct behavior:\n"
    "\n"
    "GOOD (parallel independent calls):\n"
    "user: Check the git status and read the main.py file\n"
    "assistant: I will check both.\n"
    "Tool calls:\n"
    "[{\"id\": \"call_1\", \"function\": {\"name\": \"exec_command\", \"arguments\": \"{\\\"cmd\\\": \\\"git status\\\", \\\"workdir\\\": \\\"/project\\\"}\"}, ...},\n"
    " {\"id\": \"call_2\", \"function\": {\"name\": \"read\", \"arguments\": \"{\\\"path\\\": \\\"/project/main.py\\\"}\"}, ...}]\n"
    "\n"
    "GOOD (sequential dependent calls):\n"
    "user: Fix the bug in main.py\n"
    "assistant: Let me read the file first.\n"
    "Tool calls:\n"
    "[{\"id\": \"call_1\", \"function\": {\"name\": \"exec_command\", \"arguments\": \"{\\\"cmd\\\": \\\"cat main.py\\\"}\"}, ...}]\n"
    "...user provides result...\n"
    "assistant: I see the issue. Let me fix it.\n"
    "Tool calls:\n"
    "[{\"id\": \"call_2\", \"function\": {\"name\": \"apply_patch\", \"arguments\": \"{\\\"file_path\\\": \\\"/project/main.py\\\", \\\"old_string\\\": \\\"buggy code\\\", \\\"new_string\\\": \\\"fixed code\\\"}\"}, ...}]\n"
    "\n"
    "BAD (describing without doing):\n"
    "user: Deploy the app to production\n"
    "assistant: I will first check the build, then deploy it, then verify it works.\n"
    "[no tool calls — WRONG! The assistant should call a tool immediately]\n"
    "\n"
    "# Code References\n"
    "When referencing specific functions or pieces of code include the pattern `file_path:line_number` to allow the user to easily navigate to the source code location.\n"
    "\n"
    "<example>\n"
    "user: Where are errors from the client handled?\n"
    "assistant: Errors are handled in the connectToServer function at src/services/process.ts:712.\n"
    "</example>\n"
    "\n"
    "# Call format\n"
    "When you need to call tools, use this EXACT format — the system parses this JSON to execute your tool calls:\n"
    "Tool calls:\n"
    "```json\n"
    "[{\"id\": \"call_1\", \"type\": \"function\", \"function\": {\"name\": \"tool_name\", \"arguments\": \"{...}\"}}]\n"
    "```\n"
    "\n"
    "You can call multiple tools by listing multiple objects in the JSON array. Each call must have a unique id.\n"
)






# Cache the tool description text so we don't rebuild it every request
_TOOL_DESC_CACHE = "__UNSET__"

def _get_tool_desc(incoming_tools: list | None) -> str:
    global _TOOL_DESC_CACHE
    if _TOOL_DESC_CACHE != "__UNSET__":
        return _TOOL_DESC_CACHE
    if not incoming_tools:
        return ""
    lines = []
    for t in incoming_tools:
        f = t.get("function", {})
        name = f.get("name", "?")
        desc = f.get("description", "").split(". ")[0]
        params = f.get("parameters", {})
        props = params.get("properties", {}) if isinstance(params, dict) else {}
        arg_names = ", ".join(props.keys()) if props else ""
        if arg_names:
            lines.append(f"  {name}({arg_names}): {desc}")
        else:
            lines.append(f"  {name}: {desc}")
    tool_desc = (
        "\n[AVAILABLE TOOLS]\n"
        + "\n".join(lines) + "\n"
        + "Call via: Tool calls:\n```json\n[{\"id\": \"call_...\", \"type\": \"function\", \"function\": {\"name\": \"TOOL_NAME\", \"arguments\": \"{...}\"}}]\n```\n"
        "TOOL CALL REQUIRED. DO NOT DESCRIBE — CALL NOW."
    )
    _TOOL_DESC_CACHE = tool_desc
    return tool_desc


def _build_qoder_messages(template_messages: list, incoming_messages: list[dict],
                          prompt: str, tools_enabled: bool, image_urls: list[str],
                          incoming_tools: list | None = None) -> list[dict]:
    rebuilt: list[dict] = []
    
    # Always inject tool instruction as the first system message.
    if tools_enabled:
        # Inject tool definitions into the system prompt itself, not just
        # the last user message.  This ensures tools are visible even if
        # Qoder truncates the messages array from the end.
        sys_content = TOOL_INSTRUCTION
        tool_text = _get_tool_desc(incoming_tools)
        if tool_text:
            sys_content += "\n\n" + tool_text
        rebuilt.append({"role": "system", "content": sys_content})
    
    # Include template system messages if Codex hasn't already provided one.
    keep_sys = not _has_role(incoming_messages, "system")
    if keep_sys:
        for msg in template_messages:
            if msg.get("role") == "system": rebuilt.append(copy.deepcopy(msg))

    # Track system message content hashes to dedup
    seen_sys = set()
    for msg in rebuilt:
        if msg.get("role") == "system":
            seen_sys.add(msg.get("content", ""))

    for i, message in enumerate(incoming_messages):
        allow = tools_enabled and _has_resolved_tool_response(incoming_messages, i)
        
        # Skip system messages already present in rebuilt (prevents
        # TOOL_INSTRUCTION from being duplicated every round).
        if message.get("role") == "system":
            mc = message.get("content", "")
            if mc in seen_sys:
                continue
            seen_sys.add(mc)
        
        converted_msg = _convert_openai_contents_to_qoder(message)

        contents = converted_msg.get("contents") if isinstance(converted_msg.get("contents"), list) else None
        has_images = False
        if contents:
            for c in contents:
                if isinstance(c, dict) and c.get("type") in ("image_url", "input_image"):
                    has_images = True
                    break

        if has_images and converted_msg.get("role") == "user":
            img_text = _normalize_message_text(converted_msg)
            rebuilt_msg = {
                "role": "user",
                "content": img_text,
                "contents": copy.deepcopy(contents),
                "response_meta": _blank_response_meta(),
                "reasoning_content_signature": "",
            }
            rebuilt.append(rebuilt_msg)
            continue

        converted = _convert_incoming_message(converted_msg, tools_enabled, allow)
        if converted: rebuilt.append(converted)

    if not rebuilt and prompt.strip():
        rebuilt.append(_build_user_message(prompt))
    
    # ---- Truncate long conversations ----
    # Qwen loses tool-calling ability in contexts >50 messages.
    # Keep system messages + last 10 turns (model/assistant/user groups).
    if len(rebuilt) > 25:
        sys_indices = [j for j, m in enumerate(rebuilt) if m.get("role") == "system"]
        non_sys = [m for j, m in enumerate(rebuilt) if m.get("role") != "system"]
        keep = non_sys[-18:] if len(non_sys) > 18 else non_sys
        truncated = []
        for j in sys_indices:
            truncated.append(copy.deepcopy(rebuilt[j]))
        if sys_indices and sys_indices[-1] + 1 < len(rebuilt) - len(keep):
            truncated.append({
                "role": "system",
                "content": "[Earlier steps omitted. Continue with the current task.]"
            })
        truncated.extend(keep)
        rebuilt = truncated
    
    # ---- Inject tool definitions into the LAST user message text ----
    # Only inject if the message doesn't already have them (prevents duplicate
    # accumulation across rounds).
    if tools_enabled and len(rebuilt) >= 2:
        tool_text = _get_tool_desc(incoming_tools)
        if not tool_text:
            return rebuilt
        for j in range(len(rebuilt) - 1, -1, -1):
            if rebuilt[j].get("role") == "user":
                msg = rebuilt[j]
                existing = _normalize_message_text(msg)
                # Skip if tools already injected (check for [AVAILABLE TOOLS] marker)
                if "[AVAILABLE TOOLS]" in existing:
                    break
                enriched = existing + "\n\n" + tool_text
                msg["content"] = enriched
                if msg.get("contents") and isinstance(msg["contents"], list):
                    for c in msg["contents"]:
                        if isinstance(c, dict) and c.get("type") == "text":
                            c["text"] = enriched
                            break
                break
    
    return rebuilt


def _extract_delta(data_line: str) -> dict:
    try:
        wrapper = json.loads(data_line)
        inner = wrapper.get("body", "")
        if not inner:
            return {}
        if isinstance(inner, str) and inner.strip() == "[DONE]":
            return {}
        inner_json = json.loads(inner) if isinstance(inner, str) else inner
        for ch in inner_json.get("choices", []):
            delta = ch.get("delta", {})
            role = delta.get("role", "")
            content = delta.get("content", "")
            reasoning = delta.get("reasoning_content", "")
            tc = delta.get("tool_calls")
            if role or content or reasoning or (tc and len(tc) > 0):
                return {"role": role, "content": content, "reasoning_content": reasoning, "tool_calls": tc}
    except Exception:
        pass
    return {}


def _make_chunk(req_id: str, created: int, model: str) -> dict:
    return {"id": req_id, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": None}]}

def _sse_line(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class ToolCallAccumulator:
    def __init__(self): self.calls: list[dict] = []
    def append(self, delta_calls: list[dict]):
        for dc in delta_calls:
            idx = dc.get("index", len(self.calls))
            while len(self.calls) <= idx:
                self.calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            ex = self.calls[idx]
            if dc.get("id"): ex["id"] = dc["id"]
            if dc.get("type"): ex["type"] = dc["type"]
            df = dc.get("function", {})
            if df.get("name"): ex["function"]["name"] = df["name"]
            if df.get("arguments"): ex["function"]["arguments"] += df["arguments"]
    def is_empty(self) -> bool: return not self.calls
    def snapshot(self) -> list[dict]: return copy.deepcopy(self.calls)


def _build_usage(prompt_text: str, completion_text: str) -> dict:
    pt = _estimate_tokens(prompt_text)
    ct = _estimate_tokens(completion_text)
    return {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}


def _usage_chunk(req_id: str, created: int, model: str, usage: dict) -> str:
    return _sse_line({"id": req_id, "object": "chat.completion.chunk", "created": created,
                       "model": model, "choices": [], "usage": usage})


# ─── API Endpoints ───

@app.get("/v1/models")
async def list_models():
    models = [{"id": m["id"], "object": "model", "created": 0, "owned_by": m.get("owned_by", "qoder")}
              for m in pool.model_list]
    return {"object": "list", "data": models}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    t0 = time.time()
    try:
        req = await request.json()
    except:
        return JSONResponse({"error": {"message": "Invalid JSON", "type": "qoder_error"}}, status_code=400)
    # Log top-level keys of the incoming request
    req_keys = list(req.keys())
    extra_info = {}
    for k in ("tool_choice", "parallel_tool_calls", "stream_options", "metadata", "response_format", "user"):
        if k in req:
            extra_info[k] = req[k]
    logger.info("Request keys=%s extras=%s", req_keys, extra_info)

    account = pool.get_account()
    if not account or not account.session:
        return JSONResponse({"error": {"message": "No available account", "type": "qoder_error"}}, status_code=503)

    sess = account.session
    stream = req.get("stream", False)
    model = req.get("model", "lite")
    messages = req.get("messages", [])
    include_usage = (req.get("stream_options") or {}).get("include_usage", False) if stream else False

    n_msgs = len(messages)
    tools_list = req.get("tools")
    # Log the request detail
    has_tool_choice = "tool_choice" in req
    tc_val = req.get("tool_choice") if has_tool_choice else "-"
    has_parallel = "parallel_tool_calls" in req
    has_temperature = "temperature" in req
    logger.info("POST model=%s msgs=%d tools=%d stream=%s tool_choice=%s parallel=%s temp=%s",
                model, n_msgs, len(tools_list) if tools_list else 0, stream,
                tc_val, has_parallel, has_temperature)

    processed_messages, image_urls = await _process_images(sess, messages)

    body = copy.deepcopy(pool._template_base) if pool._template_base else _load_template()
    nid = str(uuid.uuid4())
    body["request_id"] = nid
    body["chat_record_id"] = nid
    body["request_set_id"] = str(uuid.uuid4())
    body["session_id"] = str(uuid.uuid4())
    body["stream"] = True
    body["aliyun_user_type"] = sess.identity.user_type


    mc = body.get("model_config", {})
    mc["key"] = model
    mc["max_input_tokens"] = pool.default_context_length
    mc["context_config"] = {
        "1M": {"token_count": 1_000_000, "is_default": pool.default_context_length >= 1_000_000},
        "200K": {"token_count": 200_000},
        "400K": {"token_count": 400_000},
    }
    if image_urls:
        mc["is_vl"] = True

    # Forward OpenAI sampling params to Qoder body
    if "parallel_tool_calls" in req:
        mc["parallel_tool_calls"] = req["parallel_tool_calls"]
    for p in ("temperature", "top_p", "max_tokens", "max_completion_tokens"):
        if p in req:
            mc[p] = req[p]

    params = body.get("parameters", {})
    params["context_length"] = pool.default_context_length
    if "tool_choice" in req:
        params["tool_choice"] = req["tool_choice"]
    if "parallel_tool_calls" in req:
        params["parallel_tool_calls"] = req["parallel_tool_calls"]
    for p in ("temperature", "top_p", "max_tokens", "max_completion_tokens"):
        if p in req:
            params[p] = req[p]

    # reasoning_effort handling
    # Codex sends reasoning_effort ("low"|"medium"|"high") but Qoder doesn't
    # understand this field.  More importantly, "high" reasoning on Qoder
    # consistently hits a ~66s upstream gateway timeout.  We explicitly force
    # is_reasoning=false everywhere to prevent the long-thinking dead zone
    # and keep tool-call behavior reliable.
    reasoning_effort = req.get("reasoning_effort")
    if reasoning_effort:
        logger.info("reasoning_effort=%s — forcing is_reasoning=false for Qoder compatibility", reasoning_effort)
    mc["is_reasoning"] = False
    extra = body.get("chat_context", {}).get("extra", {})
    if isinstance(extra, dict):
        mc2 = extra.get("modelConfig")
        if isinstance(mc2, dict):
            mc2["is_reasoning"] = False

    biz = body.get("business", {})
    biz["id"] = str(uuid.uuid4())
    biz["begin_at"] = int(time.time() * 1000)

    prompt = _extract_latest_user_prompt(processed_messages)
    ctx = body.get("chat_context", {})
    if isinstance(ctx.get("text"), dict): ctx["text"]["text"] = prompt
    extra = ctx.get("extra", {})
    if isinstance(extra.get("originalContent"), dict): extra["originalContent"]["text"] = prompt
    biz["name"] = prompt[:30] if len(prompt) > 30 else prompt

    if image_urls:
        ctx["imageUrls"] = image_urls

    incoming_tools = req.get("tools")
    tools_enabled = isinstance(incoming_tools, list) and len(incoming_tools) > 0

    # Set chatPrompt as a tool-use reminder — this field lives at the Qoder body
    # top level, NOT in the messages array, so it won't get truncated in long convs.
    if tools_enabled:
        ctx["chatPrompt"] = (
            "Call tools immediately. Do not describe — do it."
        )
    body["messages"] = _build_qoder_messages(body.get("messages", []), processed_messages, prompt, tools_enabled, image_urls, incoming_tools)
    if tools_enabled:
        body["tools"] = copy.deepcopy(incoming_tools)

    extra_headers = {"x-model-key": model, "x-model-source": mc.get("source", "system")}
    req_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

    # Log the full request body for debugging Qoder tool-call behavior
    body_preview = json.dumps(body, ensure_ascii=False)
    logger.info("Qoder body (req=%s, msgs=%d, tools=%d): %s",
                req_id, len(body.get("messages", [])),
                len(body.get("tools", [])),
                body_preview[:2000])

    if stream:
        return StreamingResponse(
            _stream_response(sess, body, extra_headers, req_id, created, model, tools_enabled, prompt, include_usage, t0),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"})
    else:
        return await _non_stream_response(sess, body, extra_headers, req_id, created, model, tools_enabled, prompt, t0)


def _load_template() -> dict:
    candidates = [Path(__file__).parent / "baseprompt.json", Path(__file__).parent.parent / "baseprompt.json"]
    for p in candidates:
        if p.exists():
            text = p.read_text()
            for ph in ["{UUID1}", "{UUID2}", "{UUID3}", "{UUID4}", "{UUID5}"]:
                text = text.replace(ph, str(uuid.uuid4()))
            text = text.replace("{TIME1}", str(int(time.time() * 1000)))
            return json.loads(text)
    raise FileNotFoundError("baseprompt.json not found")


async def _stream_response(sess, body, extra_headers, req_id, created, model, tools_enabled, prompt, include_usage, t0):
    """Async SSE streaming response with mid-stream tool-call detection.

    Qoder's model may output tool calls as text (in the 'Tool calls:'
    format) rather than as structured delta.tool_calls.  We scan every
    chunk of text for this pattern and convert it to structured tool_calls
    on the fly so Codex gets the correct finish_reason and tool call data.
    """
    tc_acc = ToolCallAccumulator()
    pending_role = "assistant"
    emitted_chunk = False
    full_content = ""

    def _with_tc_indices(raw_tc):
        indexed = []
        for i, call in enumerate(raw_tc):
            c = copy.deepcopy(call)
            if "index" not in c: c["index"] = i
            indexed.append(c)
        return indexed

    def _emit_chunk(content=None, role=None, tc=None):
        chunk = _make_chunk(req_id, created, model)
        delta = chunk["choices"][0]["delta"]
        if role: delta["role"] = role
        if content: delta["content"] = content
        if tc: delta["tool_calls"] = tc
        return _sse_line(chunk)

    try:
        pending_buf = ""
        tc_mode = False
        text_before_tc = ""
        after_tc = ""
        line_count = 0
        
        async for line in open_stream(sess, QODER_CHAT_URL, body, extra_headers):
            if not line.startswith("data:"): continue
            line_count += 1
            raw_data = line[5:].strip()
            if line_count <= 5 and "[DONE]" not in raw_data:
                logger.debug("RAW chunk #%d: %.400s", line_count, raw_data)
            delta = _extract_delta(raw_data)
            if not delta:
                logger.debug("_extract_delta empty: %.200s", raw_data)
                continue

            d_role, d_content, d_reasoning = delta.get("role", ""), delta.get("content", ""), delta.get("reasoning_content", "")
            d_tc = delta.get("tool_calls")

            if d_role: pending_role = d_role

            if d_reasoning and not d_content and not d_tc:
                full_content += d_reasoning
                continue

            # Structured tool_calls from delta — direct path
            if d_tc and len(d_tc) > 0:
                if pending_buf:
                    full_content += pending_buf
                    yield _emit_chunk(content=pending_buf, role=pending_role if not emitted_chunk else None)
                    emitted_chunk = True
                    pending_buf = ""
                    tc_mode = False
                tc_acc.append(d_tc)
                yield _emit_chunk(role=pending_role if not emitted_chunk else None, tc=_with_tc_indices(d_tc))
                emitted_chunk = True
                continue

            if not d_content: continue

            if not tools_enabled:
                full_content += d_content
                yield _emit_chunk(content=d_content, role=pending_role if not emitted_chunk else None)
                emitted_chunk = True
                continue

            # Text-based tool call detection
            # Model outputs "Tool calls:\n```json\n[...]```" as text content
            pending_buf += d_content

            if not tc_mode:
                tc_idx = pending_buf.find("Tool calls:")
                if tc_idx >= 0:
                    tc_mode = True
                    text_before_tc = pending_buf[:tc_idx]
                    after_tc = pending_buf[tc_idx + 11:]
                elif len(pending_buf) >= 200:
                    full_content += pending_buf
                    yield _emit_chunk(content=pending_buf, role=pending_role if not emitted_chunk else None)
                    emitted_chunk = True
                    pending_buf = ""
            else:
                after_tc += d_content
                json_start = after_tc.find("[")
                if json_start >= 0:
                    depth = 0
                    json_end = -1
                    for k in range(json_start, len(after_tc)):
                        if after_tc[k] == '[':
                            depth += 1
                        elif after_tc[k] == ']':
                            depth -= 1
                            if depth == 0:
                                json_end = k + 1
                                break
                    if json_end > 0:
                        try:
                            calls = json.loads(after_tc[json_start:json_end])
                            norm = _normalize_tool_calls(calls)
                            if norm:
                                if text_before_tc.strip():
                                    full_content += text_before_tc
                                    yield _emit_chunk(content=text_before_tc, role=pending_role if not emitted_chunk else None)
                                    emitted_chunk = True
                                tc_acc.append(norm)
                                yield _emit_chunk(role=pending_role if not emitted_chunk else None, tc=_with_tc_indices(norm))
                                emitted_chunk = True
                                rest = after_tc[json_end:].strip().lstrip('```').strip()
                                if rest.startswith('\n'):
                                    rest = rest[1:].strip()
                                if rest:
                                    full_content += rest
                                    yield _emit_chunk(content=rest)
                                    emitted_chunk = True
                                pending_buf = ""
                                tc_mode = False
                                continue
                        except json.JSONDecodeError:
                            pass
                if len(after_tc) > 8000:
                    all_text = text_before_tc + "Tool calls:" + after_tc
                    full_content += all_text
                    yield _emit_chunk(content=all_text, role=pending_role if not emitted_chunk else None)
                    emitted_chunk = True
                    pending_buf = ""
                    tc_mode = False

        if pending_buf:
            if tc_mode:
                all_text = text_before_tc + "Tool calls:" + after_tc
                full_content += all_text
                yield _emit_chunk(content=all_text, role=pending_role if not emitted_chunk else None)
            else:
                full_content += pending_buf
                yield _emit_chunk(content=pending_buf, role=pending_role if not emitted_chunk else None)
            emitted_chunk = True
    except asyncio.CancelledError:
        logger.info("Stream cancelled (req=%s)", req_id)
        return
    except Exception as exc:
        err_msg = str(exc)[:200]
        is_disconnect = "peer closed connection" in err_msg or "incomplete chunked" in err_msg
        if is_disconnect:
            logger.warning("Qoder DISCONNECTED after %.1fs for req=%s: %s",
                           time.time() - t0, req_id, err_msg)
        else:
            logger.error("Stream error (req=%s): %s", req_id, err_msg, exc_info=True)

        # The pending_buf variable from the try block may hold unflushed content.
        # It's not available in this except handler, so we just check full_content.
        # If the stream was empty (no content, no tool calls) and the upstream
        # disconnected, emit an error event so Codex knows to retry rather than
        # thinking the task is done.
        had_no_output = not emitted_chunk and not full_content and tc_acc.is_empty()
        if had_no_output and is_disconnect:
            err_chunk = _make_chunk(req_id, created, model)
            err_chunk["error"] = {"message": err_msg[:200], "type": "upstream_disconnect"}
            yield _sse_line(err_chunk)

        final = _make_chunk(req_id, created, model)
        final["choices"][0]["finish_reason"] = "stop" if not (had_no_output and is_disconnect) else "length"
        final["choices"][0]["delta"] = {}
        yield _sse_line(final)
        if include_usage:
            yield _usage_chunk(req_id, created, model, _build_usage(prompt, full_content))
        yield "data: [DONE]\n\n"

        tag = "DISCONNECT" if is_disconnect else "ERR"
        content_preview = full_content[-200:].replace("\n", "\\n") if full_content else "(none)"
        logger.info("Stream %s req=%s tokens=%d elapsed=%.1fs content=%s",
                    tag, req_id, _estimate_tokens(full_content), time.time() - t0, content_preview)
        return

    # ---- Normal end of stream ----
    # Always scan full_content for missed text-based tool calls.
    # This catches cases where 'Tool calls:' appeared but the
    # streaming handler's buffer didn't capture the full JSON.
    if tools_enabled:
        parsed_tc = _parse_tool_calls_text(full_content)
        if parsed_tc:
            tc_acc.append(parsed_tc)
            if not emitted_chunk:
                yield _emit_chunk(tc=_with_tc_indices(parsed_tc))
                emitted_chunk = True

    finish_reason = "tool_calls" if not tc_acc.is_empty() else "stop"
    final = _make_chunk(req_id, created, model)
    final["choices"][0]["finish_reason"] = finish_reason
    final["choices"][0]["delta"] = {}
    yield _sse_line(final)
    if include_usage:
        yield _usage_chunk(req_id, created, model, _build_usage(prompt, full_content))
    yield "data: [DONE]\n\n"

    elapsed = time.time() - t0
    content_preview = full_content[:160].replace("\n", "\\n") if full_content else "(none)"
    logger.info("Stream done req=%s finish=%s tokens=%d elapsed=%.1fs content=%s",
                req_id, finish_reason, _estimate_tokens(full_content), elapsed, content_preview)


async def _non_stream_response(sess, body, extra_headers, req_id, created, model, tools_enabled, prompt, t0):
    full_text = ""
    tc_acc = ToolCallAccumulator()
    errored = False

    try:
        line_count = 0
        async for line in open_stream(sess, QODER_CHAT_URL, body, extra_headers):
            if not line.startswith("data:"): continue
            line_count += 1
            raw_data = line[5:].strip()
            # Log first few non-[DONE] chunks to debug Qoder's response format
            if line_count <= 5 and "[DONE]" not in raw_data:
                logger.debug("RAW chunk #%d: %.400s", line_count, raw_data)
            delta = _extract_delta(raw_data)
            if not delta:
                logger.debug("_extract_delta returned empty for: %.200s", raw_data)
                continue
            if delta.get("content"): full_text += delta["content"]
            if delta.get("tool_calls") and len(delta["tool_calls"]) > 0: tc_acc.append(delta["tool_calls"])
    except asyncio.CancelledError:
        logger.info("Non-stream cancelled (req=%s)", req_id)
        return JSONResponse(None, status_code=499)
    except Exception as exc:
        err_msg = str(exc)[:200]
        is_disconnect = "peer closed connection" in err_msg or "incomplete chunked" in err_msg
        if is_disconnect:
            logger.warning("Qoder DISCONNECTED (non-stream) for req=%s: %s", req_id, err_msg)
        else:
            logger.error("Non-stream error (req=%s): %s", req_id, err_msg, exc_info=True)
        errored = True

    fallback_tc = None
    if tc_acc.is_empty() and tools_enabled:
        fallback_tc = _parse_tool_calls_text(full_text)

    msg = {"role": "assistant"}
    if fallback_tc:
        msg["content"] = None
        msg["tool_calls"] = fallback_tc
    elif not full_text and not tc_acc.is_empty():
        msg["content"] = None
    else:
        msg["content"] = full_text
    if not tc_acc.is_empty():
        msg["tool_calls"] = tc_acc.snapshot()

    finish_reason = "tool_calls" if (not tc_acc.is_empty() or fallback_tc) else "stop"
    usage = _build_usage(prompt, full_text)

    elapsed = time.time() - t0
    content_preview = full_text[:160].replace("\n", "\\n") if full_text else "(none)"
    tag = " DISCONNECT" if errored and "peer closed" in str(exc).lower() else (" ERR" if errored else "")
    logger.info("Non-stream done req=%s finish=%s tokens=%d elapsed=%.1fs%s content=%s",
                req_id, finish_reason, usage["total_tokens"], elapsed, tag, content_preview)

    return JSONResponse({"id": req_id, "object": "chat.completion", "created": created, "model": model,
                          "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
                          "usage": usage})


# ─── Admin API ───

@app.get("/admin/api/status")
async def admin_status():
    pool.refresh_all_quotas()
    return pool.get_status_summary()


@app.post("/admin/api/accounts")
async def admin_add_account(request: Request):
    data = await request.json()
    name = data.get("name", "Unnamed")
    pat = data.get("pat", "")
    enabled = data.get("enabled", True)
    if not pat:
        return JSONResponse({"ok": False, "error": "PAT is required"}, status_code=400)
    acc = pool.add_account(name, pat, enabled)
    if enabled:
        pool.init_account(acc)
    return {"ok": True, "account": {"name": acc.name, "enabled": acc.enabled, "is_expired": acc.is_expired}}


@app.delete("/admin/api/accounts/{index}")
async def admin_remove_account(index: int):
    ok = pool.remove_account(index)
    return {"ok": ok}


@app.post("/admin/api/accounts/{index}/toggle")
async def admin_toggle_account(index: int):
    ok = pool.toggle_account(index)
    return {"ok": ok}


@app.post("/admin/api/accounts/{index}/refresh")
async def admin_refresh_account(index: int):
    if 0 <= index < len(pool.accounts):
        acc = pool.accounts[index]
        if acc.session:
            pool.refresh_quota(acc)
            return {"ok": True}
    return {"ok": False}


@app.post("/admin/api/strategy")
async def admin_set_strategy(request: Request):
    data = await request.json()
    strategy = data.get("strategy", "fill")
    if strategy not in ("round_robin", "fill"):
        return JSONResponse({"ok": False, "error": "Invalid strategy"}, status_code=400)
    pool.set_strategy(strategy)
    return {"ok": True, "strategy": strategy}


# ─── WebUI ───

@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/", response_class=HTMLResponse)
async def admin_ui():
    return HTMLResponse(content=WEBUI_HTML)


WEBUI_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QPA Manager</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0f1117;--surface:#1a1d27;--surface2:#232736;--border:#2e3347;--text:#e1e4ed;--text2:#8b8fa3;
--accent:#6c5ce7;--accent2:#a29bfe;--green:#00b894;--yellow:#fdcb6e;--red:#e17055;--blue:#0984e3}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.container{max-width:1200px;margin:0 auto;padding:24px}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:32px;padding-bottom:16px;border-bottom:1px solid var(--border)}
header h1{font-size:24px;font-weight:700;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
header .subtitle{color:var(--text2);font-size:13px;margin-top:2px}
.badge{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:20px;font-size:12px;font-weight:500}
.badge.green{background:rgba(0,184,148,.15);color:var(--green)}
.badge.red{background:rgba(225,112,85,.15);color:var(--red)}
.badge.yellow{background:rgba(253,203,110,.15);color:var(--yellow)}
.badge.blue{background:rgba(9,132,227,.15);color:var(--blue)}
.strategy-bar{display:flex;align-items:center;gap:16px;margin-bottom:24px;padding:16px;background:var(--surface);border-radius:12px;border:1px solid var(--border)}
.strategy-bar label{font-size:14px;font-weight:500;color:var(--text2)}
.strat-btns{display:flex;gap:8px}
.strat-btn{padding:8px 20px;border:1px solid var(--border);background:transparent;color:var(--text2);border-radius:8px;cursor:pointer;font-size:13px;font-weight:500;transition:all .2s}
.strat-btn:hover{border-color:var(--accent);color:var(--text)}
.strat-btn.active{background:var(--accent);border-color:var(--accent);color:#fff}
.accounts{display:grid;gap:16px}
.account-card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px;transition:all .2s}
.account-card:hover{border-color:var(--accent);box-shadow:0 0 20px rgba(108,92,231,.08)}
.account-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.account-name{font-size:16px;font-weight:600;display:flex;align-items:center;gap:10px}
.account-actions{display:flex;gap:8px}
.btn-icon{width:32px;height:32px;border:1px solid var(--border);background:transparent;color:var(--text2);border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .2s}
.btn-icon:hover{border-color:var(--accent);color:var(--text);background:var(--surface2)}
.btn-icon.danger:hover{border-color:var(--red);color:var(--red)}
.account-info{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:14px}
.info-item{font-size:13px;color:var(--text2)}
.info-item span{color:var(--text);font-weight:500}
.quota-bar-wrap{margin-top:10px}
.quota-label{display:flex;justify-content:space-between;font-size:12px;color:var(--text2);margin-bottom:6px}
.quota-bar{height:8px;background:var(--surface2);border-radius:4px;overflow:hidden}
.quota-fill{height:100%;border-radius:4px;transition:width .6s ease}
.quota-fill.green{background:linear-gradient(90deg,var(--green),#55efc4)}
.quota-fill.yellow{background:linear-gradient(90deg,var(--yellow),#ffeaa7)}
.quota-fill.red{background:linear-gradient(90deg,var(--red),#fab1a0)}
.quota-details{margin-top:12px;font-size:12px;color:var(--text2);line-height:1.8}
.add-card{background:var(--surface);border:2px dashed var(--border);border-radius:14px;padding:20px;display:flex;flex-direction:column;gap:12px;transition:all .2s}
.add-card:hover{border-color:var(--accent)}
.add-card input{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:10px 14px;border-radius:8px;font-size:14px;outline:none;transition:border .2s}
.add-card input:focus{border-color:var(--accent)}
.add-card input::placeholder{color:var(--text2)}
.btn{padding:10px 20px;border:none;border-radius:8px;cursor:pointer;font-size:14px;font-weight:500;transition:all .2s}
.btn-primary{background:var(--accent);color:#fff}
.btn-primary:hover{background:var(--accent2)}
.stats-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:24px}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center}
.stat-card .value{font-size:28px;font-weight:700;margin-bottom:4px}
.stat-card .label{font-size:12px;color:var(--text2)}
.toast{position:fixed;bottom:24px;right:24px;padding:12px 20px;background:var(--surface2);border:1px solid var(--border);border-radius:10px;font-size:13px;z-index:999;opacity:0;transition:opacity .3s}
.toast.show{opacity:1}
</style>
</head>
<body>
<div class="container">
<header>
<div><h1>QPA Manager</h1><div class="subtitle">Qoder2API Pool & Quota Dashboard</div></div>
<div id="globalBadge"></div>
</header>

<div class="stats-row" id="statsRow"></div>

<div class="strategy-bar">
<label>Dispatch Strategy</label>
<div class="strat-btns">
<button class="strat-btn" data-strategy="fill" onclick="setStrategy('fill')">Fill (exhaust first)</button>
<button class="strat-btn" data-strategy="round_robin" onclick="setStrategy('round_robin')">Round Robin</button>
</div>
</div>

<div class="accounts" id="accountList"></div>

<div class="add-card">
<input id="newName" placeholder="Account name (optional)">
<input id="newPat" placeholder="PAT token (pt-...)">
<button class="btn btn-primary" onclick="addAccount()">+ Add Account</button>
</div>
</div>

<div class="toast" id="toast"></div>

<script>
let data = null;

async function loadStatus() {
  try {
    const r = await fetch('/admin/api/status');
    data = await r.json();
    render();
  } catch(e) { console.error(e); }
}

function render() {
  if (!data) return;
  const accounts = data.accounts || [];

  // Stats
  const totalRemaining = accounts.reduce((s,a) => s + (a.quota_remaining || 0), 0);
  const activeCount = accounts.filter(a => a.enabled && a.initialized && !a.is_expired).length;
  const totalRequests = accounts.reduce((s,a) => s + (a.request_count || 0), 0);
  document.getElementById('statsRow').innerHTML = `
    <div class="stat-card"><div class="value" style="color:var(--green)">${totalRemaining}</div><div class="label">Total Remaining</div></div>
    <div class="stat-card"><div class="value" style="color:var(--accent2)">${activeCount}/${accounts.length}</div><div class="label">Active Accounts</div></div>
    <div class="stat-card"><div class="value" style="color:var(--yellow)">${totalRequests}</div><div class="label">Total Requests</div></div>
  `;

  // Global badge
  const gb = document.getElementById('globalBadge');
  if (data.strategy === 'fill') gb.innerHTML = '<span class="badge blue">Fill Mode</span>';
  else gb.innerHTML = '<span class="badge blue">Round Robin</span>';

  // Strategy buttons
  document.querySelectorAll('.strat-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.strategy === data.strategy);
  });

  // Accounts
  const list = document.getElementById('accountList');
  list.innerHTML = accounts.map((a, i) => {
    const statusBadge = a.is_expired ? '<span class="badge red">Expired</span>'
      : a.is_quota_exceeded ? '<span class="badge yellow">Quota Exceeded</span>'
      : a.enabled ? '<span class="badge green">Active</span>'
      : '<span class="badge" style="background:var(--surface2);color:var(--text2)">Disabled</span>';

    const q = a.quota_details || [];
    const mainQuota = q[0];
    let quotaHtml = '';
    if (mainQuota) {
      const pct = mainQuota.limit > 0 ? ((mainQuota.limit - mainQuota.remaining) / mainQuota.limit * 100) : 0;
      const remaining = mainQuota.remaining;
      const limit = mainQuota.limit;
      const color = pct > 80 ? 'red' : pct > 50 ? 'yellow' : 'green';
      quotaHtml = `
        <div class="quota-bar-wrap">
          <div class="quota-label"><span>${mainQuota.name || 'Quota'}</span><span>${remaining} / ${limit}</span></div>
          <div class="quota-bar"><div class="quota-fill ${color}" style="width:${pct}%"></div></div>
        </div>`;
    }

    let detailHtml = '';
    if (q.length > 0) {
      detailHtml = '<div class="quota-details">' + q.map(qi =>
        `<div>${qi.name}: ${qi.status_text || (qi.remaining + '/' + qi.limit)} ${qi.description ? '(' + qi.description + ')' : ''}</div>`
      ).join('') + '</div>';
    }

    const resetAt = a.next_reset ? new Date(a.next_reset).toLocaleString('zh-CN') : '-';

    return `<div class="account-card">
      <div class="account-header">
        <div class="account-name">${a.name} ${statusBadge}</div>
        <div class="account-actions">
          <button class="btn-icon" onclick="refreshAccount(${i})" title="Refresh">&#x21bb;</button>
          <button class="btn-icon" onclick="toggleAccount(${i})" title="Toggle">${a.enabled ? '&#x2715;' : '&#x2713;'}</button>
          <button class="btn-icon danger" onclick="removeAccount(${i})" title="Delete">&#x1f5d1;</button>
        </div>
      </div>
      <div class="account-info">
        <div class="info-item">User: <span>${a.user_name || '-'}</span></div>
        <div class="info-item">Plan: <span>${a.user_tag || a.plan || '-'}</span></div>
        <div class="info-item">Email: <span>${a.email || '-'}</span></div>
        <div class="info-item">Requests: <span>${a.request_count}</span></div>
        <div class="info-item">PAT: <span style="font-family:monospace;font-size:11px">${a.pat_masked}</span></div>
        <div class="info-item">Reset: <span>${resetAt}</span></div>
      </div>
      ${quotaHtml}${detailHtml}
    </div>`;
  }).join('');
}

async function setStrategy(s) {
  await fetch('/admin/api/strategy', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({strategy:s})});
  toast('Strategy updated to ' + s);
  loadStatus();
}

async function addAccount() {
  const name = document.getElementById('newName').value.trim() || 'Account ' + (data.accounts.length + 1);
  const pat = document.getElementById('newPat').value.trim();
  if (!pat) { toast('PAT is required'); return; }
  await fetch('/admin/api/accounts', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name, pat})});
  document.getElementById('newName').value = '';
  document.getElementById('newPat').value = '';
  toast('Account added');
  loadStatus();
}

async function removeAccount(i) {
  if (!confirm('Remove this account?')) return;
  await fetch('/admin/api/accounts/' + i, {method:'DELETE'});
  toast('Account removed');
  loadStatus();
}

async function toggleAccount(i) {
  await fetch('/admin/api/accounts/' + i + '/toggle', {method:'POST'});
  toast('Account toggled');
  loadStatus();
}

async function refreshAccount(i) {
  await fetch('/admin/api/accounts/' + i + '/refresh', {method:'POST'});
  toast('Quota refreshed');
  loadStatus();
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2000);
}

loadStatus();
setInterval(loadStatus, 30000);
</script>
</body>
</html>
"""
