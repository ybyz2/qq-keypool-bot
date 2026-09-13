"""每日播报（建议用 systemd timer / cron 每天定时调一次，独立于机器人进程）。

每个池走一遍：

1. 用**真实 key** 测活 + 探余额（这一步消耗 key，所以只在日报和 `/查询` 里做）
2. 判死（明确余额耗尽 / 站方明确报令牌无效）→ **物理删除**
3. 采集模型状态（公开接口，不消耗 key）
4. 把「本池的播报」发到群里

* ``pool_mode = "per_group"``：每个群收到的是**本群池**的播报
* ``pool_mode = "shared"``：一份全局播报，发给 ``daily_group_whitelist`` 里的所有群
* 不在 ``daily_group_whitelist`` 里的群不接收播报（但它的池照样会被检测）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

import bot
import db
import probe

ROOT = Path(__file__).resolve().parent


def render_report(conn, pool: str) -> str:
    """本池的播报正文：今日提供人 + 目前池中数量。"""

    providers = db.today_providers(conn, pool)
    total = db.count_keys(conn, pool)
    lines = ["☀️ 每日播报"]
    if providers:
        names = "、".join(f"{row['nickname']}（{row['cnt']}）" for row in providers)
        lines.append(f"📤 今日提供：{names}")
    else:
        lines.append("📤 今日暂无提供")
    lines.append(f"🗝 目前池中 key：{total} 个")
    return "\n".join(lines)


def pools_for_daily(config: dict[str, Any], conn) -> list[str]:
    """要检测的池：白名单群对应的池 + 库里已经存在 key 的池。"""

    if config.get("pool_mode") == "shared":
        return [db.SHARED_POOL]
    pools = {str(group) for group in config["daily_group_whitelist"]}
    pools |= set(db.list_pools(conn))
    return sorted(pools)


def recipients_for(config: dict[str, Any], pool: str) -> list[int]:
    """这个池的播报该发到哪些群。"""

    if config.get("pool_mode") == "shared":
        return [int(group) for group in config["daily_group_whitelist"]]
    try:
        group_id = int(pool)
    except ValueError:
        return []
    return [group_id] if group_id in config["daily_group_whitelist"] else []


def run_daily(
    config: dict[str, Any],
    database: str | Path = ROOT / "keypool.db",
    models_dir: str | Path = ROOT / "models",
    *,
    check_key: Callable[..., Any] = probe.check_key,
    probe_balance: Callable[..., Any] = probe.probe_balance,
    collect_statuses_auto: Callable[..., Any] = probe.collect_statuses_auto,
    send_message: Callable[[dict[str, Any], int, str], None] = bot.send_group_message,
) -> list[tuple[str, str]]:
    """跑一轮播报，返回 [(池, 播报文本), ...]（没发出去的池也会返回文本）。"""

    conn = db.connect(database)
    try:
        reports: list[tuple[str, str]] = []
        for pool in pools_for_daily(config, conn):
            rows = db.list_keys(conn, pool)
            if not rows:
                continue
            doomed: list[tuple[int, int]] = []      # [(key_id, seq)]
            stamp = db.now_text()
            for row in rows:
                result = check_key(
                    row["base_url"], row["api_key"],
                    timeout=config["http_timeout"], insecure_tls=config["insecure_tls"],
                )
                balance = None
                if result.status == "alive":
                    balance_result = probe_balance(
                        row["base_url"], row["api_key"],
                        timeout=config["http_timeout"], insecure_tls=config["insecure_tls"],
                    )
                    if balance_result.exhausted:
                        doomed.append((row["id"], row["seq"]))
                    else:
                        balance = balance_result.text
                elif result.status == "dead":
                    doomed.append((row["id"], row["seq"]))
                db.update_probe(conn, row["id"], stamp, balance)

            names = bot.read_models(bot.models_path_for(config, models_dir, pool))
            if names:
                collected = collect_statuses_auto(
                    db.list_keys(conn, pool), names,
                    timeout=config["http_timeout"],
                    insecure_tls=config["insecure_tls"],
                )
                with conn:
                    for row in db.list_keys(conn, pool):
                        source = probe.data_source_for(str(row["base_url"]))
                        rows_out = probe.normalize_model_rows(collected.get(row["id"], []))
                        db.write_key_models(conn, row["id"], rows_out, stamp, source)

            # 判死即物理删除（这里只删本池里、序号仍然对得上的行）
            for _key_id, seq in doomed:
                db.delete_key(conn, pool, seq)

            text = render_report(conn, pool)
            reports.append((pool, text))
            for group_id in recipients_for(config, pool):
                send_message(config, group_id, text)
        return reports
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="群 Key 池 · 每日播报")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--database", type=Path, default=ROOT / "keypool.db")
    parser.add_argument("--models-dir", type=Path, default=ROOT / "models")
    args = parser.parse_args(argv)
    try:
        config = bot.load_config(args.config)
    except bot.ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    bot.apply_secondary(config)
    reports = run_daily(config, args.database, args.models_dir)
    for pool, text in reports:
        print(f"── 池 {pool} ──")
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
