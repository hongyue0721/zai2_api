"""OpenAI-compatible proxy server for chat.z.ai + Toolify-style function calling."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from pathlib import Path
import hashlib
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
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

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
REBUILD_COOLDOWN = int(os.getenv("REBUILD_COOLDOWN", "30"))
REBUILD_MAX_RETRIES = int(os.getenv("REBUILD_MAX_RETRIES", "3"))
STATE_FILE = Path(__file__).with_name("webui_state.json")
ADMIN_DIR = Path(__file__).with_name("web")
ADMIN_INDEX = ADMIN_DIR / "admin.html"
ADMIN_CSS = ADMIN_DIR / "admin.css"
ADMIN_JS = ADMIN_DIR / "admin.js"


def _now_ts() -> float:
    return time.time()


def _iso_ts(ts: float | None) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _random_api_key(length: int = 32, prefix: str = "sk-zai-") -> str:
    alphabet = string.ascii_letters + string.digits
    return prefix + "".join(secrets.choice(alphabet) for _ in range(length))


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


class APIKeyStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._keys: list[dict[str, Any]] = []
        self._target_pool_size = POOL_SIZE
        self._rebuild_cooldown = REBUILD_COOLDOWN
        self._rebuild_max_retries = REBUILD_MAX_RETRIES
        self._admin_password_hash = _hash_password("zai2api")
        self._admin_sessions: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("State load failed: %s", e)
            return
        keys = data.get("api_keys", [])
        if isinstance(keys, list):
            self._keys = [k for k in keys if isinstance(k, dict) and k.get("key")]
        target = data.get("target_pool_size")
        if isinstance(target, int) and target > 0:
            self._target_pool_size = target
        password_hash = data.get("admin_password_hash")
        if isinstance(password_hash, str) and password_hash:
            self._admin_password_hash = password_hash
        rebuild_cooldown = data.get("rebuild_cooldown")
        if isinstance(rebuild_cooldown, int) and rebuild_cooldown >= 0:
            self._rebuild_cooldown = rebuild_cooldown
        rebuild_max_retries = data.get("rebuild_max_retries")
        if isinstance(rebuild_max_retries, int) and rebuild_max_retries > 0:
            self._rebuild_max_retries = rebuild_max_retries

    def _save_unlocked(self) -> None:
        payload = {
            "api_keys": self._keys,
            "target_pool_size": self._target_pool_size,
            "admin_password_hash": self._admin_password_hash,
            "rebuild_cooldown": self._rebuild_cooldown,
            "rebuild_max_retries": self._rebuild_max_retries,
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def list_keys(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [dict(item) for item in self._keys]

    async def create_key(self, name: str, key: str | None = None) -> dict[str, Any]:
        final_key = (key or _random_api_key()).strip()
        if len(final_key) < 8:
            raise ValueError("API Key 至少需要 8 个字符")
        async with self._lock:
            if any(item.get("key") == final_key for item in self._keys):
                raise ValueError("API Key 已存在")
            item = {
                "id": f"key_{uuid.uuid4().hex[:12]}",
                "name": name.strip() or "未命名 Key",
                "key": final_key,
                "created_at": _now_ts(),
                "last_used_at": None,
                "total_requests": 0,
            }
            self._keys.append(item)
            self._save_unlocked()
            return dict(item)

    async def delete_key(self, key_id: str) -> bool:
        async with self._lock:
            for item in list(self._keys):
                if item.get("id") == key_id:
                    self._keys.remove(item)
                    self._save_unlocked()
                    return True
        return False

    async def record_key_use(self, raw_key: str) -> None:
        async with self._lock:
            for item in self._keys:
                if secrets.compare_digest(str(item.get("key", "")), raw_key):
                    item["total_requests"] = int(item.get("total_requests", 0)) + 1
                    item["last_used_at"] = _now_ts()
                    self._save_unlocked()
                    return

    async def validate(self, raw_key: str | None) -> bool:
        if not raw_key:
            return False
        async with self._lock:
            return any(
                secrets.compare_digest(str(item.get("key", "")), raw_key)
                for item in self._keys
            )

    async def has_keys(self) -> bool:
        async with self._lock:
            return bool(self._keys)

    async def get_target_pool_size(self) -> int:
        async with self._lock:
            return self._target_pool_size

    async def set_target_pool_size(self, size: int) -> None:
        if size < 1:
            raise ValueError("账号数量至少为 1")
        async with self._lock:
            self._target_pool_size = size
            self._save_unlocked()

    async def verify_admin_password(self, password: str) -> bool:
        async with self._lock:
            return secrets.compare_digest(
                self._admin_password_hash, _hash_password(password)
            )

    async def change_admin_password(
        self, current_password: str, new_password: str
    ) -> None:
        if len(new_password) < 6:
            raise ValueError("新密码至少需要 6 个字符")
        async with self._lock:
            if not secrets.compare_digest(
                self._admin_password_hash, _hash_password(current_password)
            ):
                raise ValueError("当前密码不正确")
            self._admin_password_hash = _hash_password(new_password)
            self._admin_sessions.clear()
            self._save_unlocked()

    async def create_admin_session(self) -> str:
        async with self._lock:
            token = secrets.token_urlsafe(32)
            self._admin_sessions[token] = _now_ts() + 7 * 24 * 3600
            return token

    async def is_valid_admin_session(self, token: str | None) -> bool:
        if not token:
            return False
        async with self._lock:
            expires_at = self._admin_sessions.get(token)
            if not expires_at:
                return False
            if expires_at < _now_ts():
                self._admin_sessions.pop(token, None)
                return False
            self._admin_sessions[token] = _now_ts() + 7 * 24 * 3600
            return True

    async def clear_admin_session(self, token: str | None) -> None:
        if not token:
            return
        async with self._lock:
            self._admin_sessions.pop(token, None)

    async def get_rebuild_settings(self) -> dict[str, int]:
        async with self._lock:
            return {
                "rebuild_cooldown": self._rebuild_cooldown,
                "rebuild_max_retries": self._rebuild_max_retries,
            }

    async def update_rebuild_settings(
        self, rebuild_cooldown: int, rebuild_max_retries: int
    ) -> dict[str, int]:
        if rebuild_cooldown < 0:
            raise ValueError("冷却时间不能小于 0")
        if rebuild_max_retries < 1:
            raise ValueError("重试上限至少为 1")
        async with self._lock:
            self._rebuild_cooldown = rebuild_cooldown
            self._rebuild_max_retries = rebuild_max_retries
            self._save_unlocked()
            return {
                "rebuild_cooldown": self._rebuild_cooldown,
                "rebuild_max_retries": self._rebuild_max_retries,
            }


key_store = APIKeyStore(STATE_FILE)


class AccountInfo:
    """A single guest auth session."""

    __slots__ = (
        "token",
        "user_id",
        "username",
        "created_at",
        "active",
        "valid",
        "request_count",
        "success_count",
        "failure_count",
        "last_used_at",
        "last_success_at",
        "last_error",
    )

    def __init__(self, token: str, user_id: str, username: str) -> None:
        self.token = token
        self.user_id = user_id
        self.username = username
        self.created_at = time.time()
        self.active = 0  # number of in-flight requests
        self.valid = True
        self.request_count = 0
        self.success_count = 0
        self.failure_count = 0
        self.last_used_at: float | None = None
        self.last_success_at: float | None = None
        self.last_error = ""

    def snapshot(self) -> dict[str, str]:
        return {"token": self.token, "user_id": self.user_id, "username": self.username}

    def stats(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "token": self.token,
            "created_at": _iso_ts(self.created_at),
            "age_seconds": round(self.age, 1),
            "active": self.active,
            "valid": self.valid,
            "request_count": self.request_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "success_rate": round((self.success_count / self.request_count) * 100, 1)
            if self.request_count
            else 0.0,
            "last_used_at": _iso_ts(self.last_used_at),
            "last_success_at": _iso_ts(self.last_success_at),
            "last_error": self.last_error,
        }

    @property
    def age(self) -> float:
        return time.time() - self.created_at


class SessionPool:
    """Pool of guest accounts for concurrent, seamless use."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._accounts: list[AccountInfo] = []
        self._bg_task: asyncio.Task | None = None
        self._target_size = POOL_SIZE
        self._rr_cursor = 0
        self._rebuild_attempts: dict[str, list[float]] = {}
        self._last_rebuild_at = 0.0
        self._rebuild_cooldown = REBUILD_COOLDOWN
        self._rebuild_max_retries = REBUILD_MAX_RETRIES

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
                    need = self._target_size - len(
                        [a for a in self._accounts if a.valid]
                    )
                    for _ in range(max(0, need)):
                        try:
                            self._accounts.append(await self._new_account())
                        except Exception as e:
                            logger.warning("Pool maintain: %s", e)
            except asyncio.CancelledError:
                return
            except Exception:
                pass

    def _pick_account(self, accounts: list[AccountInfo]) -> AccountInfo:
        min_active = min(a.active for a in accounts)
        candidates = [a for a in accounts if a.active == min_active]
        if len(candidates) == 1:
            return candidates[0]
        start = self._rr_cursor % len(candidates)
        chosen = candidates[start]
        self._rr_cursor = (self._rr_cursor + 1) % max(len(candidates), 1)
        return chosen

    def _can_rebuild_now(self, failed_user_id: str | None = None) -> bool:
        now = _now_ts()
        if now - self._last_rebuild_at < self._rebuild_cooldown:
            return False
        key = failed_user_id or "global"
        attempts = [ts for ts in self._rebuild_attempts.get(key, []) if now - ts < 300]
        self._rebuild_attempts[key] = attempts
        if len(attempts) >= self._rebuild_max_retries:
            return False
        attempts.append(now)
        self._rebuild_attempts[key] = attempts
        self._last_rebuild_at = now
        return True

    # ── public API ───────────────────────────────────────────────────

    async def initialize(self) -> None:
        self._target_size = await key_store.get_target_pool_size()
        rebuild_settings = await key_store.get_rebuild_settings()
        self._rebuild_cooldown = rebuild_settings["rebuild_cooldown"]
        self._rebuild_max_retries = rebuild_settings["rebuild_max_retries"]
        async with self._lock:
            results = await asyncio.gather(
                *[self._new_account() for _ in range(self._target_size)],
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
        acc = self._pick_account(good)
        acc.active += 1
        acc.request_count += 1
        acc.last_used_at = _now_ts()
        return acc

    def release(self, acc: AccountInfo) -> None:
        acc.active = max(0, acc.active - 1)

    async def report_failure(self, acc: AccountInfo) -> None:
        """Mark account invalid, schedule cleanup, add replacement."""
        acc.failure_count += 1
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

    def mark_success(self, user_id: str) -> None:
        for a in self._accounts:
            if a.user_id == user_id:
                a.success_count += 1
                a.last_success_at = _now_ts()
                a.last_error = ""
                return

    async def mark_failure(self, user_id: str, error: str) -> None:
        found = False
        for a in self._accounts:
            if a.user_id == user_id:
                a.failure_count += 1
                a.last_error = error[:240]
                a.valid = False
                found = True
                break
        if found:
            await self.refresh_auth(user_id)

    def get_auth_snapshot(self) -> dict[str, str]:
        """Get auth snapshot from the least-busy valid account."""
        good = [a for a in self._accounts if a.valid and a.age < TOKEN_MAX_AGE]
        if not good:
            good = [a for a in self._accounts if a.valid]
        if not good:
            raise RuntimeError("No valid accounts in pool")
        acc = self._pick_account(good)
        acc.active += 1
        acc.request_count += 1
        acc.last_used_at = _now_ts()
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
        if not self._can_rebuild_now(failed_user_id):
            logger.warning(
                "SessionPool: rebuild skipped due to cooldown/retry cap uid=%s cooldown=%ss max=%d",
                failed_user_id,
                self._rebuild_cooldown,
                self._rebuild_max_retries,
            )
            return
        try:
            acc = await self._new_account()
            async with self._lock:
                self._accounts.append(acc)
            logger.info("SessionPool: auth refreshed, new user_id=%s", acc.user_id)
        except Exception as e:
            logger.warning("SessionPool: refresh_auth failed: %s", e)

    async def update_rebuild_settings(
        self, rebuild_cooldown: int, rebuild_max_retries: int
    ) -> dict[str, int]:
        settings = await key_store.update_rebuild_settings(
            rebuild_cooldown, rebuild_max_retries
        )
        self._rebuild_cooldown = settings["rebuild_cooldown"]
        self._rebuild_max_retries = settings["rebuild_max_retries"]
        return settings

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

    async def set_target_size(self, size: int) -> None:
        if size < 1:
            raise ValueError("账号数量至少为 1")
        self._target_size = size
        await key_store.set_target_pool_size(size)
        async with self._lock:
            valid_accounts = [a for a in self._accounts if a.valid]
            need = size - len(valid_accounts)
            if need > 0:
                for _ in range(need):
                    self._accounts.append(await self._new_account())
            elif need < 0:
                removable = [a for a in self._accounts if a.active == 0]
                for acc in removable[:-need]:
                    if acc in self._accounts:
                        self._accounts.remove(acc)
                        asyncio.create_task(self._del_account(acc))

    async def add_account(self) -> dict[str, Any]:
        async with self._lock:
            acc = await self._new_account()
            self._accounts.append(acc)
            self._target_size += 1
            await key_store.set_target_pool_size(self._target_size)
            return acc.stats()

    async def remove_account(self, user_id: str) -> bool:
        async with self._lock:
            for acc in list(self._accounts):
                if acc.user_id == user_id and acc.active == 0:
                    self._accounts.remove(acc)
                    self._target_size = max(1, self._target_size - 1)
                    await key_store.set_target_pool_size(self._target_size)
                    asyncio.create_task(self._del_account(acc))
                    return True
        return False

    def dashboard(self) -> dict[str, Any]:
        accounts = [a.stats() for a in self._accounts]
        total_requests = sum(a["request_count"] for a in accounts)
        total_success = sum(a["success_count"] for a in accounts)
        total_failures = sum(a["failure_count"] for a in accounts)
        return {
            "target_pool_size": self._target_size,
            "current_pool_size": len(self._accounts),
            "valid_accounts": sum(1 for a in self._accounts if a.valid),
            "active_requests": sum(a.active for a in self._accounts),
            "total_requests": total_requests,
            "total_success": total_success,
            "total_failures": total_failures,
            "success_rate": round((total_success / total_requests) * 100, 1)
            if total_requests
            else 0.0,
            "rebuild_cooldown": self._rebuild_cooldown,
            "rebuild_max_retries": self._rebuild_max_retries,
            "accounts": accounts,
        }


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
            info = (
                tool_idx.get(str(tc_id))
                if isinstance(tc_id, str)
                else {"name": msg.get("name", "unknown_tool"), "arguments": "{}"}
            )
            if not isinstance(info, dict):
                info = {"name": msg.get("name", "unknown_tool"), "arguments": "{}"}
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
        delta_raw = c0.get("delta")
        message_raw = c0.get("message")
        delta: dict[str, Any] = delta_raw if isinstance(delta_raw, dict) else {}
        message: dict[str, Any] = message_raw if isinstance(message_raw, dict) else {}
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
        delta_raw = c0.get("delta")
        message_raw = c0.get("message")
        delta_obj: dict[str, Any] = delta_raw if isinstance(delta_raw, dict) else {}
        msg_obj: dict[str, Any] = message_raw if isinstance(message_raw, dict) else {}
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


async def _extract_bearer_key(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    return header[7:].strip() or None


async def _ensure_request_allowed(request: Request) -> str | None:
    has_keys = await key_store.has_keys()
    if not has_keys:
        return None
    raw_key = await _extract_bearer_key(request)
    if raw_key and await key_store.validate(raw_key):
        await key_store.record_key_use(raw_key)
        return raw_key
    return None


async def _admin_authed(request: Request) -> bool:
    return await key_store.is_valid_admin_session(request.cookies.get("admin_session"))


def _admin_auth_failed() -> JSONResponse:
    return JSONResponse(status_code=401, content={"message": "Admin auth required"})


def _unauthorized_response() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "message": "Invalid or missing API key",
                "type": "authentication_error",
            }
        },
    )


def _read_admin_asset(path: Path, fallback: str) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return fallback


ADMIN_HTML_FALLBACK = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>zai2api</title>
  <link rel="stylesheet" href="/admin/assets/admin.css" />
</head>
<body>
  <div class="shell">
    <section class="card login-card animate-in" id="login-card">
      <div class="section-head"><strong>Admin 登录</strong><span class="hint">请输入管理密码</span></div>
      <form id="login-form">
        <input type="password" name="password" placeholder="输入管理密码" autocomplete="current-password" />
        <button type="submit">登录后台</button>
      </form>
      <div class="msg" id="login-msg"></div>
    </section>

    <div id="admin-app" class="hidden">
    <section class="hero card animate-in">
      <div class="hero-content">
        <div class="badge">zai2api</div>
        <h1>zai2api</h1>
        <div class="sub">统一查看账号池、调用情况、Key 管理和失败重建策略。</div>
      </div>
    </section>

    <section class="grid stats animate-in" id="stats"></section>

    <section class="grid trend-grid animate-in">
      <div class="card">
        <div class="section-head"><strong>实时健康度</strong><span id="health-text" class="hint">载入中...</span></div>
        <div class="health-bar"><div id="health-fill" class="health-fill"></div></div>
        <div id="health-meta" class="hint"></div>
      </div>
      <div class="card">
        <div class="section-head"><strong>最近 20 次快照</strong><span class="hint">自动刷新</span></div>
        <canvas id="trend-canvas" width="640" height="220"></canvas>
      </div>
    </section>

    <section class="grid content-grid animate-in">
      <div class="stack">
      <div class="card">
        <div class="section-head"><strong>总统计</strong><span class="hint">按账号汇总</span></div>
        <div class="row" style="margin-bottom:16px;"><button type="button" id="add-account-btn">+ 增加账号</button></div>
        <div class="table-wrapper">
          <table>
            <thead><tr><th>Token</th><th>状态</th><th>调用</th><th>成功</th><th>失败</th><th>成功率</th><th>最近状态</th><th>操作</th></tr></thead>
            <tbody id="accounts"></tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <div class="section-head"><strong>API Key 管理</strong><span class="hint">支持随机生成，也支持自定义</span></div>
        <div class="key-toolbar">
          <button type="button" class="ghost" id="toggle-keys-btn">显示完整 Key</button>
        </div>
        <form id="key-form">
          <input name="name" placeholder="Key 名称，例如 面板A / 本地脚本" />
          <input name="key" placeholder="留空则随机生成，自定义示例：sk-my-custom-key" />
          <button type="submit">创建 Key</button>
        </form>
        <div class="msg" id="key-msg"></div>
        <div class="table-wrapper">
          <table>
            <thead><tr><th>名称</th><th>Key</th><th>调用数</th><th>最近使用</th><th>操作</th></tr></thead>
            <tbody id="keys"></tbody>
          </table>
        </div>
      </div>
      </div>

      <div class="card settings-card animate-in">
        <div class="section-head"><strong>设置</strong><span class="hint">修改密码与重建策略</span></div>
        <form id="password-form">
          <div class="field-note">修改管理密码</div>
          <input type="password" name="current_password" placeholder="当前密码" autocomplete="current-password" />
          <input type="password" name="new_password" placeholder="新密码，至少 6 位" autocomplete="new-password" />
          <button type="submit">修改密码</button>
        </form>
        <div class="msg" id="password-msg"></div>
        <form id="rebuild-form" style="margin-top:24px;">
          <div class="field-note">失败重建冷却时间（秒）<span>账号失败后，至少等待多久才允许继续自动补新号</span></div>
          <input type="number" name="rebuild_cooldown" min="0" placeholder="失败重建冷却时间（秒）" />
          <div class="field-note">5 分钟内重试上限<span>同一轮异常窗口内，最多自动重建多少次</span></div>
          <input type="number" name="rebuild_max_retries" min="1" placeholder="5 分钟内重试上限" />
          <button type="submit">保存重建策略</button>
        </form>
        <div class="msg" id="rebuild-msg"></div>
      </div>
    </section>
    </div>
  </div>
  <script src="/admin/assets/admin.js"></script>
</body>
</html>
"""


ADMIN_CSS_FALLBACK = """:root {
  --google-blue: #1a73e8;
  --google-blue-light: #e8f0fe;
  --google-green: #34a853;
  --google-red: #ea4335;
  --google-yellow: #fbbc04;
  --text-primary: #202124;
  --text-secondary: #5f6368;
  --border-color: #dadce0;
  --bg-color: #f8f9fa;
  --card-bg: #ffffff;
  --hover-bg: #f1f3f4;
  --shadow-sm: 0 1px 2px 0 rgba(60,64,67,.3), 0 1px 3px 1px rgba(60,64,67,.15);
  --shadow-md: 0 1px 3px 0 rgba(60,64,67,.3), 0 4px 8px 3px rgba(60,64,67,.15);
  --shadow-lg: 0 1px 3px 0 rgba(60,64,67,.3), 0 8px 16px 4px rgba(60,64,67,.15);
  --radius: 8px;
  --radius-lg: 16px;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: 'Google Sans', 'Segoe UI', 'Microsoft YaHei', system-ui, sans-serif;
  background: var(--bg-color);
  color: var(--text-primary);
  min-height: 100vh;
  line-height: 1.5;
}

.shell { max-width: 1400px; margin: 0 auto; padding: 24px; }
.hidden { display: none !important; }

/* 卡片 */
.card {
  background: var(--card-bg);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  padding: 24px;
  transition: box-shadow 0.2s ease, transform 0.2s ease;
}
.card:hover {
  box-shadow: var(--shadow-md);
}

.login-card {
  max-width: 400px;
  margin: 80px auto 0;
}

/* Hero */
.hero {
  background: linear-gradient(135deg, var(--google-blue) 0%, #4285f4 100%);
  color: white;
  padding: 32px 40px;
  position: relative;
  overflow: hidden;
}
.hero::before {
  content: '';
  position: absolute;
  top: -50%;
  right: -20%;
  width: 400px;
  height: 400px;
  background: rgba(255,255,255,0.1);
  border-radius: 50%;
}
.hero::after {
  content: '';
  position: absolute;
  bottom: -30%;
  left: -10%;
  width: 300px;
  height: 300px;
  background: rgba(255,255,255,0.05);
  border-radius: 50%;
}
.hero-content { position: relative; z-index: 1; }
.badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  background: rgba(255,255,255,0.2);
  border-radius: 100px;
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0.5px;
  margin-bottom: 12px;
}
h1 {
  font-size: 32px;
  font-weight: 400;
  margin-bottom: 8px;
}
.sub {
  font-size: 15px;
  opacity: 0.9;
  max-width: 500px;
}

/* Grid 布局 */
.grid {
  display: grid;
  gap: 20px;
  margin-top: 20px;
}
.stats {
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
}
.trend-grid {
  grid-template-columns: 1fr 1.5fr;
}
.content-grid {
  grid-template-columns: 1.4fr 1fr;
  align-items: start;
}
.stack { display: grid; gap: 20px; }

/* 统计卡片 */
.stat-card {
  display: flex;
  flex-direction: column;
  padding: 20px;
}
.stat-num {
  font-size: 36px;
  font-weight: 400;
  color: var(--google-blue);
  margin: 8px 0;
}
.label {
  font-size: 14px;
  color: var(--text-secondary);
  font-weight: 500;
}
.stat-extra {
  font-size: 13px;
  color: var(--text-secondary);
  margin-top: 4px;
}

/* 表格 */
.table-wrapper {
  overflow-x: auto;
  margin: 0 -24px;
  padding: 0 24px;
}
table {
  width: 100%;
  border-collapse: collapse;
}
th, td {
  text-align: left;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border-color);
  font-size: 14px;
}
th {
  color: var(--text-secondary);
  font-weight: 500;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  background: var(--bg-color);
  position: sticky;
  top: 0;
}
tr {
  transition: background 0.15s ease;
}
tbody tr:hover {
  background: var(--hover-bg);
}

/* 标签 */
.pill {
  display: inline-flex;
  align-items: center;
  padding: 4px 12px;
  border-radius: 100px;
  font-size: 12px;
  font-weight: 500;
}
.ok { background: #e6f4ea; color: var(--google-green); }
.bad { background: #fce8e6; color: var(--google-red); }
.warn { background: #fef7e0; color: #b06000; }
.busy { background: var(--google-blue-light); color: var(--google-blue); }

/* 表单 */
form {
  display: grid;
  gap: 16px;
}
input {
  width: 100%;
  padding: 12px 16px;
  border: 2px solid var(--border-color);
  border-radius: var(--radius);
  font-size: 14px;
  color: var(--text-primary);
  background: white;
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}
input:focus {
  outline: none;
  border-color: var(--google-blue);
  box-shadow: 0 0 0 3px rgba(26,115,232,0.2);
}
input::placeholder { color: var(--text-secondary); }

/* 按钮 */
button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 10px 24px;
  border: none;
  border-radius: var(--radius);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s ease;
  background: var(--google-blue);
  color: white;
}
button:hover:not(:disabled) {
  background: #1557b0;
  box-shadow: var(--shadow-sm);
}
button:active:not(:disabled) {
  transform: scale(0.98);
}
button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
button.ghost {
  background: transparent;
  color: var(--google-blue);
}
button.ghost:hover:not(:disabled) {
  background: var(--google-blue-light);
}
button.danger {
  background: var(--google-red);
}
button.danger:hover:not(:disabled) {
  background: #d33426;
}

.icon-btn {
  padding: 8px;
  min-width: 36px;
  height: 36px;
}

/* Token 单元格 */
.token-cell {
  display: grid;
  gap: 4px;
  min-width: 120px;
}
.token-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.token-main {
  font-family: 'Google Sans Mono', 'Consolas', monospace;
  font-size: 13px;
  color: var(--text-primary);
  word-break: break-all;
}
.token-meta {
  font-size: 12px;
  color: var(--text-secondary);
}
.key-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}
.status-line {
  font-weight: 500;
  color: var(--text-primary);
}

/* 健康度条 */
.health-bar {
  height: 8px;
  background: var(--border-color);
  border-radius: 100px;
  overflow: hidden;
  margin: 16px 0;
}
.health-fill {
  height: 100%;
  border-radius: 100px;
  background: linear-gradient(90deg, var(--google-red), var(--google-yellow), var(--google-green));
  transition: width 0.5s ease;
}

/* Canvas */
canvas {
  width: 100%;
  height: 200px;
  border-radius: var(--radius);
}

/* 消息 */
.msg {
  min-height: 20px;
  font-size: 13px;
  color: var(--text-secondary);
  margin-top: 8px;
}
.msg.success { color: var(--google-green); }
.msg.error { color: var(--google-red); }

/* 区块标题 */
.section-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  margin-bottom: 16px;
}
.section-head strong {
  font-size: 16px;
  font-weight: 500;
}
.hint {
  color: var(--text-secondary);
  font-size: 13px;
}

/* 设置卡片 */
.settings-card {
  position: sticky;
  top: 24px;
}
.field-note {
  font-size: 14px;
  font-weight: 500;
  color: var(--text-primary);
  margin-bottom: 4px;
}
.field-note span {
  display: block;
  font-size: 13px;
  font-weight: 400;
  color: var(--text-secondary);
  margin-top: 2px;
}

/* 工具栏 */
.row {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}
.key-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 16px;
}

/* 动画 */
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}
.animate-in {
  animation: fadeIn 0.3s ease forwards;
}

/* 响应式 */
@media (max-width: 1200px) {
  .content-grid { grid-template-columns: 1fr; }
  .settings-card { position: static; }
}
@media (max-width: 900px) {
  .trend-grid { grid-template-columns: 1fr; }
  .shell { padding: 16px; }
  th, td { padding: 12px; font-size: 13px; }
}
@media (max-width: 600px) {
  h1 { font-size: 24px; }
  .hero { padding: 24px; }
  .stat-num { font-size: 28px; }
}
"""


ADMIN_JS_FALLBACK = """const historyPoints = [];
let revealKeys = false;

async function api(url, options = {}) {
  const res = await fetch(url, { headers: { 'Content-Type': 'application/json' }, credentials: 'include', ...options });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error(data.detail || data.error?.message || data.message || '请求失败');
  return data;
}

function showLoggedIn(loggedIn) {
  document.getElementById('login-card').classList.toggle('hidden', loggedIn);
  document.getElementById('admin-app').classList.toggle('hidden', !loggedIn);
}

function card(label, value, extra = '') {
  return `<div class="card stat-card"><div class="label">${label}</div><div class="stat-num">${value}</div><div class="stat-extra">${extra}</div></div>`;
}

function shortKey(v) {
  return v.length > 20 ? `${v.slice(0, 10)}...${v.slice(-6)}` : v;
}

function shortToken(v) {
  if (!v) return '暂无 token';
  return v.length > 24 ? `${v.slice(0, 8)}...${v.slice(-8)}` : v;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function copyText(value) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch (e) {
      console.warn('Clipboard API failed, falling back to legacy method');
    }
  }
  const input = document.createElement('textarea');
  input.value = value;
  input.setAttribute('readonly', '');
  input.style.position = 'fixed';
  input.style.opacity = '0';
  input.style.top = '0';
  input.style.left = '0';
  input.style.pointerEvents = 'none';
  document.body.appendChild(input);
  input.focus();
  input.select();
  try {
    const ok = document.execCommand('copy');
    document.body.removeChild(input);
    if (!ok) {
      throw new Error('copy_failed');
    }
  } catch (e) {
    document.body.removeChild(input);
    const range = document.createRange();
    range.selectNode(document.body);
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(range);
    try {
      document.execCommand('copy');
      window.getSelection().removeAllRanges();
    } catch (e2) {
      throw new Error('copy_failed');
    }
  }
}

function healthText(rate) {
  if (rate >= 95) return '状态超稳';
  if (rate >= 80) return '状态良好';
  if (rate >= 60) return '有点波动';
  return '需要关注';
}

function drawTrend() {
  const canvas = document.getElementById('trend-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = 'rgba(111,124,151,.18)';
  ctx.lineWidth = 1;
  for (let i = 1; i <= 4; i++) {
    const y = (height / 5) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke();
  }
  if (historyPoints.length < 2) return;
  const maxVal = Math.max(...historyPoints.map(v => Math.max(v.requests, v.success)), 1);
  const drawLine = (color, selector) => {
    ctx.beginPath();
    ctx.lineWidth = 3;
    ctx.strokeStyle = color;
    historyPoints.forEach((point, index) => {
      const x = (width / Math.max(historyPoints.length - 1, 1)) * index;
      const y = height - (selector(point) / maxVal) * (height - 18) - 9;
      if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  };
  drawLine('#73b7ff', p => p.requests);
  drawLine('#ff7aa2', p => p.success);
}

function pushHistory(data) {
  historyPoints.push({ time: Date.now(), requests: data.total_requests, success: data.total_success });
  if (historyPoints.length > 20) historyPoints.shift();
  drawTrend();
}

async function loadDashboard() {
  try {
    const data = await api('/admin/api/dashboard');
    showLoggedIn(true);
    document.getElementById('stats').innerHTML = [
      card('有效账号', data.valid_accounts, `目标 ${data.target_pool_size}`),
      card('活跃请求', data.active_requests, `当前池 ${data.current_pool_size}`),
      card('总调用', data.total_requests, '所有 free 账号累计'),
      card('总成功', data.total_success, `成功率 ${data.success_rate}%`),
      card('总失败', data.total_failures, `Key ${data.api_key_count} 个`),
    ].join('');

    const health = Math.max(0, Math.min(100, data.success_rate));
    document.getElementById('health-fill').style.width = `${health}%`;
    document.getElementById('health-text').textContent = `${healthText(health)} · ${health}%`;
    document.getElementById('health-meta').textContent = `当前 ${data.active_requests} 个活跃请求，${data.valid_accounts}/${data.current_pool_size} 个账号可用。失败重建冷却 ${data.rebuild_cooldown}s，5 分钟内最多 ${data.rebuild_max_retries} 次。`;

    const rebuildForm = document.getElementById('rebuild-form');
    if (rebuildForm) {
      rebuildForm.elements.rebuild_cooldown.value = data.rebuild_cooldown;
      rebuildForm.elements.rebuild_max_retries.value = data.rebuild_max_retries;
    }

    document.getElementById('accounts').innerHTML = data.accounts.map(acc => `
      <tr>
        <td>
          <div class="token-cell">
            <div class="token-row">
              <span class="token-main" title="${escapeHtml(acc.token || '')}">${escapeHtml(shortToken(acc.token || ''))}</span>
              <button class="ghost icon-btn copy-token-btn" data-token="${escapeHtml(acc.token || '')}" ${acc.token ? '' : 'disabled'}>复制</button>
            </div>
            <span class="hint token-meta">${acc.user_id}</span>
          </div>
        </td>
        <td><span class="pill ${acc.valid ? 'ok' : 'bad'}">${acc.valid ? '正常' : '失效'}</span></td>
        <td>${acc.request_count}</td>
        <td>${acc.success_count}</td>
        <td>${acc.failure_count}</td>
        <td><span class="pill ${acc.success_rate >= 80 ? 'ok' : acc.success_rate >= 50 ? 'warn' : 'bad'}">${acc.success_rate}%</span></td>
        <td><div class="status-line">成功：${acc.last_success_at || '暂无'}</div><div class="hint">错误：${acc.last_error || '无错误'}</div></td>
        <td><button class="ghost remove-account-btn" data-user-id="${acc.user_id}" ${acc.active > 0 ? 'disabled' : ''}>移除</button></td>
      </tr>`).join('');

    const keys = await api('/admin/api/keys');
    document.getElementById('keys').innerHTML = keys.data.map(item => `
      <tr>
        <td>${item.name}</td>
        <td>
          <div class="key-cell">
            <span title="${item.key}">${revealKeys ? item.key : shortKey(item.key)}</span>
            <button class="ghost icon-btn copy-key-btn" data-key-id="${item.id}">复制</button>
          </div>
        </td>
        <td>${item.total_requests}</td>
        <td>${item.last_used_at || '暂无'}</td>
        <td><button class="ghost delete-key-btn" data-key-id="${item.id}">删除</button></td>
      </tr>`).join('');

    window.__keyMap = Object.fromEntries(keys.data.map(item => [item.id, item.key]));

    pushHistory(data);
  } catch (err) {
    if (String(err.message).includes('Admin auth required')) {
      showLoggedIn(false);
      return;
    }
    throw err;
  }
}

async function addAccount() { await api('/admin/api/accounts', { method: 'POST', body: '{}' }); loadDashboard(); }
async function removeAccount(userId) { await api(`/admin/api/accounts/${userId}`, { method: 'DELETE' }); loadDashboard(); }
async function deleteKey(id) { await api(`/admin/api/keys/${id}`, { method: 'DELETE' }); loadDashboard(); }
async function logout() { await api('/admin/api/logout', { method: 'POST', body: '{}' }); showLoggedIn(false); }
async function copyAccountToken(token) {
  if (!token) return;
  try {
    await copyText(token);
    document.getElementById('key-msg').textContent = '已复制账号 Token';
  } catch (err) {
    document.getElementById('key-msg').textContent = '复制 Token 失败，请尝试使用 HTTPS、localhost，或手动长按复制';
  }
}
async function copyKey(id) {
  const value = window.__keyMap?.[id];
  if (!value) return;
  try {
    await copyText(value);
    document.getElementById('key-msg').textContent = '已复制 Key';
  } catch (err) {
    document.getElementById('key-msg').textContent = '复制失败，请尝试使用 HTTPS、localhost，或手动长按复制';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('add-account-btn').addEventListener('click', addAccount);

  document.getElementById('accounts').addEventListener('click', async (e) => {
    const copyBtn = e.target.closest('.copy-token-btn');
    if (copyBtn) {
      await copyAccountToken(copyBtn.dataset.token || '');
      return;
    }
    const btn = e.target.closest('.remove-account-btn');
    if (!btn || btn.disabled) return;
    await removeAccount(btn.dataset.userId);
  });

  document.getElementById('keys').addEventListener('click', async (e) => {
    const copyBtn = e.target.closest('.copy-key-btn');
    if (copyBtn) {
      await copyKey(copyBtn.dataset.keyId);
      return;
    }
    const deleteBtn = e.target.closest('.delete-key-btn');
    if (deleteBtn) {
      await deleteKey(deleteBtn.dataset.keyId);
    }
  });

  document.getElementById('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      await api('/admin/api/login', { method: 'POST', body: JSON.stringify({ password: fd.get('password') || '' }) });
      document.getElementById('login-msg').textContent = '';
      e.target.reset();
      loadDashboard();
    } catch (err) {
      document.getElementById('login-msg').textContent = err.message;
    }
  });

  document.getElementById('key-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const payload = { name: fd.get('name') || '', key: fd.get('key') || '' };
    try {
      const res = await api('/admin/api/keys', { method: 'POST', body: JSON.stringify(payload) });
      document.getElementById('key-msg').textContent = `已创建: ${res.key}`;
      e.target.reset();
      loadDashboard();
    } catch (err) {
      document.getElementById('key-msg').textContent = err.message;
    }
  });

  document.getElementById('toggle-keys-btn').addEventListener('click', () => {
    revealKeys = !revealKeys;
    document.getElementById('toggle-keys-btn').textContent = revealKeys ? '隐藏完整 Key' : '显示完整 Key';
    loadDashboard();
  });

  document.getElementById('password-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const payload = {
      current_password: fd.get('current_password') || '',
      new_password: fd.get('new_password') || '',
    };
    try {
      const res = await api('/admin/api/change-password', { method: 'POST', body: JSON.stringify(payload) });
      document.getElementById('password-msg').textContent = res.message || '密码已更新';
      e.target.reset();
      showLoggedIn(false);
    } catch (err) {
      document.getElementById('password-msg').textContent = err.message;
    }
  });

  document.getElementById('rebuild-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const payload = {
      rebuild_cooldown: Number(fd.get('rebuild_cooldown') || 0),
      rebuild_max_retries: Number(fd.get('rebuild_max_retries') || 1),
    };
    try {
      const res = await api('/admin/api/rebuild-settings', { method: 'POST', body: JSON.stringify(payload) });
      document.getElementById('rebuild-msg').textContent = `已保存：冷却 ${res.rebuild_cooldown}s，重试上限 ${res.rebuild_max_retries}`;
      loadDashboard();
    } catch (err) {
      document.getElementById('rebuild-msg').textContent = err.message;
    }
  });

  loadDashboard();
  setInterval(loadDashboard, 8000);
});
"""


# ── Endpoints ────────────────────────────────────────────────────────


@app.get("/v1/models")
async def list_models(request: Request):
    if await key_store.has_keys():
        allowed_key = await _ensure_request_allowed(request)
        if not allowed_key:
            return _unauthorized_response()
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


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    if not await _admin_authed(request):
        return HTMLResponse(_read_admin_asset(ADMIN_INDEX, ADMIN_HTML_FALLBACK))
    return HTMLResponse(_read_admin_asset(ADMIN_INDEX, ADMIN_HTML_FALLBACK))


@app.get("/admin/assets/admin.css")
async def admin_css():
    return Response(
        content=_read_admin_asset(ADMIN_CSS, ADMIN_CSS_FALLBACK),
        media_type="text/css",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/admin/assets/admin.js")
async def admin_js():
    return Response(
        content=_read_admin_asset(ADMIN_JS, ADMIN_JS_FALLBACK),
        media_type="application/javascript",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.post("/admin/api/login")
async def admin_login(request: Request):
    body = await request.json()
    password = str(body.get("password", "") or "")
    if not await key_store.verify_admin_password(password):
        return JSONResponse(status_code=401, content={"message": "密码错误"})
    token = await key_store.create_admin_session()
    response = JSONResponse({"ok": True})
    response.set_cookie(
        key="admin_session",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
    )
    return response


@app.post("/admin/api/logout")
async def admin_logout(request: Request):
    await key_store.clear_admin_session(request.cookies.get("admin_session"))
    response = JSONResponse({"ok": True})
    response.delete_cookie("admin_session")
    return response


@app.post("/admin/api/change-password")
async def admin_change_password(request: Request):
    if not await _admin_authed(request):
        return _admin_auth_failed()
    body = await request.json()
    try:
        await key_store.change_admin_password(
            str(body.get("current_password", "") or ""),
            str(body.get("new_password", "") or ""),
        )
    except ValueError as e:
        return JSONResponse(status_code=400, content={"message": str(e)})
    response = JSONResponse({"ok": True, "message": "密码已更新，请重新登录"})
    response.delete_cookie("admin_session")
    return response


@app.post("/admin/api/rebuild-settings")
async def admin_update_rebuild_settings(request: Request):
    if not await _admin_authed(request):
        return _admin_auth_failed()
    body = await request.json()
    try:
        settings = await pool.update_rebuild_settings(
            int(body.get("rebuild_cooldown", 0)),
            int(body.get("rebuild_max_retries", 1)),
        )
    except (ValueError, TypeError) as e:
        return JSONResponse(status_code=400, content={"message": str(e)})
    return {"ok": True, **settings}


@app.get("/admin/api/dashboard")
async def admin_dashboard(request: Request):
    if not await _admin_authed(request):
        return _admin_auth_failed()
    data = pool.dashboard()
    keys = await key_store.list_keys()
    data["api_key_count"] = len(keys)
    return data


@app.get("/admin/api/keys")
async def admin_list_keys(request: Request):
    if not await _admin_authed(request):
        return _admin_auth_failed()
    keys = await key_store.list_keys()
    return {
        "data": [
            {
                **item,
                "created_at": _iso_ts(item.get("created_at")),
                "last_used_at": _iso_ts(item.get("last_used_at")),
            }
            for item in keys
        ]
    }


@app.post("/admin/api/keys")
async def admin_create_key(request: Request):
    if not await _admin_authed(request):
        return _admin_auth_failed()
    body = await request.json()
    try:
        item = await key_store.create_key(
            str(body.get("name", "") or ""),
            str(body.get("key", "") or "").strip() or None,
        )
    except ValueError as e:
        return JSONResponse(status_code=400, content={"message": str(e)})
    return {
        **item,
        "created_at": _iso_ts(item.get("created_at")),
        "last_used_at": _iso_ts(item.get("last_used_at")),
    }


@app.delete("/admin/api/keys/{key_id}")
async def admin_delete_key(key_id: str, request: Request):
    if not await _admin_authed(request):
        return _admin_auth_failed()
    if await key_store.delete_key(key_id):
        return {"ok": True}
    return JSONResponse(status_code=404, content={"message": "Key 不存在"})


@app.post("/admin/api/accounts")
async def admin_add_account(request: Request):
    if not await _admin_authed(request):
        return _admin_auth_failed()
    account = await pool.add_account()
    return {"ok": True, "account": account}


@app.delete("/admin/api/accounts/{user_id}")
async def admin_remove_account(user_id: str, request: Request):
    if not await _admin_authed(request):
        return _admin_auth_failed()
    removed = await pool.remove_account(user_id)
    if removed:
        return {"ok": True}
    return JSONResponse(
        status_code=400,
        content={"message": "账号不存在，或当前仍有活跃请求无法移除"},
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if await key_store.has_keys():
        allowed_key = await _ensure_request_allowed(request)
        if not allowed_key:
            return _unauthorized_response()
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
                    if current_uid:
                        pool.mark_success(current_uid)
                    yield "data: [DONE]\n\n"
                    return

                except (httpcore.RemoteProtocolError, httpx.RemoteProtocolError) as e:
                    if current_uid:
                        await pool.mark_failure(current_uid, str(e))
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

                    current_uid = None
                    continue
                except (httpcore.ReadTimeout, httpx.ReadTimeout) as e:
                    if current_uid:
                        await pool.mark_failure(current_uid, str(e))
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

                    current_uid = None
                    continue
                except httpx.HTTPStatusError as e:
                    if current_uid:
                        await pool.mark_failure(current_uid, str(e))
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

                    current_uid = None
                    continue
                except Exception as e:
                    if current_uid:
                        await pool.mark_failure(current_uid, str(e))
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
                if current_uid:
                    pool.mark_success(current_uid)
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
                if current_uid:
                    pool.mark_success(current_uid)
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
            if current_uid:
                pool.mark_success(current_uid)
            return {
                "id": completion_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                "usage": usage,
            }

        except httpx.HTTPStatusError as e:
            if current_uid:
                await pool.mark_failure(current_uid, str(e))
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
            if current_uid:
                await pool.mark_failure(current_uid, str(e))
            logger.exception("[sync][%s] exception: %s", completion_id, e)
            if client is not None:
                if chat_id:
                    await client.delete_chat(chat_id)
                await client.close()
                client = None
                chat_id = None

            if attempt == 0:
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
    if await key_store.has_keys():
        allowed_key = await _ensure_request_allowed(request)
        if not allowed_key:
            return JSONResponse(
                status_code=401,
                content={
                    "type": "error",
                    "error": {
                        "type": "authentication_error",
                        "message": "Invalid or missing API key",
                    },
                },
            )
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
                if current_uid:
                    pool.mark_success(current_uid)
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
            if current_uid:
                pool.mark_success(current_uid)
            yield sse_message_stop()
            return

        except (httpcore.ReadTimeout, httpx.ReadTimeout) as e:
            if current_uid:
                await pool.mark_failure(current_uid, str(e))
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

            current_uid = None
            continue
        except (httpcore.RemoteProtocolError, httpx.RemoteProtocolError) as e:
            if current_uid:
                await pool.mark_failure(current_uid, str(e))
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

            current_uid = None
            continue
        except httpx.HTTPStatusError as e:
            if current_uid:
                await pool.mark_failure(current_uid, str(e))
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

            current_uid = None
            continue
        except Exception as e:
            if current_uid:
                await pool.mark_failure(current_uid, str(e))
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
            if current_uid:
                pool.mark_success(current_uid)
            return build_non_stream_response(
                msg_id, model, r_parts, answer, all_tcs or None, in_tk, out_tk
            )

        except httpx.HTTPStatusError as e:
            if current_uid:
                await pool.mark_failure(current_uid, str(e))
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
            if current_uid:
                await pool.mark_failure(current_uid, str(e))
            logger.exception("[claude-sync][%s] error: %s", msg_id, e)
            if client:
                if chat_id:
                    await client.delete_chat(chat_id)
                await client.close()
                client = None
                chat_id = None
            if attempt == 0:
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
