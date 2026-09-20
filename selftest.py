"""离线自测：把主考点「上游断了自己先停」的判定与动作机械地验一遍。

线上那三条 key 卡在 `probing`（放行要管理员人工审），所以第 ④ 步没法在真中心站上跑。
这里起一个**桩中心站**，只实现守护循环用到的那几个端点，然后断言：

    上游报 503            → 发出 PATCH state=paused, reason=probe_failed
    上游报余额耗尽        → 发出 PATCH state=paused, reason=balance_exhausted
    上游正常              → 不发 PATCH
    中心站状态不 pausable → 不硬撞，如实返回

跑法（依赖都在镜像里）：
    docker run --rm pushkey:local python selftest.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── 桩中心站：记录收到的每一次 PATCH，供断言 ──────────────────────
PATCHES: list[dict] = []
STATE = {"state": "available"}


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):        # 静音
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/v1/keys/77":
            return self._send({"id": "77", "state": STATE["state"]})
        self._send({"items": []})

    def do_PATCH(self):
        n = int(self.headers.get("content-length", 0))
        PATCHES.append(json.loads(self.rfile.read(n) or b"{}"))
        self._send({"ok": True})

    def do_POST(self):
        self._send({"ok": True, "actions": []})


def serve() -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ── 桩上游：只回答「这条 key 现在什么故障状态」和「探活返回什么码」──
class StubGolem:
    def __init__(self, status: str, http: int):
        self.status, self.http = status, http

    async def keys(self):
        return [{"id": 1, "name": "stub", "status": self.status, "models": ["m"]}]

    async def probe(self, api_key, fmt, model, max_tokens=None):
        return self.http, ("" if self.http < 400 else "stub error")

    async def balance(self):
        return None                    # 沙盘没有余额接口 → 如实 None


# ── 断言小工具 ────────────────────────────────────────────────────
FAILED: list[str] = []


def check(name: str, ok: bool, got: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + ("" if ok else f"   ← 实际 {got}"))
    if not ok:
        FAILED.append(name)


async def scenario(label: str, golem_status: str, http: int, janus_state: str,
                   want_reason: str | None) -> None:
    """want_reason=None 表示「不该发出 PATCH」。"""
    global PATCHES
    PATCHES = []
    STATE["state"] = janus_state

    import pathlib

    from app import store
    from app.clients import JanusClient
    from app.guard import Guard

    # 每个场景一个干净的库 —— 否则上一轮的 paused_by_us 会漏到下一轮
    store._conn = None
    pathlib.Path(os.environ["DB_PATH"]).unlink(missing_ok=True)
    store.init()
    janus = JanusClient()
    guard = Guard(janus, StubGolem(golem_status, http))       # type: ignore[arg-type]

    store.upsert_binding("gpt_pool", format="openai", golem_key_name="stub",
                         golem_key_id=1, golem_api_key="sk-golem-stub",
                         models=["m"], janus_key_id="77", quote={"price_ratio": 0.35})

    rec = (await guard.probe_all())[0]
    print(f"\n[{label}]  上游={golem_status} 响应码={http} 中心站={janus_state}")
    print(f"  判定：reason={rec['reason']}  动作：{rec['action']}")

    if want_reason is None:
        # 上游好 → 不该动手；中心站不可下架 → 想动也动不了，得如实说而不是硬撞
        if golem_status == "normal":
            check("不发 PATCH（上游是好的，不该乱停）", not PATCHES, str(PATCHES))
        else:
            check("不发 PATCH（状态机不让，不硬撞）", not PATCHES, str(PATCHES))
            check("如实说明了为什么没停成",
                  "不可下架" in rec["action"] or "已 paused" in rec["action"], rec["action"])
            check("仍如实报出了 reason", rec["reason"] is not None, str(rec["reason"]))
        await janus.aclose()
        return

    check("确实发出了 PATCH（我自己动手了，没等中心站）", len(PATCHES) == 1, str(PATCHES))
    if PATCHES:
        p = PATCHES[0]
        check('state="paused"', p.get("state") == "paused", str(p))
        check(f'reason="{want_reason}"', p.get("reason") == want_reason, str(p))
    check("reason 是手册枚举里那四个词之一",
          rec["reason"] in {"balance_exhausted", "probe_failed",
                            "heartbeat_timeout", "manual"}, str(rec["reason"]))

    await janus.aclose()


async def main() -> int:
    # 桩服务先起，环境变量必须在**第一次 import app.* 之前**注入 ——
    # config.settings 是模块级单例，导入那一刻就把值读死了。
    srv = serve()
    os.environ["JANUS_BASE"] = f"http://127.0.0.1:{srv.server_port}"
    os.environ["MGW_KEY"] = "mgw_selftest_dummy"
    os.environ["DB_PATH"] = "/tmp/selftest.db"

    import pathlib
    pathlib.Path("/tmp/selftest.db").unlink(missing_ok=True)

    print("=" * 62)
    print("守护站自测：上游断了，我有没有先知道、先动手")
    print("=" * 62)

    await scenario("上游 503", "503", 503, "available", "probe_failed")
    await scenario("上游余额耗尽", "balance_exhausted", 402, "available", "balance_exhausted")
    await scenario("上游 429 限流", "429", 429, "available", "probe_failed")
    await scenario("上游正常（对照）", "normal", 200, "available", None)
    await scenario("中心站还在 pending（不可下架）", "503", 503, "pending", None)

    srv.shutdown()
    print("\n" + "=" * 62)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "、".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
