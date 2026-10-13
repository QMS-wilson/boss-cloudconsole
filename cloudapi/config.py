# -*- coding: utf-8 -*-
"""云端配置加载（默认 cloud_config.json，可用 CLOUD_CONFIG 覆盖）。"""

import json
import os
import re


DEFAULTS = {
    "server": {"host": "127.0.0.1", "port": 8000},
    "auth": {
        # 小程序入口令牌：只用于 wechat/login 与 /me，可展示所有店名并做老板登录
        "portal_token": "",
        # 小程序/客户端令牌（上线前必须更换；openid 绑定后续接入）
        # access: guest=通用访客（免 openid）；auth=仅授权老板(users)可用
        "clients": [{"token": "dev-token-001", "name": "dev-client", "agent": "shiwantongda", "access": "guest"}],
        # 各客户本地 Agent 的注册密钥，必须与 v3agent config.json 的 agent.secret 一致
        "agents": {"shiwantongda": "change-me"},
        # ★ 商户实体（Phase 2，2026-09-26）。约定 **商户码 == agent 名 == H5 的 URL 前缀**，
        #   一个商户一条。
        #   {码: {title, enabled, databases:[账套名…], clients:[客户端 name], min_agent_version}}
        #   ★ databases 是**这个商户被允许读的账套清单**，第一个是默认库（"一台主机两本账"
        #     的老板就在这里表达）。清单为空时按老规矩「实测账套 == 商户码」判一致，
        #     所以单账套商户不用填也能正常判绿 —— 别让"没登记清单"变成"判成故障"。
        #   旧字段 database_hint 已由 load() 归一化并进 databases，代码里不再读它。
        #   enabled=false 是"停服但不删密钥"的开关（欠费/下线用），路由上一律拒绝。
        "merchants": {},
        # 后台管理页登录密码
        "admin": {"password": "admin123"},
        # 老板用户：openid → 可访问客户端；all_clients=true 表示全部(通用)
        "users": [],
        # true 时连通用访客客户端也要求带 openid（微信登录就绪后开启）
        "require_openid": False,
        # App/多端账号（账号密码登录）：[{username,name,salt,password_hash,iterations,stores[],all_stores,disabled}]
        "accounts": [],
        # 多端 access/refresh token 签名密钥（首次运行自动生成并写回配置）
        "token_secret": "",
        "access_ttl": 43200,      # access token 12 小时
        "refresh_ttl": 2592000,   # refresh token 30 天
        # 开发期允许直接用 openid 换取多端令牌（正式环境建议关闭）
        # ★ 2026-10-12 默认改为 **关**：openid 是客户端**自报**的标识，能自报就能换
        #   12h access + 30d refresh —— 它不是一个凭据，只是一个人的名字。
        #   三端走的都是 `provider: 'v3'`（账套里的操作员口令），没有任何一端需要这条通道；
        #   要开的人必须在 cloud_config.json 里**显式**写 true（并确认微信登录已配好）。
        "allow_openid_login": False,
    },
    # 微信小程序登录（code → openid），secret 只存服务器
    "wechat": {"appid": "", "secret": "", "enabled": False},
    # ★ default_agent 已于 2026-09-25 废除：平台层不该有一个"猜不到的那一家"当兜底。
    #   目标 Agent 一律由 security.target_agent() 从令牌归属裁决；
    #   存量配置里若还留着这个键，load() 会原样存进 _legacy_default_agent，
    #   save() 再原样写回 —— 只为不改坏运维手上的文件，路由上完全不参与。
    "rpc_timeout": 60,
    # ★ 最低 Agent 版本（Phase 4 灰度抓手）。空 = 不强制；
    #   灰度时先给一两家设门槛，别全局一刀切（各家装机时间不同是常态）。
    #   写在 merchants[码].min_agent_version 可按商户覆盖。
    "min_agent_version": "",
    # ★★ 分级在途闸门（2026-09-26 改）。原来这里是"一家总共 2 条，超了立刻 429"，
    #   实测出的问题不是保护过头而是**误伤**：打印一条 op 要跑几十秒，它占掉 2 个位之一时，
    #   老板随手点一下预览、或从列表切到报表页（报表页一次并发 6~8 条），第 3 条就被拒，
    #   屏幕上是一句"本地服务正在处理其他查询，请稍等几秒再试"。
    #   ⇒ 按 op 的**性质**分开限并发，并且"先等一会儿再拒"而不是当场拒：
    #     · report：真压本地 SQL Server 的那几张大表，仍要窄（默认 3）；
    #     · print：慢但不查大表（等打印机/渲染），给它独立的位，别和报表抢；
    #     · quick：列表/明细/单头/能力清单这一类，几十毫秒级，给到 16 就该感觉不到排队；
    #     · total：兜底总闸，防某个页面失控刷屏把协程占满。
    #   每家可在 `auth.merchants[码].inflight` 里按机器规模覆盖（32G 那台可以把 report 给到 4~5）。
    "max_inflight": 24,
    "inflight_by_class": {
        "report": {"cap": 3, "wait_sec": 8},
        "print": {"cap": 2, "wait_sec": 25},
        "quick": {"cap": 16, "wait_sec": 3},
    },
    "max_running_per_agent": 3,  # 每商户同时在跑/排队的异步任务数
    "max_tasks_per_agent": 20,   # 每商户保留的任务结果数
    "rate_limit": {"rate": 8.0, "burst": 20},   # 按 (客户端, op) 的令牌桶
    "log_dir": "logs",
    # ★ H5 站点根（`deploy.py` 的 WEB_ROOT）。后台只用到两个动作：
    #   读 `logs/h5_state.json` 显示发布态、以及「收链」时删 `tenants/<码>` 这一条**软链**。
    #   做成配置项是为了本地验收能指向临时目录，也是为了让"能删什么"这件事只有一处定义。
    "h5_root": "/var/www/jxc-h5",
}


def load(path=None):
    path = path or os.environ.get("CLOUD_CONFIG") or "cloud_config.json"
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for section in DEFAULTS.keys():
            if section in data:
                cfg[section] = data[section]
        if "default_agent" in data:
            cfg["_legacy_default_agent"] = data["default_agent"]
        # ★ 整段赋值意味着"配置文件里没写的新键永远不会出现"（这是历史坑：
        #   auth 里加一个键，线上那份不写就等于没加）。所以只补缺失的默认键，
        #   已存在的一律以文件为准 —— 不覆盖运维手改的值。
        missing = {k: v for k, v in DEFAULTS["auth"].items() if k not in cfg["auth"]}
        for k, v in missing.items():
            cfg["auth"][k] = json.loads(json.dumps(v))
    touched = bootstrap_merchants(cfg)
    # ★ database_hint → databases 的归一化每次启动都跑（幂等）：
    #   线上那份配置是运维手改的，不靠"迁移脚本跑过一次"这种假设。
    if normalize_merchants(cfg):
        touched = True
    if normalize_accounts(cfg):
        touched = True
    # ★ V3 操作员表的键归一小写 —— 必须做：不归的话 `system` 与 `SYSTEM`
    #   会变成两条独立记录，其中一条**永远匹配不上**（2026-10-14 用户实测）。
    #   逻辑放在 security.py 是因为要用它那份权限白名单（PERMS）；
    #   而 config **不能模块级 import security**（security 反过来依赖 config）
    #   ⇒ 这里延迟导入。运行时两个模块都已加载完，拿到的是完整模块。
    from . import security as _security
    if _security.normalize_v3operators(cfg):
        touched = True
    if touched:
        try:
            save(cfg, path)      # 只在真的派生/迁移出内容时落一次盘
        except OSError:
            pass                 # 只读挂载等场景：派生结果本轮内存里仍然有效
    try:
        cfg["_path"] = os.path.abspath(path)
    except Exception:  # noqa: BLE001
        cfg["_path"] = path
    return cfg


# ---------------- 商户（Phase 2：商户成为一等公民） ----------------

def merchants(cfg):
    """{码: 实体}。缺哪个 agent 就现场补一个派生条目，避免"登记了 Agent 却不在商户表里"。"""
    auth = (cfg or {}).get("auth") or {}
    return auth.get("merchants") or {}


def merchant_codes(cfg):
    return list(merchants(cfg) or [])


def enabled_merchants(cfg):
    return [c for c, m in (merchants(cfg) or {}).items() if m.get("enabled", True)]


def merchant_of(cfg, code):
    return (merchants(cfg) or {}).get(str(code or "").strip())


# ------------------------------------------------------- 分级在途闸门（谁算哪一类）

#: 压本地 SQL Server 的那几张宽表/聚合。**必须与 Agent 侧 `agent_daemon.HEAVY_OPS` 同源**
#: （`check-capacity.py` 逐条比对，漂了就红）。两边不一致的后果很具体：
#: 云端按 report 排队、Agent 却把它丢进快池 ⇒ 8 条重查询一起打到 SQL Server 上。
REPORT_OPS = (
    "report.product.ranking",
    "report.customer.ranking",
    # ★ 供应商排行与顾客排行同构（一张 JH/Gysdan 的聚合宽表），2026-10-12 才补进这一类：
    #   漏掉时它落进 quick 池（16 位 / Agent 侧 8 线程），一家连着点几次就把轻活的位占满，
    #   症状是"本地服务正忙"，而这正是分级闸门当初要治的那个病。
    #   ★★ 两边必须一起加：Agent 的 `agent_daemon.HEAVY_OPS` 是同一条判据的另一半。
    "report.supplier.ranking",
    "report.profit.summary",
    "report.customer.product.detail",
    "report.customer.statement",
    "report.receivables.summary",
    "report.purchase.summary",
    "report.purchase.trend",
)
#: 慢但不查大表（等打印机/渲染）。★ 单独一类是必须的：以前它和别的 op 抢同一个总闸，
#: 一条打印能把整家的并发吃光，症状就是"打印之后点什么都说在忙"。
PRINT_OPS = ("print.salesbill",)


def op_class(op):
    """report / print / quick。

    ★ 用**白名单**而不是"以 report. 开头就算重"：`report.sales.summary`、`report.sales.trend`
      实测只有 30~60ms（本地就两张小表），归进重类会让老板每次开报表页都白白堵住
      那 3 个位 —— 分类必须跟着实测代价走，不能跟着名字走。
    """
    name = str(op or "").strip()
    if name in set(REPORT_OPS):
        return "report"
    if name in set(PRINT_OPS):
        return "print"
    return "quick"


def admission_for(cfg, code=None):
    """这家商户每一类能给几个位、最多等几秒。全局默认 + 商户级覆盖，合成一张表。

    商户级写法：`auth.merchants[码].inflight = {"report": {"cap": 5}}`
    （只写 cap 也行：`{"report": 5}` —— 装机现场手改配置时少打几个字少一个错）。
    """
    table = json.loads(json.dumps((cfg or {}).get("inflight_by_class")
                                  or DEFAULTS["inflight_by_class"]))
    total = int((cfg or {}).get("max_inflight") or DEFAULTS["max_inflight"])
    m = merchant_of(cfg, code) if code else None
    over = (m or {}).get("inflight") or {}
    for cls, val in over.items():
        if cls == "total":
            total = int(val if isinstance(val, (int, float)) else (val or {}).get("cap") or total)
            continue
        cur = table.setdefault(cls, {})
        if isinstance(val, (int, float)):
            cur["cap"] = int(val)
        elif isinstance(val, dict):
            cur.update(val)
    out = {cls: {"cap": int(info.get("cap") or 1),
                 "wait_sec": float(info.get("wait_sec") or 0)}
           for cls, info in table.items()}
    out["_total"] = {"cap": max(1, total), "wait_sec": float(table.get("quick", {}).get("wait_sec") or 0)}
    return out


def clients_of_merchant(cfg, code):
    """该商户名下的客户端。clients[].agent 是归属真源，merchants[].clients 只作展示冗余。"""
    auth = (cfg or {}).get("auth") or {}
    code = str(code or "").strip()
    listed = set(((auth.get("merchants") or {}).get(code) or {}).get("clients") or [])
    return [c for c in auth.get("clients", [])
            if str(c.get("agent") or "").strip() == code or c.get("name") in listed]


def bootstrap_merchants(cfg):
    """存量配置里没有 merchants 时，从 agents + clients 派生一份。返回是否改动过。

    ★ 不做双写长期兼容：派生只在"键不存在"时发生一次，之后 merchants 就是事实源，
      admin 侧建 Agent 会同步建商户条目。
    """
    auth = (cfg or {}).get("auth") or {}
    if isinstance(auth.get("merchants"), dict) and auth["merchants"]:
        return False
    derived = {}
    for code in (auth.get("agents") or {}):
        derived[code] = {
            "title": code,
            "enabled": True,
            "databases": [],
            "clients": [c.get("name") for c in auth.get("clients", [])
                        if str(c.get("agent") or "").strip() == code],
        }
    auth["merchants"] = derived
    return bool(derived)


# -------------------------------------------------------- 商户的账套清单（一台主机多本账）
#
# ★ 为什么要它：真实客户里有**一位老板一台主机两个账套**，而旧模型只有
#   「商户码 == 实测账套」这一条判据 —— 于是他的第二本账（库名不可能等于商户码）
#   在后台**永久标红**，"合法的多账套"被当成事故来报警（见 insights._row_alerts）。
#   清单落地后：判据从"等于商户码"变成"属于这家"，越界才是故障。
#   第一步只做登记与判据，请求级路由（带库名的 RPC）在后面。

# SQL Server 库名：常见是字母数字下划线，也有中文与连字符。
# 这里挡掉的是**分隔符与注入字符**（清单会被解析、也会进命令行与 SQL 标识符位置），
# 不是做完整性校验 —— 库名到底存不存在由 Agent 那侧 `sys.databases` 实测说了算。
DB_NAME_RE = re.compile(r"^[\w一-鿿.-]{1,128}$")
_DB_SPLIT_RE = re.compile(r"[,，、;；\n\r\t ]+")


def parse_databases(value):
    """把后台/脚本传来的账套清单归一成 list（去空、去重、保序，第一个=默认库）。

    接受 list，也接受"一行一本 / 逗号分隔"的字符串 —— 向导里那是一个文本框，
    技术员从客户 V3 里抄出来的就是这种形状。
    """
    if value is None:
        return []
    parts = value if isinstance(value, (list, tuple)) else _DB_SPLIT_RE.split(str(value))
    out = []
    for p in parts:
        name = str(p or "").strip().strip("'\"")
        if not name or name in out:
            continue
        out.append(name)
    return out


def normalize_accounts(cfg):
    """给账号补上新字段的默认值（幂等，每次 load 都跑）。

    ★ 为什么必须单独补：load() 里那段 `missing` 只补 `auth` **这一层**的键，
      数组元素里的字段它管不着 —— 老配置里的账号没有 `perms` / `token_version`。
      security.verify_token 要拿账号的 token_version 跟令牌里的 tv 比
      （`int(... or 0)` 兜住了，能跑）；这里补上是让"配置里有什么"和
      "后台/接口显示什么"保持一致，免得界面上一片空白让人以为功能没生效。
    """
    changed = False
    accounts = ((cfg or {}).get("auth") or {}).get("accounts") or []
    for a in accounts:
        if not isinstance(a, dict):
            continue
        if "token_version" not in a:
            a["token_version"] = 0
            changed = True
        if "perms" not in a:
            a["perms"] = []
            changed = True
    return changed


def normalize_merchants(cfg):
    """把旧字段 database_hint 折进 databases。**幂等**，所以每次 load 都跑。

    ★ 迁移是单向的（跑完就没有 database_hint 了），不是"两个字段长期并存各读一半"：
      两处真源迟早会漂移，漂移的症状就是"后台显示的账套和判绿用的账套不是同一个"。
      配置文件里若还留着 database_hint，save() 之后自然消失。
    """
    changed = False
    for _code, m in ((cfg or {}).get("auth") or {}).get("merchants", {}).items():
        if not isinstance(m, dict):
            continue
        if "database_hint" in m:
            legacy = str(m.pop("database_hint") or "").strip()
            if legacy and legacy not in parse_databases(m.get("databases")):
                m["databases"] = [legacy] + parse_databases(m.get("databases"))
            changed = True
        dbs = parse_databases(m.get("databases"))
        if dbs != (m.get("databases") or []):
            m["databases"] = dbs
            changed = True
    return changed


def merchant_databases(cfg, code):
    """这家商户被允许读的账套清单（第一个是默认库）。没登记就是空清单。"""
    m = merchant_of(cfg, code) or {}
    return parse_databases(m.get("databases"))


def default_database(cfg, code):
    return (merchant_databases(cfg, code) or [""])[0]


def allowed_databases(cfg, code):
    """判据用的集合。**空清单 ⇒ 退回「商户码本身」**（单账套商户不登记也判绿）。"""
    return merchant_databases(cfg, code) or [str(code or "").strip()]


def database_state(cfg, code, verified):
    """实测账套的判定：unknown / match / allowed / mismatch。

    match     = 在读的就是默认库（第一本）
    allowed   = 在读的是这家登记过的**另一本**账 —— 合法状态，不是故障
    mismatch  = 不在清单里 ⇒ 才是"客户看到的数字来自另一套账"那种事故
    """
    v = str(verified or "").strip()
    if not v:
        return "unknown"
    dbs = merchant_databases(cfg, code)
    if not dbs:
        return "match" if v == str(code or "").strip() else "mismatch"
    if v == dbs[0]:
        return "match"
    if v in dbs:
        return "allowed"
    return "mismatch"


def database_allowed(cfg, code, name):
    return str(name or "").strip() in allowed_databases(cfg, code)


def save(cfg, path=None):
    """把运行期配置写回文件（后台改设置、自动生成 token_secret、账号变动时调用）。

    ★ 原子替换：这份文件里是**全部**商户的 Agent 密钥、客户端令牌与 token_secret，
      以前是 `open(path,'w')` 直接截断重写 —— 进程在写中途被 kill（systemctl restart
      就会这样）会留下一份残缺 JSON，下次启动直接起不来，等于自造一次全平台故障。
    """
    path = path or cfg.get("_path") or os.environ.get("CLOUD_CONFIG") or "cloud_config.json"
    # ★ 顶层键**由 DEFAULTS 推导**，不写死白名单：以前 load/save 各有一份 5 键列表，
    #   往 DEFAULTS 加一个顶层配置的实际结果是"能读默认值、改了存不住、下次启动
    #   悄悄回到默认"，排查起来极费时间（Phase 4/5 一口气加了 5 个这类键）。
    data = {k: (cfg[k] if k in cfg else json.loads(json.dumps(v)))
            for k, v in DEFAULTS.items()}
    if "_legacy_default_agent" in cfg:      # 原样回写，不参与路由
        data["default_agent"] = cfg["_legacy_default_agent"]
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)                   # 同一目录内的 rename，不会留下半个文件
