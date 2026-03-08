"""OpenAI-compatible proxy server for chat.z.ai + Toolify-style function calling."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import secrets
import string
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpcore
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from main import ZaiClient
from claude_compat import (
    claude_messages_to_openai,
    claude_tools_to_openai,
    claude_tool_choice_prompt,
    make_claude_id,
    build_tool_call_blocks,
    build_non_stream_response,
    sse_message_start,
    sse_ping,
    sse_content_block_start,
    sse_content_block_delta,
    sse_content_block_stop,
    sse_message_delta,
    sse_message_stop,
    sse_error,
)

# ── Logging ──────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
HTTP_DEBUG = os.getenv("HTTP_DEBUG", "0") == "1"
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("zai.openai")
if not HTTP_DEBUG:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# ── Multi-Account Pool ───────────────────────────────────────────────

POOL_SIZE = int(os.getenv("POOL_SIZE", "3"))
TOKEN_MAX_AGE = int(os.getenv("TOKEN_MAX_AGE", "480"))  # seconds


class AccountInfo:
    """A single guest auth session."""

    __slots__ = ("token", "user_id", "username", "created_at", "active", "valid")

    def __init__(self, token: str, user_id: str, username: str) -> None:
        self.token = token
        self.user_id = user_id
        self.username = username
        self.created_at = time.time()
        self.active = 0  # number of in-flight requests
        self.valid = True

    def snapshot(self) -> dict[str, str]:
        return {"token": self.token, "user_id": self.user_id, "username": self.username}

    @property
    def age(self) -> float:
        return time.time() - self.created_at


class SessionPool:
    """Pool of guest accounts for concurrent, seamless use."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._accounts: list[AccountInfo] = []
        self._bg_task: asyncio.Task | None = None

    # ── internal ─────────────────────────────────────────────────────

    async def _new_account(self) -> AccountInfo:
        c = ZaiClient()
        try:
            d = await c.auth_as_guest()
            acc = AccountInfo(
                d["token"], d["id"], d.get("name") or d.get("email", "").split("@")[0]
            )
            logger.info(
                "Pool: +account uid=%s (total=%d)", acc.user_id, len(self._accounts) + 1
            )
            return acc
        finally:
            await c.close()

    async def _del_account(self, acc: AccountInfo) -> None:
        try:
            c = ZaiClient()
            c.token, c.user_id, c.username = acc.token, acc.user_id, acc.username
            await c.delete_all_chats()
            await c.close()
        except Exception:
            pass

    async def _maintain(self) -> None:
        """Background loop: prune expired accounts, top up pool."""
        while True:
            try:
                await asyncio.sleep(30)
                async with self._lock:
                    dead = [
                        a
                        for a in self._accounts
                        if (not a.valid or a.age > TOKEN_MAX_AGE) and a.active == 0
                    ]
                    for a in dead:
                        self._accounts.remove(a)
                        asyncio.create_task(self._del_account(a))
                    need = POOL_SIZE - len([a for a in self._accounts if a.valid])
                    for _ in range(max(0, need)):
                        try:
                            self._accounts.append(await self._new_account())
                        except Exception as e:
                            logger.warning("Pool maintain: %s", e)
            except asyncio.CancelledError:
                return
            except Exception:
                pass

    # ── public API ───────────────────────────────────────────────────

    async def initialize(self) -> None:
        async with self._lock:
            results = await asyncio.gather(
                *[self._new_account() for _ in range(POOL_SIZE)],
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, AccountInfo):
                    self._accounts.append(r)
                else:
                    logger.warning("Pool init failed: %s", r)
            if not self._accounts:
                self._accounts.append(await self._new_account())
            logger.info("Pool: ready with %d accounts", len(self._accounts))
        self._bg_task = asyncio.create_task(self._maintain())

    async def close(self) -> None:
        if self._bg_task:
            self._bg_task.cancel()
        for a in list(self._accounts):
            await self._del_account(a)
        self._accounts.clear()

    async def acquire(self) -> AccountInfo:
        """Get the least-busy valid account (creates one if needed)."""
        good = [a for a in self._accounts if a.valid and a.age < TOKEN_MAX_AGE]
        if not good:
            async with self._lock:
                good = [a for a in self._accounts if a.valid and a.age < TOKEN_MAX_AGE]
                if not good:
                    acc = await self._new_account()
                    self._accounts.append(acc)
                    good = [acc]
        acc = min(good, key=lambda a: a.active)
        acc.active += 1
        return acc

    def release(self, acc: AccountInfo) -> None:
        acc.active = max(0, acc.active - 1)

    async def report_failure(self, acc: AccountInfo) -> None:
        """Mark account invalid, schedule cleanup, add replacement."""
        acc.valid = False
        asyncio.create_task(self._del_account(acc))
        try:
            new = await self._new_account()
            async with self._lock:
                self._accounts.append(new)
        except Exception as e:
            logger.warning("Pool replace failed: %s", e)

    async def get_models(self) -> list | dict:
        acc = await self.acquire()
        c = ZaiClient()
        try:
            c.token, c.user_id, c.username = acc.token, acc.user_id, acc.username
            return await c.get_models()
        finally:
            self.release(acc)
            await c.close()

    # ── compat methods (called by request handlers) ──────────────────

    async def ensure_auth(self) -> None:
        """Ensure at least one valid account exists in the pool."""
        good = [a for a in self._accounts if a.valid]
        if not good:
            async with self._lock:
                good = [a for a in self._accounts if a.valid]
                if not good:
                    self._accounts.append(await self._new_account())

    def get_auth_snapshot(self) -> dict[str, str]:
        """Get auth snapshot from the least-busy valid account."""
        good = [a for a in self._accounts if a.valid and a.age < TOKEN_MAX_AGE]
        if not good:
            good = [a for a in self._accounts if a.valid]
        if not good:
            raise RuntimeError("No valid accounts in pool")
        acc = min(good, key=lambda a: a.active)
        acc.active += 1
        return acc.snapshot()

    def _release_by_user_id(self, user_id: str) -> None:
        """Release (decrement active) for the account matching user_id."""
        for a in self._accounts:
            if a.user_id == user_id:
                a.active = max(0, a.active - 1)
                return

    async def refresh_auth(self, failed_user_id: str | None = None) -> None:
        """Invalidate the failed account (if given) and create a fresh one."""
        if failed_user_id:
            for a in self._accounts:
                if a.user_id == failed_user_id:
                    a.valid = False
                    a.active = max(0, a.active - 1)
                    asyncio.create_task(self._del_account(a))
                    logger.info(
                        "SessionPool: invalidated failed account uid=%s", failed_user_id
                    )
                    break
        try:
            acc = await self._new_account()
            async with self._lock:
                self._accounts.append(acc)
            logger.info("SessionPool: auth refreshed, new user_id=%s", acc.user_id)
        except Exception as e:
            logger.warning("SessionPool: refresh_auth failed: %s", e)

    async def cleanup_chats(self) -> None:
        """Clean up chats for idle accounts to free concurrency slots."""
        for a in list(self._accounts):
            if a.valid and a.active == 0:
                try:
                    c = ZaiClient()
                    c.token, c.user_id, c.username = a.token, a.user_id, a.username
                    await c.delete_all_chats()
                    await c.close()
                except Exception:
                    pass


pool = SessionPool()

THINKING_SUFFIX = "-think"
NO_THINKING_SUFFIX = "-nothink"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await pool.initialize()
    yield
    await pool.close()


app = FastAPI(lifespan=lifespan)


# ── Toolify-style helpers ────────────────────────────────────────────


def _generate_trigger_signal() -> str:
    chars = string.ascii_letters + string.digits
    rand = "".join(secrets.choice(chars) for _ in range(4))
    return f"<Function_{rand}_Start/>"


GLOBAL_TRIGGER_SIGNAL = _generate_trigger_signal()


def _split_model_and_thinking(
    model: str, explicit_enable_thinking: Any = None
) -> tuple[str, bool]:
    base_model = model or "glm-5"
    enable_thinking = True

    if isinstance(explicit_enable_thinking, bool):
        enable_thinking = explicit_enable_thinking

    if base_model.endswith(THINKING_SUFFIX):
        return base_model[: -len(THINKING_SUFFIX)] or "glm-5", True

    if base_model.endswith(NO_THINKING_SUFFIX):
        return base_model[: -len(NO_THINKING_SUFFIX)] or "glm-5", False

    return base_model, enable_thinking


def _expand_model_variants(model_id: str) -> list[dict]:
    return [
        {
            "id": f"{model_id}{THINKING_SUFFIX}",
            "object": "model",
            "created": 0,
            "owned_by": "z.ai",
        },
        {
            "id": f"{model_id}{NO_THINKING_SUFFIX}",
            "object": "model",
            "created": 0,
            "owned_by": "z.ai",
        },
    ]


def _extract_text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text", "")))
        return " ".join(parts).strip()
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def _build_tool_call_index_from_messages(
    messages: list[dict],
) -> dict[str, dict[str, str]]:
    idx: dict[str, dict[str, str]] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        tcs = msg.get("tool_calls")
        if not isinstance(tcs, list):
            continue
        for tc in tcs:
            if not isinstance(tc, dict):
                continue
            tc_id = tc.get("id")
            fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
            name = str(fn.get("name", ""))
            args = fn.get("arguments", "{}")
            if not isinstance(args, str):
                try:
                    args = json.dumps(args, ensure_ascii=False)
                except Exception:
                    args = "{}"
            if isinstance(tc_id, str) and name:
                idx[tc_id] = {"name": name, "arguments": args}
    return idx


def _format_tool_result_for_ai(
    tool_name: str, tool_arguments: str, result_content: str
) -> str:
    return (
        "<tool_execution_result>\n"
        f"<tool_name>{tool_name}</tool_name>\n"
        f"<tool_arguments>{tool_arguments}</tool_arguments>\n"
        f"<tool_output>{result_content}</tool_output>\n"
        "</tool_execution_result>"
    )


def _format_assistant_tool_calls_for_ai(
    tool_calls: list[dict], trigger_signal: str
) -> str:
    blocks: list[str] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
        name = str(fn.get("name", "")).strip()
        if not name:
            continue
        args = fn.get("arguments", "{}")
        if isinstance(args, str):
            args_text = args
        else:
            try:
                args_text = json.dumps(args, ensure_ascii=False)
            except Exception:
                args_text = "{}"
        blocks.append(
            "<function_call>\n"
            f"<name>{name}</name>\n"
            f"<args_json>{args_text}</args_json>\n"
            "</function_call>"
        )
    if not blocks:
        return ""
    return (
        f"{trigger_signal}\n<function_calls>\n"
        + "\n".join(blocks)
        + "\n</function_calls>"
    )


def _preprocess_messages(messages: list[dict]) -> list[dict]:
    tool_idx = _build_tool_call_index_from_messages(messages)
    out: list[dict] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")

        if role == "tool":
            tc_id = msg.get("tool_call_id")
            content = _extract_text_from_content(msg.get("content", ""))
            info = tool_idx.get(
                tc_id, {"name": msg.get("name", "unknown_tool"), "arguments": "{}"}
            )
            out.append(
                {
                    "role": "user",
                    "content": _format_tool_result_for_ai(
                        info["name"], info["arguments"], content
                    ),
                }
            )
            continue

        if role == "assistant" and isinstance(msg.get("tool_calls"), list):
            xml_calls = _format_assistant_tool_calls_for_ai(
                msg["tool_calls"], GLOBAL_TRIGGER_SIGNAL
            )
            content = (
                _extract_text_from_content(msg.get("content", "")) + "\n" + xml_calls
            ).strip()
            out.append({"role": "assistant", "content": content})
            continue

        if role == "developer":
            cloned = dict(msg)
            cloned["role"] = "system"
            out.append(cloned)
            continue

        out.append(msg)

    return out


def _generate_function_prompt(tools: list[dict], trigger_signal: str) -> str:
    tool_lines: list[str] = []
    for i, t in enumerate(tools):
        if not isinstance(t, dict) or t.get("type") != "function":
            continue
        fn = t.get("function", {}) if isinstance(t.get("function"), dict) else {}
        name = str(fn.get("name", "")).strip()
        if not name:
            continue
        desc = str(fn.get("description", "")).strip() or "None"
        params = fn.get("parameters", {})
        required = params.get("required", []) if isinstance(params, dict) else []
        try:
            params_json = json.dumps(params, ensure_ascii=False)
        except Exception:
            params_json = "{}"

        tool_lines.append(
            f'{i + 1}. <tool name="{name}">\n'
            f"   Description: {desc}\n"
            f"   Required: {', '.join(required) if isinstance(required, list) and required else 'None'}\n"
            f"   Parameters JSON Schema: {params_json}"
        )

    tools_block = "\n\n".join(tool_lines) if tool_lines else "(no tools)"

    return (
        "You have access to tools.\n\n"
        "When you need to call tools, you MUST output exactly:\n"
        f"{trigger_signal}\n"
        "<function_calls>\n"
        "  <function_call>\n"
        "    <name>tool_name</name>\n"
        '    <args_json>{"arg":"value"}</args_json>\n'
        "  </function_call>\n"
        "</function_calls>\n\n"
        "Rules:\n"
        "1) args_json MUST be valid JSON object\n"
        "2) For multiple calls, output one <function_calls> with multiple <function_call> children\n"
        "3) If no tool is needed, answer normally\n\n"
        f"Available tools:\n{tools_block}"
    )


def _safe_process_tool_choice(tool_choice: Any, tools: list[dict]) -> str:
    if tool_choice is None:
        return ""

    if isinstance(tool_choice, str):
        if tool_choice == "required":
            return "\nIMPORTANT: You MUST call at least one tool in your next response."
        if tool_choice == "none":
            return "\nIMPORTANT: Do not call tools. Answer directly."
        return ""

    if isinstance(tool_choice, dict):
        fn = (
            tool_choice.get("function", {})
            if isinstance(tool_choice.get("function"), dict)
            else {}
        )
        name = fn.get("name")
        if isinstance(name, str) and name:
            return f"\nIMPORTANT: You MUST call this tool: {name}"

    return ""


def _flatten_messages_for_zai(messages: list[dict]) -> list[dict]:
    parts: list[str] = []
    for msg in messages:
        role = str(msg.get("role", "user")).upper()
        content = _extract_text_from_content(msg.get("content", ""))
        parts.append(f"<{role}>{content}</{role}>")
    return [{"role": "user", "content": "\n".join(parts)}]


def _remove_think_blocks(text: str) -> str:
    while "<think>" in text and "</think>" in text:
        start = text.find("<think>")
        if start == -1:
            break
        pos = start + 7
        depth = 1
        while pos < len(text) and depth > 0:
            if text[pos : pos + 7] == "<think>":
                depth += 1
                pos += 7
            elif text[pos : pos + 8] == "</think>":
                depth -= 1
                pos += 8
            else:
                pos += 1
        if depth == 0:
            text = text[:start] + text[pos:]
        else:
            break
    return text


def _find_last_trigger_signal_outside_think(text: str, trigger_signal: str) -> int:
    if not text or not trigger_signal:
        return -1
    i = 0
    depth = 0
    last = -1
    while i < len(text):
        if text.startswith("<think>", i):
            depth += 1
            i += 7
            continue
        if text.startswith("</think>", i):
            depth = max(0, depth - 1)
            i += 8
            continue
        if depth == 0 and text.startswith(trigger_signal, i):
            last = i
            i += 1
            continue
        i += 1
    return last


def _parse_function_calls_xml(xml_string: str, trigger_signal: str) -> list[dict]:
    if not xml_string or trigger_signal not in xml_string:
        return []

    cleaned = _remove_think_blocks(xml_string)
    pos = cleaned.rfind(trigger_signal)
    if pos == -1:
        return []

    sub = cleaned[pos:]
    m = re.search(r"<function_calls>([\s\S]*?)</function_calls>", sub)
    if not m:
        return []

    calls_block = m.group(1)
    chunks = re.findall(r"<function_call>([\s\S]*?)</function_call>", calls_block)
    out: list[dict] = []

    for c in chunks:
        name_m = re.search(r"<name>([\s\S]*?)</name>", c)
        args_m = re.search(r"<args_json>([\s\S]*?)</args_json>", c)
        if not name_m:
            continue
        name = name_m.group(1).strip()
        args_raw = args_m.group(1).strip() if args_m else "{}"
        try:
            parsed = json.loads(args_raw) if args_raw else {}
            if not isinstance(parsed, dict):
                parsed = {"value": parsed}
        except Exception:
            parsed = {"raw": args_raw}

        out.append(
            {
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(parsed, ensure_ascii=False),
                },
            }
        )

    return out


# ── OpenAI response helpers ──────────────────────────────────────────


def _make_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:29]}"


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 2))


def _build_usage(prompt_text: str, completion_text: str) -> dict:
    p = _estimate_tokens(prompt_text)
    c = _estimate_tokens(completion_text)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _openai_chunk(
    completion_id: str,
    model: str,
    *,
    content: str | None = None,
    reasoning_content: str | None = None,
    finish_reason: str | None = None,
) -> dict:
    delta: dict = {}
    if content is not None:
        delta["content"] = content
    if reasoning_content is not None:
        delta["reasoning_content"] = reasoning_content
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _extract_upstream_tool_calls(data: dict) -> list[dict]:
    # Native Toolify/Z.ai style
    tcs = data.get("tool_calls")
    if isinstance(tcs, list):
        return tcs

    # OpenAI-like style: choices[0].delta.tool_calls or choices[0].message.tool_calls
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        c0 = choices[0] if isinstance(choices[0], dict) else {}
        delta = c0.get("delta") if isinstance(c0.get("delta"), dict) else {}
        message = c0.get("message") if isinstance(c0.get("message"), dict) else {}
        for candidate in (delta.get("tool_calls"), message.get("tool_calls")):
            if isinstance(candidate, list):
                return candidate

    return []


def _extract_upstream_delta(data: dict) -> tuple[str, str]:
    """Best-effort extract (phase, delta_text) from upstream event payload."""
    phase = str(data.get("phase", "") or "")

    # OpenAI-like envelope
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        c0 = choices[0] if isinstance(choices[0], dict) else {}
        delta_obj = c0.get("delta") if isinstance(c0.get("delta"), dict) else {}
        msg_obj = c0.get("message") if isinstance(c0.get("message"), dict) else {}
        if not phase:
            phase = str(c0.get("phase", "") or "")
        for v in (
            delta_obj.get("reasoning_content"),
            delta_obj.get("content"),
            msg_obj.get("reasoning_content"),
            msg_obj.get("content"),
        ):
            if isinstance(v, str) and v:
                return phase, v

    candidates = [
        data.get("delta_content"),
        data.get("content"),
        data.get("delta"),
        (data.get("message") or {}).get("content")
        if isinstance(data.get("message"), dict)
        else None,
    ]

    for v in candidates:
        if isinstance(v, str) and v:
            return phase, v

    return phase, ""


# ── Endpoints ────────────────────────────────────────────────────────


@app.get("/v1/models")
async def list_models():
    models_resp = await pool.get_models()
    if isinstance(models_resp, dict) and "data" in models_resp:
        models_list = models_resp["data"]
    elif isinstance(models_resp, list):
        models_list = models_resp
    else:
        models_list = []

    return {
        "object": "list",
        "data": [
            variant
            for m in models_list
            for variant in _expand_model_variants(
                m.get("id") or m.get("name", "unknown")
            )
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()

    requested_model: str = body.get("model", "glm-5")
    messages: list[dict] = body.get("messages", [])
    stream: bool = body.get("stream", False)
    tools: list[dict] | None = body.get("tools")
    tool_choice = body.get("tool_choice")
    model, enable_thinking = _split_model_and_thinking(
        requested_model, body.get("enable_thinking")
    )

    # signature prompt: last user message in original request
    prompt = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            prompt = _extract_text_from_content(msg.get("content", ""))
            break
    if not prompt:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "No user message found in messages",
                    "type": "invalid_request_error",
                }
            },
        )

    processed_messages = _preprocess_messages(messages)

    has_fc = bool(tools)
    if has_fc:
        fc_prompt = _generate_function_prompt(tools or [], GLOBAL_TRIGGER_SIGNAL)
        fc_prompt += _safe_process_tool_choice(tool_choice, tools or [])
        processed_messages.insert(0, {"role": "system", "content": fc_prompt})

    flat_messages = _flatten_messages_for_zai(processed_messages)
    usage_prompt_text = "\n".join(
        _extract_text_from_content(m.get("content", "")) for m in processed_messages
    )

    req_id = f"req_{uuid.uuid4().hex[:10]}"
    logger.info(
        "[entry][%s] model=%s stream=%s tools=%d input_messages=%d flat_chars=%d est_prompt_tokens=%d",
        req_id,
        model,
        stream,
        len(tools or []),
        len(messages),
        len(flat_messages[0].get("content", "")),
        _estimate_tokens(usage_prompt_text),
    )

    async def run_once(auth: dict[str, str]):
        client = ZaiClient()
        try:
            client.token = auth["token"]
            client.user_id = auth["user_id"]
            client.username = auth["username"]
            chat = await client.create_chat(
                prompt, model, enable_thinking=enable_thinking
            )
            chat_id = chat["id"]
            upstream = client.chat_completions(
                chat_id=chat_id,
                messages=flat_messages,
                prompt=prompt,
                model=model,
                enable_thinking=enable_thinking,
                tools=None,
            )
            return upstream, client, chat_id
        except Exception:
            await client.close()
            raise

    if stream:

        async def gen_sse():
            completion_id = _make_id()
            retried = False
            current_uid: str | None = None

            while True:
                client: ZaiClient | None = None
                chat_id: str | None = None
                try:
                    await pool.ensure_auth()
                    auth = pool.get_auth_snapshot()
                    current_uid = auth["user_id"]
                    upstream, client, chat_id = await run_once(auth)

                    yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"

                    reasoning_parts: list[str] = []
                    answer_parts: list[str] = []
                    native_tool_calls: list[dict] = []

                    async for data in upstream:
                        phase, delta = _extract_upstream_delta(data)

                        upstream_tcs = _extract_upstream_tool_calls(data)
                        if upstream_tcs:
                            for tc in upstream_tcs:
                                native_tool_calls.append(
                                    {
                                        "id": tc.get(
                                            "id", f"call_{uuid.uuid4().hex[:24]}"
                                        ),
                                        "type": "function",
                                        "function": {
                                            "name": tc.get("function", {}).get(
                                                "name", ""
                                            ),
                                            "arguments": tc.get("function", {}).get(
                                                "arguments", ""
                                            ),
                                        },
                                    }
                                )
                            continue

                        if phase == "thinking" and delta:
                            reasoning_parts.append(delta)
                            chunk = _openai_chunk(
                                completion_id, model, reasoning_content=delta
                            )
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                        elif delta:
                            answer_parts.append(delta)

                    if native_tool_calls:
                        logger.info(
                            "[stream][%s] native_tool_calls=%d",
                            completion_id,
                            len(native_tool_calls),
                        )
                        for i, tc in enumerate(native_tool_calls):
                            tc_chunk = {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": model,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"tool_calls": [{"index": i, **tc}]},
                                        "finish_reason": None,
                                    }
                                ],
                            }
                            yield f"data: {json.dumps(tc_chunk, ensure_ascii=False)}\n\n"
                        finish = _openai_chunk(
                            completion_id, model, finish_reason="tool_calls"
                        )
                        yield f"data: {json.dumps(finish, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    answer_text = "".join(answer_parts)
                    logger.info(
                        "[stream][%s] collected answer_len=%d reasoning_len=%d",
                        completion_id,
                        len(answer_text),
                        len("".join(reasoning_parts)),
                    )
                    parsed = (
                        _parse_function_calls_xml(answer_text, GLOBAL_TRIGGER_SIGNAL)
                        if has_fc
                        else []
                    )

                    if parsed:
                        logger.info(
                            "[stream][%s] parsed_tool_calls=%d",
                            completion_id,
                            len(parsed),
                        )
                        prefix_pos = _find_last_trigger_signal_outside_think(
                            answer_text, GLOBAL_TRIGGER_SIGNAL
                        )
                        if prefix_pos > 0:
                            prefix = answer_text[:prefix_pos].rstrip()
                            if prefix:
                                yield f"data: {json.dumps(_openai_chunk(completion_id, model, content=prefix), ensure_ascii=False)}\n\n"

                        for i, tc in enumerate(parsed):
                            tc_chunk = {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": model,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"tool_calls": [{"index": i, **tc}]},
                                        "finish_reason": None,
                                    }
                                ],
                            }
                            yield f"data: {json.dumps(tc_chunk, ensure_ascii=False)}\n\n"

                        finish = _openai_chunk(
                            completion_id, model, finish_reason="tool_calls"
                        )
                        yield f"data: {json.dumps(finish, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    if answer_text:
                        yield f"data: {json.dumps(_openai_chunk(completion_id, model, content=answer_text), ensure_ascii=False)}\n\n"
                    else:
                        # Never return an empty stream response body to clients.
                        yield f"data: {json.dumps(_openai_chunk(completion_id, model, content=''), ensure_ascii=False)}\n\n"

                    finish = _openai_chunk(completion_id, model, finish_reason="stop")
                    yield f"data: {json.dumps(finish, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                except (httpcore.RemoteProtocolError, httpx.RemoteProtocolError) as e:
                    logger.error(
                        "[stream][%s] server disconnected: %s", completion_id, e
                    )
                    if client is not None:
                        if chat_id:
                            await client.delete_chat(chat_id)
                        await client.close()
                        client = None
                    if retried:
                        error_msg = "上游服务断开连接，请稍后重试"
                        yield f"data: {json.dumps(_openai_chunk(completion_id, model, content=f'[{error_msg}]'), ensure_ascii=False)}\n\n"
                        yield f"data: {json.dumps(_openai_chunk(completion_id, model, finish_reason='error'), ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    retried = True
                    logger.info(
                        "[stream][%s] switching account and retrying...", completion_id
                    )
                    await pool.refresh_auth(current_uid)
                    current_uid = None
                    continue
                except (httpcore.ReadTimeout, httpx.ReadTimeout) as e:
                    logger.error("[stream][%s] read timeout: %s", completion_id, e)
                    if client is not None:
                        if chat_id:
                            await client.delete_chat(chat_id)
                        await client.close()
                        client = None

                    if retried:
                        error_msg = "上游服务响应超时，请稍后重试或减少消息长度"
                        yield f"data: {json.dumps(_openai_chunk(completion_id, model, content=f'[{error_msg}]'), ensure_ascii=False)}\n\n"
                        yield f"data: {json.dumps(_openai_chunk(completion_id, model, finish_reason='error'), ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    retried = True
                    logger.info("[stream][%s] retrying after timeout...", completion_id)
                    await pool.refresh_auth(current_uid)
                    current_uid = None
                    continue
                except httpx.HTTPStatusError as e:
                    # Handle upstream 400 with concurrency limit (code 429)
                    is_concurrency = False
                    try:
                        err_body = e.response.json() if e.response else {}
                        is_concurrency = err_body.get("code") == 429
                    except Exception:
                        pass

                    logger.error(
                        "[stream][%s] HTTP %s (concurrency=%s): %s",
                        completion_id,
                        e.response.status_code if e.response else "?",
                        is_concurrency,
                        e,
                    )
                    if client is not None:
                        if chat_id:
                            await client.delete_chat(chat_id)
                        await client.close()
                        client = None

                    if retried:
                        yield f"data: {json.dumps({'error': {'message': 'Upstream concurrency limit' if is_concurrency else 'Upstream error after retry', 'type': 'server_error'}}, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    retried = True
                    if is_concurrency:
                        logger.info(
                            "[stream][%s] concurrency limit hit, cleaning up chats...",
                            completion_id,
                        )
                        await pool.cleanup_chats()
                        await asyncio.sleep(1)
                    await pool.refresh_auth(current_uid)
                    current_uid = None
                    continue
                except Exception as e:
                    logger.exception("[stream][%s] exception: %s", completion_id, e)
                    if client is not None:
                        if chat_id:
                            await client.delete_chat(chat_id)
                        await client.close()
                        client = None

                    if retried:
                        yield f"data: {json.dumps({'error': {'message': 'Upstream Zai error after retry', 'type': 'server_error'}}, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    retried = True
                    logger.info(
                        "[stream][%s] refreshing auth and retrying...", completion_id
                    )
                    await pool.refresh_auth(current_uid)
                    current_uid = None
                    continue
                finally:
                    if client is not None:
                        if chat_id:
                            await client.delete_chat(chat_id)
                        await client.close()
                    if current_uid:
                        pool._release_by_user_id(current_uid)
                        current_uid = None

        return StreamingResponse(
            gen_sse(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    completion_id = _make_id()
    client: ZaiClient | None = None
    chat_id: str | None = None
    current_uid: str | None = None

    for attempt in range(2):
        try:
            await pool.ensure_auth()
            auth = pool.get_auth_snapshot()
            current_uid = auth["user_id"]
            upstream, client, chat_id = await run_once(auth)
            reasoning_parts: list[str] = []
            answer_parts: list[str] = []
            native_tool_calls: list[dict] = []

            async for data in upstream:
                phase, delta = _extract_upstream_delta(data)

                upstream_tcs = _extract_upstream_tool_calls(data)
                if upstream_tcs:
                    for tc in upstream_tcs:
                        native_tool_calls.append(
                            {
                                "id": tc.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                                "type": "function",
                                "function": {
                                    "name": tc.get("function", {}).get("name", ""),
                                    "arguments": tc.get("function", {}).get(
                                        "arguments", ""
                                    ),
                                },
                            }
                        )
                elif phase == "thinking" and delta:
                    reasoning_parts.append(delta)
                elif delta:
                    answer_parts.append(delta)

            if native_tool_calls:
                message: dict = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": native_tool_calls,
                }
                if reasoning_parts:
                    message["reasoning_content"] = "".join(reasoning_parts)
                usage = _build_usage(usage_prompt_text, "".join(reasoning_parts))
                return {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {"index": 0, "message": message, "finish_reason": "tool_calls"}
                    ],
                    "usage": usage,
                }

            answer_text = "".join(answer_parts)
            parsed = (
                _parse_function_calls_xml(answer_text, GLOBAL_TRIGGER_SIGNAL)
                if has_fc
                else []
            )
            if parsed:
                prefix_pos = _find_last_trigger_signal_outside_think(
                    answer_text, GLOBAL_TRIGGER_SIGNAL
                )
                prefix_text = (
                    answer_text[:prefix_pos].rstrip() if prefix_pos > 0 else None
                )
                message = {
                    "role": "assistant",
                    "content": prefix_text or None,
                    "tool_calls": parsed,
                }
                if reasoning_parts:
                    message["reasoning_content"] = "".join(reasoning_parts)
                usage = _build_usage(
                    usage_prompt_text, (prefix_text or "") + "".join(reasoning_parts)
                )
                return {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {"index": 0, "message": message, "finish_reason": "tool_calls"}
                    ],
                    "usage": usage,
                }

            usage = _build_usage(
                usage_prompt_text, answer_text + "".join(reasoning_parts)
            )
            msg: dict = {"role": "assistant", "content": answer_text}
            if reasoning_parts:
                msg["reasoning_content"] = "".join(reasoning_parts)
            return {
                "id": completion_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                "usage": usage,
            }

        except httpx.HTTPStatusError as e:
            is_concurrency = False
            try:
                err_body = e.response.json() if e.response else {}
                is_concurrency = err_body.get("code") == 429
            except Exception:
                pass
            logger.error(
                "[sync][%s] HTTP %s (concurrency=%s): %s",
                completion_id,
                e.response.status_code if e.response else "?",
                is_concurrency,
                e,
            )
            if client is not None:
                if chat_id:
                    await client.delete_chat(chat_id)
                await client.close()
                client = None
                chat_id = None
            if attempt == 0:
                if is_concurrency:
                    await pool.cleanup_chats()
                    await asyncio.sleep(1)
                await pool.refresh_auth(current_uid)
                current_uid = None
                continue
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "message": "Upstream concurrency limit"
                        if is_concurrency
                        else "Upstream error after retry",
                        "type": "server_error",
                    }
                },
            )
        except Exception as e:
            logger.exception("[sync][%s] exception: %s", completion_id, e)
            if client is not None:
                if chat_id:
                    await client.delete_chat(chat_id)
                await client.close()
                client = None
                chat_id = None

            if attempt == 0:
                await pool.refresh_auth(current_uid)
                current_uid = None
                continue
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "message": "Upstream Zai error after retry",
                        "type": "server_error",
                    }
                },
            )
        finally:
            if client is not None:
                if chat_id:
                    await client.delete_chat(chat_id)
                await client.close()
            if current_uid:
                pool._release_by_user_id(current_uid)
                current_uid = None

    return JSONResponse(
        status_code=502,
        content={"error": {"message": "Unexpected error", "type": "server_error"}},
    )


# ── Anthropic Claude Messages Endpoint ───────────────────────────────


@app.post("/v1/messages")
async def claude_messages(request: Request):
    """Anthropic Claude Messages API compatible endpoint for new-api."""
    body = await request.json()
    requested_model: str = body.get("model", "glm-5")
    claude_msgs: list[dict] = body.get("messages", [])
    system = body.get("system")
    stream: bool = body.get("stream", False)
    tools_claude: list[dict] | None = body.get("tools")
    tool_choice = body.get("tool_choice")
    model, enable_thinking = _split_model_and_thinking(
        requested_model, body.get("enable_thinking")
    )

    openai_messages = claude_messages_to_openai(system, claude_msgs)
    openai_tools = claude_tools_to_openai(tools_claude)

    prompt = ""
    for msg in reversed(openai_messages):
        if msg.get("role") == "user":
            prompt = _extract_text_from_content(msg.get("content", ""))
            break
    if not prompt:
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "No user message",
                },
            },
        )

    processed_messages = _preprocess_messages(openai_messages)
    has_fc = bool(openai_tools)
    if has_fc:
        fc_prompt = _generate_function_prompt(openai_tools, GLOBAL_TRIGGER_SIGNAL)
        fc_prompt += claude_tool_choice_prompt(tool_choice)
        processed_messages.insert(0, {"role": "system", "content": fc_prompt})

    flat_messages = _flatten_messages_for_zai(processed_messages)
    usage_prompt = "\n".join(
        _extract_text_from_content(m.get("content", "")) for m in processed_messages
    )

    msg_id = make_claude_id()
    req_id = f"req_{uuid.uuid4().hex[:10]}"
    logger.info(
        "[claude][%s] model=%s stream=%s tools=%d",
        req_id,
        model,
        stream,
        len(openai_tools or []),
    )

    async def _run(auth):
        c = ZaiClient()
        try:
            c.token, c.user_id, c.username = (
                auth["token"],
                auth["user_id"],
                auth["username"],
            )
            chat = await c.create_chat(prompt, model, enable_thinking=enable_thinking)
            chat_id = chat["id"]
            up = c.chat_completions(
                chat_id=chat_id,
                messages=flat_messages,
                prompt=prompt,
                model=model,
                enable_thinking=enable_thinking,
            )
            return up, c, chat_id
        except Exception:
            await c.close()
            raise

    if stream:
        return StreamingResponse(
            _claude_stream(msg_id, model, _run, has_fc, usage_prompt),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return await _claude_sync(msg_id, model, _run, has_fc, usage_prompt)


async def _claude_stream(msg_id, model, run_once, has_fc, usage_prompt):
    """Generator for Claude SSE streaming."""
    retried = False
    current_uid: str | None = None
    while True:
        client = None
        chat_id = None
        try:
            await pool.ensure_auth()
            auth = pool.get_auth_snapshot()
            current_uid = auth["user_id"]
            upstream, client, chat_id = await run_once(auth)
            input_tk = _estimate_tokens(usage_prompt)

            yield sse_message_start(msg_id, model, input_tk)
            yield sse_ping()

            r_parts, a_parts = [], []
            bidx = 0
            thinking_on = False
            native_tcs: list[dict] = []

            async for data in upstream:
                phase, delta = _extract_upstream_delta(data)
                up_tcs = _extract_upstream_tool_calls(data)
                if up_tcs:
                    native_tcs.extend(up_tcs)
                    continue
                if phase == "thinking" and delta:
                    if not thinking_on:
                        yield sse_content_block_start(
                            bidx, {"type": "thinking", "thinking": ""}
                        )
                        thinking_on = True
                    r_parts.append(delta)
                    yield sse_content_block_delta(
                        bidx, {"type": "thinking_delta", "thinking": delta}
                    )
                elif delta:
                    a_parts.append(delta)

            # close thinking block
            if thinking_on:
                yield sse_content_block_stop(bidx)
                bidx += 1

            answer = "".join(a_parts)
            all_tcs = native_tcs
            if not all_tcs and has_fc:
                all_tcs = _parse_function_calls_xml(answer, GLOBAL_TRIGGER_SIGNAL)
                if all_tcs:
                    pp = _find_last_trigger_signal_outside_think(
                        answer, GLOBAL_TRIGGER_SIGNAL
                    )
                    answer = answer[:pp].rstrip() if pp > 0 else ""

            if all_tcs:
                if answer:
                    yield sse_content_block_start(bidx, {"type": "text", "text": ""})
                    yield sse_content_block_delta(
                        bidx, {"type": "text_delta", "text": answer}
                    )
                    yield sse_content_block_stop(bidx)
                    bidx += 1
                for tc in all_tcs:
                    fn = (
                        tc.get("function", {})
                        if isinstance(tc.get("function"), dict)
                        else tc
                    )
                    nm = fn.get("name", tc.get("name", ""))
                    args_s = fn.get("arguments", "{}")
                    tid = tc.get("id", f"toolu_{uuid.uuid4().hex[:20]}").replace(
                        "call_", "toolu_"
                    )
                    yield sse_content_block_start(
                        bidx, {"type": "tool_use", "id": tid, "name": nm, "input": {}}
                    )
                    yield sse_content_block_delta(
                        bidx, {"type": "input_json_delta", "partial_json": args_s}
                    )
                    yield sse_content_block_stop(bidx)
                    bidx += 1
                out_tk = _estimate_tokens("".join(r_parts) + "".join(a_parts))
                yield sse_message_delta("tool_use", out_tk)
                yield sse_message_stop()
                return

            yield sse_content_block_start(bidx, {"type": "text", "text": ""})
            if answer:
                yield sse_content_block_delta(
                    bidx, {"type": "text_delta", "text": answer}
                )
            yield sse_content_block_stop(bidx)
            out_tk = _estimate_tokens("".join(r_parts) + answer)
            yield sse_message_delta("end_turn", out_tk)
            yield sse_message_stop()
            return

        except (httpcore.ReadTimeout, httpx.ReadTimeout) as e:
            logger.error("[claude-stream][%s] timeout: %s", msg_id, e)
            if client:
                if chat_id:
                    await client.delete_chat(chat_id)
                await client.close()
                client = None
            if retried:
                yield sse_error("overloaded_error", "Upstream timeout")
                return
            retried = True
            await pool.refresh_auth(current_uid)
            current_uid = None
            continue
        except (httpcore.RemoteProtocolError, httpx.RemoteProtocolError) as e:
            logger.error("[claude-stream][%s] server disconnected: %s", msg_id, e)
            if client:
                if chat_id:
                    await client.delete_chat(chat_id)
                await client.close()
                client = None
            if retried:
                yield sse_error("api_error", "Server disconnected, please retry")
                return
            retried = True
            await pool.refresh_auth(current_uid)
            current_uid = None
            continue
        except httpx.HTTPStatusError as e:
            is_concurrency = False
            try:
                err_body = e.response.json() if e.response else {}
                is_concurrency = err_body.get("code") == 429
            except Exception:
                pass
            logger.error(
                "[claude-stream][%s] HTTP %s (concurrency=%s): %s",
                msg_id,
                e.response.status_code if e.response else "?",
                is_concurrency,
                e,
            )
            if client:
                if chat_id:
                    await client.delete_chat(chat_id)
                await client.close()
                client = None
            if retried:
                yield sse_error(
                    "overloaded_error" if is_concurrency else "api_error",
                    "Upstream concurrency limit"
                    if is_concurrency
                    else "Upstream error after retry",
                )
                return
            retried = True
            if is_concurrency:
                logger.info(
                    "[claude-stream][%s] concurrency limit hit, cleaning up chats...",
                    msg_id,
                )
                await pool.cleanup_chats()
                await asyncio.sleep(1)
            await pool.refresh_auth(current_uid)
            current_uid = None
            continue
        except Exception as e:
            logger.exception("[claude-stream][%s] error: %s", msg_id, e)
            if client:
                if chat_id:
                    await client.delete_chat(chat_id)
                await client.close()
                client = None
            if retried:
                yield sse_error("api_error", "Upstream error after retry")
                return
            retried = True
            await pool.refresh_auth(current_uid)
            current_uid = None
            continue
        finally:
            if client:
                if chat_id:
                    await client.delete_chat(chat_id)
                await client.close()
            if current_uid:
                pool._release_by_user_id(current_uid)
                current_uid = None


async def _claude_sync(msg_id, model, run_once, has_fc, usage_prompt):
    """Non-streaming Claude response."""
    client = None
    chat_id = None
    current_uid: str | None = None
    for attempt in range(2):
        try:
            await pool.ensure_auth()
            auth = pool.get_auth_snapshot()
            current_uid = auth["user_id"]
            upstream, client, chat_id = await run_once(auth)
            r_parts, a_parts = [], []
            native_tcs: list[dict] = []

            async for data in upstream:
                phase, delta = _extract_upstream_delta(data)
                up_tcs = _extract_upstream_tool_calls(data)
                if up_tcs:
                    native_tcs.extend(up_tcs)
                elif phase == "thinking" and delta:
                    r_parts.append(delta)
                elif delta:
                    a_parts.append(delta)

            answer = "".join(a_parts)
            all_tcs = native_tcs
            if not all_tcs and has_fc:
                all_tcs = _parse_function_calls_xml(answer, GLOBAL_TRIGGER_SIGNAL)
                if all_tcs:
                    pp = _find_last_trigger_signal_outside_think(
                        answer, GLOBAL_TRIGGER_SIGNAL
                    )
                    answer = answer[:pp].rstrip() if pp > 0 else ""

            in_tk = _estimate_tokens(usage_prompt)
            out_tk = _estimate_tokens("".join(r_parts) + "".join(a_parts))
            return build_non_stream_response(
                msg_id, model, r_parts, answer, all_tcs or None, in_tk, out_tk
            )

        except httpx.HTTPStatusError as e:
            is_concurrency = False
            try:
                err_body = e.response.json() if e.response else {}
                is_concurrency = err_body.get("code") == 429
            except Exception:
                pass
            logger.error(
                "[claude-sync][%s] HTTP %s (concurrency=%s): %s",
                msg_id,
                e.response.status_code if e.response else "?",
                is_concurrency,
                e,
            )
            if client:
                if chat_id:
                    await client.delete_chat(chat_id)
                await client.close()
                client = None
                chat_id = None
            if attempt == 0:
                if is_concurrency:
                    await pool.cleanup_chats()
                    await asyncio.sleep(1)
                await pool.refresh_auth(current_uid)
                current_uid = None
                continue
            return JSONResponse(
                status_code=500,
                content={
                    "type": "error",
                    "error": {
                        "type": "overloaded_error" if is_concurrency else "api_error",
                        "message": "Upstream concurrency limit"
                        if is_concurrency
                        else "Upstream error",
                    },
                },
            )
        except Exception as e:
            logger.exception("[claude-sync][%s] error: %s", msg_id, e)
            if client:
                if chat_id:
                    await client.delete_chat(chat_id)
                await client.close()
                client = None
                chat_id = None
            if attempt == 0:
                await pool.refresh_auth(current_uid)
                current_uid = None
                continue
            return JSONResponse(
                status_code=500,
                content={
                    "type": "error",
                    "error": {"type": "api_error", "message": "Upstream error"},
                },
            )
        finally:
            if client:
                if chat_id:
                    await client.delete_chat(chat_id)
                await client.close()
            if current_uid:
                pool._release_by_user_id(current_uid)
                current_uid = None

    return JSONResponse(
        status_code=500,
        content={
            "type": "error",
            "error": {"type": "api_error", "message": "Unexpected"},
        },
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=30016)
