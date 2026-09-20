"""本地状态库（SQLite）。

中心站是「货」的事实来源，本地库只存**推货所需的映射**和**审计事件**：
哪把上游 key 推成了中心站的哪条 key、报的什么价、是不是「我自己停的」。

上游 key 明文必须在本地留一份 —— 守护循环要靠它探活，而 GOLEM 只在创建时回显一次。
它加密落库在中心站那边，这边只能在 UI 上做脱敏。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from .config import settings

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS bindings (
    resource_type   TEXT PRIMARY KEY,
    format          TEXT NOT NULL,
    golem_key_name  TEXT NOT NULL,
    golem_key_id    INTEGER,
    golem_api_key   TEXT NOT NULL,
    models          TEXT NOT NULL DEFAULT '[]',
    janus_key_id    TEXT,
    task_id         TEXT,
    quote           TEXT NOT NULL DEFAULT '{}',
    paused_by_us    INTEGER NOT NULL DEFAULT 0,
    pause_reason    TEXT,
    last_probe_at   TEXT,
    last_probe_ok   INTEGER,
    last_probe_note TEXT,
    upstream_balance REAL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    kind          TEXT NOT NULL,
    resource_type TEXT,
    janus_key_id  TEXT,
    detail        TEXT
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def init() -> None:
    global _conn
    _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(SCHEMA)
    _conn.commit()


def _c() -> sqlite3.Connection:
    assert _conn is not None, "store.init() 未调用"
    return _conn


def log(kind: str, resource_type: str | None = None,
        janus_key_id: str | None = None, detail: str = "") -> None:
    """审计事件。detail 里**永远不许出现上游 key**。"""
    with _lock:
        _c().execute(
            "INSERT INTO events (ts, kind, resource_type, janus_key_id, detail) VALUES (?,?,?,?,?)",
            (_now(), kind, resource_type, janus_key_id, detail[:500]),
        )
        _c().commit()


def _bind(v: Any) -> Any:
    """SQLite 只认标量：list/dict 落库前转 JSON 字符串（读的时候 _row 会转回来）。"""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return int(v)
    return v


def upsert_binding(resource_type: str, **fields: Any) -> None:
    with _lock:
        cur = _c().execute("SELECT resource_type FROM bindings WHERE resource_type=?", (resource_type,))
        fields["updated_at"] = _now()
        values = [_bind(v) for v in fields.values()]
        if cur.fetchone() is None:
            cols = ["resource_type", *fields]
            q = f"INSERT INTO bindings ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
            _c().execute(q, [resource_type, *values])
        else:
            sets = ", ".join(f"{k}=?" for k in fields)
            _c().execute(f"UPDATE bindings SET {sets} WHERE resource_type=?",
                         [*values, resource_type])
        _c().commit()


def get_binding(resource_type: str) -> dict | None:
    row = _c().execute("SELECT * FROM bindings WHERE resource_type=?", (resource_type,)).fetchone()
    return _row(row) if row else None


def all_bindings() -> list[dict]:
    rows = _c().execute("SELECT * FROM bindings ORDER BY resource_type").fetchall()
    return [_row(r) for r in rows]


def recent_events(limit: int = 60) -> list[dict]:
    rows = _c().execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def _row(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("models", "quote"):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                d[k] = [] if k == "models" else {}
    return d
