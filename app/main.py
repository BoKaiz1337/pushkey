"""pushkey —— 上游守护站。

我卖的东西，我自己盯着：推货、报价、心跳、**上游断了自己先停**。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import secrets
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .clients import ApiError, GolemClient, JanusClient, mask
from .config import settings
from .guard import Guard
from . import store

app = FastAPI(title="pushkey · 上游守护站", version="1.0.0")
templates = Jinja2Templates(directory="app/templates")


# ── 页面上的数字格式 ────────────────────────────────────────────
def _money(v: Any) -> str:
    """金额按大小自适应小数位 —— $0.0159 别被四舍五入成 $0.02。"""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"${f:,.6f}" if 0 < abs(f) < 0.01 else f"${f:,.4f}"


def _num(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"
    return str(v)


templates.env.globals.update(money=_money, num=_num)

janus = JanusClient()
golem = GolemClient()
guard = Guard(janus, golem)


# ── 面板鉴权（公网裸奔不可接受）─────────────────────────────────
def _check(request: Request) -> None:
    if not settings.panel_password:
        return
    hdr = request.headers.get("authorization", "")
    if hdr.startswith("Basic "):
        with contextlib.suppress(Exception):
            user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
            if user == settings.panel_user and secrets.compare_digest(pw, settings.panel_password):
                return
    raise HTTPException(status_code=401, detail="需要登录",
                        headers={"WWW-Authenticate": 'Basic realm="pushkey"'})


# ── 两端账单（「能查到用量与每日账单」这条要在**面板上**看得见）────
_money_cache: dict[str, tuple[float, str, dict]] = {}


async def _cached_money(name: str, fn, *, refresh: bool = False,
                        timeout: float = 12.0) -> dict:
    """两件事：① 失败不许把面板带崩；② 别每次刷新都去现拉。

    读一次两端账单要 ~3 秒（GOLEM 那边得先登控制台拿 cookie 会话），而面板
    每 60 秒自动刷一次 —— 每次都现实拉的话页面就一直转圈。所以进程内缓存
    `money_ttl` 秒；**失败不入缓存**，下一轮立刻重试。
    """
    now = time.monotonic()
    hit = _money_cache.get(name)
    if hit and not refresh and now - hit[0] < settings.money_ttl:
        return {**hit[2], "_read_at": hit[1]}
    try:
        val = await asyncio.wait_for(fn(), timeout=timeout)
    except Exception as e:                       # 旁路信息：读不到就如实说读不到
        return {"error": f"{type(e).__name__}: {e}"}
    # 跟面板其它时间戳保持一致：ISO UTC 带 Z，别给裸的 HH:MM:SS（本地时区一差就是 8 小时）
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _money_cache[name] = (now, at, val)
    return {**val, "_read_at": at}


async def _golem_money() -> dict:
    """进货口视角：我还剩多少、花了多少、每一分花在哪个模型上。"""
    return {"overview": await golem.overview(),
            "bill": await golem.bill(7),
            "usage": await golem.usage(7),
            "balance": await golem.balance()}       # 沙盘没有余额接口 → None


async def _janus_money() -> dict:
    """卖货口视角：我的 key 进了哪个池、跑了多少量、结算单开了几张。"""
    return {"me": await janus.me(),
            "usage": await janus.usage(),
            "settlements": await janus.settlements()}


@app.on_event("startup")
async def _startup() -> None:
    store.init()
    guard.seed()                      # 上游明文从 Secret 播种（GOLEM 不回显第二次）
    try:
        await golem.login()
        store.log("startup", detail="GOLEM 控制台已登录")
    except ApiError as e:
        store.log("startup_error", detail=f"GOLEM 登录失败 {e.status} {e.message}")
    guard.start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await guard.stop()
    await janus.aclose()
    await golem.aclose()


# ══ 只读 ════════════════════════════════════════════════════════
@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/api/state")
async def api_state(request: Request) -> Any:
    _check(request)
    rows = []
    for b in store.all_bindings():
        rows.append({
            "resource_type": b["resource_type"], "format": b["format"],
            "golem_key_name": b["golem_key_name"],
            "golem_api_key": mask(b["golem_api_key"]),      # 只给脱敏串
            "janus_key_id": b.get("janus_key_id"), "task_id": b.get("task_id"),
            "quote": b.get("quote"), "models": b.get("models"),
            "paused_by_us": bool(b.get("paused_by_us")), "pause_reason": b.get("pause_reason"),
            "last_probe_at": b.get("last_probe_at"), "last_probe_ok": b.get("last_probe_ok"),
            "last_probe_note": b.get("last_probe_note"),
            "upstream_balance": b.get("upstream_balance"),
        })
    remote = {}
    try:
        remote = {str(k["id"]): k for k in (await janus.list_keys()).get("items", [])}
    except ApiError as e:
        remote = {"_error": f"{e.status} {e.message}"}
    for r in rows:
        live = remote.get(str(r["janus_key_id"])) or {}
        r["state"] = live.get("state")
        r["pause_reason_remote"] = live.get("pause_reason")
        r["last_probe_remote"] = live.get("last_probe")
        # 注意：列表接口**不返回** quote，别在这里读 quote_ratio 当成报价 ——
        # 那会恒为 None。报价以本地库为准（推货/改价时写入，与中心站一致）。
    return {"guard": guard.status(), "bindings": rows,
            "events": store.recent_events(40), "remote_count": len(remote)}


@app.get("/api/upstream")
async def api_upstream(request: Request, refresh: bool = False) -> Any:
    """进货口视角：余额/账单/用量。`?refresh=1` 绕过缓存。"""
    _check(request)
    return await _cached_money("golem", _golem_money, refresh=refresh)


@app.get("/api/earnings")
async def api_earnings(request: Request, refresh: bool = False) -> Any:
    """卖货口视角：中心站的用量与结算单。`?refresh=1` 绕过缓存。"""
    _check(request)
    return await _cached_money("janus", _janus_money, refresh=refresh)


# ══ 写 ══════════════════════════════════════════════════════════
@app.post("/api/sync")
async def api_sync(request: Request) -> Any:
    _check(request)
    try:
        return await guard.sync()
    except ApiError as e:
        raise HTTPException(status_code=502, detail=f"{e.status} {e.message}")


@app.post("/api/run/{what}")
async def api_run(what: str, request: Request) -> Any:
    _check(request)
    if what == "probe":
        return {"result": await guard.probe_all()}
    if what == "heartbeat":
        return {"result": await guard.heartbeat_all()}
    raise HTTPException(status_code=404, detail="what ∈ {probe, heartbeat}")


@app.post("/api/keys/{rt}/pause")
async def api_pause(rt: str, request: Request) -> Any:
    _check(request)
    b = store.get_binding(rt)
    if not b or not b.get("janus_key_id"):
        raise HTTPException(status_code=404, detail="没有这条绑定")
    return {"result": await guard._pause(rt, b["janus_key_id"], "manual")}


@app.post("/api/keys/{rt}/resume")
async def api_resume(rt: str, request: Request) -> Any:
    _check(request)
    b = store.get_binding(rt)
    if not b or not b.get("janus_key_id"):
        raise HTTPException(status_code=404, detail="没有这条绑定")
    try:
        await janus.resume(b["janus_key_id"])
        store.upsert_binding(rt, paused_by_us=0, pause_reason=None)
        store.log("manual_resume", rt, b["janus_key_id"], "面板手动申请恢复")
        return {"ok": True}
    except ApiError as e:
        raise HTTPException(status_code=409, detail=f"{e.code or e.status} {e.message}")


@app.post("/api/keys/{rt}/quote")
async def api_quote(rt: str, request: Request) -> Any:
    _check(request)
    b = store.get_binding(rt)
    if not b or not b.get("janus_key_id"):
        raise HTTPException(status_code=404, detail="没有这条绑定")
    body = await request.json()
    quote = {k: v for k, v in body.items() if k in ("price_ratio", "model_overrides")}
    if not quote:
        raise HTTPException(status_code=400, detail="quote 只认 price_ratio / model_overrides")
    try:
        await janus.patch_key(b["janus_key_id"], quote=quote)
    except ApiError as e:                                  # 越界会回 400 invalid_quote（带区间）
        raise HTTPException(status_code=e.status, detail=f"{e.code} {e.message}")
    store.upsert_binding(rt, quote=quote)
    store.log("quote", rt, b["janus_key_id"], f"新报价 {quote}")
    return {"ok": True, "quote": quote}


@app.delete("/api/keys/{rt}")
async def api_revoke(rt: str, request: Request) -> Any:
    _check(request)
    b = store.get_binding(rt)
    if not b or not b.get("janus_key_id"):
        raise HTTPException(status_code=404, detail="没有这条绑定")
    try:
        await janus.delete_key(b["janus_key_id"])
    except ApiError as e:
        raise HTTPException(status_code=e.status, detail=f"{e.code} {e.message}")
    store.upsert_binding(rt, janus_key_id=None, paused_by_us=0, pause_reason=None)
    store.log("revoke", rt, b["janus_key_id"], "面板撤回")
    return {"ok": True}


# ══ 面板 ════════════════════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> Any:
    _check(request)
    try:
        state = await api_state(request)
    except HTTPException:
        raise
    except Exception as e:
        state = {"guard": guard.status(), "bindings": [], "events": [],
                 "remote_count": 0, "boot_error": f"{type(e).__name__}: {e}"}
    golem_view, janus_view = await asyncio.gather(
        _cached_money("golem", _golem_money), _cached_money("janus", _janus_money))
    return templates.TemplateResponse("index.html", {"request": request, **state,
                                                     "golem_view": golem_view,
                                                     "janus_view": janus_view})
