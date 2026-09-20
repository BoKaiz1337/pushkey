"""pushkey —— 上游守护站。

我卖的东西，我自己盯着：推货、报价、心跳、**上游断了自己先停**。
"""
from __future__ import annotations

import base64
import contextlib
import secrets
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
        r["quote_remote"] = live.get("quote_ratio")
    return {"guard": guard.status(), "bindings": rows,
            "events": store.recent_events(40), "remote_count": len(remote)}


@app.get("/api/upstream")
async def api_upstream(request: Request) -> Any:
    """进货口视角：余额/账单/用量。"""
    _check(request)
    try:
        return {"overview": await golem.overview(),
                "bill": await golem.bill(7),
                "usage": await golem.usage(7),
                "balance": await golem.balance()}
    except ApiError as e:
        raise HTTPException(status_code=502, detail=f"{e.status} {e.message}")


@app.get("/api/earnings")
async def api_earnings(request: Request) -> Any:
    """卖货口视角：中心站的用量与结算单。"""
    _check(request)
    try:
        return {"usage": await janus.usage(), "settlements": await janus.settlements(),
                "me": await janus.me()}
    except ApiError as e:
        raise HTTPException(status_code=502, detail=f"{e.status} {e.message}")


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
    return templates.TemplateResponse("index.html", {"request": request, **state,
                                                     "mask": mask})
