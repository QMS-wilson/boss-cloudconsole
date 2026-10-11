# -*- coding: utf-8 -*-
"""云端后台管理 API（/api/admin/*）。

访问模型：
- 客户端(Client)= 一个 V3 账套的访问入口，持有 token；
- 老板用户(User) = 微信 openid，可授权多个客户端（all_clients=true 表示全部/通用）；
- 客户端 access=guest 时免 openid 通用访问（访客模式）；access=auth 时仅限已授权老板。
登录会话在内存（重启失效）；正式部署必须 HTTPS 并修改后台密码。
"""

import json
import os
import re
import secrets
import time
from collections import Counter

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from . import audit as auditmod
from .registry import AgentNoDbRouting
from . import config as cfgmod
from . import insights
from . import security

router = APIRouter()

SESSIONS = {}  # token -> expiry(unix)
SESSION_HOURS = 8


def _save_cfg(cfg):
    path = cfg.get("_path")
    if path:
        cfgmod.save(cfg, path)


def _admin_ok(request):
    token = request.cookies.get("v3admin") or ""
    exp = SESSIONS.get(token)
    return exp is not None and exp > time.time()


def _require(request):
    if not _admin_ok(request):
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")


def _read_audit(cfg, days=7, include_legacy=True):
    """按日切分后只读需要的那几天（Phase 5）。

    ★ 以前每次看板都把整份 audit.jsonl 全量读进内存再数一遍 —— 那是一份只增不减的
      文件，运营页每 5 秒刷一次，读得越久越慢，重启还占着一整份。
    """
    return auditmod.read(cfg, days=days, include_legacy=include_legacy)


def _audit(cfg, op, agent="", client="admin", ok=True, code=None, note="",
           database="", served_database=""):
    entry = {"ts": time.time(), "client": client, "agent": agent,
             "mkt": agent,                       # 约定：商户码 == agent 名
             "op": op, "ok": ok, "code": code, "note": note}
    # Step 3：运营在调试台点了哪本账也要留痕 —— 一台主机两本账时，
    # "谁动了这家"不够，还得能回答"动的是哪一本"
    if database:
        entry["database"] = str(database)
    if served_database and served_database != database:
        entry["database_served"] = str(served_database)
    auditmod.write(cfg, entry)


def ver_tuple(v):
    """"0.2.0+821db9af" → (0,2,0)；解析不出来就当最低（返回 (0,0,0)）。"""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def min_version_for(cfg, code):
    """每商户可覆盖全局（灰度时先要求先发的几家，别一刀切）。空值 = 不强制。"""
    m = cfgmod.merchant_of(cfg, code) or {}
    return (m.get("min_agent_version") or cfg.get("min_agent_version") or "").strip()


def _agent_view(cfg, reg):
    cfg_agents = cfg["auth"].get("agents", {})
    online = reg.list_online()
    seen = reg.last_seen_versions() if hasattr(reg, "last_seen_versions") else {}
    rows = []
    for name in sorted(set(cfg_agents) | set(online)):
        info = online.get(name)
        last = seen.get(name) or {}
        version = (info or {}).get("version") or last.get("version") or ""
        floor = min_version_for(cfg, name)
        rows.append({
            "name": name,
            "secret": cfg_agents.get(name, ""),
            "online": info is not None,
            "source": info["source"] if info else "",
            "version": version,
            "build_id": version.split("+")[-1] if "+" in version else "",
            "ops": info["ops"] if info else [],
            "connected_at": info["connected_at"] if info else None,
            "last_seen": reg.last_seen.get(name),
            # 装机指纹：同名背后到底是哪台机器，出顶号事故时全靠这一列分辨
            "install_id": (info or {}).get("install_id", "") or last.get("install_id", ""),
            "hostname": (info or {}).get("hostname", "") or last.get("hostname", ""),
            # ★ Phase 4：离线也保留"最后一次看见的版本"，否则灰度看不出还剩谁家没换包
            "last_version_seen": last.get("version") or "",
            "last_online_at": last.get("last_online"),
            # ★ B3：配置里的账套名 + Agent 实测回来的账套名。
            #   两者不一致 = 这家客户机正在读另一套账（09-26 00:38 那起事故的唯一外部征兆）。
            #   老包不上报，所以这里必须允许空值，并把"未知"与"一致"分开显示。
            "database": (info or {}).get("database") or last.get("database") or "",
            "database_verified": ((info or {}).get("database_verified")
                                  or last.get("database_verified") or ""),
            "db_state": cfgmod.database_state(
                cfg, name,
                (info or {}).get("database_verified") or last.get("database_verified") or ""),
            "databases": cfgmod.merchant_databases(cfg, name),
            # 客户机自己放行的清单与它实际服务过哪几本（Step 2 起才有；老包为空）
            "allowed_databases": (info or {}).get("allowed_databases")
                                 or last.get("allowed_databases") or [],
            "served_databases": (info or {}).get("served_databases") or [],
            "min_agent_version": floor,
            # 调试台的说明与示例参数（Agent 自报）。只在这里与 /agents 回，
            # 健康一览那种"一次回所有家"的接口不带 —— 一份就 8KB，×N 家会把列表接口撑肥。
            "op_docs": (info or {}).get("op_docs") or {},
            "version_too_old": bool(floor and ver_tuple(version) < ver_tuple(floor)),
        })
    return rows


def _norm_user(body, clients):
    name = str(body.get("name") or "").strip()
    openid = str(body.get("openid") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="缺少用户名称")
    if not openid or len(openid) < 6:
        raise HTTPException(status_code=400, detail="openid 不能为空且至少 6 位")
    names = [str(c.get("name")) for c in clients]
    user_clients = [x for x in (body.get("clients") or []) if x in names]
    return {"name": name, "openid": openid, "clients": user_clients,
            "all_clients": bool(body.get("all_clients"))}


@router.post("/login")
async def login(request: Request):
    cfg = request.app.state.cfg
    try:
        body = await request.json()
    except Exception:
        body = {}
    admin = cfg["auth"].get("admin", {})
    if str(body.get("password") or "") != str(admin.get("password", "admin123")):
        _audit(cfg, "admin.login", ok=False, code="BAD_PASSWORD", note="后台登录失败")
        raise HTTPException(status_code=401, detail="密码错误")
    token = secrets.token_hex(24)
    SESSIONS[token] = time.time() + SESSION_HOURS * 3600
    _audit(cfg, "admin.login", ok=True, note="后台登录成功")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("v3admin", token, httponly=True, samesite="lax", max_age=SESSION_HOURS * 3600)
    return resp


@router.post("/logout")
async def logout(request: Request):
    SESSIONS.pop(request.cookies.get("v3admin") or "", None)
    _audit(request.app.state.cfg, "admin.logout")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("v3admin")
    return resp


@router.get("/state")
async def state(request: Request):
    _require(request)
    cfg = request.app.state.cfg
    return {
        "agents": _agent_view(cfg, request.app.state.registry),
        "clients": cfg["auth"].get("clients", []),
        "users": cfg["auth"].get("users", []),
        "accounts": security.list_accounts(cfg),
        "default_agent": cfg.get("_legacy_default_agent"),   # 已废除，仅为兼容旧页面读取
        "rpc_timeout": cfg.get("rpc_timeout"),
        # 同名冲突记录：哪家正被第二台机器试图占用，运营者一眼看得到
        "conflicts": list(reversed(getattr(request.app.state.registry, "conflicts", [])[-20:])),
        "takeover_open": {k: v for k, v in
                          getattr(request.app.state.registry, "takeover_until", {}).items()},
        # ★ Phase 2 新增（页面用 || [] 兜底，多一个键是纯增量的）
        "merchants": _merchant_rows(cfg, request.app.state.registry),
    }


@router.get("/agents")
async def agents_list(request: Request):
    _require(request)
    return {"agents": _agent_view(request.app.state.cfg, request.app.state.registry)}


@router.post("/agents")
async def agent_upsert(request: Request):
    _require(request)
    cfg = request.app.state.cfg
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体不是 JSON")
    name = str(body.get("name") or "").strip()
    secret = str(body.get("secret") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="缺少 Agent 名称")
    agents = cfg["auth"].setdefault("agents", {})
    existed = name in agents
    if existed and not body.get("confirm_rotate"):
        # ★★ 改造前这里是无条件 `agents[name] = secret`，而不填 secret 时又自动生成一个
        #   —— 也就是说：在后台把已有商户的名字打错一个字母再提交，就会**静默换掉那一家
        #   正在使用的 Agent 密钥**，客户机立刻连不上，表现为"接口突然全挂"。
        #   现在同名必须显式 confirm_rotate（前端引导走「轮换密钥」按钮），否则拒。
        raise HTTPException(
            status_code=409,
            detail="Agent「%s」已登记。要改密钥请用「轮换密钥」，要新建请换一个名字" % name)
    if not secret:
        secret = secrets.token_urlsafe(16)
    if len(secret) < 8:
        raise HTTPException(status_code=400, detail="密钥至少 8 位")
    agents[name] = secret
    # ★ 登记 Agent 就同步建商户条目（Phase 2：merchants 是事实源，agents 只存密钥）。
    #   不这么做的话，新建的 Agent 能连上 WS，但 resolve_rpc_context 会因为
    #   "商户表里没这个码" 把它的客户全部拒掉 —— 那种"能上线但不能用"最难查。
    ms = cfg["auth"].setdefault("merchants", {})
    if name not in ms:
        ms[name] = {"title": str(body.get("title") or name), "enabled": True,
                    "databases": [], "clients": []}
    _save_cfg(cfg)
    _audit(cfg, "admin.agent.save", agent=name,
           note="登记 Agent" if not existed else "★ 确认轮换已存在的 Agent 密钥")
    return {"ok": True, "existed": existed, "agent": {"name": name, "secret": secret}}


@router.delete("/agents/{name}")
async def agent_delete(name: str, request: Request):
    _require(request)
    cfg = request.app.state.cfg
    agents = cfg["auth"].get("agents", {})
    if name not in agents:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    if name in request.app.state.registry.agents:
        raise HTTPException(status_code=409, detail="Agent 在线，请先断开再删除登记")
    del agents[name]
    _save_cfg(cfg)
    _audit(cfg, "admin.agent.delete", agent=name, note="删除 Agent 登记")
    return {"ok": True}


# ---------------- 商户（Phase 2：商户成为一等公民） ----------------

# 与 H5 发布脚本 deploy.py 用同一套字符集：商户码要进 URL 前缀、nginx 正则、令牌 claims，
# 三处任何一处放宽都会在"第二家"身上出事。
MERCHANT_CODE_RE = re.compile(r"^[a-z][a-z0-9-]{1,29}$")


def _merchant_rows(cfg, reg):
    ms = cfgmod.merchants(cfg)
    agents = cfg["auth"].get("agents", {}) or {}
    online = (reg.list_online() if reg is not None else {}) or {}
    rows = []
    for code in sorted(ms):
        m = ms[code] or {}
        rows.append({
            "code": code, "title": m.get("title") or code,
            "enabled": bool(m.get("enabled", True)),
            # 账套清单（第一个=默认库）。"一台主机两本账"的老板在这里表达；
            # 判一致用的是 config.database_state（实测 ∈ 清单），不再是"实测 == 商户码"。
            "databases": cfgmod.parse_databases(m.get("databases")),
            "database_default": cfgmod.default_database(cfg, code),
            "clients": [c.get("name") for c in cfgmod.clients_of_merchant(cfg, code)],
            "accounts": [a.get("username") for a in cfg["auth"].get("accounts", [])
                         if code in security.merchants_for_names(
                             cfg, a.get("stores"), a.get("all_stores"))],
            "agent_registered": code in agents,
            "online": code in online,
            # 灰度时"这家还没换包"要一眼看得到（Phase 4）
            "min_agent_version": (m.get("min_agent_version")
                                  or (cfg or {}).get("min_agent_version") or ""),
        })
    return rows


@router.get("/merchants")
async def merchants_list(request: Request):
    _require(request)
    return {"merchants": _merchant_rows(request.app.state.cfg, request.app.state.registry)}


@router.post("/merchants")
async def merchant_upsert(request: Request):
    """建/改一家商户。

    ★ 新建时顺手生成 Agent 密钥并**只在这一次**回显（Phase 3 的 provision-merchant.py
      就是这条 + 客户端 + 首个账号打成一份开通包）。之后任何接口都不再返回密钥。
    """
    _require(request)
    cfg = request.app.state.cfg
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体不是 JSON")
    code = str(body.get("code") or "").strip()
    if not MERCHANT_CODE_RE.match(code):
        raise HTTPException(status_code=400, detail=(
            "商户码不合法：%s（要求小写字母开头，可含数字与 -，长度 2-30）" % code))
    ms = cfg["auth"].setdefault("merchants", {})
    agents = cfg["auth"].setdefault("agents", {})
    created = code not in ms
    m = ms.setdefault(code, {"title": code, "enabled": True, "databases": [], "clients": []})
    if "title" in body:
        m["title"] = str(body.get("title") or code).strip() or code
    if "databases" in body:
        dbs = cfgmod.parse_databases(body.get("databases"))
        bad = [d for d in dbs if not cfgmod.DB_NAME_RE.match(d)]
        if bad:
            raise HTTPException(status_code=400, detail=(
                "账套名不合法：%s（不能带空格、引号、逗号这类分隔符之外的字符，单个不超过 128 位）"
                % "、".join(bad[:3])))
        if len(dbs) > 8:
            raise HTTPException(status_code=400,
                                detail="单个商户最多登记 8 个账套（当前 %d 个）"
                                % len(dbs))
        m["databases"] = dbs
    if "enabled" in body:
        m["enabled"] = bool(body.get("enabled"))
    if "min_agent_version" in body:
        mv = str(body.get("min_agent_version") or "").strip()
        if mv and not re.match(r"^\d+\.\d+\.\d+$", mv):
            raise HTTPException(status_code=400,
                                detail="最低 Agent 版本要写成 0.2.0 这种三段号（留空表示不强制）")
        m["min_agent_version"] = mv
    secret = None
    if code not in agents:
        secret = secrets.token_urlsafe(16)
        agents[code] = secret
        note = "新建商户（同时登记同名 Agent 并生成密钥）"
    else:
        note = "修改商户条目"
    _save_cfg(cfg)
    _audit(cfg, "admin.merchant.save", agent=code, note=note)
    out = {"ok": True, "created": created, "code": code, "merchant": m}
    if secret:
        out["agent_secret"] = secret
    return out


@router.delete("/merchants/{code}")
async def merchant_delete(code: str, request: Request, confirm: str = ""):
    """下线一家商户。默认只停用（enabled=false，密钥留着随时恢复）；
    真删要显式 confirm=delete —— 呆会人只要重装一次就要重新发密钥。"""
    _require(request)
    cfg = request.app.state.cfg
    ms = cfg["auth"].get("merchants") or {}
    if code not in ms:
        raise HTTPException(status_code=404, detail="商户不存在：%s" % code)
    if code in request.app.state.registry.agents:
        raise HTTPException(status_code=409, detail="该商户的 Agent 在线，请先在 Agent 页断开")
    bound = cfgmod.clients_of_merchant(cfg, code)
    if bound and confirm != "delete":
        raise HTTPException(status_code=409, detail=(
            "名下还有 %d 个客户端（%s）。只想停服请用 enabled=false；"
            "确认连带删客户端归属请带 confirm=delete"
            % (len(bound), "、".join(str(c.get("name") or "") for c in bound))))
    # ★ 应用账号还指着这家的，一律先处理账号再删商户。
    #   以前这里只挡客户端：商户删掉之后账号的 stores 指向一个刚被解绑的客户端，
    #   那个账号还能登录但可及商户变成空集 —— 症状是"老板说登录进去什么都没有"，
    #   而审计里查不到任何异常。宁可现在拒得明确。
    still = [a.get("username") for a in cfg["auth"].get("accounts", [])
             if code in security.merchants_for_names(cfg, a.get("stores"), a.get("all_stores"))]
    if still:
        raise HTTPException(status_code=409, detail=(
            "名下还有 %d 个应用账号（%s）。请先在「设置 → 应用账号」里删掉或改绑，再删商户"
            % (len(still), "、".join(x for x in still if x))))
    # ★ 前缀还开着就先收链 —— 这是"删除闭环"的最后一道门。
    #   以前删完商户，nginx 上那条 `tenants/<码>` 软链还留着，于是公网多出一个
    #   "能打开、但永远停在登录页"的地址，而且没有任何地方记着它是谁的。
    live = insights.scan_h5(cfg)
    if code in insights.published_codes(live):
        info = (live.get("tenants") or {}).get(code) or {}
        raise HTTPException(status_code=409, detail=(
            "访问前缀 /%s/ 还在发布中%s。请先点「收链」下线这个前缀，再删商户"
            % (code, "（指向构建 %s）" % info.get("build_id") if info.get("build_id")
               else "（tenants/%s 是真目录，要用发布脚本人工处理）" % code)))
    if confirm == "delete":
        ms.pop(code, None)
        (cfg["auth"].get("agents") or {}).pop(code, None)
        for c in bound:
            c["agent"] = ""       # 归属清空：路由上会以 TENANT_UNBOUND 明确拒绝，不会静默进别家
        _audit(cfg, "admin.merchant.delete", agent=code, note="删除商户与同名 Agent 登记")
    else:
        ms[code]["enabled"] = False
        _audit(cfg, "admin.merchant.disable", agent=code,
               note="停用商户（保留密钥，可随时恢复）")
    _save_cfg(cfg)
    return {"ok": True}


@router.get("/merchant-check")
async def merchant_check(request: Request, code: str = ""):
    """B8：开通向导第 1 步的即时校验（格式 / 保留字 / 是否已被占用）。"""
    _require(request)
    return {"ok": True, "data": insights.check_code(request.app.state.cfg, code)}


@router.get("/merchant-health")
async def merchant_health(request: Request, days: int = 1):
    """B1：一商户一行的健康数据 + 顶部告警条要的那份汇总。

    ★ 为什么需要它：以前"这家怎么样"要前端发 4 个请求（agents / merchants / clients /
      accounts）再自己 join —— 商户一多就是 N 倍请求，而且每个请求各拿各的时间点，
      拼出来的行会互相矛盾（这条在线、那条已离线）。聚合放服务端，一次拿全、同一时刻。
    """
    _require(request)
    cfg = request.app.state.cfg
    rows = insights.merchant_rows(cfg, request.app.state.registry, days=days)
    return {"ok": True, "days": max(1, min(days, 90)), "generated_at": time.time(),
            "merchants": rows, "alerts": insights.alerts(rows),
            "h5": insights.h5_state(cfg)}


@router.get("/monitor")
async def monitor(request: Request):
    """监控页：Phase 5 那几道闸门的实况 + 合并时间线。

    闸门此前是"静默生效"的保护 —— 回 429 之后运营者什么都看不见，
    就会把"某家页面变慢"误判成"云端坏了"。"""
    _require(request)
    cfg = request.app.state.cfg
    reg = request.app.state.registry
    rows = insights.merchant_rows(cfg, reg, days=1)
    return {"ok": True, "generated_at": time.time(),
            "stats": insights.monitor(cfg, reg, rows=rows),
            "merchants": rows,
            "events": insights.timeline(cfg, reg, limit=40)}


@router.get("/timeline")
async def timeline(request: Request, code: str = "", limit: int = 30):
    _require(request)
    return {"ok": True,
            "events": insights.timeline(request.app.state.cfg, request.app.state.registry,
                                        code=code or None, limit=limit)}


@router.get("/h5")
async def h5_state(request: Request):
    """B4：H5 发布态。`snapshot` 是发布脚本回写的那份（可能过期），
    `live` 是此刻直接看文件系统的结果 —— 动手（收链）之前必须看 live。"""
    _require(request)
    cfg = request.app.state.cfg
    snap = insights.h5_state(cfg)
    return {"ok": True, "data": snap, "live": insights.scan_h5(cfg),
            "differs": sorted((snap.get("tenants") or {})) != sorted((insights.scan_h5(cfg).get("tenants") or {}))}


@router.get("/impact")
async def impact(request: Request, code: str = "", action: str = "disable"):
    """B7：危险动作的活值影响面。确认框里写"确定吗"没有价值，
    决定要不要点下去的是"这家现在在线吗、几个客户端在用、刚才还在被调用吗"。"""
    _require(request)
    if not code:
        raise HTTPException(status_code=400, detail="缺少 code")
    data = insights.impact(request.app.state.cfg, request.app.state.registry,
                           code, action=action)
    if data is None:
        raise HTTPException(status_code=404, detail="商户不存在：%s" % code)
    return {"ok": True, "data": data}


@router.get("/merchants/{code}")
async def merchant_detail(code: str, request: Request, days: int = 1):
    """B2：单商户详情（含由事实推导的接入进度、本商户审计与失败清单）。"""
    _require(request)
    data = insights.merchant_detail(request.app.state.cfg, request.app.state.registry,
                                    code, days=days)
    if data is None:
        raise HTTPException(status_code=404, detail="商户不存在：%s" % code)
    return {"ok": True, "data": data}


@router.post("/merchants/{code}/unpublish-prefix")
async def merchant_unpublish_prefix(code: str, request: Request):
    """收掉这家商户的 H5 前缀：只删 `tenants/<码>` 这一条软链，删完自己看一眼是不是 404。

    ★ 为什么"收链"可以让后台动手、"发布"不行：发布带逐文件 sha256 校验与
      `nginx -t` 试跑两道门，那些都在 `deploy.py` 里，后台代跑等于绕过它们。
      收链只是删一条符号链接，nginx 按路径解析、不需要 reload，也不会碰别家的链。
    ★ 命令行等价物：`deploy.py --merchant <码> --unpublish`（它同样会回写 h5_state.json）。
    """
    _require(request)
    cfg = request.app.state.cfg
    try:
        r = insights.unpublish_prefix(cfg, code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not r.get("removed"):
        # 本来就没这条链：不算错误，但要说清楚，别让界面显示"已收链"这种假成功
        _audit(cfg, "admin.merchant.unpublish", agent=code, ok=True,
               note="收链：无此前缀（%s）" % r.get("reason"))
        return {"ok": True, "removed": False, "message": r.get("reason") or "无需处理"}
    _audit(cfg, "admin.merchant.unpublish", agent=code,
           note="收链 /%s/（原指向 %s）；本机复测 HTTP %s"
                % (code, r.get("build_id"), r.get("probe_status")))
    return {"ok": True, "removed": True, "build_id": r.get("build_id"),
            "probe_status": r.get("probe_status"),
            "message": "已下线 /%s/%s" % (code, "" if r.get("probe_status") in (404, None)
                                          else "，但本机复测返回 %s（请检查 nginx）"
                                               % r.get("probe_status"))}


@router.post("/agents/{name}/secret")
async def agent_secret(name: str, request: Request):
    _require(request)
    cfg = request.app.state.cfg
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体不是 JSON")
    secret = str(body.get("secret") or "").strip()
    if len(secret) < 8:
        raise HTTPException(status_code=400, detail="密钥至少 8 位")
    cfg["auth"].setdefault("agents", {})[name] = secret
    _save_cfg(cfg)
    _audit(cfg, "admin.agent.secret", agent=name, note="后台修改 Agent 密钥")
    return {"ok": True, "message": "已更新；Agent 下次重连生效"}


@router.post("/agents/{name}/disconnect")
async def agent_disconnect(name: str, request: Request):
    _require(request)
    ok = await request.app.state.registry.disconnect(name)
    if not ok:
        raise HTTPException(status_code=404, detail="Agent 不在线")
    _audit(request.app.state.cfg, "admin.agent.disconnect", agent=name, note="后台断开 Agent")
    return {"ok": True}


@router.post("/agents/{name}/takeover")
async def agent_takeover(name: str, request: Request):
    """允许另一台机器用这个名字接管上线（有效 3 分钟）。

    ★ 为什么需要人工点一下：同名只允许一条连接，而"换机/重装系统/硬盘坏了"
      都会带来新的装机指纹。自动放行就等于回到"谁先连上算谁"——
      那正是把在线商户顶掉的那类事故；所以宁可要一次明确的确认。
    """
    _require(request)
    cfg = request.app.state.cfg
    if name not in (cfg["auth"].get("agents") or {}):
        raise HTTPException(status_code=404, detail="Agent 未登记：%s" % name)
    until = request.app.state.registry.allow_takeover(name, 180)
    _audit(cfg, "admin.agent.takeover", agent=name,
           note="授权换机接管（3 分钟内有效），请让对方机器现在重连")
    return {"ok": True, "until": until,
            "message": "已放行 3 分钟：让对方机器上的 Agent 现在重连（或点『启动后台 Agent』）"}


@router.get("/audit")
async def audit(request: Request, op: str = "", agent: str = "", client: str = "",
                mkt: str = "", sub: str = "", ip: str = "", ok: str = "",
                kind: str = "", rid: str = "", code_filter: str = "",
                days: int = 7, limit: int = 300, offset: int = 0):
    """运营侧审计查询。

    ★ 匹配一律改精确（以前是 `needle.lower() in value.lower()`）：
      按商户过滤时 `shiwantongda` 会把别家里名字含它的商户一起带出来 ——
      在多租户表上做子串过滤本身就是跨商户泄露。op 仍允许子串（运营就是想看一类操作）。

    ★ B5 新增：`kind=admin|data` 把"运营者动过什么"从上万条数据请求里分出来
      （出事先翻这本账），`rid` 把同一条请求的整条链路串起来，
      `offset/limit` 从最新往回翻页。
      游标用"距最新第几条"而不是数组下标 —— 文件一直在追加，下标会漂；
      从尾部数则只在新请求进来时整体位移，翻页时顶多重复看到一条，不会漏。
    """
    _require(request)
    days = max(1, min(days, 90))
    entries = _read_audit(request.app.state.cfg, days=days)
    if op:
        entries = [e for e in entries if op.lower() in (e.get("op") or "").lower()]
    for key, want in (("agent", agent), ("client", client), ("mkt", mkt),
                      ("sub", sub), ("ip", ip), ("rid", rid)):
        if not want:
            continue
        entries = [e for e in entries if str(e.get(key) or "") == want]
    if code_filter:
        entries = [e for e in entries if str(e.get("code") or "") == code_filter]
    if kind == "admin":
        entries = [e for e in entries if str(e.get("op") or "").startswith("admin.")]
    elif kind == "data":
        entries = [e for e in entries if not str(e.get("op") or "").startswith("admin.")]
    if ok in ("true", "1"):
        entries = [e for e in entries if e.get("ok")]
    elif ok in ("false", "0"):
        entries = [e for e in entries if not e.get("ok")]
    limit = max(10, min(limit, 2000))
    offset = max(0, min(int(offset or 0), 500000))
    newest_first = list(reversed(entries))
    page = newest_first[offset:offset + limit]
    nxt = offset + len(page)
    return {"entries": page, "window_days": days, "total": len(newest_first),
            "offset": offset, "next_offset": nxt, "has_more": nxt < len(newest_first)}


@router.get("/stats")
async def stats(request: Request, days: int = 7):
    _require(request)
    cfg = request.app.state.cfg
    reg = request.app.state.registry
    days = max(1, min(days, 90))
    entries = _read_audit(cfg, days=days)
    ok_n = sum(1 for e in entries if e.get("ok"))
    today = time.strftime("%Y-%m-%d")
    by_op = Counter(e.get("op", "-") for e in entries).most_common(8)
    # 按日切分之后，文件名就是日期 —— 分桶不需要再把每条记录的 ts 换算一遍
    days_chart = []
    today_n = 0
    for i in range(6, -1, -1):
        day = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
        n = auditmod.count_day(cfg, day)
        if day == today:
            today_n = n
        days_chart.append({"date": day[5:], "count": n})
    per = insights._per_merchant(entries)
    return {"total": len(entries), "ok": ok_n, "fail": len(entries) - ok_n,
            "window_days": days,
            "today": today_n, "by_op": [{"op": k, "count": v} for k, v in by_op],
            "days": days_chart, "online_agents": len(reg.list_online()),
            "by_merchant": [{"mkt": k, "count": v} for k, v in Counter(
                e.get("mkt") or e.get("agent") or "-" for e in entries).most_common(10)],
            # ★ B6：光有"每家几条"判不出好坏 —— 失败数与最近成功时间才是判断依据
            "per_merchant": [{"mkt": k, "calls": v["calls"], "fail": v["fail"],
                              "last_ok_at": v["last_ok_at"], "last_ok_op": v["last_ok_op"],
                              "codes": v["codes"]}
                             for k, v in sorted(per.items(), key=lambda kv: -kv[1]["calls"])],
            "clients": len(cfg["auth"].get("clients", [])),
            "users": len(cfg["auth"].get("users", []))}


@router.get("/settings")
async def settings_get(request: Request):
    _require(request)
    cfg = request.app.state.cfg
    return {"rpc_timeout": cfg.get("rpc_timeout"),
            "min_agent_version": str(cfg.get("min_agent_version") or ""),
            "h5_root": insights.h5_root(cfg),
            "log_dir": cfg.get("log_dir"), "server": cfg.get("server"),
            "require_openid": bool(cfg["auth"].get("require_openid")),
            "admin_password_set": bool(cfg["auth"].get("admin", {}).get("password")),
            "portal_token": str(cfg["auth"].get("portal_token") or ""),
            # ★ 明文直出（2026-09-26 用户拍板）：这些值本来就躺在同一个登录态能读到的
            #   接口里（/api/admin/agents 每行都带 secret、/state 带所有 client token），
            #   界面上遮一半并不构成防线，只会让人在审查时去"重置密钥"——那才是事故来源。
            #   所以这里给全值，并把代价写在页面文案里（截图/投屏/远程协助前先收起来）。
            "admin_password": str(cfg["auth"].get("admin", {}).get("password") or ""),
            "token_secret": str(cfg["auth"].get("token_secret") or ""),
            "wechat": {
                "enabled": bool(cfg.get("wechat", {}).get("enabled")),
                "appid": str(cfg.get("wechat", {}).get("appid") or ""),
                "secret_set": bool(cfg.get("wechat", {}).get("secret")),
                "secret": str(cfg.get("wechat", {}).get("secret") or ""),
            }}


@router.post("/settings")
async def settings_set(request: Request):
    _require(request)
    cfg = request.app.state.cfg
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体不是 JSON")
    if body.get("password"):
        pwd = str(body["password"])
        if len(pwd) < 6:
            raise HTTPException(status_code=400, detail="密码至少 6 位")
        cfg["auth"].setdefault("admin", {})["password"] = pwd
    if body.get("default_agent"):
        # 键已废除（目标商户只由令牌裁决，见 security.target_agent）；
        # 这里接收但不生效，避免旧页面一次保存就把在线路由改到别家
        _audit(cfg, "admin.settings.ignored", note="default_agent 已废除，本次提交被忽略")
    if body.get("rpc_timeout"):
        cfg["rpc_timeout"] = min(max(int(body["rpc_timeout"]), 5), 300)
    if body.get("require_openid") is not None:
        cfg["auth"]["require_openid"] = bool(body["require_openid"])
    if "portal_token" in body:
        pt = str(body.get("portal_token") or "").strip()
        if pt and len(pt) < 8:
            raise HTTPException(status_code=400, detail="入口令牌至少 8 位")
        cfg["auth"]["portal_token"] = pt
    if "min_agent_version" in body:
        # 全局门槛：灰度时先给一两家单独设（merchants[码]），全局值兜底。
        mv = str(body.get("min_agent_version") or "").strip()
        if mv and not re.match(r"^\d+\.\d+\.\d+$", mv):
            raise HTTPException(status_code=400,
                                detail="最低 Agent 版本要写成 0.2.0 这种三段号（留空表示不强制）")
        cfg["min_agent_version"] = mv
    if isinstance(body.get("wechat"), dict):
        wx = body["wechat"]
        cfg.setdefault("wechat", {})
        if wx.get("enabled") is not None:
            cfg["wechat"]["enabled"] = bool(wx["enabled"])
        if wx.get("appid") is not None:
            cfg["wechat"]["appid"] = str(wx["appid"]).strip()
        if wx.get("secret"):
            cfg["wechat"]["secret"] = str(wx["secret"]).strip()
    _save_cfg(cfg)
    changed = [k for k in ("password", "rpc_timeout", "require_openid",
                           "portal_token", "wechat") if body.get(k) is not None]
    _audit(cfg, "admin.settings", note="修改设置：" + "、".join(changed))
    return {"ok": True}


@router.post("/rpc")
async def rpc(request: Request):
    _require(request)
    cfg = request.app.state.cfg
    reg = request.app.state.registry
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体不是 JSON")
    # 后台调试台是运营者的工具，允许点名任意商户 —— 但必须**显式**点名且已登记，
    # 不再兜底到某一家（旧行为：不填 agent 就发给 default_agent，等于误操作别家的库）
    agent_name = str(body.get("agent") or "").strip()
    op = str(body.get("op") or "").strip()
    if not agent_name or not op:
        raise HTTPException(status_code=400, detail="缺少 agent/op（调试台必须显式指定商户）")
    if agent_name not in (cfg["auth"].get("agents") or {}):
        raise HTTPException(status_code=404, detail="Agent 未登记：%s" % agent_name)
    # ★ Step 3：调试台也要能指定读哪本账（一台主机两本账时，
    #   平台方远程替老板核对某张单要落到哪本上，不能只能"整店切库"）
    db_name, derr = security.resolve_database(cfg, None, agent_name, body)
    if derr:
        # 回标准错误信封而不是 HTTPException：前端 api() 两种都认，
        # 但只有前者带 code —— 审计与自动化脚本要靠 code 分类，不能靠读中文文案
        return JSONResponse(status_code=derr[0], content=derr[1])
    timeout = min(max(int(body.get("timeout") or cfg.get("rpc_timeout", 60)), 5), 300)
    try:
        resp = await reg.rpc(agent_name, op, body.get("params") or {}, timeout,
                             database=db_name)
        _audit(cfg, op, agent=agent_name, ok=bool(resp.get("ok")),
               code=resp.get("error", {}).get("code"), database=db_name,
               served_database=str(resp.get("database") or ""),
               note="后台调试台手动执行")
        return {"ok": True, "response": resp}
    except AgentNoDbRouting:
        _audit(cfg, op, agent=agent_name, ok=False, code="AGENT_NO_DB_ROUTING",
               database=db_name, note="后台调试台：该客户机是旧包，不支持选账套")
        return {"ok": False, "error": {"code": "AGENT_NO_DB_ROUTING",
                                       "message": "这台客户机还是旧版本包，不会按请求选账套；先换包"}}
    except Exception as exc:  # noqa: BLE001
        _audit(cfg, op, agent=agent_name, ok=False,
               code=type(exc).__name__, note="后台调试台手动执行失败")
        return {"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)}}


# ---------------- 客户端 ----------------
@router.post("/clients")
async def client_add(request: Request):
    _require(request)
    cfg = request.app.state.cfg
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体不是 JSON")
    name = str(body.get("name") or "").strip()
    agent = str(body.get("agent") or "").strip()
    access = str(body.get("access") or "guest").strip()
    if access not in ("guest", "auth"):
        raise HTTPException(status_code=400, detail="访问模式只能是 guest(通用访客) 或 auth(仅授权老板)")
    if not name:
        raise HTTPException(status_code=400, detail="缺少客户端名称")
    # ★ 必须指向一个已登记的 Agent。改造前这里会兜底到 default_agent，
    #   于是"客户端名字打错"这种失误的后果是**整家商户的流量静默进别家的库** ——
    #   数字全都正常，只是不是这家的。宁可当场拒掉。
    if agent not in (cfg["auth"].get("agents") or {}):
        raise HTTPException(status_code=400,
                            detail="Agent「%s」未在平台登记，请先到「Agent」页建好再绑" % (agent or "(空)"))
    clients = cfg["auth"].setdefault("clients", [])
    if any(c.get("name") == name for c in clients):
        raise HTTPException(status_code=409, detail="客户端名称已存在")
    client = {"token": secrets.token_hex(16), "name": name,
              "title": str(body.get("title") or "").strip() or name,
              "agent": agent, "access": access}
    clients.append(client)
    _save_cfg(cfg)
    _audit(cfg, "admin.client.add", agent=agent, note=f"新增客户端：{name}（{access}）")
    return {"ok": True, "client": client}


@router.post("/clients/{token}")
async def client_update(token: str, request: Request):
    _require(request)
    cfg = request.app.state.cfg
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体不是 JSON")
    clients = cfg["auth"].get("clients", [])
    for c in clients:
        if c.get("token") == token:
            if body.get("name"):
                if any(x.get("name") == str(body["name"]).strip() and x is not c for x in clients):
                    raise HTTPException(status_code=409, detail="名称已存在")
                c["name"] = str(body["name"]).strip()
            if "title" in body:
                c["title"] = str(body.get("title") or "").strip() or c.get("name") or ""
            if body.get("agent"):
                new_agent = str(body["agent"]).strip()
                # 同 client_add：改绑定也必须指向已登记的 Agent，打错字不能静默进别家
                if new_agent not in (cfg["auth"].get("agents") or {}):
                    raise HTTPException(status_code=400,
                                        detail="Agent「%s」未在平台登记" % new_agent)
                c["agent"] = new_agent
            if body.get("access"):
                access = str(body["access"]).strip()
                if access not in ("guest", "auth"):
                    raise HTTPException(status_code=400, detail="访问模式只能是 guest 或 auth")
                c["access"] = access
            _save_cfg(cfg)
            _audit(cfg, "admin.client.update", agent=c.get("agent", ""),
                   note=f"更新客户端：{c.get('name')}（{c.get('access', 'guest')}）")
            return {"ok": True, "client": c}
    raise HTTPException(status_code=404, detail="客户端不存在")


@router.delete("/clients/{token}")
async def client_delete(token: str, request: Request):
    _require(request)
    cfg = request.app.state.cfg
    clients = cfg["auth"].get("clients", [])
    new = [c for c in clients if c.get("token") != token]
    if len(new) == len(clients):
        raise HTTPException(status_code=404, detail="客户端不存在")
    cfg["auth"]["clients"] = new
    _save_cfg(cfg)
    _audit(cfg, "admin.client.delete", note="删除客户端令牌")
    return {"ok": True}


@router.get("/recent-logins")
async def recent_logins(request: Request):
    _require(request)
    rec = getattr(request.app.state, "recent_logins", {})
    rows = [info for info in rec.values() if info.get("openid")]
    rows.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return {"logins": rows}


# ---------------- 老板用户 ----------------
@router.get("/users")
async def users_list(request: Request):
    _require(request)
    cfg = request.app.state.cfg
    rec = getattr(request.app.state, "recent_logins", {})
    out = []
    for u in cfg["auth"].get("users", []):
        info = rec.get(u.get("openid"))
        out.append({**u, "last_seen": info.get("ts") if info else None})
    return {"users": out}


@router.post("/users")
async def user_add(request: Request):
    _require(request)
    cfg = request.app.state.cfg
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体不是 JSON")
    users = cfg["auth"].setdefault("users", [])
    u = _norm_user(body, cfg["auth"].get("clients", []))
    if any(x.get("openid") == u["openid"] for x in users):
        raise HTTPException(status_code=409, detail="该 openid 已存在")
    users.append(u)
    _save_cfg(cfg)
    _audit(cfg, "admin.user.add", note=f"新增老板：{u['name']}（{u['openid']}）")
    return {"ok": True, "user": u}


@router.post("/users/{openid}")
async def user_update(openid: str, request: Request):
    _require(request)
    cfg = request.app.state.cfg
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体不是 JSON")
    users = cfg["auth"].setdefault("users", [])
    for u in users:
        if u.get("openid") == openid:
            patch = _norm_user({**body, "openid": openid}, cfg["auth"].get("clients", []))
            u.update(patch)
            _save_cfg(cfg)
            _audit(cfg, "admin.user.update", note=f"更新老板：{u['name']}")
            return {"ok": True, "user": u}
    raise HTTPException(status_code=404, detail="用户不存在")


@router.delete("/users/{openid}")
async def user_delete(openid: str, request: Request):
    _require(request)
    cfg = request.app.state.cfg
    users = cfg["auth"].get("users", [])
    new = [u for u in users if u.get("openid") != openid]
    if len(new) == len(users):
        raise HTTPException(status_code=404, detail="用户不存在")
    cfg["auth"]["users"] = new
    _save_cfg(cfg)
    _audit(cfg, "admin.user.delete", note=f"删除老板 openid：{openid}")
    return {"ok": True}


# ---------------- 应用账号（App/多端，账号密码登录） ----------------
@router.get("/accounts")
async def accounts_list(request: Request):
    _require(request)
    cfg = request.app.state.cfg
    return {"ok": True, "accounts": security.list_accounts(cfg)}


@router.post("/accounts")
async def account_upsert(request: Request):
    _require(request)
    cfg = request.app.state.cfg
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体不是 JSON")
    try:
        accounts = security.upsert_account(cfg, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(cfg, "admin.account.save", note=f"应用账号：{body.get('username')}")
    return {"ok": True, "accounts": accounts}


@router.delete("/accounts/{username}")
async def account_delete(username: str, request: Request):
    _require(request)
    cfg = request.app.state.cfg
    if not security.delete_account(cfg, username):
        raise HTTPException(status_code=404, detail="账号不存在")
    _audit(cfg, "admin.account.delete", note=f"删除应用账号：{username}")
    return {"ok": True}


@router.post("/accounts/{username}/password")
async def account_password(username: str, request: Request):
    _require(request)
    cfg = request.app.state.cfg
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体不是 JSON")
    try:
        security.set_password(cfg, username, body.get("password"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _audit(cfg, "admin.account.password", note=f"重置密码：{username}")
    return {"ok": True}
