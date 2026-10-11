# 财鲸 V3 进销存 —— 云端中转服务（cloudapi）

部署在云服务器上的 **API 中转层**。它**不存任何业务数据** —— 只把前端请求
转发给客户本地的 Agent，再把结果回传。客户数据始终留在客户自己的机器上。

```
H5 / 小程序 / App ──HTTP──► cloudapi ──WebSocket──► 客户本地 Agent ──► 财鲸V3 SQL Server
                  （本仓）            （boss-agent）      （客户机上的 V3 库）
```

> ★ 数据流是**穿透**的：云端只在内存里转发，不落库、不落盘。
> 云端的定位是**平台方**（谁有哪几家商户、谁在线、发了什么请求），
> 而不是业务方（不进销存数据）。

---

## 目录结构

```
进销存-云端后台源码/
├── cloudapi/                      ★ 云端服务本体（Python / FastAPI）
│   ├── main.py                      进程入口（uvicorn）
│   ├── server.py                    HTTP 路由 + WebSocket 接入 + 前端站点托管
│   ├── registry.py                ★ Agent 连接注册表 + 分级在途闸门
│   ├── business.py                  业务接口（/api/v1/sales、/products…）
│   ├── auth_routes.py               登录鉴权（多端令牌 / 操作员清单 / 能力清单）
│   ├── security.py                ★ 令牌校验、商户归属裁决、审计条目
│   ├── config.py                  ★ 配置默认值 + 商户模型 + admission_for 分级闸门
│   ├── insights.py                  后台观测（商户健康、时间线、告警、影响面）
│   ├── admin.py                     后台管理接口（/admin/api/*）
│   ├── tasks.py                     异步任务
│   ├── limit.py                     令牌桶限流
│   ├── audit.py                     审计落盘（按日切分 jsonl）
│   └── web/
│       ├── admin/                   后台管理页（拆成 index.html + css/js + views/）
│       └── admin_legacy.html        旧版单文件后台（保留对照）
│
├── miniapp/                      ★ 微信小程序前端（TypeScript）
│   └── miniprogram/
│       ├── app.ts / app.json        小程序入口
│       ├── config.ts              ★ 云端地址（改这里切环境）
│       ├── pages/                   11 个页面组：login / home / customers /
│       │                            customer / product / order / sales /
│       │                            report / pick / profile
│       ├── components/              公共组件（含 tabbar）
│       ├── custom-tab-bar/          自定义底部导航
│       ├── utils/                   请求 / 鉴权 / 格式化 / 日期
│       └── assets/icons/            图标
│
├── scripts/                      部署与运维工具
│   ├── deploy_cloud.py           ★ 全量部署（上传 + venv + systemd + 随机新密钥）
│   ├── deploy_cloudapi.py          增量上线（多文件 + 先备份 + 失败自动还原）
│   ├── deploy_cloud_update.py      只更新代码并重启（保留现有配置）
│   ├── deploy_business.py          只更新 business.py（md5 双边校验 + py_compile）
│   ├── upload_cloud.py             上传 cloudapi 并重启
│   ├── provision-merchant.py     ★ 一次开通一家商户（密钥 + 客户端 + 应用账号 + 装机包）
│   ├── deprovision-merchant.py     反向：下线一家商户
│   ├── sshtarget.py              ★ SSH 目标与认证的唯一入口（无兜底密码）
│   ├── toolchain.py                工具链路径解析
│   └── start_dev.ps1 / stop_dev.ps1  本地起停
│
└── cloud_config.example.json       云端配置模板（真配置不入库）
```

---

## 快速开始

```bash
# 1) 准备配置（真配置不入库，从模板复制）
cp cloud_config.example.json cloud_config.json

# 2) 依赖
pip install fastapi uvicorn websockets

# 3) 起服务
python -m cloudapi.main --config cloud_config.json
# 或 PowerShell：scripts\start_dev.ps1
```

打开 `http://127.0.0.1:8000/admin` 进后台。

---

## 接口约定

所有业务接口都在 **`/api/v1/`** 前缀下（**不是** `/api/` —— 这是最容易踩的坑）。
不确定时读 `GET /openapi.json`，那是最权威的清单。

### 鉴权与身份

| 用途 | 方法 | 路径 | 说明 |
|---|---|---|---|
| 登录页清单 | POST | `/api/v1/auth/options` | **不鉴权**：这家有哪几本账、有哪些操作员 |
| 登录 | POST | `/api/v1/auth/login` | `provider` 可为 `v3`（账号密码）/ 微信 |
| 刷新 | POST | `/api/v1/auth/refresh` | |
| 登出 | POST | `/api/v1/auth/logout` | |
| 当前身份 | GET | `/api/v1/auth/me` | 含最后一次上线/离线 |
| 能力清单 | GET | `/api/v1/capabilities` | **需鉴权**，返回 Agent 在线态与 ops |

### 业务（销售示例，采购同构）

| 用途 | 方法 | 路径 |
|---|---|---|
| 列表 | GET | `/api/v1/sales?state=&date_from=&date_to=` |
| 详情 | GET | `/api/v1/sales/{bill_no}` |
| 建单 | POST | `/api/v1/sales` |
| 改单 | PUT | `/api/v1/sales/{bill_no}` |
| 审核 / 反审 | POST | `/api/v1/sales/{bill_no}/audit`、`.../unaudit` |
| 作废 / 复制 | POST | `/api/v1/sales/{bill_no}/void`、`.../copy` |

另有 `/api/v1/customers`、`/api/v1/products`、`/api/v1/suppliers`、
`/api/v1/inventory`、`/api/v1/price`、`/api/v1/reports/*`、
`/api/v1/print/salesbill`（+ `/preview`）、`/api/v1/tasks`。

### 后台管理（`/admin/api/*`）

`login` / `agents` / `merchants` / `merchant-health` / `monitor` / `timeline` /
`impact` / `audit` / `stats` / `settings` / `rpc`（调试台）/ `clients` /
`users` / `accounts` / `recent-logins`。

### 返回结构

统一包一层，业务字段在 **`data`** 里：

```json
{
  "ok": true,
  "data": { "items": [...], "total": 48586, "limit": 3, "offset": 0, "hasMore": true },
  "op": "product.search",
  "agent": "shiwantongda"
}
```

失败时 `ok:false`，`error` 带 `code` / `message`（必要时 `retry_after`）。

---

## ★ 关键机制

### 1. 商户是一等公民，`default_agent` 已废除

历史配置里有个 `default_agent` 兜底：请求不带 agent 就发给"默认那一家"。
这在单商户时代能用，多商户时代**等于误操作别家的库** ——
某家做了个停用操作，请求静默落到另一家身上。

2026-09-25 起废除：目标 Agent 一律由 `security.target_agent()` 从**令牌归属**裁决。
存量配置里若还留着这个键，`config.load()` 会原样存进 `_legacy_default_agent`、
`save()` 再原样写回 —— 只为不改坏运维手上的文件，**路由上完全不参与**。
后台提交 `default_agent` 会被明确忽略并记一条 `admin.settings.ignored` 审计。

### 2. 分级在途闸门（`config.admission_for`）

**老行为**：一家总共 2 条在途，超了立刻 429。
**实测问题**：打印一条 op 要跑几十秒，占掉 2 个位之一时，老板随手点一下预览、
或从列表切到报表页（报表页一次并发 6~8 条），第 3 条就被拒 ——
屏幕上是"本地服务正在处理其他查询"，体验上等于**这家坏了**。

**现行为**：按 op 的**性质**分开限并发，且"先等一会儿再拒"：

| 类 | 默认 cap | 默认 wait | 归入的 op |
|---|---|---|---|
| `report` | 3 | 8s | `REPORT_OPS` 白名单：8 个真压大表的聚合报表 |
| `print` | 2 | 25s | `print.salesbill`（慢但不查大表，等在打印机/渲染） |
| `quick` | 16 | 3s | 其余（列表 / 明细 / 单头 / 能力清单，几十毫秒级） |
| `_total` | 24 | — | 兜底总闸，防某个页面失控刷屏把协程占满 |

★ **用白名单，不是"以 `report.` 开头就算重"**：`report.sales.summary` 实测只有
30~60ms，归进重类会让老板每次开报表页都白白堵住那 3 个位 ——
分类必须跟着**实测代价**走，不能跟着名字走。

★ `REPORT_OPS` **必须与 Agent 侧 `agent_daemon.HEAVY_OPS` 同源**（`check-capacity.py`
逐条比对）。漂了的后果很具体：云端按 report 排队、Agent 却把它丢进快池 ⇒
8 条重查询一起打到 SQL Server 上。

★ 商户级覆盖：`auth.merchants[码].inflight = {"report": {"cap": 5}}`
（只写 cap 也行：`{"report": 5}`）。32G 那台可以把 report 给到 4~5。

★ **真源是 cfg**。`registry.MAX_INFLIGHT_PER_AGENT` 只在配置被删空时兜住，
**绝不当第二处"实际生效的上限"** —— 否则改配置不生效、排查全白干。

★ 为什么不用 `asyncio.Semaphore`：它会**先占位再等**（FIFO 排队），
一条 90s 的报表能让后面 15 条轻查询全挂在队里。这里改成"只在有余量时才占位"，
满就先回 `Retry-After`，云端不留排队大队。

### 3. 同一 Agent 名只允许一条连接（+ 装机指纹）

`registry.register()` 里新连接必定踢掉旧连接：

```python
await old.ws.close(code=4001, reason="replaced by new connection")
```

设计本身合理（保证状态唯一），但意味着**客户机上不能同时跑两个同名 Agent**。
表现：两边无限互踢 → 接口时好时坏 → 分页请求偶发失败。
排查：看云端日志有没有密集的 `4001 replaced by new connection`。

为此引入**装机指纹 `install_id`**（Agent 首次启动生成一次、写进自己的
`config.json`）：

- 同名 + 同指纹 ⇒ "这台机器网络抖了一下重连"，放行；
- 同名 + 不同指纹 ⇒ "另一台机器拿着同一份密钥上线"，走准入流程（接管）。

离线超过 `STALE_INSTALL_SEC`（24h）指纹自动作废：换机/重装可未经批准直接接管
（不然硬盘坏一次就要等运营者点按钮，那是人造停机）。

### 4. 账套路由：从"等于商户码"到"属于这家"

真实客户里有**一位老板一台主机两个账套**。旧模型只有「商户码 == 实测账套」这一条
判据，于是他的第二本账（库名不可能等于商户码）在后台**永久标红** ——
"合法的多账套"被当成事故来报警。

现在分两层登记：

- 云端 `auth.merchants[码].databases` = **平台允许**这家有哪几本；
- Agent 侧 `allowed_databases` = **那台机器上的进程允许**被读到哪几本。

两边都放行才真的读得到，差集就是装机现场最容易卡住的地方
（后台看着登记好了、请求却被 Agent 拒）。Agent 还会上报 `database_verified`
（**实测**账套名）—— 名字叫 A、实际读 B 这类事故，后台能直接标红。

### 5. WebSocket 心跳必须放宽

`uvicorn` 默认 `ws_ping_interval=20s` / `ws_ping_timeout=20s`，
服务端 20 秒收不到 pong 就发 1011 断开。而客户机执行某些报表 SQL 可能要几十秒
（历史上 `product-ranking` 在真库跑到 48~89s）—— 一旦超过 20s，Agent 被判定
keepalive 超时踢掉，表现为「Agent 假死 + 前端 502 `AGENT_OFFLINE`」。

⇒ `main.py` 里放宽到 **60s / 120s**，给慢查询留余量。
（Agent 侧已把 op 执行移到线程池，这里是链路层的第二道保险。）

### 6. 凭据收敛，不落仓库

同一个生产 root 密码曾被复制进 21 个 `.py`，应用账号口令被复制进 50 个脚本 ——
任何一个文件被备份、截图、贴进问答上下文都是一次泄露。

修复不是"加一层封装"，而是**收敛到唯一入口且不留兜底**：

- `scripts/sshtarget.py`：SSH 目标与认证的唯一入口，四条认证途径
  （env `JXC_SSH_KEY` → `~/.ssh/config` 的 `IdentityFile` → `~/.ssh/jxc_deploy`
  → env `JXC_SSH_PASSWORD`）全失败就**直接退出**，**没有任何兜底密码**。
- `scripts/livecreds.py`：线上应用账号口令来源 = env `JXC_APP_PWD`
  → `~/.ssh/jxc_app_pw`，都没有就报清楚怎么补。轮换口令只改一处文件。

`cloud_config.json` 含 Agent 密钥 / admin 口令 / 令牌签名密钥，**已 gitignore**。

---

## 部署

```bash
# 全量部署（首次 / 换机）
python scripts/deploy_cloud.py
```

自动完成：① SFTP 上传 `cloudapi/**` 到 `/opt/v3cloud/`；② 生成新的随机密钥
（admin 密码 / Agent 密钥 / 客户端令牌）；③ 建 venv 并装 `fastapi`+`uvicorn`+`websockets`；
④ 注册 systemd 服务 `v3cloud`（`Restart=always`，开机自启）；⑤ 重启并本地健康检查；
⑥ 凭据存到 `logs/prod_credentials.json`。

只改了代码、没改配置时用增量：

```bash
python scripts/deploy_cloudapi.py        # 多文件，先备份，失败自动还原
python scripts/deploy_business.py        # 只更新 business.py
```

一键开通一家商户：

```bash
python scripts/provision-merchant.py --code <商户码> --title <名称> --databases <账套1,账套2>
```

产出：商户条目 + Agent 密钥 + 客户端令牌 + 首个应用账号 + 一份装机包。

---

## 配置（`/opt/v3cloud/cloud_config.json`）

```jsonc
{
  "server": { "host": "0.0.0.0", "port": 8000 },
  "auth": {
    "portal_token": "…",                        // 小程序入口令牌
    "clients": [
      { "token": "…", "name": "shiwantongda-小程序",
        "agent": "shiwantongda", "access": "guest" }   // guest | auth
    ],
    "agents": { "shiwantongda": "…" },           // ★ 必须与本地 Agent 的 secret 一致
    "merchants": {
      "shiwantongda": { "title": "同达", "enabled": true,
                        "databases": ["shiwantongda", "tongda"],
                        "clients": ["shiwantongda-小程序"] }
    },
    "accounts": [],                              // 多端账号（账号密码登录）
    "users": [],                                 // 微信 openid → 可访问客户端
    "token_secret": "",                          // 首启动自动生成
    "access_ttl": 43200,
    "refresh_ttl": 2592000
  },
  "wechat": { "appid": "", "secret": "", "enabled": false },
  "rpc_timeout": 60,
  "min_agent_version": "",
  "max_inflight": 24,
  "inflight_by_class": {
    "report": { "cap": 3, "wait_sec": 8 },
    "print":  { "cap": 2, "wait_sec": 25 },
    "quick":  { "cap": 16, "wait_sec": 3 }
  },
  "rate_limit": { "rate": 8.0, "burst": 20 },
  "h5_root": "/var/www/jxc-h5",
  "log_dir": "/opt/v3cloud/logs"
}
```

★ `config.load()` 逐段合并：**只补缺失的默认键，已存在的一律以文件为准** ——
不覆盖运维手改的值。（历史坑：整段赋值意味着"配置文件里没写的新键永远不会出现"，
auth 里加一个键、线上那份不写就等于没加。）

---

## 小程序

改 `miniapp/miniprogram/config.ts`：

```ts
export const CLOUD_BASE = 'https://<你的域名>'   // 云端地址
```

用微信开发者工具打开 `miniapp/` 目录即可预览。

> 正式发布要求**域名已备案 + HTTPS**，且需在小程序后台配置 request 合法域名。

---

## 运维

```bash
systemctl status v3cloud          # 服务状态
journalctl -u v3cloud -f          # 实时日志
systemctl restart v3cloud         # 重启
curl http://127.0.0.1:8000/api/v1/health   # 健康检查
```

审计日志按日切分落在 `log_dir`，字段含 `op / agent / code / http / ms / via`，
**参数已脱敏**（`security._strip_sensitive`，口令不会以可离线验证的形态落盘）。

---

## 待办 / 安全建议

- [ ] **SSH 仍是密码登录** —— 建议改密钥认证并关闭密码登录
- [ ] **HTTP 明文** —— 小程序正式发布要求 HTTPS + 域名备案
- [ ] `access: 'guest'` 的写权限边界需按业务再确认
- [ ] 后台管理员口令建议定期轮换

---

## 路径说明

`scripts/` 下的脚本用**相对自身位置**定位项目根
（`os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`），
不写死盘符，整个目录可随意搬迁。
