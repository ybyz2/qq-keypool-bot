"""备用机保底探测脚本（stdin 单行 JSON 请求 → stdout 单行 JSON 结果）。

**这是可选件**：只有把 `config.json` 的 `secondary.enabled` 置 true 才用得到。
日常单机部署不需要它。

用法：把它和 `probe.py` 一起放到备用机上（同一目录），再在主机上配好到备用机的
SSH 免密登录（例如 `~/.ssh/config` 里写一个别名 `relay`），然后：

    "secondary": {
      "enabled": true,
      "ssh_target": "relay",
      "script_path": "/opt/keypool-relay/relay_probe.py",
      "label": "备用"
    }

主机就会在「被 Cloudflare 拦截」或「测活判死/连不上」时，经
`ssh <ssh_target> python3 <script_path>` 让备用机重发同一个请求复测。

两种请求：

  {"op":"http",  "url","method","headers","body","timeout","insecure_tls"}
    → {"status":int,"text":str}            （原始响应文本，解析在主机侧做）

  {"op":"check", "base_url","api_key","timeout","insecure_tls"}
    → {"status":"alive|dead|unknown","reason":str,"models":[...]}

只处理一条请求就退出；任何异常都返回 `{"error": "..."}`，不吐堆栈。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import probe  # noqa: E402


def main() -> int:
    try:
        request = json.loads(sys.stdin.readline() or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"bad request: {exc}"}))
        return 0
    try:
        op = request.get("op")
        if op == "http":
            response = probe._request_json(
                str(request["url"]),
                method=str(request.get("method") or "GET"),
                headers=request.get("headers") or None,
                body=request.get("body"),
                timeout=float(request.get("timeout") or probe.DEFAULT_TIMEOUT),
                insecure_tls=bool(request.get("insecure_tls")),
            )
            print(json.dumps({"status": response.status, "text": response.text}))
        elif op == "check":
            # 备用机自己不再往下一层转发（避免自连），单机跑一遍就回结果
            probe.configure_secondary(enabled=False, ssh_target="")
            result = probe._check_key_once(
                str(request["base_url"]),
                str(request["api_key"]),
                timeout=float(request.get("timeout") or probe.DEFAULT_TIMEOUT),
                insecure_tls=bool(request.get("insecure_tls")),
            )
            print(json.dumps({
                "status": result.status,
                "reason": result.reason,
                "models": list(result.models),
            }))
        else:
            print(json.dumps({"error": f"unknown op: {op!r}"}))
    except Exception as exc:  # noqa: BLE001 —— 兜底回错误，不带堆栈
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
