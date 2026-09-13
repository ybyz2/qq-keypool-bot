# 部署指引（写给「被叫来部署的那个 AI」）

这份文档是给**替你部署的 AI 助手**看的。如果你自己动手，照着做也一样。

先讲结论：**部署前必须问部署者 4 个问题**（其它都别问，尤其是 OneBot 客户端，
那是部署方自己搞定的事）。

---

## 第 0 步：要问部署者的 4 件事

用一段话一次性问完，别一条条挤牙膏：

| # | 问什么 | 默认值 | 说明 |
|---|---|---|---|
| 1 | **机器人的 QQ 号** + **要服务的群号**（可多个） | 无默认，必填 | 群号进 `group_whitelist`；不在名单里的群一律不理 |
| 2 | **key 池怎么分**：每个群一个独立池，还是所有群共用一个池？ | **每个群独立**（`pool_mode: "per_group"`） | 独立池时序号各自从 1 开始、A 群看不到 B 群的 key；共用则 `"shared"` |
| 3 | **检测名单**：要监控哪些模型名？（可以现在给，也可以留空以后再填） | **留空**（空名单 = 列表显示「待检测」） | 名字必须是**站点侧真实模型名**（见下面「坑」一节）；可以先空着，部署完整天再填 |
| 4 | **每日播报发到哪些群**？ | 同 `group_whitelist` | 不想要播报就留空 `[]`（脚本照样会检测，只是不发消息） |

**不要问**：OneBot 用哪个实现、监听什么端口、access_token 是多少——这些由部署方
自己决定并配置（见第 1 步）。除非部署者主动说「你顺便把客户端也装上」。

---

## 第 1 步：准备 OneBot v11 客户端（部署方自己搞定）

本项目**不包含**任何聊天客户端。推荐 [NapCatQQ](https://github.com/NapNeko/NapCatQQ)，
其它 OneBot v11 实现（Lagrange、go-cqhttp 等）同样可用。要配两件事：

1. **HTTP 事件上报（POST）** 指到本项目：`http://<本机>:3020/onebot`
2. **HTTP API 端口**（发消息用）与 access_token → 填进 `config.json` 的
   `onebot_url` / `onebot_token`（例如 `http://127.0.0.1:3000` + token）

建议：事件上报和 API 都只绑 `127.0.0.1`，别把控制面板或端口暴露到公网。

---

## 第 2 步：装代码 + 写配置

```bash
git clone <本仓库> /opt/qq-keypool-bot
cd /opt/qq-keypool-bot
cp config.example.json config.json
```

`config.json` 的关键字段（完整表见 [README](README.md#配置字段)）：

```json
{
  "onebot_url": "http://127.0.0.1:3000",
  "onebot_token": "<客户端里配的 token，没有就留空>",
  "listen_host": "127.0.0.1",
  "listen_port": 3020,
  "self_qq": 10001,
  "group_whitelist": [111111111, 222222222],
  "daily_group_whitelist": [111111111],
  "pool_mode": "per_group",
  "model_list_mode": "per_pool",
  "seq_limit": 99,
  "insecure_tls": false,
  "http_timeout": 20,
  "secondary": {"enabled": false}
}
```

**检测名单**放 `models/<池名>.txt`，一行一个模型名；`per_group` 模式下池名就是群号
（比如 `models/111111111.txt`），`shared` 模式下是 `models/shared.txt`。
名单可以为空（空文件、或文件不存在都行）。

> 名单留空时，部署者随时可以在群里发 `/检测列表更新 模型A 模型B` 现场填。

---

## 第 3 步：先自检，再起服务

```bash
python3 bot.py --selftest      # 不联网、不连 QQ：跑完整指令链路并打印每步回复
python3 bot.py                 # 前台起服务，看到「正在监听 …」即成功
```

**坑（模型名）**：检测名单里写的是**站点侧真实的模型名**（用该站 `/v1/models` 或
`/api/pricing` 里名字原样），写错了会被判成「不支持」，列表里就不显示——不是 bug。
部署时如果不确定，可以先用部署者的一个 key 拉一次 `/v1/models` 看真实名字。

想省事就先空着名单：机器人能正常收发指令，等部署者把名字给齐再填。

---

## 第 4 步：挂成常驻服务 + 定时任务

`/etc/systemd/system/keypool-bot.service`：

```ini
[Unit]
Description=QQ Key Pool Bot (OneBot HTTP listener)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/qq-keypool-bot
ExecStart=/usr/bin/python3 /opt/qq-keypool-bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`keypool-update.service` + `keypool-update.timer`（每小时刷新模型状态）：

```ini
# keypool-update.service
[Unit]
Description=QQ Key Pool hourly model status update

[Service]
Type=oneshot
WorkingDirectory=/opt/qq-keypool-bot
ExecStart=/usr/bin/python3 /opt/qq-keypool-bot/update.py

# keypool-update.timer
[Unit]
Description=Run keypool update every hour

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

`keypool-daily.service` + `keypool-daily.timer`（每天播报一次，时间自选）：

```ini
# keypool-daily.service
[Unit]
Description=QQ Key Pool daily report

[Service]
Type=oneshot
WorkingDirectory=/opt/qq-keypool-bot
ExecStart=/usr/bin/python3 /opt/qq-keypool-bot/daily.py

# keypool-daily.timer
[Unit]
Description=Run keypool daily report

[Timer]
OnCalendar=*-*-* 08:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl daemon-reload
systemctl enable --now keypool-bot.service keypool-update.timer keypool-daily.timer
systemctl status keypool-bot --no-pager
```

---

## 第 5 步：验收清单

- [ ] `python3 bot.py --selftest` 通过
- [ ] `systemctl is-active keypool-bot` = active，且 `journalctl -u keypool-bot` 无 Traceback
- [ ] 群里发 `/帮助` 有回复（这条必须由**群里的人**发，别 curl 模拟事件）
- [ ] 群友 `/提供 站名 昵称 地址 密钥` 能拿到一个序号，`/列表` 能看到
- [ ] `systemctl list-timers | grep keypool` 能看到两个定时器
- [ ] 部署者确认：池模式、检测名单、播报群三项跟他说的一致

---

## 建议的部署默认值（部署者没特别要求时）

| 项 | 值 |
|---|---|
| `pool_mode` | `per_group`（每个群各自一个池） |
| `model_list_mode` | `per_pool`（每个池各自的检测名单） |
| `seq_limit` | `99` |
| 检测名单 | **空**，等部署者给或群里 `/检测列表更新` |
| `secondary` | 关闭 |
| 监听 | `127.0.0.1:3020` / `127.0.0.1` 上的 OneBot API |
