"""守护循环 —— 本毕设的主考点。

课件口径：**上游断了，你的程序要先知道、先动手。等中心站发现，算你违规。**

    ① 推 key（POST /keys）
    ② 每 5 分钟心跳（POST /keys/{id}/heartbeat），带上游余额
    ③ 判断：余额耗尽？503？429？
          ├─ 正常 → 回 ②
          └─ 异常 ↓
    ④ 立刻下架：PATCH /keys/{id} {"state":"paused","reason":…}
    ⑤ 充上钱 → POST /keys/{id}/resume（中心端会重新探测）

判定用**两个独立信号**，任一命中就下架：
  · 权威信号 —— GOLEM 控制台上那条 key 的 `status`（故障开关直接写在它上面）
  · 业务信号 —— 真发一次最小请求，认响应码（429/503/500/402），**不当普通错误吞掉**
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from .clients import ApiError, GolemClient, JanusClient, mask
from .config import settings
from . import store

# 中心站状态机（只能往前走）：只有这几种状态能被客户端置为 paused
PAUSABLE = {"approved", "available", "imported"}
# GOLEM 的故障开关 → 我该报的标准原因（枚举只有四个，别自己造词）
GOLEM_STATUS_REASON = {
    "balance_exhausted": "balance_exhausted",
    "429": "probe_failed",
    "503": "probe_failed",
    "500": "probe_failed",
}


def classify_http(status: int, note: str) -> str | None:
    """从**响应码**判定该报哪个 reason；健康返回 None。"""
    low = note.lower()
    if status == 402 or "insufficient balance" in low or "余额" in note:
        return "balance_exhausted"
    if status in (429, 500, 502, 503, 504):
        return "probe_failed"
    return None


class Guard:
    def __init__(self, janus: JanusClient, golem: GolemClient) -> None:
        self.janus = janus
        self.golem = golem
        self._tasks: list[asyncio.Task] = []
        self._busy = asyncio.Lock()
        self.last_sync: str | None = None

    # ══ ① 推货 ═══════════════════════════════════════════════════
    async def sync(self, *, push_missing: bool = True) -> dict:
        """拉当前在收的类型与任务单，认领名额，把还没有的 key 推上去。

        **类型/格式一律从接口读，绝不写死** —— 平台改类型时零改动。
        """
        async with self._busy:
            report: dict[str, Any] = {"types": [], "pushed": [], "adopted": [],
                                      "skipped": [], "errors": []}

            rts = (await self.janus.resource_types()).get("items", [])
            tasks = {t["resource_type"]: t for t in (await self.janus.tasks(status="open")).get("items", [])}
            keys = (await self.janus.list_keys()).get("items", [])
            mine = {str(k.get("id")): k for k in keys}
            # 中心站上按资源类型索引 —— 用来对账，避免本地库一丢就重复推货
            remote_by_rt: dict[str, dict] = {}
            for k in keys:
                remote_by_rt.setdefault(str(k.get("resource_type")), k)

            for rt in rts:
                key_name = rt["key"]
                fmt = rt["format"]                       # ← 平台说了算，不是我写死
                report["types"].append({"key": key_name, "format": fmt, "label": rt.get("label")})

                b = store.get_binding(key_name)
                # 本地记的 id 还在中心站上 → 什么都不用做
                if b and b.get("janus_key_id") and str(b["janus_key_id"]) in mine:
                    report["skipped"].append(key_name)
                    continue
                # 本地库不认识，但中心站上已经有这个类型的货 → 认领它，别重复推
                exist = remote_by_rt.get(key_name)
                if exist:
                    # 列表接口不返回 quote，得回读详情才拿得到真实报价
                    detail = await self.janus.get_key(str(exist.get("id")))
                    store.upsert_binding(
                        key_name, janus_key_id=str(exist.get("id")),
                        quote=detail.get("quote") or {},
                        paused_by_us=1 if detail.get("state") == "paused" else 0,
                        pause_reason=detail.get("pause_reason"))
                    store.log("adopt", key_name, str(exist.get("id")),
                              f"中心站已有该类型的货，认领现有 key（state={detail.get('state')}）")
                    report["adopted"].append({"resource_type": key_name,
                                              "janus_key_id": str(exist.get("id")),
                                              "state": detail.get("state"),
                                              "quote": detail.get("quote")})
                    continue

                tpl = settings.golem_key_name.get(key_name)
                if not tpl:
                    report["errors"].append(f"{key_name}: 本地没配对应的 GOLEM key")
                    continue

                # 上游 key 明文：本地库优先，其次 Secret 播种的那份。
                # GOLEM 只在创建时回显一次，没有第三条路。
                b = store.get_binding(key_name) or {}
                api_key = b.get("golem_api_key") or settings.golem_key_seed.get(key_name) or ""
                models = b.get("models") or []
                golem_key_id = b.get("golem_key_id")

                # key_id 和模型清单从 GOLEM 现读 —— 以沙盘为准
                gk = next((k for k in await self.golem.keys() if k["name"] == tpl), None)
                if gk:
                    golem_key_id = golem_key_id or gk["id"]
                    models = models or (gk.get("models") or [])
                if not api_key:
                    report["errors"].append(
                        f"{key_name}: 没有上游 key 明文 —— GOLEM 不回显，请用 GOLEM_KEY_* 播种")
                    continue

                # 任务：认领名额，报价才受区间保护
                task = tasks.get(key_name)
                task_id, quote = None, {}
                if task:
                    ratio = settings.quote_ratio.get(key_name)
                    lo, hi = task.get("min_price_ratio", 0.0), task.get("max_price_ratio", 1.0)
                    if ratio is None:
                        ratio = round((lo + hi) / 2, 4)
                    if not (lo <= ratio <= hi):          # 别等平台回 400，自己先夹住
                        ratio = min(max(ratio, lo), hi)
                    quote = {"price_ratio": ratio}
                    try:
                        await self.janus.claim_task(str(task["id"]))
                        task_id = str(task["id"])
                    except ApiError as e:
                        report["errors"].append(f"{key_name}: 认领任务 {task['id']} 失败 {e.code or e.status} {e.message}")
                    if not models:
                        models = task.get("models") or []
                if not quote:
                    quote = {"price_ratio": 0.5}

                if not push_missing:
                    report["skipped"].append(key_name)
                    continue

                try:
                    res = await self.janus.push_key(
                        resource_type=key_name, fmt=fmt, base_url=settings.golem_base,
                        api_key=api_key, models=models, quote=quote,
                        task_id=task_id, note=f"pushkey 站自动推送 · {tpl}")
                    store.upsert_binding(
                        key_name, format=fmt, golem_key_name=tpl, golem_key_id=golem_key_id,
                        golem_api_key=api_key, models=models, janus_key_id=str(res.get("id")),
                        task_id=task_id, quote=quote, paused_by_us=0, pause_reason=None)
                    store.log("push", key_name, str(res.get("id")),
                              f"format={fmt} models={models} quote={quote} task={task_id}")
                    report["pushed"].append({"resource_type": key_name, "janus_key_id": res.get("id"),
                                             "quote": quote, "task_id": task_id})
                except ApiError as e:
                    store.log("push_failed", key_name, None, f"{e.code or e.status} {e.message}")
                    report["errors"].append(f"{key_name}: 推送失败 {e.code or e.status} {e.message}")

            self.last_sync = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            return report

    def seed(self) -> list[str]:
        """把 Secret 里的上游明文种进本地库（幂等）。

        GOLEM 只在创建 key 时回显一次明文，重启丢库就再也拿不回来 ——
        所以明文必须能从 Secret 重新播种。
        """
        done = []
        for rt, key in settings.golem_key_seed.items():
            if not key:
                continue
            b = store.get_binding(rt)
            if b is None:
                store.upsert_binding(rt, format=settings.format_rules.get(rt, ""),
                                     golem_key_name=settings.golem_key_name.get(rt, ""),
                                     golem_api_key=key, models=[])
            elif not b.get("golem_api_key"):
                store.upsert_binding(rt, golem_api_key=key)
            done.append(rt)
        if done:
            store.log("seed", detail=f"已播种上游明文：{','.join(done)}")
        return done

    # ══ ③④ 监测 + 自动下架 ═══════════════════════════════════════
    async def probe_all(self) -> list[dict]:
        """对每条绑定做一次健康判定，该停的立刻停，该复的申请复。"""
        out: list[dict] = []
        for b in store.all_bindings():
            rt = b["resource_type"]
            kid = b.get("janus_key_id")
            if not kid:
                continue
            rec: dict[str, Any] = {"resource_type": rt, "janus_key_id": kid}

            # ① 权威信号：GOLEM 那条 key 的故障开关
            golem_status = "?"
            try:
                gk = next((k for k in await self.golem.keys() if k["id"] == b.get("golem_key_id")), None)
                if gk:
                    golem_status = gk.get("status", "normal")
            except ApiError as e:
                rec["note"] = f"读 GOLEM key 状态失败：{e.status}"

            # ② 业务信号：真发一次最小请求
            model = (b.get("models") or ["?"])[0]
            http_status, note = 0, ""
            try:
                http_status, note = await self.golem.probe(b["golem_api_key"], b["format"], model)
            except Exception as e:                       # 连不上也算异常，不能吞
                http_status, note = 599, f"probe 异常：{type(e).__name__}"

            reason = GOLEM_STATUS_REASON.get(golem_status) or classify_http(http_status, note)
            healthy = reason is None
            rec.update(golem_status=golem_status, http_status=http_status, reason=reason, healthy=healthy)

            store.upsert_binding(rt, last_probe_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                 last_probe_ok=1 if healthy else 0,
                                 last_probe_note=f"golem={golem_status} http={http_status} {note[:120]}".strip())

            if not healthy:
                rec["action"] = await self._pause(rt, kid, reason)
            else:
                rec["action"] = await self._maybe_resume(rt, kid)
            out.append(rec)
        return out

    async def _pause(self, rt: str, kid: str, reason: str) -> str:
        b = store.get_binding(rt) or {}
        if b.get("paused_by_us"):
            return "已是本站在停"
        try:
            cur = await self.janus.get_key(kid)
            state = cur.get("state")
        except ApiError as e:
            return f"读状态失败 {e.status}"
        if state == "paused":
            store.upsert_binding(rt, paused_by_us=1, pause_reason=reason)
            return "中心站已 paused"
        if state not in PAUSABLE:
            return f"状态 {state} 不可下架（状态机单向）"
        try:
            await self.janus.patch_key(kid, state="paused", reason=reason)
            store.upsert_binding(rt, paused_by_us=1, pause_reason=reason)
            store.log("auto_pause", rt, kid, f"reason={reason}")
            return f"已主动下架 reason={reason}"
        except ApiError as e:
            store.log("auto_pause_failed", rt, kid, f"{e.code or e.status} {e.message}")
            return f"下架失败 {e.code or e.status} {e.message}"

    async def _maybe_resume(self, rt: str, kid: str) -> str:
        b = store.get_binding(rt) or {}
        if not b.get("paused_by_us"):
            return "正常"
        try:
            cur = await self.janus.get_key(kid)
        except ApiError as e:
            return f"读状态失败 {e.status}"
        if cur.get("state") != "paused":
            store.upsert_binding(rt, paused_by_us=0, pause_reason=None)
            return f"恢复正常（{cur.get('state')}）"
        try:
            await self.janus.resume(kid)          # 中心站会重新探测，通过才回到可用
            store.upsert_binding(rt, paused_by_us=0, pause_reason=None)
            store.log("resume", rt, kid, "上游恢复，申请复探")
            return "已申请恢复（中心站重新探测）"
        except ApiError as e:
            return f"申请恢复失败 {e.code or e.status} {e.message}"

    # ══ ② 心跳 ═══════════════════════════════════════════════════
    async def heartbeat_all(self) -> list[dict]:
        out = []
        for b in store.all_bindings():
            rt, kid = b["resource_type"], b.get("janus_key_id")
            if not kid:
                continue
            balance = await self.golem.balance()      # 沙盘没有余额接口 → None
            models_ok, endpoint_ok = [], True
            try:
                st, _ = await self.golem.probe(b["golem_api_key"], b["format"], (b.get("models") or ["?"])[0])
                endpoint_ok = st < 400
                if endpoint_ok:
                    models_ok = b.get("models") or []
            except Exception:
                endpoint_ok = False
            try:
                res = await self.janus.heartbeat(kid, endpoint_ok=endpoint_ok,
                                                 upstream_balance=balance,  # 探不到就 null，不瞎猜
                                                 models_ok=models_ok)
            except ApiError as e:
                out.append({"resource_type": rt, "error": f"{e.code or e.status} {e.message}"})
                continue

            # 回包里的 actions 出现 pause —— 中心端在让我停，必须照做
            actions = res.get("actions") or []
            acted = ""
            if any(a == "pause" for a in actions):
                acted = await self._pause(rt, kid, res.get("reason") or "manual")
            out.append({"resource_type": rt, "state": res.get("state"),
                        "next": res.get("next_heartbeat_seconds"), "actions": actions, "acted": acted})
        return out

    # ══ 后台循环 ═════════════════════════════════════════════════
    async def _loop(self, name: str, every: int, fn) -> None:
        while True:
            try:
                await fn()
            except Exception as e:                    # 循环绝不能因为一次异常就死掉
                store.log("loop_error", detail=f"{name}: {type(e).__name__} {e}")
            await asyncio.sleep(every)

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._loop("probe", settings.probe_seconds, self.probe_all)),
            asyncio.create_task(self._loop("heartbeat", settings.heartbeat_seconds, self.heartbeat_all)),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks = []

    def status(self) -> dict:
        """给面板看的状态。**凭证一律脱敏**，邮箱也是（公网页面没必要露）。"""
        return {"last_sync": self.last_sync,
                "probe_seconds": settings.probe_seconds,
                "heartbeat_seconds": settings.heartbeat_seconds,
                "balance_floor": settings.balance_floor,
                "mgw": mask(settings.mgw_key),
                "golem_account": mask(settings.golem_email),
                "can_push": bool(settings.mgw_key and settings.golem_email
                                 and settings.golem_password)}
