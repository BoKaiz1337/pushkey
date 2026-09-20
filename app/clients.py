"""两端 API 客户端。

JANUS（中心站）走 `Authorization: Bearer mgw_…`，契约在 /openapi.json。
GOLEM（沙盘上游）控制台走 cookie 会话，数据面走 `sk-golem-…`。

⚠️ 这两个客户端**任何情况下都不打印凭证** —— mask() 是唯一允许外露的形式。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .config import settings


class ApiError(Exception):
    def __init__(self, status: int, code: str = "", message: str = "", payload: Any = None):
        self.status = status
        self.code = code
        self.message = message
        self.payload = payload
        super().__init__(f"HTTP {status} {code} {message}".strip())

    @property
    def is_rate_limited(self) -> bool:
        return self.status == 429

    @property
    def is_upstream_down(self) -> bool:
        return self.status in (500, 502, 503, 504)


def mask(secret: str | None) -> str:
    """凭证唯一允许出现在日志/界面上的形式。"""
    if not secret:
        return "—"
    if len(secret) <= 10:
        return secret[:3] + "****"
    return f"{secret[:9]}…{secret[-4:]}"


def _extract_error(r: httpx.Response) -> tuple[str, str]:
    """把 JANUS 的结构化错误读出来：{code, message} 或 FastAPI 的 {detail}。"""
    try:
        j = r.json()
    except Exception:
        return "", (r.text or "")[:200]
    if isinstance(j, dict):
        code = str(j.get("code") or "")
        msg = j.get("message") or j.get("detail") or ""
        if isinstance(msg, list):          # FastAPI 422 校验错误
            msg = "; ".join(str(x.get("msg", x)) for x in msg)
        return code, str(msg)[:300]
    return "", str(j)[:200]


class JanusClient:
    """中心站：我的交易对手。推货 / 报价 / 心跳 / 下架全走这里。"""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.janus_base,
            headers={"Authorization": f"Bearer {settings.mgw_key}",
                     "accept": "application/json"},
            timeout=httpx.Timeout(25.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _req(self, method: str, path: str, **kw) -> Any:
        r = await self._client.request(method, path, **kw)
        if r.status_code >= 400:
            code, msg = _extract_error(r)
            raise ApiError(r.status_code, code, msg)
        if not r.content:
            return None
        try:
            return r.json()
        except Exception:
            return r.text

    # ── 只读 ────────────────────────────────────────────────────
    async def me(self):                     return await self._req("GET", "/api/v1/me")
    async def resource_types(self):         return await self._req("GET", "/api/v1/resource-types")
    async def tasks(self, status: str | None = None):
        p = {"status": status} if status else None
        return await self._req("GET", "/api/v1/tasks", params=p)
    async def list_keys(self):              return await self._req("GET", "/api/v1/keys")
    async def get_key(self, kid: str):      return await self._req("GET", f"/api/v1/keys/{kid}")
    async def model_prices(self):           return await self._req("GET", "/api/v1/model-prices")
    async def usage(self, **p):             return await self._req("GET", "/api/v1/usage", params=p or None)
    async def settlements(self):            return await self._req("GET", "/api/v1/settlements")
    async def payout_methods(self):         return await self._req("GET", "/api/v1/me/payout-methods")

    # ── 写 ──────────────────────────────────────────────────────
    async def claim_task(self, task_id: str):
        return await self._req("POST", f"/api/v1/tasks/{task_id}/claims")

    async def push_key(self, *, resource_type: str, fmt: str, base_url: str,
                       api_key: str, models: list[str], quote: dict,
                       task_id: str | None = None, note: str = "") -> dict:
        body = {"resource_type": resource_type, "format": fmt, "base_url": base_url,
                "api_key": api_key, "models": models, "quote": quote, "note": note}
        if task_id:
            body["task_id"] = task_id
        return await self._req("POST", "/api/v1/keys", json=body)

    async def patch_key(self, kid: str, *, quote: dict | None = None,
                        state: str | None = None, reason: str | None = None):
        body: dict[str, Any] = {}
        if quote is not None:  body["quote"] = quote
        if state is not None:  body["state"] = state
        if reason is not None: body["reason"] = reason
        return await self._req("PATCH", f"/api/v1/keys/{kid}", json=body)

    async def delete_key(self, kid: str):
        return await self._req("DELETE", f"/api/v1/keys/{kid}")

    async def heartbeat(self, kid: str, *, endpoint_ok: bool,
                        upstream_balance: float | None, models_ok: list[str],
                        note: str = "") -> dict:
        body = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "endpoint_ok": endpoint_ok, "upstream_balance": upstream_balance,
                "models_ok": models_ok, "note": note}
        return await self._req("POST", f"/api/v1/keys/{kid}/heartbeat", json=body)

    async def probe(self, kid: str):        return await self._req("POST", f"/api/v1/keys/{kid}/probe")
    async def resume(self, kid: str):       return await self._req("POST", f"/api/v1/keys/{kid}/resume")

    async def set_payout(self, method: str, account: str, real_name: str):
        return await self._req("PUT", "/api/v1/me/payout-methods",
                               json={"method": method, "account": account, "real_name": real_name})


class GolemClient:
    """沙盘上游：我的货源。控制台读余额/故障状态，数据面探活。"""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(base_url=settings.golem_base,
                                         timeout=httpx.Timeout(25.0))
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── 控制台会话 ──────────────────────────────────────────────
    async def login(self) -> None:
        r = await self._client.post("/console/api/auth/login",
                                    json={"email": settings.golem_email,
                                          "password": settings.golem_password})
        if r.status_code >= 400:
            code, msg = _extract_error(r)
            raise ApiError(r.status_code, code, msg)

    async def _console(self, method: str, path: str, **kw) -> Any:
        async with self._lock:
            for attempt in (0, 1):
                r = await self._client.request(method, path, **kw)
                if r.status_code == 401 and attempt == 0:
                    await self.login()          # 会话过期，续一次
                    continue
                break
        if r.status_code >= 400:
            code, msg = _extract_error(r)
            raise ApiError(r.status_code, code, msg)
        return r.json() if r.content else None

    async def keys(self) -> list[dict]:
        return (await self._console("GET", "/console/api/keys")).get("items", [])

    async def overview(self) -> dict:
        return await self._console("GET", "/console/api/overview")

    async def bill(self, days: int = 7) -> dict:
        return await self._console("GET", "/console/api/bill", params={"days": days})

    async def usage(self, days: int = 7) -> dict:
        return await self._console("GET", "/console/api/usage", params={"days": days})

    async def set_key_status(self, key_id: int, status: str) -> dict:
        """拨故障开关。五种：normal / 429 / 503 / 500 / balance_exhausted"""
        return await self._console("PATCH", f"/console/api/keys/{key_id}", json={"status": status})

    async def balance(self) -> float | None:
        """沙盘**没有余额接口** —— 探不到就如实返回 None，绝不瞎猜（手册硬要求）。"""
        return None

    # ── 数据面探活 ──────────────────────────────────────────────
    async def probe(self, api_key: str, fmt: str, model: str,
                    max_tokens: int | None = None) -> tuple[int, str]:
        """真发一次最小请求，返回 (HTTP 状态码, 说明)。

        探测要认的是**响应码**：429 限流、503/500 上游故障、402 没钱。
        """
        mt = max_tokens or settings.probe_max_tokens
        if fmt == "openai":
            r = await self._client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "max_tokens": mt,
                      "messages": [{"role": "user", "content": "ping"}]})
        else:
            r = await self._client.post(
                "/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                json={"model": model, "max_tokens": mt,
                      "messages": [{"role": "user", "content": "ping"}]})
        note = ""
        if r.status_code >= 400:
            note = (r.text or "")[:200]
        return r.status_code, note
