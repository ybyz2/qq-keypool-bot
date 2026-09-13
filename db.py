"""SQLite 数据层。

两张表：

* ``keys``   —— 池子里的一个 key。``pool`` 是**池**（默认就是群号；混合模式下固定
  为 ``"shared"``），``seq`` 是**池内序号**（1..``SEQ_LIMIT``，群里看到的 #N）。
* ``models`` —— 某个 key 的一个模型状态。

池内序号规则（产品决定）：上限 99，分配时从「当前最大号 +1」往后找第一个空号，
到上限后回绕到 1；99 个号全被占用时不再接受新 key（池满，由调用方回复提示）。
序号是**会被复用**的：删掉的号会被后来的 key 拿走，群友缓存的旧 #N 可能已经不是
原来那个 key 了。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

UTC8 = timezone(timedelta(hours=8))

#: 池内序号上限（到顶后回绕到 1 重新找空号）
SEQ_LIMIT = 99

#: 混合模式（所有群共用一个池）时使用的池名
SHARED_POOL = "shared"


def now_text() -> str:
    return datetime.now(UTC8).strftime("%Y-%m-%d %H:%M")


def connect(path: str | Path = "keypool.db") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pool TEXT NOT NULL,
            seq INTEGER NOT NULL,
            site TEXT NOT NULL,
            nickname TEXT NOT NULL,
            base_url TEXT NOT NULL,
            api_key TEXT NOT NULL,
            added_at TEXT NOT NULL,
            last_check TEXT,
            balance TEXT,
            UNIQUE (pool, seq)
        );
        CREATE TABLE IF NOT EXISTS models (
            key_id INTEGER NOT NULL,
            model_name TEXT NOT NULL,
            status TEXT NOT NULL,
            key_status TEXT,
            rate REAL,
            source TEXT,
            collected_at TEXT NOT NULL,
            PRIMARY KEY (key_id, model_name)
        );
        CREATE INDEX IF NOT EXISTS idx_keys_pool ON keys(pool, seq);
        """
    )
    conn.commit()


def next_seq(conn: sqlite3.Connection, pool: str, limit: int = SEQ_LIMIT) -> int | None:
    """给这个池挑一个新的空号：从最大号 +1 起，到顶回绕；池满返回 None。"""

    used = {int(row["seq"]) for row in conn.execute(
        "SELECT seq FROM keys WHERE pool=?", (pool,))}
    if len(used) >= limit:
        return None
    start = 1 if not used else (max(used) % limit) + 1
    for step in range(limit):
        candidate = (start - 1 + step) % limit + 1
        if candidate not in used:
            return candidate
    return None


def add_key(conn: sqlite3.Connection, pool: str, seq: int, site: str, nickname: str,
            base_url: str, api_key: str) -> int:
    cur = conn.execute(
        "INSERT INTO keys(pool,seq,site,nickname,base_url,api_key,added_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (pool, int(seq), site, nickname, base_url, api_key, now_text()),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_key(conn: sqlite3.Connection, pool: str, seq: int) -> sqlite3.Row | None:
    """按「池 + 池内序号」取 key —— 群里 /查询 /删除 用这个。"""

    return conn.execute(
        "SELECT * FROM keys WHERE pool=? AND seq=?", (pool, int(seq))).fetchone()


def get_key_by_id(conn: sqlite3.Connection, key_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM keys WHERE id=?", (key_id,)).fetchone()


def list_keys(conn: sqlite3.Connection, pool: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM keys WHERE pool=? ORDER BY seq", (pool,)))


def list_pools(conn: sqlite3.Connection) -> list[str]:
    """库里出现过的池（每池一份检测名单 / 每小时采集要按池遍历）。"""

    return [row["pool"] for row in conn.execute(
        "SELECT DISTINCT pool FROM keys ORDER BY pool")]


def count_keys(conn: sqlite3.Connection, pool: str | None = None) -> int:
    if pool is None:
        row = conn.execute("SELECT COUNT(*) AS c FROM keys").fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS c FROM keys WHERE pool=?", (pool,)).fetchone()
    return int(row["c"])


def delete_key(conn: sqlite3.Connection, pool: str, seq: int) -> bool:
    """物理删除一个 key 及其模型状态行；池里没有这个序号时返回 False。"""

    row = get_key(conn, pool, seq)
    if row is None:
        return False
    with conn:
        conn.execute("DELETE FROM models WHERE key_id=?", (row["id"],))
        conn.execute("DELETE FROM keys WHERE id=?", (row["id"],))
    return True


def update_probe(conn: sqlite3.Connection, key_id: int, last_check: str | None = None,
                 balance: str | None = None) -> None:
    fields: list[str] = []
    values: list[Any] = []
    if last_check is not None:
        fields.append("last_check=?")
        values.append(last_check)
    if balance is not None:
        fields.append("balance=?")
        values.append(balance)
    if fields:
        values.append(key_id)
        conn.execute(f"UPDATE keys SET {', '.join(fields)} WHERE id=?", values)
        conn.commit()


def replace_models(conn: sqlite3.Connection, key_id: int,
                   statuses: dict[str, str], collected_at: str | None = None) -> None:
    """整批替换某个 key 的模型状态（不带可用率，测试与脚本用）。"""

    stamp = collected_at or now_text()
    conn.execute("DELETE FROM models WHERE key_id=?", (key_id,))
    conn.executemany(
        "INSERT INTO models(key_id,model_name,status,collected_at) VALUES(?,?,?,?)",
        [(key_id, name, status, stamp) for name, status in statuses.items()],
    )
    conn.commit()


def write_key_models(conn: sqlite3.Connection, key_id: int, rows: list[tuple],
                     collected_at: str, source: str | None = None) -> None:
    """写一个 key 的采集结果，``rows`` 是 (model_name, status, key_status, rate) 四元组。"""

    conn.execute("DELETE FROM models WHERE key_id=?", (key_id,))
    conn.executemany(
        "INSERT INTO models(key_id,model_name,status,key_status,rate,source,collected_at)"
        " VALUES(?,?,?,?,?,?,?)",
        [(key_id, m, st, kst, rate, source, collected_at)
         for m, st, kst, rate in rows],
    )


def get_models(conn: sqlite3.Connection, key_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM models WHERE key_id=? ORDER BY rowid", (key_id,)))


def model_statuses(conn: sqlite3.Connection, key_id: int) -> dict[str, str]:
    return {row["model_name"]: row["status"] for row in get_models(conn, key_id)}


def latest_collected_at(conn: sqlite3.Connection, pool: str | None = None) -> str | None:
    if pool is None:
        row = conn.execute("SELECT MAX(collected_at) AS value FROM models").fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(m.collected_at) AS value FROM models m"
            " JOIN keys k ON k.id = m.key_id WHERE k.pool=?", (pool,)).fetchone()
    return row["value"] if row else None


def today_providers(conn: sqlite3.Connection, pool: str) -> list[sqlite3.Row]:
    """今天在这个池里投过 key 的昵称与条数（日报用）。"""

    today = now_text()[:10]
    return list(conn.execute(
        "SELECT nickname, COUNT(*) AS cnt FROM keys"
        " WHERE pool=? AND added_at LIKE ? GROUP BY nickname",
        (pool, today + "%")))
