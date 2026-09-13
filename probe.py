"""站点模型状态采集、Key 测活和余额探测。

本模块只负责网络探测，不写数据库。调用方应先完成所有网络请求，再开启短事务
写入结果，以免网络等待阻塞数据库。

**全程不使用任何大语言模型**：判定全部是固定分支（HTTP 状态码 + 响应体关键词），
输出全部是模板字符串拼接。

可选的「备用机保底」（默认关闭，`config.json` 的 `secondary.enabled=true` 才启用）：
本机被判 CF 拦截的 HTTP 请求、以及判死/连不上的测活结论，会自动经 SSH 通道转发给
另一台机器上的 `relay_probe.py` 复测。备用机结果与本地一致则不标注；冲突时保留 key
（不删除），`/查询` 显示「（备用:…）」。SSH 别名由 `secondary.ssh_target` 指定，
需事先配好免密登录。
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.client import HTTPResponse
from typing import Any, Iterable, Mapping, NamedTuple, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


DEFAULT_TIMEOUT = 20.0
UTC_PLUS_8 = timezone(timedelta(hours=8))
MODEL_STATUSES = ("良好", "不稳", "较差", "无数据", "不支持")
CHEAP_MODEL_MARKERS = ("flash", "mini", "lite", "turbo")
FALLBACK_MODEL = "gpt-4o-mini"
# 判死口径（本项目最关键的一条取舍）：
#   只有「明确余额耗尽」（402/429 + 余额关键词）和「站方明确回令牌无效」（401 +
#   Invalid token / 无效的令牌）才判死并删除；401/403 这类认证拒绝一律保留在池中
#   —— 站点会瞬时误报，删掉就再也找不回来了。
DEAD_KEYWORDS = (
    "insufficient",
    "quota",
    "额度",
    "余额不足",
    "已用尽",
)

INVALID_TOKEN_KEYWORDS = (
    "invalid token",
    "无效的令牌",
)

# ── 可选的「备用机保底」 ──────────────────────────────────────────────
# 默认关闭（config.json 里 secondary.enabled 置 true 才启用）。开启后：本机被 CF
# 拦截、或测活判死/连不上时，经 SSH 让备用机复测。约定：备用机结果与本地一致则不
# 标注；冲突以备用机为准（保底不删除），reason 附「（备用:…）」；本机读不到、由
# 备用机代答的数据以「（备用）」标注来源。
SECONDARY_ENABLED = False
SECONDARY_SSH_TARGET = ""
SECONDARY_SCRIPT_PATH = "/opt/keypool-relay/relay_probe.py"
SECONDARY_LABEL = "备用"
_SECONDARY_CACHE_TTL = 300.0
_SECONDARY_CACHE = {"ts": 0.0, "ok": False}
_SECONDARY_STATE = threading.local()


def configure_secondary(*, enabled: bool | None = None, ssh_target: str | None = None,
                        script_path: str | None = None, label: str | None = None) -> None:
    """按 config.json 的 ``secondary`` 段配置保底链路（默认全关，不碰就是单机）。"""

    global SECONDARY_ENABLED, SECONDARY_SSH_TARGET, SECONDARY_SCRIPT_PATH, SECONDARY_LABEL
    if enabled is not None:
        SECONDARY_ENABLED = bool(enabled)
    if ssh_target is not None:
        SECONDARY_SSH_TARGET = str(ssh_target)
    if script_path is not None:
        SECONDARY_SCRIPT_PATH = str(script_path)
    if label is not None:
        SECONDARY_LABEL = str(label)
    _SECONDARY_CACHE.update(ts=0.0, ok=False)


def secondary_suffix() -> str:
    """「（备用）」这种标注样式，列表/查询共用。"""

    return f"（{SECONDARY_LABEL}）"


def _secondary_available() -> bool:
    """备用机探测只在开关打开、SSH 免密可用时启用；Windows 开发环境/未配目标时关闭。"""

    if not SECONDARY_ENABLED or os.name == "nt" or not SECONDARY_SSH_TARGET:
        return False
    now = time.monotonic()
    if now - _SECONDARY_CACHE["ts"] < _SECONDARY_CACHE_TTL:
        return bool(_SECONDARY_CACHE["ok"])
    try:
        ok = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             SECONDARY_SSH_TARGET, "true"],
            capture_output=True, timeout=15,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    _SECONDARY_CACHE.update(ts=now, ok=ok)
    return ok


def _mark_secondary(url: str) -> None:
    host = (urlsplit(url).hostname or "").casefold()
    if not hasattr(_SECONDARY_STATE, "hosts"):
        _SECONDARY_STATE.hosts = set()
        _SECONDARY_STATE.count = 0
    _SECONDARY_STATE.hosts.add(host)
    _SECONDARY_STATE.count = int(_SECONDARY_STATE.count) + 1


def _secondary_rescue_count() -> int:
    return int(getattr(_SECONDARY_STATE, "count", 0))


def secondary_hosts() -> frozenset[str]:
    """本次线程已由 备用机代答过的主机集合（模型采集写 source 列用）。"""
    return frozenset(getattr(_SECONDARY_STATE, "hosts", set()))


def data_source_for(base_url: str) -> str:
    """该 key 的检测数据是否来自 备用机代答（"2"）还是本机（"1"）。"""
    host = (urlsplit(base_url).hostname or "").casefold()
    return "2" if host in secondary_hosts() else "1"


@dataclass(frozen=True)
class ProbeResult:
    """一次 Key 测活结果。

    ``status`` 为 ``alive``、``dead`` 或 ``unknown``；``reason`` 可用于日报的
    自动删除说明；``models`` 是 ``/models`` 返回的真实模型名列表。
    ``note`` 是给 /查询 的补充行（备用机保底冲突说明等）；``via_secondary`` 表示最终
    结论由 备用机保底复测得出。
    """

    status: str
    reason: str | None = None
    models: tuple[str, ...] = ()
    note: str | None = None
    via_secondary: bool = False


@dataclass(frozen=True)
class BalanceResult:
    """余额探测结果；``daily`` 可直接读取 ``text`` 和 ``exhausted``。"""

    text: str | None
    exhausted: bool

    @property
    def balance(self) -> str | None:
        """兼容更直观的余额属性名。"""

        return self.text


class _JsonResponse(NamedTuple):
    status: int
    data: Any
    text: str


def _timeout_value(timeout: float | int) -> float:
    value = float(timeout)
    if value <= 0:
        raise ValueError("HTTP 超时必须大于 0")
    return value


def _open(request: Request, timeout: float | int, insecure_tls: bool) -> HTTPResponse:
    """打开一个请求；仅在明确配置时跳过 TLS 证书校验。"""

    kwargs: dict[str, Any] = {"timeout": _timeout_value(timeout)}
    if insecure_tls:
        # 此开关只应给确实使用自签证书的站点开启。
        kwargs["context"] = ssl._create_unverified_context()
    return urlopen(request, **kwargs)


def _request_json(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
    timeout: float | int = DEFAULT_TIMEOUT,
    insecure_tls: bool = False,
) -> _JsonResponse:
    encoded: bytes | None = None
    # 浏览器 UA：部分中转站用 Cloudflare/WAF 按 UA 指纹拦截 python-urllib 默认 UA
    request_headers = {
        "Accept": "application/json",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36"),
    }
    if headers:
        request_headers.update(headers)
    if body is not None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = Request(
        url,
        data=encoded,
        headers=request_headers,
        method=method,
    )
    try:
        with _open(request, timeout, insecure_tls) as response:
            status = int(response.getcode())
            raw = response.read()
    except HTTPError as error:
        status = int(error.code)
        try:
            raw = error.read()
        finally:
            error.close()

    text = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(text) if text else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = None
    return _JsonResponse(status, data, text)


def _secondary_request_json(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
    timeout: float | int = DEFAULT_TIMEOUT,
    insecure_tls: bool = False,
) -> _JsonResponse | None:
    """经 SSH 让 备用机重发同一 HTTP 请求。

    请求/结果走 stdin/stdout 单行 JSON；SSH 或脚本失败返回 None（保底不可用，
    按本地结果继续）。备用机不解析 JSON，只回传原始文本，解析仍在本机做。
    """
    payload = json.dumps({
        "op": "http",
        "url": url,
        "method": method,
        "headers": dict(headers or {}),
        "body": body,
        "timeout": float(timeout),
        "insecure_tls": bool(insecure_tls),
    }, ensure_ascii=False)
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             SECONDARY_SSH_TARGET, f"python3 {SECONDARY_SCRIPT_PATH}"],
            input=payload, capture_output=True, text=True, timeout=float(timeout) + 20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        reply = json.loads(result.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError:
        return None
    status = reply.get("status")
    text = reply.get("text", "")
    if not isinstance(status, int) or not isinstance(text, str):
        return None
    try:
        data = json.loads(text) if text else None
    except json.JSONDecodeError:
        data = None
    return _JsonResponse(status, data, text)


def _is_cf_response(response: _JsonResponse) -> bool:
    return response.status == 403 and _is_cloudflare_blocked(response.text)


def _get_or_secondary(url: str, **kwargs: Any) -> _JsonResponse:
    """统一请求入口：本地发出；命中 CF 拦截页且启用备用机时自动换备用机重发。"""
    response = _request_json(url, **kwargs)
    if _is_cf_response(response) and _secondary_available():
        relayed = _secondary_request_json(url, **kwargs)
        if relayed is not None and not _is_cf_response(relayed):
            _mark_secondary(url)
            return relayed
    return response


def _validated_base_url(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("地址必须以 http:// 或 https:// 开头")
    return value


def api_base_url(base_url: str) -> str:
    """返回包含且只包含一个尾部 ``/v1`` 的 API 地址。"""

    value = _validated_base_url(base_url)
    parts = urlsplit(value)
    path = parts.path.rstrip("/")
    if not path.lower().endswith("/v1"):
        path += "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def site_root(base_url: str) -> str:
    """返回站点根地址，即只移除地址末尾完整的 ``/v1`` 路径段。"""

    value = _validated_base_url(base_url)
    parts = urlsplit(value)
    path = parts.path.rstrip("/")
    if path.lower().endswith("/v1"):
        path = path[:-3].rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def origin_url(base_url: str) -> str:
    """返回只保留协议和网络位置的地址。"""

    parts = urlsplit(_validated_base_url(base_url))
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def domain_key(base_url: str) -> str:
    """给按域名去重采集使用的稳定键。

    规格要求按域名复用，因此协议、路径和端口都不参与分组。
    """

    parts = urlsplit(_validated_base_url(base_url))
    return (parts.hostname or "").casefold()


def _pricing_model_names(payload: Any) -> set[str] | None:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None
    rows = payload.get("data")
    if not isinstance(rows, list):
        return None
    names: set[str] = set()
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("model_name"), str):
            names.add(row["model_name"])
    return names


def classify_model_status(
    model_name: str,
    pricing_models: Iterable[str],
    perf_payload: Any | None,
) -> str:
    """严格按规格的五档规则判定单个模型状态。"""

    if model_name not in set(pricing_models):
        return "不支持"
    if not isinstance(perf_payload, dict) or perf_payload.get("success") is not True:
        return "不支持"
    data = perf_payload.get("data")
    # 兼容少数站点把 data 内字段直接放在顶层的同结构响应。
    container = data if isinstance(data, dict) else perf_payload
    groups = container.get("groups")
    if not isinstance(groups, list) or not groups:
        return "无数据"
    first_group = groups[0]
    if not isinstance(first_group, dict):
        return "无数据"
    rate = first_group.get("success_rate")
    if isinstance(rate, bool):
        return "无数据"
    try:
        success_rate = float(rate)
    except (TypeError, ValueError):
        return "无数据"
    if success_rate >= 90:
        return "良好"
    if success_rate >= 50:
        return "不稳"
    return "较差"


def model_success_rate(perf_payload: Any | None) -> float | None:
    """从 perf-metrics 响应取 groups[0].success_rate（0-100 的百分数）。

    取不到（非 new-api 响应、groups 为空、字段缺失或不是数字）时返回 None，
    调用方据此退回旧的状态词展示，不编造数字。
    """

    if not isinstance(perf_payload, dict) or perf_payload.get("success") is not True:
        return None
    data = perf_payload.get("data")
    # 兼容少数站点把 data 内字段直接放在顶层的同结构响应。
    container = data if isinstance(data, dict) else perf_payload
    groups = container.get("groups")
    if not isinstance(groups, list) or not groups:
        return None
    first_group = groups[0]
    if not isinstance(first_group, dict):
        return None
    rate = first_group.get("success_rate")
    if isinstance(rate, bool):
        return None
    try:
        return float(rate)
    except (TypeError, ValueError):
        return None


# 便于调用方和测试使用的同义名称。
determine_model_status = classify_model_status


def collect_domain_model_details(
    base_url: str,
    model_names: Sequence[str],
    *,
    timeout: float | int = DEFAULT_TIMEOUT,
    insecure_tls: bool = False,
) -> dict[str, tuple[str, float | None]] | None:
    """采集一个域名下每个模型的 (状态, 可用率)。

    pricing 每域名只请求一次；只有 pricing 确认支持的模型才请求一次 perf。
    可用率取 perf-metrics 的 success_rate（百分数），拿不到时为 None。
    返回 None 表示该域名没有 new-api 公开接口（pricing 拿不到），
    调用方应以 key 对照 /v1/models 名单兜底（collect_domain_by_key）。
    """

    names = list(dict.fromkeys(model_names))
    root = site_root(base_url)
    try:
        pricing = _get_or_secondary(
            f"{root}/api/pricing",
            timeout=timeout,
            insecure_tls=insecure_tls,
        )
    except (URLError, OSError, TimeoutError):
        return None

    if pricing.status != 200:
        if _is_cloudflare_blocked(pricing.text):
            return {name: ("CF拦截", None) for name in names}
        return None
    supported = _pricing_model_names(pricing.data)
    if supported is None:
        return None

    details: dict[str, tuple[str, float | None]] = {}
    for name in names:
        if name not in supported:
            details[name] = ("不支持", None)
            continue
        query = urlencode({"model": name})
        try:
            perf = _get_or_secondary(
                f"{root}/api/perf-metrics?{query}",
                timeout=timeout,
                insecure_tls=insecure_tls,
            )
        except (URLError, OSError, TimeoutError):
            details[name] = ("不支持", None)
            continue
        if perf.status != 200:
            details[name] = ("不支持", None)
        else:
            details[name] = (
                classify_model_status(name, supported, perf.data),
                model_success_rate(perf.data),
            )
    return details


def collect_domain_model_statuses(
    base_url: str,
    model_names: Sequence[str],
    *,
    timeout: float | int = DEFAULT_TIMEOUT,
    insecure_tls: bool = False,
) -> dict[str, str] | None:
    """采集一个域名下的模型状态词（旧接口，内部走 collect_domain_model_details）。

    pricing 每域名只请求一次；只有 pricing 确认支持的模型才请求一次 perf。
    返回 None 表示该域名没有 new-api 公开接口（pricing 拿不到），
    调用方应以 key 对照 /v1/models 名单兜底（collect_domain_by_key）。
    """

    details = collect_domain_model_details(
        base_url, model_names, timeout=timeout, insecure_tls=insecure_tls,
    )
    if details is None:
        return None
    return {name: value[0] for name, value in details.items()}


def _is_cloudflare_blocked(text: str) -> bool:
    """Cloudflare 托管质询/拦截页特征（Attention Required / cf-error 页面）。"""
    if not text:
        return False
    lowered = text.casefold()
    if "attention required" in lowered:
        return True
    return "<html" in lowered and ("cloudflare" in lowered or "cf-error" in lowered)


def collect_domain_by_key(
    base_url: str,
    api_key: str,
    model_names: Sequence[str],
    *,
    timeout: float | int = DEFAULT_TIMEOUT,
    insecure_tls: bool = False,
) -> dict[str, str]:
    """非 new-api 站点兜底：用 key 查 /v1/models，对照名单给 有→可用 / 无→不支持。"""

    names = list(dict.fromkeys(model_names))
    base = api_base_url(base_url)
    try:
        response = _get_or_secondary(
            f"{base}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            insecure_tls=insecure_tls,
        )
    except (URLError, OSError, TimeoutError):
        return {name: "不支持" for name in names}
    if response.status != 200:
        if _is_cloudflare_blocked(response.text):
            return {name: "CF拦截" for name in names}
        return {name: "不支持" for name in names}
    available = set(_extract_model_ids(response.data))
    return {name: ("可用" if name in available else "不支持") for name in names}


def collect_key_statuses(
    keys: Iterable[Any],
    model_names: Sequence[str],
    *,
    timeout: float | int = DEFAULT_TIMEOUT,
    insecure_tls: bool = False,
) -> dict[int, dict[str, str]]:
    """key 级检测：每个 key 用自身查 /v1/models 对照名单（有→可用，无→不支持）。

    与站点级（collect_model_statuses）并列调用：站点级反映平台整体支持情况，
    这里反映该 key 的真实权限（new-api 等网关会对受限 key 过滤 /v1/models）。
    """

    result: dict[int, dict[str, str]] = {}
    for item in keys:
        result[int(_field(item, "id"))] = collect_domain_by_key(
            str(_field(item, "base_url")),
            str(_field(item, "api_key")),
            model_names,
            timeout=timeout,
            insecure_tls=insecure_tls,
        )
    return result


def select_fallback_models(
    model_ids: Sequence[str],
    n: int = 5,
) -> list[str]:
    """无白名单命中时自动补选的模型（确定性方案）。

    直接按站点 /v1/models 列表顺序取前 n 个（列表靠前≈站点更常用）。
    不用随机：每次 /列表 结果会漂移；无用量统计接口，做不了真实“最多使用”。
    """

    return list(dict.fromkeys(model_ids))[:n]


def collect_statuses_auto(
    keys: Iterable[Any],
    model_names: Sequence[str],
    *,
    timeout: float | int = DEFAULT_TIMEOUT,
    insecure_tls: bool = False,
    n: int = 5,
) -> dict[int, list[tuple[str, str, str, float | None]]]:
    """同时采集站点级与 key 级状态；白名单无命中时自动补选 n 个模型检测。

    返回 {key_id: [(model_name, status, key_status, rate), ...]}，
    rate 为该模型的可用率百分数（perf-metrics 的 success_rate），采不到为 None：
    - 白名单有可用模型 → 白名单模型四元组（现行为）
    - 白名单无可用但站点有模型 → 自动补选 n 个（key_status 为“可用”，
      status/rate 取站点采集结果；站点没有公开接口时 status 记“可用”、rate 为 None）
    - 站点模型列表为空/连不上 → []（列表显示“待检测”）
    - CF 拦截 → 白名单模型四元组全部 “CF拦截”
    """

    rows = list(keys)
    names = list(dict.fromkeys(model_names))

    # 站点级按域名去重（用完整 base_url 请求，域名仅作分组键）
    groups: dict[str, list[Any]] = {}
    for item in rows:
        groups.setdefault(domain_key(str(_field(item, "base_url"))), []).append(item)
    site_by_domain: dict[str, dict[str, tuple[str, float | None]]] = {}
    for items in groups.values():
        base = str(_field(items[0], "base_url"))
        details = collect_domain_model_details(
            base, names, timeout=timeout, insecure_tls=insecure_tls,
        )
        if details is None:
            fallback = collect_domain_by_key(
                base, str(_field(items[0], "api_key")), names,
                timeout=timeout, insecure_tls=insecure_tls,
            )
            details = {name: (status, None) for name, status in fallback.items()}
        site_by_domain[domain_key(base)] = details

    result: dict[int, list[tuple[str, str, str, float | None]]] = {}
    for item in rows:
        key_id = int(_field(item, "id"))
        base = str(_field(item, "base_url"))
        key_st = collect_domain_by_key(
            base, str(_field(item, "api_key")), names,
            timeout=timeout, insecure_tls=insecure_tls,
        )
        if set(key_st.values()) == {"CF拦截"}:
            result[key_id] = [(m, "CF拦截", "CF拦截", None) for m in names]
            continue
        hits = [m for m in names if key_st.get(m) == "可用"]
        if hits:
            site_dt = site_by_domain.get(domain_key(base), {})
            key_rows: list[tuple[str, str, str, float | None]] = []
            for m in names:
                status, rate = site_dt.get(m, ("不支持", None))
                key_rows.append((m, status, key_st.get(m, "不支持"), rate))
            result[key_id] = key_rows
            continue
        # 无白名单命中：补选 n 个
        try:
            resp = _get_or_secondary(
                api_base_url(base) + "/models",
                headers={"Authorization": "Bearer " + str(_field(item, "api_key"))},
                timeout=timeout,
                insecure_tls=insecure_tls,
            )
            ids = list(_extract_model_ids(resp.data)) if resp.status == 200 else []
        except (URLError, OSError, TimeoutError):
            ids = []
        chosen = select_fallback_models(ids, n)
        # 补选出来的模型也顺手采一次站点级状态/可用率（采不到就只记「可用」）
        chosen_dt = collect_domain_model_details(
            base, chosen, timeout=timeout, insecure_tls=insecure_tls,
        ) or {}
        fallback_rows: list[tuple[str, str, str, float | None]] = []
        for m in chosen:
            status, rate = chosen_dt.get(m, ("可用", None))
            fallback_rows.append((m, status, "可用", rate))
        result[key_id] = fallback_rows
    return result


def normalize_model_rows(collected: Iterable[Any] | None) -> list[tuple[str, str, str, float | None]]:
    """把采集结果规整成四元组 (model_name, status, key_status, rate)。

    兼容旧的三元组（无 rate），供写库方统一 unpack 用。
    """

    rows: list[tuple[str, str, str, float | None]] = []
    for row in collected or []:
        parts = list(row)
        while len(parts) < 4:
            parts.append(None)
        rows.append((parts[0], parts[1], parts[2], parts[3]))
    return rows


def _field(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item[name]
    try:
        return item[name]
    except (KeyError, IndexError, TypeError):
        return getattr(item, name)


def collect_model_statuses(
    keys: Iterable[Any],
    model_names: Sequence[str],
    *,
    timeout: float | int = DEFAULT_TIMEOUT,
    insecure_tls: bool = False,
) -> dict[int, dict[str, str]]:
    """按域名去重采集，并把同域结果分发给每个 Key ID。"""

    key_rows = list(keys)
    groups: dict[str, list[Any]] = {}
    for item in key_rows:
        groups.setdefault(domain_key(str(_field(item, "base_url"))), []).append(item)

    result: dict[int, dict[str, str]] = {}
    for items in groups.values():
        first = items[0]
        statuses = collect_domain_model_statuses(
            str(_field(first, "base_url")),
            model_names,
            timeout=timeout,
            insecure_tls=insecure_tls,
        )
        if statuses is None:
            # 非 new-api 站点：用 key 对照 /v1/models 兜底
            statuses = collect_domain_by_key(
                str(_field(first, "base_url")),
                str(_field(first, "api_key")),
                model_names,
                timeout=timeout,
                insecure_tls=insecure_tls,
            )
        for item in items:
            result[int(_field(item, "id"))] = dict(statuses)
    return result


# 简短名称供 daily/commands 调用。
collect_models = collect_model_statuses


def _extract_model_ids(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return ()
    result: list[str] = []
    for row in payload["data"]:
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]:
            result.append(row["id"])
    return tuple(dict.fromkeys(result))


def _contains_dead_keyword(text: str) -> bool:
    lowered = text.casefold()
    return any(keyword.casefold() in lowered for keyword in DEAD_KEYWORDS)


def _contains_invalid_token(text: str) -> bool:
    lowered = text.casefold()
    return any(keyword.casefold() in lowered for keyword in INVALID_TOKEN_KEYWORDS)


def choose_probe_model(models: Sequence[str]) -> str:
    """优先从真实列表选择名称带便宜模型标记的模型。"""

    for model in models:
        lowered = model.casefold()
        if any(marker in lowered for marker in CHEAP_MODEL_MARKERS):
            return model
    return models[0] if models else FALLBACK_MODEL


def _check_key_once(
    base_url: str,
    api_key: str,
    *,
    timeout: float | int = DEFAULT_TIMEOUT,
    insecure_tls: bool = False,
) -> ProbeResult:
    """单遍测活：``/models`` + 必要时最小对话探测（本机视角，可能已由 备用机代答）。"""

    base = api_base_url(base_url)
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        models_response = _get_or_secondary(
            f"{base}/models",
            headers=headers,
            timeout=timeout,
            insecure_tls=insecure_tls,
        )
    except (URLError, OSError, TimeoutError):
        return ProbeResult("unknown", "连接失败", ())

    models = _extract_model_ids(models_response.data)
    if models_response.status == 401:
        if _contains_invalid_token(models_response.text):
            # 站方明确报「令牌无效」= key 已死 → 判死
            return ProbeResult("dead", "Invalid token", models)
        # 认证拒绝一律保留不删除（只有明确余额耗尽才判死）
        return ProbeResult("unknown", "401", models)
    if (
        models_response.status == 403
        and _is_cloudflare_blocked(models_response.text)
    ):
        # CF 托管质询/拦截页的 403 不代表 key 死了：按「无法读取」处理，不删除
        return ProbeResult("unknown", "CF拦截", models)
    if models_response.status == 403:
        return ProbeResult("unknown", "403", models)
    if (
        models_response.status in (402, 429)
        and _contains_dead_keyword(models_response.text)
    ):
        return ProbeResult("dead", "余额耗尽", models)

    # /models 即使返回 200 也可能完全不校验 key；必须继续用对话确认。
    model = choose_probe_model(models)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }
    try:
        chat_response = _get_or_secondary(
            f"{base}/chat/completions",
            method="POST",
            headers=headers,
            body=body,
            timeout=timeout,
            insecure_tls=insecure_tls,
        )
    except (URLError, OSError, TimeoutError):
        return ProbeResult("unknown", "连接失败", models)

    if chat_response.status == 401:
        if _contains_invalid_token(chat_response.text):
            # 站方明确报「令牌无效」= key 已死 → 判死
            return ProbeResult("dead", "Invalid token", models)
        return ProbeResult("unknown", "401", models)
    if (
        chat_response.status == 403
        and _is_cloudflare_blocked(chat_response.text)
    ):
        return ProbeResult("unknown", "CF拦截", models)
    if chat_response.status == 403:
        return ProbeResult("unknown", "403", models)
    if 200 <= chat_response.status < 300 or chat_response.status in (400, 404):
        return ProbeResult("alive", None, models)
    # 兜底关键词分支必须带状态码门（只认 402/429）：5xx 等服务器侧错误的文案里也
    # 可能带余额字样（例如 503「上游账号都处于限流冷却或额度暂停中…与你的账户余额和
    # 套餐额度无关」命中裸词「额度」），那不是死号 → unknown 保留。
    if (
        chat_response.status in (402, 429)
        and _contains_dead_keyword(chat_response.text)
    ):
        return ProbeResult("dead", "余额耗尽", models)
    return ProbeResult("unknown", str(chat_response.status), models)


def check_key(
    base_url: str,
    api_key: str,
    *,
    timeout: float | int = DEFAULT_TIMEOUT,
    insecure_tls: bool = False,
) -> ProbeResult:
    """测活编排：先本机一遍；判死/连不上时由备用机保底复测（未启用备用机就只跑本机）。

    保底规则：
    - 备用机结果与本地一致 → 不强调（按本地结论走）
    - 结论冲突 → **以备用机为准且不删除**，note 附「（备用:…）」说明差异
    - 本机判 CF 拦截的请求已在 _get_or_secondary 层自动由备用机代答（标注由
      data_source_for 在采集层落库）
    """

    local = _check_key_once(
        base_url, api_key, timeout=timeout, insecure_tls=insecure_tls
    )
    if local.status == "alive":
        return local
    if not _secondary_available():
        return local

    relay = _check_key_secondary(base_url, api_key, timeout=timeout, insecure_tls=insecure_tls)
    if relay is None:
        return local
    if relay.status == local.status:
        return local
    # 结论冲突：以备用机保底（不删除），并标注差异
    note = f"（{SECONDARY_LABEL}:{_SECONDARY_STATUS_TEXT.get(relay.status, relay.status)}"
    if relay.reason:
        note += f":{relay.reason}"
    note += "）"
    if relay.status == "dead":
        # 备用机也判死但死因不同（如本地 CF 拦截 vs 备用机 401）→ 按备用机死因判死
        return replace(
            local, status="dead", reason=relay.reason or local.reason,
            note=note, via_secondary=True,
        )
    return replace(
        local,
        status=relay.status,
        reason=relay.reason if relay.status == "unknown" else None,
        note=note,
        via_secondary=True,
    )


_SECONDARY_STATUS_TEXT = {"alive": "可用", "dead": "失效", "unknown": "不可达"}


def _check_key_secondary(
    base_url: str,
    api_key: str,
    *,
    timeout: float | int = DEFAULT_TIMEOUT,
    insecure_tls: bool = False,
) -> ProbeResult | None:
    """让 备用机跑同一套单遍测活（relay_probe.py check 模式），失败返回 None。

    请求走 stdin JSON、结果走 stdout JSON，key 不进远端 shell 命令行
    （key 来自群友提交，必须防 shell 注入）。
    """

    payload = json.dumps({
        "op": "check",
        "base_url": base_url,
        "api_key": api_key,
        "timeout": float(timeout),
        "insecure_tls": bool(insecure_tls),
    }, ensure_ascii=False)
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             SECONDARY_SSH_TARGET, f"python3 {SECONDARY_SCRIPT_PATH}"],
            input=payload, capture_output=True, text=True,
            timeout=float(timeout) * 2 + 30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        reply = json.loads(result.stdout.strip().splitlines()[-1])
        status = reply.get("status")
        if status not in ("alive", "dead", "unknown"):
            return None
        return ProbeResult(status, reply.get("reason"), tuple(reply.get("models") or ()))
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


probe_key = check_key


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


def _get_path(value: Any, *path: str) -> Any:
    current = value
    for name in path:
        if not isinstance(current, dict):
            return None
        current = current.get(name)
    return current


def _balance_get(
    url: str,
    api_key: str,
    timeout: float | int,
    insecure_tls: bool,
) -> _JsonResponse | None:
    try:
        response = _get_or_secondary(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            insecure_tls=insecure_tls,
        )
    except (URLError, OSError, TimeoutError):
        return None
    return response if response.status == 200 else None


def probe_balance(
    base_url: str,
    api_key: str,
    *,
    timeout: float | int = DEFAULT_TIMEOUT,
    insecure_tls: bool = False,
    now: datetime | None = None,
) -> BalanceResult:
    """探测余额，返回结构化的余额文本和额度耗尽标记。"""

    host = (urlsplit(_validated_base_url(base_url)).hostname or "").casefold()
    origin = origin_url(base_url)

    if "deepseek.com" in host:
        response = _balance_get(
            f"{origin}/user/balance", api_key, timeout, insecure_tls
        )
        infos = _get_path(response.data, "balance_infos") if response else None
        if not isinstance(infos, list) or not infos or not isinstance(infos[0], dict):
            return BalanceResult(None, False)
        amount = _decimal(infos[0].get("total_balance"))
        currency = infos[0].get("currency")
        text = None if amount is None else _money(amount)
        if text is not None and isinstance(currency, str) and currency:
            text = f"{text} {currency}"
        exhausted = _get_path(response.data, "is_available") is False
        return BalanceResult(text, exhausted)

    if "moonshot" in host:
        response = _balance_get(
            f"{origin}/v1/users/me/balance", api_key, timeout, insecure_tls
        )
        amount = _decimal(_get_path(response.data, "data", "available_balance")) if response else None
        return (
            BalanceResult(None, False)
            if amount is None
            else BalanceResult(_money(amount), amount <= 0)
        )

    if "siliconflow" in host:
        response = _balance_get(
            f"{origin}/v1/user/info", api_key, timeout, insecure_tls
        )
        amount = _decimal(_get_path(response.data, "data", "totalBalance")) if response else None
        return (
            BalanceResult(None, False)
            if amount is None
            else BalanceResult(_money(amount), amount <= 0)
        )

    if "openrouter.ai" in host:
        response = _balance_get(
            f"{origin}/api/v1/key", api_key, timeout, insecure_tls
        )
        data = _get_path(response.data, "data") if response else None
        if not isinstance(data, dict):
            return BalanceResult(None, False)
        limit_value = data.get("limit")
        usage = _decimal(data.get("usage"))
        if limit_value is None:
            return BalanceResult("无上限", False)
        limit_amount = _decimal(limit_value)
        if limit_amount is None or usage is None:
            return BalanceResult(None, False)
        remaining = limit_amount - usage
        return BalanceResult(
            f"剩 ${_money(remaining)} / ${_money(limit_amount)}",
            remaining <= 0,
        )

    # 通用 one-api/new-api 旧版 OpenAI 计费接口。
    root = site_root(base_url)
    subscription = _balance_get(
        f"{root}/v1/dashboard/billing/subscription",
        api_key,
        timeout,
        insecure_tls,
    )
    hard_limit = _decimal(_get_path(subscription.data, "hard_limit_usd")) if subscription else None
    if hard_limit is None:
        # 非 one-api 面板兜底：/v1/usage quota_limited（部分自定义面板）
        usage_response = _balance_get(
            f"{origin}/v1/usage",
            api_key,
            timeout,
            insecure_tls,
        )
        if usage_response is not None:
            mode = _get_path(usage_response.data, "mode")
            quota = _get_path(usage_response.data, "quota")
            if mode == "quota_limited" and isinstance(quota, dict):
                remaining = _decimal(quota.get("remaining"))
                if remaining is not None:
                    limit_value = _decimal(quota.get("limit"))
                    unit = quota.get("unit")
                    text = f"剩 ${_money(remaining)}"
                    if isinstance(unit, str) and unit and unit.upper() != "USD":
                        text = f"{text} {unit}"
                    if limit_value is not None:
                        text = f"{text} / 总额 ${_money(limit_value)}"
                    return BalanceResult(text, remaining <= 0)
            if mode == "unrestricted":
                # 订阅制面板：无总额度，按每日额度展示。
                # remaining = daily_limit - 今日已用；每日用尽次日自动重置，不算死号。
                remaining = _decimal(usage_response.data.get("remaining"))
                if remaining is not None:
                    daily = _decimal(_get_path(usage_response.data, "subscription", "daily_limit_usd"))
                    text = f"剩 ${_money(remaining)}"
                    if daily is not None:
                        text = f"{text} / 每日 ${_money(daily)}"
                    return BalanceResult(text, False)
        return BalanceResult(None, False)

    current = now.astimezone(UTC_PLUS_8) if now else datetime.now(UTC_PLUS_8)
    start = current.date().replace(day=1).isoformat()
    end = current.date().isoformat()
    query = urlencode({"start_date": start, "end_date": end})
    usage_response = _balance_get(
        f"{root}/v1/dashboard/billing/usage?{query}",
        api_key,
        timeout,
        insecure_tls,
    )
    usage_cents = _decimal(_get_path(usage_response.data, "total_usage")) if usage_response else None
    if usage_cents is None:
        return BalanceResult(None, False)
    used = usage_cents / Decimal(100)
    remaining = hard_limit - used
    return BalanceResult(
        f"剩 ${_money(remaining)} / ${_money(hard_limit)}",
        remaining <= 0,
    )


check_balance = probe_balance


__all__ = [
    "BalanceResult",
    "DEFAULT_TIMEOUT",
    "MODEL_STATUSES",
    "ProbeResult",
    "api_base_url",
    "check_balance",
    "check_key",
    "choose_probe_model",
    "classify_model_status",
    "collect_domain_model_statuses",
    "collect_model_statuses",
    "collect_models",
    "determine_model_status",
    "domain_key",
    "origin_url",
    "probe_balance",
    "probe_key",
    "site_root",
]
