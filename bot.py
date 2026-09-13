"""OneBot v11 群机器人入口（HTTP 上报接收 + 调 OneBot 发送接口回消息）。

**不使用任何大语言模型**：指令是固定字符串解析，回复是固定模板拼接。

部署形态：本文件是一个常驻 HTTP 服务，监听 ``listen_host:listen_port``，接收
OneBot v11 实现的**事件上报**（POST ``/onebot``），把回复通过 OneBot 的
``/send_group_msg`` 接口发回群里。任何 OneBot v11 实现都能对接（NapCat、Lagrange、
go-cqhttp 等）；本项目**不包含**任何聊天客户端本身及其部署脚本。

池（pool）：默认每个群一个独立池（``pool_mode: "per_group"``），也能配成所有群
共用一个池（``"shared"``）。池内序号从 1 开始、上限 99、到顶回绕复用。
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import commands
import db
import probe

ROOT = Path(__file__).resolve().parent

POOL_MODES = ("per_group", "shared")
MODEL_LIST_MODES = ("per_pool", "shared")

REQUIRED_CONFIG = {
    "onebot_url": str,
    "onebot_token": str,
    "listen_host": str,
    "listen_port": int,
    "self_qq": int,
    "group_whitelist": list,
    "daily_group_whitelist": list,
    "pool_mode": str,
    "model_list_mode": str,
    "insecure_tls": bool,
    "http_timeout": (int, float),
}


class ConfigError(ValueError):
    pass


def load_config(path: str | Path = ROOT / "config.json") -> dict[str, Any]:
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as stream:
            config = json.load(stream)
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在：{path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"配置文件格式错误：{exc}") from exc
    if not isinstance(config, dict):
        raise ConfigError("配置文件顶层必须是对象")
    for name, expected in REQUIRED_CONFIG.items():
        if name not in config:
            raise ConfigError(f"配置缺少字段：{name}")
        if not isinstance(config[name], expected) or isinstance(config[name], bool) and name == "http_timeout":
            raise ConfigError(f"配置字段类型错误：{name}")
    for name in ("group_whitelist", "daily_group_whitelist"):
        if not all(isinstance(group, int) and not isinstance(group, bool)
                   for group in config[name]):
            raise ConfigError(f"{name} 必须是群号整数列表")
    if config["pool_mode"] not in POOL_MODES:
        raise ConfigError(f'pool_mode 只能是 "per_group"（每群一个池）或 "shared"（全局共用一个池）')
    if config["model_list_mode"] not in MODEL_LIST_MODES:
        raise ConfigError(f'model_list_mode 只能是 "per_pool"（每池一份检测名单）或 "shared"（全局共用一份）')
    if not 1 <= config["listen_port"] <= 65535:
        raise ConfigError("listen_port 必须在 1 到 65535 之间")
    if config["http_timeout"] <= 0:
        raise ConfigError("http_timeout 必须大于 0")
    seq_limit = config.get("seq_limit", db.SEQ_LIMIT)
    if not isinstance(seq_limit, int) or isinstance(seq_limit, bool) or seq_limit <= 0:
        raise ConfigError("seq_limit 必须是正整数（默认 99）")
    secondary = config.get("secondary", {})
    if not isinstance(secondary, dict):
        raise ConfigError("secondary 必须是对象（可以留空 {}）")
    return config


def apply_secondary(config: dict[str, Any]) -> None:
    """把 config 的 secondary 段交给 probe（默认全关 = 纯单机运行）。"""

    secondary = config.get("secondary") or {}
    probe.configure_secondary(
        enabled=bool(secondary.get("enabled", False)),
        ssh_target=secondary.get("ssh_target", ""),
        script_path=secondary.get("script_path", probe.SECONDARY_SCRIPT_PATH),
        label=secondary.get("label", "备用"),
    )


def seq_limit_of(config: dict[str, Any]) -> int:
    return int(config.get("seq_limit", db.SEQ_LIMIT))


def pool_of(config: dict[str, Any], group_id: int) -> str:
    """事件来自哪个群 → 落到哪个池。"""

    if config.get("pool_mode") == "shared":
        return db.SHARED_POOL
    return str(group_id)


def models_path_for(config: dict[str, Any], models_dir: str | Path, pool: str) -> Path:
    """某个池的检测名单文件；``model_list_mode: "shared"`` 时所有池共用一份。"""

    if config.get("model_list_mode") == "shared":
        return Path(models_dir) / "shared.txt"
    return Path(models_dir) / f"{pool}.txt"


def read_models(path: str | Path) -> list[str]:
    """读检测名单；文件不存在等价于空名单（空名单 = 不采集）。"""

    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            return [line.strip() for line in stream if line.strip()]
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ConfigError(f"读取检测名单失败：{exc}") from exc


def write_models(models: list[str], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        temporary.write_text("".join(f"{name}\n" for name in models), encoding="utf-8")
        temporary.replace(target)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConfigError(f"写入检测名单失败：{exc}") from exc


def has_self_at(message: Any, self_qq: int) -> bool:
    if not isinstance(message, list):
        return False
    return any(
        isinstance(segment, dict)
        and segment.get("type") == "at"
        and str(segment.get("data", {}).get("qq")) == str(self_qq)
        for segment in message
    )


def event_command(event: Any, config: dict[str, Any]) -> tuple[str, list[str]] | None:
    if not isinstance(event, dict):
        return None
    if event.get("post_type") != "message" or event.get("message_type") != "group":
        return None
    if event.get("group_id") not in config["group_whitelist"]:
        return None
    message = event.get("message")
    # 白名单群里直接发中文斜杠指令即可（不强制 @ 机器人）；
    # 非指令文本由 parse_command 返回 None，不会触发回复。
    return commands.parse_event_command(message, config["self_qq"])


def send_group_message(config: dict[str, Any], group_id: int, text: str) -> None:
    """调 OneBot v11 的 ``/send_group_msg`` 发群消息。"""

    url = config["onebot_url"].rstrip("/") + "/send_group_msg"
    payload = json.dumps({"group_id": group_id, "message": text},
                         ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if config["onebot_token"]:
        headers["Authorization"] = "Bearer " + config["onebot_token"]
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=config["http_timeout"]) as response:
        response.read()


def _key_view(conn, row) -> dict[str, Any]:
    value = dict(row)
    value["models"] = [dict(item) for item in db.get_models(conn, row["id"])]
    return value


def render_pool(conn, model_names: list[str], pool: str, page: int = 1) -> str:
    rows = db.list_keys(conn, pool)
    views = [_key_view(conn, row) for row in rows]
    return commands.render_list(
        views, model_names, db.latest_collected_at(conn, pool), page=page,
        secondary_label=probe.SECONDARY_LABEL,
    )


class CommandService:
    """一条指令 → 一段回复文本。``pool`` 是本次指令所属的池（群）。"""

    def __init__(self, conn, config: dict[str, Any], models_dir: str | Path,
                 check_key: Callable[..., Any] | None = None,
                 probe_balance: Callable[..., Any] | None = None,
                 collect_statuses: Callable[..., Any] | None = None,
                 collect_statuses_auto: Callable[..., Any] | None = None):
        self.conn = conn
        self.config = config
        self.models_dir = Path(models_dir)
        self.check_key = check_key or probe.check_key
        self.probe_balance = probe_balance or probe.probe_balance
        self.collect_statuses = collect_statuses or probe.collect_model_statuses
        self.collect_statuses_auto = collect_statuses_auto or probe.collect_statuses_auto

    # ── 指令分发 ──────────────────────────────────────────────────────
    def execute(self, name: str, args: list[str], pool: str) -> str | None:
        if name == "帮助":
            return commands.render_help()
        if name == "列表":
            # 只读记录：默认第 1 页；/列表2 == /列表 2
            return self._render(pool, page=self._page_arg(args))
        if name == "更新":
            # 手动立即刷新本池模型状态
            self._refresh_model_statuses(pool)
            return self._render(pool)
        if name == "查询":
            return self._query(args, pool)
        if name == "查询全部":
            # 静默命令：本池全部检测，判死即删，不输出
            self._query_all(pool)
            return None
        if name == "提供":
            return self._provide(args, pool)
        if name == "删除":
            return self._delete(args, pool)
        if name == "检测列表更新":
            return self._update_models(args, pool)
        return "未知指令，用 /帮助 查看用法"

    def _models_path(self, pool: str) -> Path:
        return models_path_for(self.config, self.models_dir, pool)

    def _models(self, pool: str) -> list[str]:
        return read_models(self._models_path(pool))

    def _render(self, pool: str, page: int = 1) -> str:
        return render_pool(self.conn, self._models(pool), pool, page)

    def _page_arg(self, args: list[str]) -> int:
        if not args:
            return 1
        value = self._number(args, "/列表 <页数>")
        return 1 if isinstance(value, str) else value

    @staticmethod
    def _number(args: list[str], usage: str) -> int | str:
        if len(args) != 1:
            return usage
        raw = args[0][1:] if args[0].startswith("#") else args[0]
        try:
            value = int(raw)
        except ValueError:
            return usage
        if value <= 0:
            return usage
        return value

    # ── 模型状态采集 ──────────────────────────────────────────────────
    def _refresh_model_statuses(self, pool: str) -> None:
        """采集本池的 key：站点级+key级双状态；白名单无命中时自动补选 5 个模型。"""

        rows = db.list_keys(self.conn, pool)
        names = self._models(pool)
        if not rows or not names:
            return
        collected = self.collect_statuses_auto(
            rows, names,
            timeout=self.config["http_timeout"],
            insecure_tls=self.config["insecure_tls"],
        )
        stamp = db.now_text()
        with self.conn:
            for row in rows:
                source = probe.data_source_for(str(row["base_url"]))
                rows_out = probe.normalize_model_rows(collected.get(row["id"], []))
                db.write_key_models(self.conn, row["id"], rows_out, stamp, source)

    # ── /查询 ─────────────────────────────────────────────────────────
    def _query(self, args: list[str], pool: str) -> str | None:
        seq = self._number(args, "/查询 <序号>")
        if isinstance(seq, str):
            return seq
        row = db.get_key(self.conn, pool, seq)
        if row is None:
            # 本池里没有这个序号（含已被删除的）→ 静默不回复
            return None
        result = self.check_key(
            row["base_url"], row["api_key"],
            timeout=self.config["http_timeout"],
            insecure_tls=self.config["insecure_tls"],
        )
        balance = None
        if result.status == "alive":
            balance_result = self.probe_balance(
                row["base_url"], row["api_key"],
                timeout=self.config["http_timeout"],
                insecure_tls=self.config["insecure_tls"],
            )
            if balance_result.exhausted:
                result = probe.ProbeResult("dead", "余额耗尽", result.models)
            else:
                balance = balance_result.text
                if balance and probe.data_source_for(row["base_url"]) == "2":
                    # 余额请求由备用机代答 → 标注来源
                    balance = f"{balance}{probe.secondary_suffix()}"
        db.update_probe(self.conn, row["id"], db.now_text(), balance)
        note = None
        if result.status == "dead":
            # 判死即物理删除；unknown=连不上/被墙，保留不删
            db.delete_key(self.conn, pool, seq)
            note = f"⛔ 检测失败（{result.reason or '已失效'}）— 已自动删除"
        elif result.status == "unknown":
            if result.reason == "CF拦截":
                note = "🛡 被CF拦截无法读取，暂不删除"
            elif result.reason in ("401", "403"):
                # 认证/权限拒绝一律保留，只有「明确余额耗尽」才判死删除
                note = f"🔒 站点拒绝访问（{result.reason}），暂不删除"
            else:
                note = "⚠️ 暂时连不上，暂不删除"
        if result.note:
            # 备用机保底复测结论与本地冲突时的标注（（备用:…））
            note = f"{note}\n{result.note}" if note else result.note
        return commands.render_query(dict(row), balance, note)

    def _query_all(self, pool: str) -> None:
        """本池全部 key 检测、判死即物理删除；静默执行不输出。"""

        for row in db.list_keys(self.conn, pool):
            result = self.check_key(
                row["base_url"], row["api_key"],
                timeout=self.config["http_timeout"],
                insecure_tls=self.config["insecure_tls"],
            )
            balance = None
            if result.status == "alive":
                balance_result = self.probe_balance(
                    row["base_url"], row["api_key"],
                    timeout=self.config["http_timeout"],
                    insecure_tls=self.config["insecure_tls"],
                )
                if balance_result.exhausted:
                    result = probe.ProbeResult("dead", "余额耗尽", result.models)
                else:
                    balance = balance_result.text
            db.update_probe(self.conn, row["id"], db.now_text(), balance)
            if result.status == "dead":
                # 与 /查询 口径一致：判死即物理删除
                db.delete_key(self.conn, pool, row["seq"])

    # ── /提供 /删除 /检测列表更新 ─────────────────────────────────────
    def _provide(self, args: list[str], pool: str) -> str:
        if len(args) != 4:
            return commands.usage_provide()
        site, nickname, base_url, api_key = args
        if not base_url.startswith(("http://", "https://")):
            return "地址必须以 http:// 或 https:// 开头"
        seq = db.next_seq(self.conn, pool, seq_limit_of(self.config))
        if seq is None:
            return commands.render_pool_full(seq_limit_of(self.config))
        db.add_key(self.conn, pool, seq, site, nickname, base_url, api_key)
        return commands.render_key_added(seq, site)

    def _delete(self, args: list[str], pool: str) -> str:
        seq = self._number(args, "/删除 <序号>")
        if isinstance(seq, str):
            return seq
        if not db.delete_key(self.conn, pool, seq):
            return f"没有 #{seq}"
        return commands.render_deleted(seq)

    def _update_models(self, args: list[str], pool: str) -> str:
        if not args:
            return commands.render_model_list(self._models(pool))
        write_models(args, self._models_path(pool))
        # 只重设名单；模型状态采集由每小时定时任务（update.py）自动进行。
        return commands.render_model_update(args)


def make_handler(service: CommandService, config: dict[str, Any]):
    class OneBotHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/onebot":
                self.send_error(404)
                return
            try:
                transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
                if "chunked" in transfer_encoding:
                    chunks = bytearray()
                    while True:
                        line = self.rfile.readline(64).strip()
                        if not line:
                            raise ValueError("chunk 长度无效")
                        chunk_size = int(line.split(b";", 1)[0], 16)
                        if chunk_size == 0:
                            self.rfile.readline()
                            break
                        if chunk_size > 1024 * 1024 or len(chunks) + chunk_size > 1024 * 1024:
                            raise ValueError("消息体过大")
                        chunk = self.rfile.read(chunk_size)
                        if len(chunk) != chunk_size:
                            raise ValueError("chunk 数据不完整")
                        chunks.extend(chunk)
                        if self.rfile.read(2) != b"\r\n":
                            raise ValueError("chunk 结尾无效")
                    raw_body = bytes(chunks)
                else:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 1024 * 1024:
                        raise ValueError("消息体长度无效")
                    raw_body = self.rfile.read(length)
                event = json.loads(raw_body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self.send_response(400, "Bad Request")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(b'{"status":"failed","message":"invalid json"}')
                return
            parsed = event_command(event, config)
            self.send_response(204)
            self.end_headers()
            if parsed is None:
                return
            group_id = event["group_id"]
            text = service.execute(parsed[0], parsed[1], pool_of(config, group_id))
            if not text:
                # 静默：/查询全部，以及 /查询 一个本池不存在的序号
                return
            try:
                send_group_message(config, group_id, text)
            except (OSError, urllib.error.URLError) as exc:
                print(f"发送群消息失败：{exc}", file=sys.stderr)

        def log_message(self, format: str, *args: Any) -> None:
            print("OneBot：" + format % args)

    return OneBotHandler


def run_selftest() -> int:
    """不联网、不连 QQ 的端到端自检：走完整指令链路，打印每步回复。"""

    from dataclasses import dataclass

    @dataclass
    class FakeProbe:
        status: str
        reason: str
        models: list[str]
        note: str | None = None
        via_secondary: bool = False

    @dataclass
    class FakeBalance:
        text: str | None
        exhausted: bool

    config = {
        "onebot_url": "http://127.0.0.1:3010", "onebot_token": "",
        "listen_host": "127.0.0.1", "listen_port": 3020, "self_qq": 1,
        "group_whitelist": [10001, 10002], "daily_group_whitelist": [10001],
        "pool_mode": "per_group", "model_list_mode": "per_pool",
        "insecure_tls": False, "http_timeout": 1, "seq_limit": 99,
        "secondary": {"enabled": False},
    }
    names = ["demo-model", "demo-model-2"]
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        conn = db.connect(directory / "selftest.db")
        models_dir = directory / "models"
        write_models(names, models_dir / "10001.txt")
        service = CommandService(
            conn, config, models_dir,
            check_key=lambda *a, **k: FakeProbe("alive", "200", names),
            probe_balance=lambda *a, **k: FakeBalance("剩 $1.00 / $2.00", False),
            collect_statuses_auto=lambda keys, model_names, **kw: {
                row["id"]: [(name, "良好", "可用", 97.5) for name in model_names]
                for row in keys
            },
        )
        steps = [
            ("提供", ["自检站", "自检用户", "https://example.test/v1", "sk-selftest"], "10001"),
            ("列表", [], "10001"),
            ("查询", ["1"], "10001"),
            # 另一个群的池里没有 #1 → 静默不回复（打印 None）
            ("查询", ["1"], "10002"),
            ("更新", [], "10001"),
            ("列表", [], "10001"),
            ("删除", ["1"], "10001"),
            ("查询", ["1"], "10001"),
            ("列表", [], "10001"),
            ("检测列表更新", ["demo-model-3"], "10001"),
            ("列表", [], "10001"),
        ]
        for name, args, pool in steps:
            print(f"\n> [{pool}] /{name} {' '.join(args)}".rstrip())
            print(service.execute(name, args, pool))
        conn.close()
    print("\n离线自检通过")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="群 Key 池 OneBot HTTP 服务")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json", help="配置文件路径")
    parser.add_argument("--database", type=Path, default=ROOT / "keypool.db", help="数据库路径")
    parser.add_argument("--models-dir", type=Path, default=ROOT / "models",
                        help="检测名单目录（每池一个文件；shared 模式共用 shared.txt）")
    parser.add_argument("--selftest", action="store_true", help="运行不联网的端到端自检")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.selftest:
        return run_selftest()
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    apply_secondary(config)
    conn = db.connect(args.database)
    service = CommandService(conn, config, args.models_dir)
    server = ThreadingHTTPServer(
        (config["listen_host"], config["listen_port"]),
        make_handler(service, config),
    )
    print(f"正在监听 http://{config['listen_host']}:{config['listen_port']}/onebot")
    print(f"池模式：{config['pool_mode']}；检测名单模式：{config['model_list_mode']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
