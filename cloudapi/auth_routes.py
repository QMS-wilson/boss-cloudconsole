# -*- coding: utf-8 -*-
"""多端统一认证与能力发现：/api/v1/auth/*、/api/v1/capabilities。"""

import asyncio
import json
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .registry import AgentBadOp, AgentBusy, AgentNoDbRouting, AgentOffline, busy_message
from . import audit as auditmod
from . import config as cfgmod
from . import limit as limiter
from . import security
from . import business, tasks as tasks_mod

router = APIRouter()

def _rest_endpoints():
    """从真实路由对象导出接口清单。

    ★ 原来这里是一份手写的字符串清单，已经和实际接口严重脱节
      （采购/供应商/审核/作废/复制/多张报表/打印预览全都没登记），
      而 `/api/v1/capabilities` 是把它当"能力自述"发给前端的 —— 过期清单会让
      前端以为接口不存在。改成从 router 反射，从此不可能再漂移。
    """
    out = set()
    for router in (business.router, tasks_mod.router):
        for route in router.routes:
            for m in sorted(getattr(route, "methods", set()) or ()):
                if m == "HEAD":
                    continue
                out.add("%s %s" % (m, route.path))
    return sorted(out)


REST_ENDPOINTS = _rest_endpoints()


def _wechat_code2session(appid, secret, code):
    try:
        url = "https://api.weixin.qq.com/sns/jscode2session?" + urlencode({
            "appid": appid, "secret": secret, "js_code": code,
            "grant_type": "authorization_code",
        })
        with urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"errcode": -1, "errmsg": "请求微信接口失败：" + str(exc)}


async def _body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return {}


# ---------------- provider=v3：口令校验发生在客户机上 ----------------

V3_OP = "auth.v3check"
#: 一次"查一个人 + MD5 比对"在本地 SQL Server 上是毫秒级，但要排进那台机器的在途闸门，
#: 所以比业务快活给一点余量；再长就宁可让人重刷，不要挂着协程。
V3_TIMEOUT = 25


def _v3_fail(status, code, message, **extra):
    e = {"code": code, "message": message}
    e.update(extra)
    return JSONResponse(status_code=status, content={"ok": False, "error": e})


def _v3_audit(request, cfg, code, login, vcode, ok_flag, status, note="", db=""):
    """登录留痕：**只记谁在试、结果是哪条规则**，一个字的口令都不落。

    ★ 为什么限流之外还要这条：被拒的次数云端 `limiter` 里有累计，但"谁在磨哪个操作员"
      只有审计能回答。运维看到某家连续 NO_PASSWORD/BAD_CREDENTIAL，才知道该去 V3 里
      设口令、还是该怀疑有人在试口令。
    ★ `db` 走 audit_entry 的 database 字段（与业务请求同一列），这样"登录进了哪本账"
      和后面那条业务读的是哪本，能在同一份审计里对上。
    """
    entry = security.audit_entry(request, {"name": code, "agent": code}, None,
                                 agent=code, op=V3_OP, ok=ok_flag, code=vcode,
                                 note=("operator=%s%s" % (login, (" " + note) if note else "")),
                                 database=db)
    entry["via"] = "rest-auth"
    entry["path"] = request.url.path
    entry["http"] = status
    auditmod.write(cfg, entry)


def _v3_throttle(cfg, code, login, ip):
    """两道桶，缺一不可：

    - 「商户+操作员」：对着**一个编号**磨口令的节拍（默认突发 5 次，此后 20 秒一次）；
    - 「IP」：换着编号枚举的节拍（默认 30 次突发，此后 4 秒一次）——
      只加第一道的话，把 101/102/103 轮着试一遍是完全免费的。
    """
    rl = cfg.get("v3_login") or {}
    rate = float(rl.get("rate") or (1 / 20.0))
    burst = float(rl.get("burst") or 5)
    ok1, wait1 = limiter.allow(("login:%s:%s" % (code, login.lower()), V3_OP), rate, burst)
    if not ok1:
        return False, wait1, "重试太快了，请 %d 秒后再试（防口令被反复试）" % (int(wait1) + 1)
    ip_rate = float(rl.get("ip_rate") or (1 / 4.0))
    ip_burst = float(rl.get("ip_burst") or 30)
    ok2, wait2 = limiter.allow(("login-ip:%s" % (ip or "?"), V3_OP), ip_rate, ip_burst)
    if not ok2:
        return False, wait2, "这台网络出口试的次数太多了，请 %d 秒后再试" % (int(wait2) + 1)
    return True, 0.0, ""


async def _login_v3(request, cfg, reg, body):
    """V3 操作员登录：云端**不存也不比对**口令，原样转给该商户的本地 Agent 校验。

    ★ 为什么必须转给客户机而不是云端查库：`UserInfo` 在账套库里，而那本库只在客户机房。
      云端要校验就得把库（或哈希）搬上网 —— 等于给平台新增一个泄露面。
    ★ 三条纪律的落点：无令牌可达（登录本来就是拿令牌的入口）、_v3_throttle 限流、
      _v3_audit 不落口令。
    """
    code, merr = security.resolve_merchant_hint(cfg, request, body)
    if merr:
        return JSONResponse(status_code=merr[0], content=merr[1])
    login = str(body.get("user") or body.get("operator") or body.get("login") or "").strip()
    password = str(body.get("password") or "")
    if not login or not password:
        return _v3_fail(400, "BAD_REQUEST", "要填 V3 的操作员编号（或姓名）与口令")
    ip = getattr(getattr(request, "client", None), "host", "") or ""
    passed, wait, why = _v3_throttle(cfg, code, login, ip)
    if not passed:
        _v3_audit(request, cfg, code, login, "RATE_LIMITED", False, 429)
        return JSONResponse(status_code=429, headers={"Retry-After": str(int(wait) + 1)},
                            content={"ok": False, "error": {"code": "RATE_LIMITED",
                                                            "message": why}})
    db_name, derr = security.resolve_database(cfg, request, code, body)
    if derr:
        return JSONResponse(status_code=derr[0], content=derr[1])
    # ★ 一本账套一次校验；一家两本账时**先试默认那本**（不点名 = 清单第一本）。
    #   第二版的操作员身份是"账套内的操作员"，`v3:<商户>:<编号>` 这个令牌形状还撑不起
    #   "同一人在 A 本能开单、在 B 本只能看库存"这种分裂 —— 所以现在明确：登录读的就是
    #   默认那本，响应里回 `database`，前端要切另一本得重新登录（切了就是两个人）。
    ts = time.time()
    try:
        resp = await reg.rpc(code, V3_OP, {"user": login, "password": password},
                             V3_TIMEOUT, database=db_name)
    except AgentOffline:
        _v3_audit(request, cfg, code, login, "AGENT_OFFLINE", False, 502, db=db_name)
        return _v3_fail(502, "AGENT_OFFLINE",
                        "本地服务不在线：V3 的口令要在这台机器上校验，请先开机并启动财鲸的本地服务")
    except AgentBadOp:
        _v3_audit(request, cfg, code, login, "OP_NOT_PUBLISHED", False, 409, db=db_name)
        return _v3_fail(409, "V3_LOGIN_UNAVAILABLE",
                        "这台机器上的本地服务还不支持 V3 操作员登录，请换新版后再试",
                        hint="把 dist/V3AgentGui.exe 换成新包并在控制台里重启本地服务")
    except AgentNoDbRouting:
        _v3_audit(request, cfg, code, login, "AGENT_NO_DB_ROUTING", False, 409, db=db_name)
        return _v3_fail(409, "V3_LOGIN_UNAVAILABLE",
                        "该客户机上的本地服务暂不支持指定账套，请更新版本；"
                        "或先不选择账套，使用默认账套登录")
    except AgentBusy as exc:
        _v3_audit(request, cfg, code, login, "AGENT_BUSY", False, 429, db=db_name)
        return JSONResponse(status_code=429, headers={"Retry-After": str(exc.retry_after)},
                            content={"ok": False, "error": {"code": "AGENT_BUSY",
                                                            "message": busy_message(exc)}})
    except (asyncio.TimeoutError, TimeoutError):
        _v3_audit(request, cfg, code, login, "AGENT_TIMEOUT", False, 504, db=db_name)
        return _v3_fail(504, "AGENT_TIMEOUT",
                        "本地服务 %d 秒没应答：确认 V3 与数据库开着，再试一次" % V3_TIMEOUT,
                        timeout=V3_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        _v3_audit(request, cfg, code, login, "INTERNAL", False, 502, db=db_name)
        return _v3_fail(502, "INTERNAL", "本地服务回来了一个没见过的应答：" + str(exc)[:120])

    data = (resp or {}).get("data") or {}
    if (resp or {}).get("ok") is False or not data.get("code"):
        # Agent 那边是"查人失败"（连不上库/列缺失），不是"口令不对" —— 两者话术必须分开，
        # 否则客户会对着一个没开的数据库反复改口令。
        err_ = (resp or {}).get("error") or {}
        _v3_audit(request, cfg, code, login, err_.get("code") or "V3_CHECK_FAILED", False, 502,
                  db=db_name)
        return _v3_fail(502, err_.get("code") or "V3_CHECK_FAILED",
                        err_.get("message") or "本地服务没能完成校验")
    vcode = str(data.get("code") or "")
    if not data.get("ok"):
        # 话术直接用 Agent 给的那句（"去 V3 设口令"/"已被停用"/"请用编号"），云端不改写
        status = {"USER_STOPPED": 403, "NO_PASSWORD": 403, "AMBIGUOUS_NAME": 409}.get(vcode, 401)
        _v3_audit(request, cfg, code, login, vcode, False, status, db=db_name)
        return _v3_fail(status, vcode,
                        str(data.get("message") or "操作员编号或口令不对，请核对后重试"))

    user = data.get("user") or {}
    uid = str(user.get("id") or login)
    uname = str(user.get("name") or uid)
    names = [str(c.get("name")) for c in cfgmod.clients_of_merchant(cfg, code) if c.get("name")]
    tokens = security.issue_token_pair(cfg, "v3:%s:%s" % (code, uid), uname,
                                        names, False, "v3", merchants=[code],
                                        extra={"v3uid": uid, "v3name": uname})
    stores = security.stores_for(cfg, reg, names, False)
    merchants = security.merchants_view(cfg, reg, names, False)
    _v3_audit(request, cfg, code, uid, "OK", True, 200,
              note="ms=%d" % int((time.time() - ts) * 1000))
    return {"ok": True, "data": {
        **tokens,
        "provider": "v3",
        "user": {"username": uid, "name": uname, "kind": "v3"},
        "operator": {"id": uid, "name": uname},
        "perms": data.get("perms") or {},
        "authorized": bool(stores), "stores": stores, "merchants": merchants,
        "merchant": code,
        "database": str(resp.get("database") or db_name or ""),
    }}


#: 清单接口打的是客户机，比登录更要节制
V3_LIST_OP = "auth.v3users"
V3_LIST_TIMEOUT = 12


def _opts_throttle(cfg, code, ip):
    """`/auth/options` 的桶：一个 IP 狂刷就能把全平台客户的员工名单抄一份，
    所以**按商户**和**按 IP**各来一道（数值比登录宽松些，因为一次进登录页就要打两次）。
    """
    rl = cfg.get("v3_login") or {}
    ok1, w1 = limiter.allow(("options:%s" % code, V3_LIST_OP),
                            float(rl.get("list_rate") or (1 / 3.0)), float(rl.get("list_burst") or 10))
    if not ok1:
        return False, w1, "取名单太快了，请 %d 秒后再试" % (int(w1) + 1)
    ok2, w2 = limiter.allow(("options-ip:%s" % (ip or "?"), V3_LIST_OP),
                            float(rl.get("list_ip_rate") or (1 / 2.0)),
                            float(rl.get("list_ip_burst") or 40))
    if not ok2:
        return False, w2, "这个网络出口取名单太频繁，请 %d 秒后再试" % (int(w2) + 1)
    return True, 0.0, ""


@router.post("/api/v1/auth/options")
async def options(request: Request):
    """登录页进门前要的两份清单：这家有哪几本账、那本账里有哪些操作员。**不鉴权**。

    ★ 为什么要它：登录页把"输编号"改成了"选操作员"，而账套清单与操作员名单都只存在于
      **客户那台机器的账套库**里 —— 不登录就没法拿。要么退回手输（老板记不住 101），
      要么开一条免登录通道，产品选了后者（2026-09-29 定），那就把代价说清楚：
      **任何人拿到这条商户链接都能看到这家有几本账、有哪些人**。口令不在这一份里，
      哈希一个字节都不出客户机（`auth.v3users` 的 SELECT 里连 UserPassword 都没写）。
    ★ 因此这条必须：双层限流、审计不落参数、客户机不在时**降级**成 null 让界面回落手输
      —— 报 502 会把登录页整个卡死，而手输本来就该能用。
    """
    cfg = request.app.state.cfg
    reg = request.app.state.registry
    body = await _body(request)
    code, merr = security.resolve_merchant_hint(cfg, request, body)
    if merr:
        return JSONResponse(status_code=merr[0], content=merr[1])
    ip = getattr(getattr(request, "client", None), "host", "") or ""
    passed, wait, why = _opts_throttle(cfg, code, ip)
    if not passed:
        entry = security.audit_entry(request, {"name": code, "agent": code}, None, agent=code,
                                     op="auth.options", ok=False, code="RATE_LIMITED")
        entry["via"] = "rest-auth"
        entry["http"] = 429
        auditmod.write(cfg, entry)
        return JSONResponse(status_code=429, headers={"Retry-After": str(int(wait) + 1)},
                            content={"ok": False, "error": {"code": "RATE_LIMITED", "message": why}})
    db_name, derr = security.resolve_database(cfg, request, code, body)
    if derr:
        return JSONResponse(status_code=derr[0], content=derr[1])

    users, uerr = None, None
    try:
        resp = await reg.rpc(code, V3_LIST_OP, {}, V3_LIST_TIMEOUT, database=db_name)
        if (resp or {}).get("ok"):
            users = ((resp.get("data") or {}).get("users")) or []
            uerr = None if users else {"code": "NO_OPERATORS",
                                      "message": "该账套未读取到操作员，请核对账套，或先在 V3 中添加操作员"}
        else:
            uerr = (resp or {}).get("error") or {"code": "V3_LIST_FAILED", "message": "没读到操作员名单"}
    except AgentOffline:
        uerr = {"code": "AGENT_OFFLINE",
                "message": "本地服务不在线，先按编号手输（口令对一样能进）"}
    except (AgentBadOp, AgentNoDbRouting):
        uerr = {"code": "V3_LIST_UNAVAILABLE",
                "message": "这台机器上的本地服务还不支持列操作员，先按编号手输；换新版后即可选",
                "hint": "把 dist/V3AgentGui.exe 换成新包并在控制台里重启本地服务"}
    except AgentBusy as exc:
        uerr = {"code": "AGENT_BUSY", "message": busy_message(exc), "retry_after": exc.retry_after}
    except (asyncio.TimeoutError, TimeoutError):
        uerr = {"code": "AGENT_TIMEOUT", "message": "本地服务 %d 秒没应答，先按编号手输"
                % V3_LIST_TIMEOUT}
    except Exception as exc:  # noqa: BLE001
        uerr = {"code": "INTERNAL", "message": "读名单时本地服务回了一句没见过的话：%s" % str(exc)[:120]}

    names = [str(c.get("name")) for c in cfgmod.clients_of_merchant(cfg, code) if c.get("name")]
    ms = security.merchants_view(cfg, reg, names, False)
    entry = security.audit_entry(request, {"name": code, "agent": code}, None, agent=code,
                                 op="auth.options", ok=not uerr,
                                 code=(uerr or {}).get("code") or "OK",
                                 note="operators=%d" % (len(users) if users else 0),
                                 database=db_name)
    entry["via"] = "rest-auth"
    entry["http"] = 200
    auditmod.write(cfg, entry)
    return {"ok": True, "data": {
        "merchant": (ms[0] if ms else {"code": code, "title": code}),
        "database": db_name or ((ms[0].get("database_default") if ms else "") or ""),
        "operators": users,
        "operators_error": uerr,
    }}


@router.post("/api/v1/auth/login")
async def login(request: Request):
    cfg = request.app.state.cfg
    reg = request.app.state.registry
    body = await _body(request)
    provider = str(body.get("provider") or "").strip().lower()
    if not provider:
        if body.get("code"):
            provider = "wechat"
        elif body.get("openid"):
            provider = "openid"
        else:
            provider = "password"

    if provider == "v3":
        return await _login_v3(request, cfg, reg, body)

    if provider == "password":
        account = security.find_account(cfg, body.get("account") or body.get("username"))
        if not account or account.get("disabled"):
            return JSONResponse(status_code=401, content={
                "ok": False, "error": {"code": "ACCOUNT_INVALID", "message": "账号或密码错误"}})
        if not security.verify_password(str(body.get("password") or ""), account):
            return JSONResponse(status_code=401, content={
                "ok": False, "error": {"code": "ACCOUNT_INVALID", "message": "账号或密码错误"}})
        stores = security.stores_for(cfg, reg, account.get("stores"), account.get("all_stores"))
        merchants = security.merchants_view(cfg, reg, account.get("stores"),
                                            account.get("all_stores"))
        tokens = security.issue_token_pair(
            cfg, "account:" + account["username"], account.get("name") or account["username"],
            [c["name"] for c in stores] if not account.get("all_stores") else [], account.get("all_stores"), "app",
            merchants=[m["code"] for m in merchants])
        return {"ok": True, "data": {
            **tokens,
            "user": {"username": account["username"], "name": account.get("name") or account["username"], "kind": "app"},
            "authorized": bool(stores), "stores": stores, "merchants": merchants,
            # ★ 前端拿这个跟 URL 前缀（X-Merchant 的来源）比对：不匹配就是"用错了入口"，
            #   不能让第二家的链接顶着第一家的会话打开（2026-09-26 实测过的那种串号）。
            "merchant": (merchants[0]["code"] if len(merchants) == 1 else ""),
        }}

    if provider in ("wechat", "openid"):
        if provider == "openid":
            if cfg["auth"].get("allow_openid_login", True) is False:
                return JSONResponse(status_code=403, content={
                    "ok": False, "error": {"code": "OPENID_LOGIN_DISABLED", "message": "当前环境不允许 openid 直接登录"}})
            openid = str(body.get("openid") or "").strip()
            if not openid:
                return JSONResponse(status_code=400, content={
                    "ok": False, "error": {"code": "BAD_REQUEST", "message": "缺少 openid"}})
        else:
            wc = cfg.get("wechat", {})
            missing = []
            if not wc.get("enabled"):
                missing.append("未启用微信登录")
            if not wc.get("appid"):
                missing.append("缺 AppID")
            if not wc.get("secret"):
                missing.append("缺 AppSecret")
            if missing:
                return JSONResponse(status_code=503, content={
                    "ok": False, "error": {"code": "WECHAT_NOT_CONFIGURED",
                                           "message": "云端微信登录配置不完整（" + "、".join(missing) + "）"}})
            code = str(body.get("code") or "").strip()
            if not code:
                return JSONResponse(status_code=400, content={
                    "ok": False, "error": {"code": "BAD_REQUEST", "message": "缺少 wx.login 的 code"}})
            data = _wechat_code2session(str(wc.get("appid")), str(wc.get("secret")), code)
            if data.get("errcode"):
                return JSONResponse(status_code=400, content={
                    "ok": False, "error": {"code": "WX_LOGIN_FAILED",
                                           "message": "微信登录失败（%s）：%s" % (data.get("errcode"), data.get("errmsg", ""))}})
            openid = str(data.get("openid") or "").strip()
            if not openid:
                return JSONResponse(status_code=502, content={
                    "ok": False, "error": {"code": "WX_LOGIN_FAILED", "message": "微信未返回 openid"}})
        user = security.user_by_openid(cfg, openid)
        names = (user or {}).get("clients") or []
        all_stores = bool((user or {}).get("all_clients"))
        stores = security.stores_for(cfg, reg, names, all_stores) if user else []
        merchants = security.merchants_view(cfg, reg, names, all_stores) if user else []
        tokens = security.issue_token_pair(
            cfg, "wx:" + openid, (user or {}).get("name") or "", names, all_stores, "wechat",
            merchants=[m["code"] for m in merchants])
        return {"ok": True, "data": {
            **tokens,
            "user": {"openid": openid, "name": (user or {}).get("name") or "", "kind": "wechat"},
            "authorized": bool(stores), "stores": stores, "merchants": merchants,
            "merchant": (merchants[0]["code"] if len(merchants) == 1 else ""),
        }}

    return JSONResponse(status_code=400, content={
        "ok": False, "error": {"code": "BAD_PROVIDER", "message": "不支持的登录方式：" + provider}})


@router.post("/api/v1/auth/refresh")
async def refresh(request: Request):
    cfg = request.app.state.cfg
    reg = request.app.state.registry
    body = await _body(request)
    token = str(body.get("refreshToken") or security.bearer(request))
    claims = security.verify_token(cfg, token, "refresh")
    if not claims:
        return JSONResponse(status_code=401, content={
            "ok": False, "error": {"code": "TOKEN_INVALID", "message": "refreshToken 无效或已过期"}})
    # ★ 续期时按**当前配置**重算归属，而不是把旧 claims 照抄一份。
    #   旧实现照抄 stores/all，结果是"后台已经把这家解绑了，客户端还能靠 30 天的
    #   refreshToken 一直续下去"。账号类令牌尤其要重算；openid 类用户表里查不到就退回原 claims。
    sub = str(claims.get("sub") or "")
    names, all_stores = claims.get("stores") or [], bool(claims.get("all"))
    if sub.startswith("account:"):
        account = security.find_account(cfg, sub.split(":", 1)[1])
        if account and not account.get("disabled"):
            names = account.get("stores") or []
            all_stores = bool(account.get("all_stores"))
        elif account is None:
            return JSONResponse(status_code=401, content={
                "ok": False, "error": {"code": "ACCOUNT_GONE", "message": "该账号已被删除，请重新登录"}})
        else:
            return JSONResponse(status_code=401, content={
                "ok": False, "error": {"code": "ACCOUNT_DISABLED", "message": "该账号已停用"}})
    extra = {}
    if sub.startswith("v3:"):
        # `v3:<商户码>:<操作员编号>` —— 按**现在这家登记了哪些客户端**重算，不照抄旧 stores。
        code = sub.split(":", 2)[1] if len(sub.split(":", 2)) > 2 else ""
        if not cfgmod.merchant_of(cfg, code):
            return JSONResponse(status_code=401, content={
                "ok": False, "error": {"code": "MERCHANT_GONE",
                                       "message": "商户「%s」已不在平台上，请联系平台" % code}})
        names = [str(c.get("name")) for c in cfgmod.clients_of_merchant(cfg, code) if c.get("name")]
        all_stores = False
        # 操作员身份跟着令牌走（制单人以后读这里，不再写死 SYSTEM）。
        # ★ 这里**不重查** V3：那需要口令。所以在 V3 里被停用/改名的操作员，最长还会带着
        #   这份身份用到 access token 过期（默认 12 小时）。要立刻生效得走后台强制下线，
        #   或以后加一条"按编号查在用状态"的 op —— 现在不假装有这个能力。
        extra = {k: claims[k] for k in ("v3uid", "v3name") if claims.get(k)}
    stores = security.stores_for(cfg, reg, names, all_stores)
    merchants = security.merchants_view(cfg, reg, names, all_stores)
    tokens = security.issue_token_pair(cfg, claims.get("sub"), claims.get("name"),
                                       names if not all_stores else [], all_stores,
                                       claims.get("kind") or "app",
                                       merchants=[m["code"] for m in merchants], extra=extra)
    return {"ok": True, "data": {**tokens, "authorized": bool(stores), "stores": stores,
                                "merchants": merchants,
                                "merchant": (merchants[0]["code"] if len(merchants) == 1 else "")}}


@router.post("/api/v1/auth/logout")
async def logout(request: Request):
    return {"ok": True, "data": {"message": "已退出（客户端请丢弃本地令牌）"}}


@router.get("/api/v1/auth/me")
async def me(request: Request):
    cfg = request.app.state.cfg
    reg = request.app.state.registry
    claims = security.verify_token(cfg, security.bearer(request), "access")
    if not claims:
        return JSONResponse(status_code=401, content={
            "ok": False, "error": {"code": "TOKEN_INVALID", "message": "accessToken 无效或已过期"}})
    stores = security.stores_for(cfg, reg, claims.get("stores"), claims.get("all"))
    merchants = security.merchants_view(cfg, reg, claims.get("stores"), claims.get("all"))
    return {"ok": True, "data": {
        "user": {"subject": claims.get("sub"), "name": claims.get("name"), "kind": claims.get("kind")},
        "authorized": bool(stores), "stores": stores, "merchants": merchants,
        "merchant": str(claims.get("mkt") or ""),
        # ★ 云端这一头的"现在"（unix 秒）。手机上那条掉线通知要算"离线距今多久"，
        #   而减法两头必须来自同一只表：两个时刻是云端给的，"现在"就得也用云端的。
        #   客户机没对准时（自动对时关掉很常见）本机时钟能把通知整段吃掉且不报错。
        "server_now": round(time.time(), 3),
    }}


@router.get("/api/v1/capabilities")
async def capabilities(request: Request):
    cfg = request.app.state.cfg
    reg = request.app.state.registry
    client, label, claims, error = security.resolve_rpc_context(cfg, request)
    if error:
        return JSONResponse(status_code=error[0], content=error[1])
    agent, aerr, _ignored = security.target_agent(cfg, client)
    if aerr:
        return JSONResponse(status_code=aerr[0], content=aerr[1])
    info = reg.list_online().get(agent) or {}
    data = {
        "client": {"label": label, "kind": (claims or {}).get("kind") or "legacy"},
        "agent": {"name": agent, "online": bool(info),
                  "version": info.get("version"), "ops": sorted(info.get("ops") or [])},
        "rest": REST_ENDPOINTS,
    }
    try:
        resp = await reg.rpc(agent, "agent.capabilities", {}, 15)
        if resp.get("ok"):
            data["capabilities"] = resp.get("data")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "data": data}
