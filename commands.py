"""群 Key 池指令解析与文本渲染（纯函数，不碰网络、不碰数据库）。

**不使用任何大语言模型**：所有回复都是固定模板拼接。
"""
from __future__ import annotations

HELP_TEXT = """📖 群 Key 池 · 使用说明
━━━━━━━━━━━━━━

📋 /列表 [页数] — 查看池子里的 key（模型状态每小时自动刷新）

🔄 /更新 — 立即刷新本池的模型状态

🔍 /查询 <序号> — 余额 + 地址 + 密钥

🔍 /查询全部 — 检测全部 key 并删除失效的（静默，无回复）

➕ /提供 站名 你的昵称 地址 密钥 — 投一个 key

🗑 /删除 <序号> — 直接删除（不可恢复）

🔄 /检测列表更新 模型名… — 重设检测名单（整体覆盖，不限个数）

ℹ️ /帮助 — 本说明

━━━━━━━━━━━━━━
📌 序号是池内序号：上限 99，到顶后回绕复用；配置也支持所有群共用一个池。"""

#: 可用率低于这个值显示红叉（可用率 >= 60 显示绿勾）
LOW_AVAILABILITY = 60

LIST_MARK_OK = "✅"
LIST_MARK_BAD = "❌"


def strip_at_segments(message: list[dict], self_qq: int | str) -> str:
    """剥离所有 at 段，保留文本段；只认结构化段。"""
    parts = []
    for segment in message or []:
        if segment.get("type") == "at":
            continue
        if segment.get("type") == "text":
            parts.append(str(segment.get("data", {}).get("text", "")))
    return "".join(parts).strip()


def parse_command(text: str) -> tuple[str, list[str]] | None:
    # 刻意严格：剥离 at 段后的文本必须以 '/' 开头才算指令。
    # 宽松匹配（在句中任意位置找 '/xxx'）会让闲聊里偶然出现的
    # 「/删除 3」触发真实操作，在人人可用的群里不划算。
    text = text.strip()
    if not text.startswith("/"):
        return None
    fields = text.split()
    if not fields:
        return None
    name = fields[0][1:]
    args = fields[1:]
    # 数字可直接贴在命令后：/查询2 == /查询 2（仅无参数时拆分）
    if not args and name:
        stripped = name.rstrip("0123456789")
        if stripped != name:
            args = [name[len(stripped):]]
            name = stripped
    return name, args


def parse_event_command(message: list[dict], self_qq: int | str) -> tuple[str, list[str]] | None:
    return parse_command(strip_at_segments(message, self_qq))


def usage_provide() -> str:
    return "用法：/提供 站名 你的昵称 地址 密钥\n例如：/提供 某中转 小明 https://x.com/v1 sk-xxx"


def _rate_value(rate) -> float | None:
    if rate is None or isinstance(rate, bool):
        return None
    try:
        return float(rate)
    except (TypeError, ValueError):
        return None


def render_model_cell(model_name: str, rate=None) -> str:
    """列表里的单个模型单元格（检测到的模型一律带勾或叉）。

    - 拿得到可用率 → `模型名【✅/❌】可用率NN%`（✅ = 可用率 >= 60，❌ = 低于 60）
    - 拿不到可用率（站点没公开接口 / pricing 不支持 / 近 24h 无流量）→ 只给绿勾
      `模型名【✅】`，不硬凑百分比
    """

    value = _rate_value(rate)
    if value is None:
        return f"{model_name}【{LIST_MARK_OK}】"
    mark = LIST_MARK_OK if value >= LOW_AVAILABILITY else LIST_MARK_BAD
    # 截尾取整（不四舍五入）：否则 59.6 会显示成「❌ 可用率60%」自相矛盾
    return f"{model_name}【{mark}】可用率{int(value)}%"


def render_list(keys, models: list[str], latest: str | None,
                page: int = 1, per_page: int = 5, secondary_label: str = "备用") -> str:
    """渲染 /列表。``keys`` 是**本池**的 key（已按池内序号排序）。"""

    if not keys:
        return "📭 池子是空的\n用 /提供 投一个 key 吧"
    total_pages = max(1, (len(keys) + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    begin = (page - 1) * per_page
    page_keys = keys[begin:begin + per_page]
    lines = ["📋 群 Key 池", "━━━━━━━━━━━━━━"]
    for key in page_keys:
        statuses = {row["model_name"]: row["status"] for row in key.get("models", [])}
        key_st = {row["model_name"]: (row.get("key_status") or "不支持")
                  for row in key.get("models", [])}
        rates = {row["model_name"]: row.get("rate") for row in key.get("models", [])}
        # 检测数据由备用机代答时标注来源；与本地一致则不强调
        suffix = f"（{secondary_label}）" if str(key.get("source") or "1") == "2" else ""
        lines.append(f"#{key['seq']} {key['site']}{suffix}")
        lines.append(f"└ by {key['nickname']}")
        if "CF拦截" in statuses.values() or "CF拦截" in key_st.values():
            lines.append("　被CF拦截无法读取")
            lines.append("")  # key 之间空一行
            continue
        shown = [m for m in key_st if key_st[m] == "可用"]
        if shown:
            values = [render_model_cell(m, rates.get(m)) for m in shown]
            for i in range(0, len(values), 3):
                lines.append("　" + " ｜ ".join(values[i:i + 3]))
        elif key_st:
            lines.append("　无可用模型")
        else:
            lines.append("　待检测")
        lines.append("")  # key 之间空一行
    if len(lines) == 2:
        return "📭 池子里没有可用的 key\n用 /提供 投一个 key 吧"
    lines.append("━━━━━━━━━━━━━━")
    if latest:
        lines.append(f"🕐 更新于 {latest[5:10]} {latest[11:16]}")
    lines.append(f"📄 第 {page}/{total_pages} 页")
    return "\n".join(lines)


def render_query(key, balance: str | None = None, note: str | None = None) -> str:
    lines = [f"🔍 #{key['seq']} {key['site']}"]
    if note:
        lines.append(note)
    if balance:
        lines.append(f"💰 余额：{balance}")
    elif not note or note.startswith("（"):
        lines.append("💰 余额：无接口")
    lines += ["━━━━━━━━━━━━━━", f"🔗 {key['base_url']}", f"🔑 {key['api_key']}"]
    return "\n".join(lines)


def render_help() -> str:
    return HELP_TEXT


def render_model_list(models: list[str]) -> str:
    if not models:
        return "本池的检测名单现在是空的\n用 /检测列表更新 模型名… 填一个（空格分隔）"
    return "🔄 当前检测名单：\n" + "、".join(models)


def render_model_update(models: list[str]) -> str:
    return f"✅ 检测名单已更新（{len(models)} 个）：\n" + "、".join(models)


def render_key_added(seq: int, site: str) -> str:
    return f"✅ 已加入 #{seq} {site}\n模型状态将在下次采集后显示"


def render_deleted(seq: int) -> str:
    return f"🗑 已删除 #{seq}"


def render_pool_full(seq_limit: int) -> str:
    return f"⚠️ 池子满了（序号上限 {seq_limit}），先用 /删除 <序号> 腾个位置再来投"
