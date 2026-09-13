"""每小时模型状态刷新（建议用 systemd timer / cron 调用，独立于机器人进程）。

按**池**采集：每个池用它自己的检测名单、采它自己池子里的 key。名单为空或池里没
key 就跳过。这里只做「无鉴权的公开接口采集」（`/api/pricing` + `/api/perf-metrics`），
**不碰任何 key、不消耗额度、不需要 Authorization**。

**不使用任何大语言模型**：模型状态 = 固定分支判档（见 probe.classify_model_status）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

import bot
import db
import probe

ROOT = Path(__file__).resolve().parent


def pools_to_update(config: dict[str, Any], conn) -> list[str]:
    """要采集的池：白名单群对应的池 + 库里已经存在 key 的池。"""

    if config.get("pool_mode") == "shared":
        return [db.SHARED_POOL]
    pools = {str(group) for group in config["group_whitelist"]}
    pools |= set(db.list_pools(conn))
    return sorted(pools)


def run_update(
    config: dict[str, Any],
    database: str | Path = ROOT / "keypool.db",
    models_dir: str | Path = ROOT / "models",
    *,
    collect_statuses_auto: Callable[..., Any] = probe.collect_statuses_auto,
) -> int:
    conn = db.connect(database)
    try:
        stamp = db.now_text()
        total = 0
        for pool in pools_to_update(config, conn):
            rows = db.list_keys(conn, pool)
            names = bot.read_models(bot.models_path_for(config, models_dir, pool))
            if not rows or not names:
                continue
            collected = collect_statuses_auto(
                rows, names,
                timeout=config["http_timeout"],
                insecure_tls=config["insecure_tls"],
            )
            with conn:
                for row in rows:
                    source = probe.data_source_for(str(row["base_url"]))
                    rows_out = probe.normalize_model_rows(collected.get(row["id"], []))
                    db.write_key_models(conn, row["id"], rows_out, stamp, source)
            total += len(rows)
            print(f"池 {pool}：{len(rows)} 个 key × {len(names)} 个模型")
        print(f"更新完成 {stamp}，共 {total} 个 key")
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="群 Key 池 · 每小时模型状态刷新")
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
    return run_update(config, args.database, args.models_dir)


if __name__ == "__main__":
    raise SystemExit(main())
