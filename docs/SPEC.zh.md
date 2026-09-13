# 实现规格（中文）

这份文档描述 `qq-keypool-bot` 的**实现口径**：为什么这么写、边界在哪、哪些是刻意的
取舍。想改代码前建议先读一遍。

---

## 1. 设计原则

1. **全程不使用任何大语言模型**。指令用固定字符串解析，回复用固定模板拼接，key 死活
   用固定分支判定。理由：不会抄错 key、不会被群消息里的文字当成指令执行、响应是毫秒级、
   不用花钱、不会「看着像」就乱判。
2. **只用 Python 标准库**。没有 requests / aiohttp / websockets / 任何框架，`urllib` +
   `http.server` + `sqlite3` 就够。目标是扔到最便宜的 1 核小机器上也能一直趴着。
3. **不内置聊天客户端**。只按 **OneBot v11** 协议收发：收事件用 HTTP POST 上报，发消息调
   `/send_group_msg`。用 NapCat / Lagrange / go-cqhttp 都行，本仓库不含它们及其部署脚本。
4. **不做权限控制**。群里谁都能调所有指令——这是熟人小群的刻意决定。唯一的门槛是
   `group_whitelist`（只服务指定群）。
5. **key 明文显示**。`/查询` 会把地址和密钥原样打出来：群友投 key 的目的就是让别人拿去用，
   打码等于没用。所以只能投「自己愿意公开」的 key。

## 2. 组件与数据流

| 文件 | 角色 | 跑法 |
|---|---|---|
| `bot.py` | 常驻服务：收 OneBot 事件 → 解析指令 → 回文本 | systemd service，`Restart=always` |
| `update.py` | 每小时刷新模型状态（公开接口，不消耗 key） | systemd timer / cron |
| `daily.py` | 每天：测活 + 探余额 + 判死即删 + 发播报 | systemd timer / cron |
| `probe.py` | 所有网络探测（模型状态 / 测活 / 余额），不写库 | 被上面三个 import |
| `commands.py` | 指令解析与文本渲染，纯函数 | 被 `bot.py` import |
| `db.py` | SQLite 数据层（池、序号、模型状态） | 被上面三个 import |
| `relay_probe.py` | *可选*：备用机保底脚本，只有开了 `secondary` 才用 | 复制到备用机，被主机的 `ssh` 调起 |

**写库约定：先把所有网络请求做完，再开一个短事务一次性写库**，不要在打开的事务里发 HTTP。
WAL + `busy_timeout=5000` 让「采集」和「群友查列表」不互相堵。

## 3. OneBot 对接

### 收事件

`bot.py` 起一个 HTTP 服务，接收 OneBot v11 的**事件上报**（`POST /onebot`，兼容
`chunked` 与 `Content-Length`）。满足**全部**条件才处理：

1. `post_type == "message"` 且 `message_type == "group"`
2. `group_id` 在 `group_whitelist` 里
3. 去掉所有 `at` 段后的文本 strip 后以 `/` 开头（`parse_command` 严格匹配：必须整条
   消息就是一条指令，避免闲聊里出现「/删除 3」被当成指令）

其余事件一律返回 HTTP 204，不回复。（本项目**不强制** @ 机器人：白名单群里直接发
`/列表` 就行。）

### 发消息

`POST {onebot_url}/send_group_msg`，body `{"group_id": …, "message": …}`；
配了 `onebot_token` 就带 `Authorization: Bearer`。发送失败只打日志，不影响服务。

## 4. 指令规格

| 指令 | 行为 |
|---|---|
| `/列表 [页数]` | **只读数据库，不发任何网络请求**，毫秒级。每页 5 个 key，超界自动钳到最后一页 |
| `/更新` | 立刻采集**本池**模型状态，然后回列表 |
| `/查询 <序号>` | 当场用真实 key 测活 + 探余额；序号在本池不存在（含已删除）→ **静默不回复** |
| `/查询全部` | 检测本池全部 key，判死即删；**静默执行，不回复** |
| `/提供 站名 昵称 地址 密钥` | 四个参数全必填；地址必须以 `http://` 或 `https://` 开头；不存 QQ 号，只存昵称 |
| `/删除 <序号>` | **物理删除**（`keys` 行 + 它的 `models` 行一起删）；序号不存在回 `没有 #N` |
| `/检测列表更新 模型名…` | **整体覆盖**本池检测名单；不带参数 = 查看当前名单 |
| `/帮助` | 用法说明 |

没有 `/恢复`：下架就是删除，删掉就找不回来。

数字可以直接贴在命令后：`/列表2` == `/列表 2`。

## 5. 池与序号

* `pool_mode: "per_group"`（默认）：**池 = 群号**，各群互不可见；`"shared"`：所有群共用
  一个池（池名固定 `shared`）。
* 池内序号 `seq`：
  * 范围 `1..seq_limit`，默认 **99**；
  * 分配规则：从「当前池内最大序号 + 1」开始找第一个空号，越过上限后回绕到 1 继续找；
  * 池内号占满 → `/提供` 回复「池子满了」，**绝不覆盖**已有 key；
  * **序号会被复用**：删除不立刻回收，等计数器回绕到末尾才会用到空出来的号。所以群友
    手里缓存的 `#N` 可能指向新的 key——这是「99 上限 + 回绕」的必然结果。
* 库里有全局自增主键 `keys.id`（内部用，不对外显示）。

## 6. 模型状态与可用率

### 数据来源

new-api / one-api 系站点有两个**免鉴权**公开接口（不需要 Authorization）：

```
GET <站点根>/api/pricing              → 该站支持的模型清单
GET <站点根>/api/perf-metrics?model=X → 该模型近 24h 的全站流量统计
```

「站点根」= 投 key 时的地址去掉末尾 `/v1`。多个 key 指向同一域名时，**按域名去重**，
`pricing` / `perf-metrics` 每域名只请求一次。

### 判定（全是固定分支）

```
不在 pricing 的 model_name 列表里        → 不支持
在 pricing 里，但 groups 为空数组         → 无数据（近 24h 没人用）
在 pricing 里，success_rate >= 90        → 良好
在 pricing 里，50 <= success_rate < 90   → 不稳
在 pricing 里，success_rate < 50         → 较差
```

**必须先查 pricing**：`perf-metrics` 对**根本不存在的模型**也返回
`{"success": true, "groups": []}`，和「模型存在但没人用」一模一样。

除了档位，`success_rate` 原始数值会存进 `models.rate`，用于列表展示。

### 列表怎么显示

只显示 **key 级「可用」**（用这把 key 查 `/v1/models` 能看到）的模型：

* 拿得到可用率 → `模型名【✅】可用率NN%`；**可用率 < 60 显示 `【❌】`**；
  百分数**截尾取整**（99.45 → `99%`），免得出现「❌ 可用率60%」这种自相矛盾；
* 拿不到可用率（站点没公开接口 / 不支持 / 近 24h 无流量）→ `模型名【✅】`，**不编数字**；
* 检测到的模型**一律带勾或叉**，列表里不出现「良好/不稳/无数据」这类状态词。

站点查不动（CF 拦截）时整行显示「被CF拦截无法读取」；池里还没有采集数据时显示「待检测」。

## 7. Key 测活与余额

只在 `/查询`、`/查询全部`、`daily.py` 里用**真实 key**发请求；`/列表` 绝对不发。

### 测活（`probe.check_key`）

1. `GET <地址>/models`（带 Bearer）
   * 200 → 存活，并顺手拿到真实模型名列表
   * 401 → 响应体含「Invalid token / 无效的令牌」→ **dead**；其他 401 → unknown（保留）
   * 402 / 429 且响应体含余额类关键词 → **dead「余额耗尽」**
   * 403（含 CF 拦截页）→ unknown（保留，一律不删）
   * 连不上（DNS/超时/TLS）→ unknown（不算死号）
2. 兜底对话探测：`POST <地址>/chat/completions`，模型名**从第 1 步拿到的真实列表里挑**
   （优先 `flash` / `mini` / `lite` / `turbo`），`max_tokens=1`
   * 400 / 404 → **判存活**（多半只是模型名不对，key 本身是好的）
   * 401 / 403 → unknown；401 + 「令牌无效」→ dead
   * 402 / 429 + 余额关键词 → dead「余额耗尽」
   * 其他（含 5xx）→ unknown。注意：503 的「…与你的账户余额和套餐额度无关…」里有裸词
     「额度」，但它只是上游限流冷却，**不能**判死

余额类关键词（大小写不敏感，只在 402/429 下判死）：`insufficient`、`quota`、`额度`、
`余额不足`、`已用尽`。

### 结论怎么用

* `dead` → **物理删除**该 key（`/查询`、`/查询全部`、日报三处口径一致）；
* `unknown`（连不上 / CF / 401 / 403 / 5xx）→ **保留不删**，`/查询` 里说明原因。

理由：站点瞬时误报很常见，「看着像死了」不等于真死；删除不可逆，宁可不删。

### 备用机保底（可选）

配了 `secondary`（默认关闭）后：CF 拦截的请求自动换备用机重发；测活判死/连不上时由
备用机复测。两台结论一致则不强调；**冲突以备用机为准且不删除**，并在 `/查询`
（「（备用:可用）」）和 `/列表`（「#N 站名（备用）」）标出来源。SSH 不可达时自动退回
纯单机行为——保底是增强，不是依赖。

## 8. 数据库

```sql
CREATE TABLE IF NOT EXISTS keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 内部主键
    pool        TEXT NOT NULL,        -- 池（默认=群号；shared 模式固定 'shared'）
    seq         INTEGER NOT NULL,     -- 池内序号，群里看到的 #N
    site        TEXT NOT NULL,        -- 站名
    nickname    TEXT NOT NULL,        -- 提供人昵称（不存 QQ 号）
    base_url    TEXT NOT NULL,        -- 地址
    api_key     TEXT NOT NULL,        -- 明文
    added_at    TEXT NOT NULL,        -- 'YYYY-MM-DD HH:MM' UTC+8
    last_check  TEXT,                 -- 最后测活时间
    balance     TEXT,                 -- 最后一次探到的余额文本
    UNIQUE (pool, seq)
);

CREATE TABLE IF NOT EXISTS models (
    key_id      INTEGER NOT NULL,
    model_name  TEXT NOT NULL,
    status      TEXT NOT NULL,        -- 站方档位（良好/不稳/较差/无数据/不支持）
    key_status  TEXT,                 -- key 级：可用/不支持/CF拦截
    rate        REAL,                 -- 可用率百分数，采不到为 NULL
    source      TEXT,                 -- '1'=本机，'2'=备用机代答
    collected_at TEXT NOT NULL,
    PRIMARY KEY (key_id, model_name)
);
```

连接时 `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout=5000`。

**字段所有权**：机器人进程只写 `keys` 表的整行（增删）与用户填的字段；采集流程只写
`last_check` / `balance` / `models` 表。两者不写同一列。

## 9. 配置

见 [README 的配置字段表](../README.md#配置字段) 与 `config.example.json`。
校验在 `bot.load_config`：缺字段、类型不对、`pool_mode` / `model_list_mode` 取值不合法、
端口越界、`http_timeout <= 0`、`seq_limit` 非正数 → 启动即报错退出（不静默降级）。

## 10. 测试

```bash
python -m unittest discover tests -p "test_*.py"   # 必须全绿
python3 bot.py --selftest                          # 不联网的指令链路自检
```

覆盖范围：序号分配与 99 回绕、池隔离、跨池不可见、删除后同号静默、检测名单按池、
模型状态五档与可用率、测活各分支（200/401/403/402/429/5xx/连不上）、备用机开关与冲突
标注、OneBot 事件过滤与发送 payload、日报按池分发。**测试全部打桩，不联网、不连 QQ。**

## 11. 明确不做的事

* 不引入第三方包（不用 requests / aiohttp / pytest / websockets）
* 不调用任何 LLM 来判断或生成
* 不内置聊天客户端及其部署脚本
* 不给 key 打码——明文输出是产品决定
* 不做权限/白名单/限流（除 `group_whitelist`）
* 不在 `/列表` 里发网络请求
* 不做「手动刷新日报」的指令
* 不给列表长度加截断兜底
