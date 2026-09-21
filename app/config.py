"""集中配置。所有密钥只从环境变量读，绝不落盘、绝不进日志。"""
from __future__ import annotations

import os


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class Settings:
    # ── 两端地址 ────────────────────────────────────────────────
    janus_base: str = _env("JANUS_BASE", "https://gate.oaiai.ai")
    golem_base: str = _env("GOLEM_BASE", "https://fake.oaiai.ai")

    # ── 凭证（Secret 注入，只存在于进程内存）────────────────────
    mgw_key: str = _env("MGW_KEY")                    # 卖货：中心站 API key
    golem_email: str = _env("GOLEM_EMAIL")            # 进货：沙盘控制台账号
    golem_password: str = _env("GOLEM_PASSWORD")

    # ── 守护循环节奏（秒）──────────────────────────────────────
    heartbeat_seconds: int = int(_env("HEARTBEAT_SECONDS", "300"))   # 手册建议 5 分钟
    probe_seconds: int = int(_env("PROBE_SECONDS", "60"))

    # ── 余额阈值：低于它就在同一次动作里直接下架 ────────────────
    # 沙盘没有余额接口，探不到时如实报 null（手册明确要求，不许瞎猜）
    balance_floor: float = float(_env("BALANCE_FLOOR", "1.0"))

    # ── 端口与存储 ──────────────────────────────────────────────
    port: int = int(_env("PORT", "8080"))
    db_path: str = _env("DB_PATH", "/data/pushkey.db")

    # ── 控制台登录（保护面板，避免公网裸奔）────────────────────
    panel_user: str = _env("PANEL_USER", "zbk")
    panel_password: str = _env("PANEL_PASSWORD", "")

    # ── 三类资源：格式是硬约束，推错直接 400 format_mismatch ──
    # resource_type -> 允许的 format
    format_rules: dict[str, str] = {
        "gpt_pool": "openai",
        "deepseek_relay": "anthropic",
        "ccmax_key": "anthropic",
    }

    # ── 我报的折扣（相对官方基准价）。留空则按任务区间自动取中位 ──
    quote_ratio: dict[str, float] = {
        "gpt_pool": 0.35,
        "deepseek_relay": 0.50,
        "ccmax_key": 0.30,
    }

    # ── 每个 resource_type 对应我在 GOLEM 开的哪把 key ─────────
    golem_key_name: dict[str, str] = {
        "gpt_pool": "gpt-pool",
        "deepseek_relay": "deepseek-relay",
        "ccmax_key": "ccmax",
    }

    # ── 上游 key 明文（Secret 播种）─────────────────────────────
    # 守护循环必须拿明文去探活，而 GOLEM **只在创建时回显一次** —— 丢了就再也取不回来。
    # 所以明文随其它凭证一起进 Secret，进程启动时种进本地库。
    # 变量名规则：GOLEM_KEY_<resource_type 大写>。
    # 在类外构建 —— 类体里的推导式看不到类变量（Python 作用域坑）。
    golem_key_seed: dict[str, str] = {}

    # ── 探测时给上游的最小请求参数 ──────────────────────────────
    # 手册提醒：推理模型思维链会吃光 max_tokens，探测要给到 300 以上
    probe_max_tokens: int = int(_env("PROBE_MAX_TOKENS", "300"))

    # ── 账单缓存（秒）──────────────────────────────────────────
    # 读一次两端账单要 ~3s（GOLEM 那边得先登控制台）。面板 60s 自动刷一次，
    # 不能每次都现拉 —— 缓存 TTL 与自动刷新同拍。
    money_ttl: int = int(_env("MONEY_TTL", "60"))


settings = Settings()
settings.golem_key_seed = {rt: _env(f"GOLEM_KEY_{rt.upper()}") for rt in settings.format_rules}
