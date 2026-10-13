# -*- coding: utf-8 -*-
"""FastAPI 应用：HTTP RPC（给小程序）+ WebSocket（接本地 Agent）。"""

import asyncio
import json
import logging
import os
import re
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from .admin import router as admin_router
from .auth_routes import router as auth_router
from .business import router as business_router
from . import audit as auditmod
from . import security
from . import sessions
from . import __version__
from .tasks import router as tasks_router
from .registry import (AgentBadOp, AgentBadSecret, AgentBusy, AgentNameConflict,
                        busy_message,
                       AgentNoDbRouting, AgentOffline, AgentRegistry)
from . import limit as limiter

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

log = logging.getLogger("cloud.server")


def _audit(cfg, entry):
    """落盘见 cloudapi/audit.py（按日切分；写失败不阻断业务）。"""
    auditmod.write(cfg, entry)


def _client_by_token(cfg, token):
    for c in cfg["auth"].get("clients", []):
        if c.get("token") == token:
            return c
    return None


def _bearer_token(request):
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def _user_by_openid(cfg, openid):
    return next((u for u in cfg["auth"].get("users", []) if u.get("openid") == openid), None)


def _client_auth_error(cfg, client, openid):
    """校验 openid 是否允许访问该客户端；通过返回 None，否则 (http_status, payload)。"""
    access = client.get("access", "guest")
    if access == "auth":
        user = _user_by_openid(cfg, openid) if openid else None
        if not openid:
            return (401, {"ok": False, "error": {"code": "OPENID_REQUIRED",
                                                 "message": "该客户端需要微信登录"}})
        if user is None:
            return (403, {"ok": False, "error": {"code": "USER_NOT_FOUND",
                                                 "message": "该微信未开通访问权限，请联系管理员绑定"}})
        if not (user.get("all_clients") or client["name"] in user.get("clients", [])):
            return (403, {"ok": False, "error": {"code": "NO_ACCESS",
                                                 "message": "该微信无权访问此账套"}})
    elif cfg["auth"].get("require_openid") and not openid:
        return (401, {"ok": False, "error": {"code": "OPENID_REQUIRED",
                                             "message": "请先微信登录"}})
    return None


RECENT_LOGINS_MAX = 500


def _record_login(app, client_label, openid):
    if not openid:
        return
    bucket = app.state.recent_logins
    bucket[openid] = {"client": client_label, "openid": openid, "ts": time.time()}
    # ★ 这个表的键是**客户端自报的 X-Openid**，不限长度就会被人用一堆随机 openid
    #   把云端内存喂胖（后台那个「最近登录」面板只需要最近的几条）。
    if len(bucket) > RECENT_LOGINS_MAX:
        for k in sorted(bucket, key=lambda x: bucket[x].get("ts") or 0)[:len(bucket) - RECENT_LOGINS_MAX]:
            bucket.pop(k, None)


def _client_online(cfg, reg, client):
    return (client.get("agent") or "") in reg.list_online()


def _wechat_code2session(appid, secret, code):
    try:
        url = "https://api.weixin.qq.com/sns/jscode2session?" + urlencode({
            "appid": appid, "secret": secret, "js_code": code,
            "grant_type": "authorization_code",
        })
        with urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 微信接口异常统一回结构化错误
        return {"errcode": -1, "errmsg": "请求微信接口失败：" + str(exc)}


def create_app(cfg):
    app = FastAPI(title="V3 云端 API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    reg = AgentRegistry(cfg)
    app.state.cfg = cfg
    app.state.registry = reg
    app.state.recent_logins = {}
    # ★ 会话表落盘位置（与 cloud_config.json 同目录的 sessions.json）。
    #   幂等；main() 里已经调过一次，这里再调是为了直接 create_app 的场合
    #   （测试、嵌入运行）也能落到同一个地方。
    sessions.configure(cfg)

    # ---- 后台页面：新版是多文件（原生 ES module），旧版整页保留做回退 ----
    ADMIN_DIR = os.path.join(BASE_DIR, "web", "admin")
    ADMIN_SUB = re.compile(r"^[\w.\-/]+$")

    def _admin_file(rel):
        """把 /admin/<rel> 解析成 web/admin 下的真实文件；越界一律 None。

        ★ 为什么卡得这么死：这本质上是一个静态文件服务器，而它旁边就是
          `cloud_config.json`（全平台密钥）。任何一次"顺手把用户输入拼进路径"
          都会变成任意文件读取，所以：白名单字符 + 规范路径 + 必须落在目录内 + 只放三种后缀。
        """
        rel = (rel or "").lstrip("/")
        if not rel or not ADMIN_SUB.match(rel):
            return None
        norm = os.path.normpath(rel).replace("\\", "/")
        if norm.startswith("..") or ".." in norm.split("/"):
            return None
        if not norm.endswith((".js", ".css", ".html")):
            return None
        p = os.path.realpath(os.path.join(ADMIN_DIR, norm))
        root = os.path.realpath(ADMIN_DIR)
        if not p.startswith(root + os.sep):
            return None
        return p if os.path.isfile(p) else None

    def _legacy_admin():
        for name in ("admin_legacy.html", "admin.html"):
            p = os.path.join(BASE_DIR, "web", name)
            if os.path.isfile(p):
                return p
        return None

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    async def admin_page():
        return FileResponse(os.path.join(ADMIN_DIR, "index.html"))

    @app.get("/admin/legacy", include_in_schema=False)
    async def admin_legacy_page():
        p = _legacy_admin()
        if not p:
            return JSONResponse({"ok": False, "error": {"code": "GONE", "message": "旧版页面已移除"}},
                                status_code=404)
        return FileResponse(p)

    @app.get("/admin/{rel:path}", include_in_schema=False)
    async def admin_asset(rel: str):
        p = _admin_file(rel)
        if not p:
            return JSONResponse({"ok": False, "error": {"code": "NOT_FOUND", "message": "没有这个文件"}},
                                status_code=404)
        return FileResponse(p)

    app.include_router(admin_router, prefix="/api/admin")
    app.include_router(auth_router)
    app.include_router(business_router)
    app.include_router(tasks_router)

    @app.get("/api/v1/health")
    async def health():
        """存活探针。**不再列商户名/版本/op 清单**（2026-09-25）：
        以前它无需任何凭据就能枚举出"平台上有哪几家商户、各跑什么版本、支持哪些操作"，
        等于把租户名单和攻击面清单公开挂在公网上。运营视角的明细在 /api/admin/agents。
        """
        return {"ok": True, "version": __version__, "agents": len(reg.list_online())}


    @app.get("/api/v1/app-release")
    async def app_release():
        """App 端热更清单（wgt）。文件由 uniapp 侧 `scripts/publish-wgt.py` 上传，
        云端**只转发不解释**：版本号、下载地址、是否强制都写在那份 JSON 里。

        ★ 为什么走云端而不是让 App 直连静态站：这个端已经知道云端地址
          （`utils/config.js: apiBase()`，含商户码前缀），再写一个域名/端口进代码
          就等于多一处"只有改代码才能改"的事实。
        ★ 文件不在 = 这台服务器还没发布过任何 wgt ⇒ 404 + 明确 code，
          App 端把 404 当"没这回事"（静默跳过），不许报"检查更新失败"吓人。
        """
        path = os.environ.get("V3_APP_RELEASE_FILE", "/var/www/jxc-h5/release/app-version.json")
        if not os.path.isfile(path):
            return JSONResponse(status_code=404, content={
                "ok": False,
                "error": {"code": "NO_RELEASE", "message": "还没有发布过应用更新包"}})
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            return JSONResponse(status_code=500, content={
                "ok": False,
                "error": {"code": "BAD_RELEASE",
                          "message": "更新清单读不出来：" + str(e)[:80]}})
        return {"ok": True, "data": data}

    @app.get("/api/v1/agents")
    async def agents_removed():
        return JSONResponse(status_code=410, content={
            "ok": False, "error": {"code": "GONE",
                                   "message": "该接口已下线（会泄露全部商户清单），请改用 /api/admin/agents"}})

    @app.post("/api/v1/wechat/login")
    async def wechat_login(request: Request):
        """小程序 wx.login 的 code → openid（静默，无授权弹窗）。"""
        token = _bearer_token(request)
        client = _client_by_token(cfg, token)
        portal = token and token == cfg["auth"].get("portal_token")
        if client is None and not portal:
            return JSONResponse({"ok": False, "error": {"code": "UNAUTHORIZED",
                                                        "message": "无效令牌"}}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": {"code": "BAD_REQUEST",
                                                        "message": "请求体不是 JSON"}}, status_code=400)
        code = str(body.get("code") or "").strip()
        if not code:
            return JSONResponse({"ok": False, "error": {"code": "BAD_REQUEST",
                                                        "message": "缺少 wx.login 的 code"}}, status_code=400)
        wc = cfg.get("wechat", {})
        appid, secret = str(wc.get("appid") or ""), str(wc.get("secret") or "")
        missing = []
        if not wc.get("enabled"):
            missing.append("未启用微信登录")
        if not appid:
            missing.append("缺 AppID")
        if not secret:
            missing.append("缺 AppSecret")
        if missing:
            return JSONResponse({"ok": False, "error": {"code": "WECHAT_NOT_CONFIGURED",
                                                        "message": "云端微信登录配置不完整（" + "、".join(missing) + "），请在后台「系统设置」补充"}},
                                status_code=503)
        data = await asyncio.to_thread(_wechat_code2session, appid, secret, code)
        errcode = data.get("errcode")
        if errcode:
            return JSONResponse(
                {"ok": False, "error": {"code": "WX_LOGIN_FAILED",
                                        "message": "微信登录失败（%s）：%s" % (errcode, data.get("errmsg", ""))}},
                status_code=400,
            )
        openid = str(data.get("openid") or "").strip()
        if not openid:
            return JSONResponse({"ok": False, "error": {"code": "WX_LOGIN_FAILED",
                                                        "message": "微信未返回 openid"}}, status_code=502)
        _record_login(app, "入口登录", openid)
        _audit(cfg, {"ts": time.time(), "client": "入口登录", "agent": "",
                     "op": "wechat.login", "ok": True, "code": None})
        return {"ok": True, "openid": openid}

    @app.get("/api/v1/me")
    async def me(request: Request):
        """按 openid 查询该老板可访问的店铺列表（0 家=未绑定）。"""
        token = _bearer_token(request)
        client = _client_by_token(cfg, token)
        portal = token and token == cfg["auth"].get("portal_token")
        if client is None and not portal:
            return JSONResponse({"ok": False, "error": {"code": "UNAUTHORIZED",
                                                        "message": "无效令牌"}}, status_code=401)
        openid = (request.headers.get("x-openid") or "").strip()
        if not openid:
            return JSONResponse({"ok": False, "error": {"code": "OPENID_REQUIRED",
                                                        "message": "缺少 X-Openid，请先微信登录"}}, status_code=401)
        label = "入口" if portal else (client.get("name") or "客户端")
        _record_login(app, label, openid)
        user = _user_by_openid(cfg, openid)
        stores = []
        if user:
            clients = cfg["auth"].get("clients", [])
            picks = clients if user.get("all_clients") else [
                c for c in clients if c.get("name") in user.get("clients", [])]
            for c in picks:
                stores.append({
                    "name": c.get("name"),
                    "title": c.get("title") or c.get("name"),
                    "agent": c.get("agent", ""),
                    "access": c.get("access", "guest"),
                    # ★ 这里以前回吐 `"token": c["token"]` —— 那是一把**长期有效的静态客户端令牌**，
                    #   而这个接口只要拿着 portal_token + 任意 X-Openid 就能调，
                    #   等于把全部商户的入口凭据发给任何能猜到 portal_token 的人（Phase 2 收口）。
                    #   需要令牌请走 /api/v1/auth/login 换 access/refresh（带过期与归属）。
                    "online": _client_online(cfg, reg, c),
                })
        merchants = security.merchants_view(cfg, reg,
                                            user.get("clients") if user else [],
                                            bool(user.get("all_clients")) if user else False)
        _audit(cfg, security.audit_entry(request, client, None, op="me", ok=True,
                                        note="openid=%s 店铺数=%d" % (security.oid_ref(openid), len(stores))))
        return {"ok": True, "openid": openid,
                "name": user.get("name") if user else "",
                "authorized": bool(user and stores),
                "stores": stores, "merchants": merchants}

    @app.post("/agent/poll")
    async def agent_poll(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
        name = str(body.get("agent") or "").strip()
        if reg.cfg["auth"].get("agents", {}).get(name) != str(body.get("secret") or ""):
            return JSONResponse({"ok": False, "error": "bad secret"}, status_code=403)
        requests = reg.http_poll(
            name,
            version=str(body.get("version") or ""),
            ops=body.get("ops"),
            install_id=str(body.get("install_id") or ""),
            hostname=str(body.get("hostname") or ""),
            database=str(body.get("database") or ""),
            database_verified=str(body.get("database_verified") or ""),
            op_docs=body.get("op_docs") if isinstance(body.get("op_docs"), dict) else None,
            # Step 2/3：HTTP 通道也要上报这两份清单，否则走 http 的客户机会被
            # 当成"不支持选账套的老包"（两条通道能力不一致 = 最难查的那类分叉）
            allowed_databases=body.get("allowed_databases")
            if isinstance(body.get("allowed_databases"), list) else None,
            served_databases=body.get("served_databases")
            if isinstance(body.get("served_databases"), list) else None,
        )
        return {"ok": True, "requests": requests}

    @app.post("/agent/result")
    async def agent_result(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
        name = str(body.get("agent") or "").strip()
        if reg.cfg["auth"].get("agents", {}).get(name) != str(body.get("secret") or ""):
            return JSONResponse({"ok": False, "error": "bad secret"}, status_code=403)
        reg.http_result(name, body.get("response") or {})
        return {"ok": True}

    # ---- 会话（本地控制台用）2026-10-12 ----
    #
    # ★ 鉴权与 /agent/poll 完全一致：body 里 agent + secret 比对 cfg.auth.agents。
    #   本地控制台用它**本来就有的** agent.secret —— 不为这个功能新发一套密钥
    #   （少一套密钥 = 少一处会过期、会配错、会在换机器时忘记改的东西）。
    # ★ 不做商户过滤：这台主机就是老板的，本机控制台看到全部在线会话是预期行为
    #   （用户明确说过"只有老板能接触主机，没有安全顾虑"）。要收窄时带上 merchant。

    def _agent_ok(body):
        name = str(body.get("agent") or "").strip()
        want = reg.cfg["auth"].get("agents", {}).get(name)
        if not name or want != str(body.get("secret") or ""):
            return ""
        return name

    @app.post("/agent/sessions")
    async def agent_sessions(request: Request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
        if not _agent_ok(body):
            return JSONResponse({"ok": False, "error": "bad secret"}, status_code=403)
        kk = str(body.get("kind") or "").strip()
        rows = sessions.list_sessions(merchant=str(body.get("merchant") or "").strip(),
                                      only_online=bool(body.get("online")),
                                      kinds=[kk] if kk else None)
        # ★ stats 要按**过滤后**的结果重算：直接回 sessions.stats() 是全表口径，
        #   本地控制台明明只发了 kind=v3，统计里却带着平台账号的条数 —— 对不上账。
        st = sessions.stats()
        st["total"] = len(rows)
        st["live"] = sum(1 for r in rows if not r.get("revoked"))
        st["online"] = sum(1 for r in rows if r.get("online"))
        return {"ok": True, "sessions": rows, "stats": st}

    @app.post("/agent/sessions/revoke")
    async def agent_sessions_revoke(request: Request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
        name = _agent_ok(body)
        if not name:
            return JSONResponse({"ok": False, "error": "bad secret"}, status_code=403)
        jti = str(body.get("jti") or "").strip()
        subject = str(body.get("subject") or "").strip()
        username = str(body.get("username") or "").strip().lower()
        if jti:
            if not sessions.revoke(jti, by="agent:" + name):
                return JSONResponse({"ok": False, "error": "会话不存在，或已被踢过"},
                                    status_code=404)
            return {"ok": True, "revoked": 1}
        # ★ subject 优先（`account:xxx` / `v3:<商户>:<编号>`）：V3 操作员不在平台账号表里，
        #   拿 username 拼出来的 `account:<编号>` 是查不到的 —— 那样"踢出全部"对
        #   V3 操作员会 404，而用户看到的只是"踢不动"（2026-10-14 修）。
        if not subject and username:
            subject = "account:" + username
        if subject:
            kd, kcode, kuid = security.subject_kind(subject)
            try:
                if kd == "account":
                    tv = security.bump_token_version(reg.cfg, kuid)
                elif kd == "v3":
                    tv = security.bump_v3_token_version(reg.cfg, kcode, kuid)
                else:
                    return JSONResponse({"ok": False, "error": "认不出的主体：%s" % subject},
                                        status_code=400)
            except ValueError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
            return {"ok": True, "revoked": sessions.drop_subject(subject),
                    "token_version": tv}
        return JSONResponse({"ok": False, "error": "要么给 jti，要么给 subject/username"},
                            status_code=400)

    @app.post("/agent/accounts/disable")
    async def agent_account_disable(request: Request):
        """本地控制台禁用/启用账号 —— 用户要的"云端＋本地双重控制"里的本地那一半。"""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
        if not _agent_ok(body):
            return JSONResponse({"ok": False, "error": "bad secret"}, status_code=403)
        username = str(body.get("username") or "").strip().lower()
        disabled = bool(body.get("disabled", True))
        try:
            info = security.set_account_disabled(reg.cfg, username, disabled)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        dropped = sessions.drop_subject("account:" + username) if disabled else 0
        return {"ok": True, **info, "dropped": dropped}

    @app.post("/api/v1/rpc")
    async def rpc(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": {"code": "BAD_REQUEST", "message": "请求体不是 JSON"}}, status_code=400)
        client, label, claims, auth_err = security.resolve_rpc_context(cfg, request, body)
        if auth_err:
            # ★ 归属类拒绝（TENANT_MISMATCH / MERCHANT_NOT_FOUND / MERCHANT_DISABLED…）
            #   必须留痕：这正是"有人拿错入口/拿别家令牌试"的信号，只回给调用方不够。
            code = ((auth_err[1] or {}).get("error") or {}).get("code") or "AUTH_ERROR"
            _audit(cfg, security.audit_entry(request, client, claims, op=str(
                (body or {}).get("op") or ""), ok=False, code=code,
                note="鉴权上下文未通过 label=%s" % (label or "")))
            return JSONResponse(status_code=auth_err[0], content=auth_err[1])
        openid = (request.headers.get("x-openid") or "").strip()
        if client.get("name"):
            _record_login(app, label or client["name"], openid or str((claims or {}).get("sub") or ""))
        op = str(body.get("op") or "").strip()
        if not op:
            return JSONResponse({"ok": False, "error": {"code": "BAD_REQUEST", "message": "缺少 op"}}, status_code=400)
        agent_name, terr, ignored = security.target_agent(cfg, client, body)
        if ignored:
            # 除了进程日志，也在审计里留一条：越权尝试必须事后可查
            # （等这条计数归零，就可以把 body.agent 参数彻底删掉了）
            log.warning("忽略调用方自带的 agent=%s（真实租户=%s）op=%s client=%s",
                        ignored, agent_name, op, client.get("name"))
            _audit(cfg, security.audit_entry(request, client, claims, agent=agent_name or "",
                                             op=op, ok=False, code="BODY_AGENT_IGNORED",
                                             note="调用方试图点名 agent=%s" % ignored))
        if terr:
            _audit(cfg, security.audit_entry(request, client, claims, agent=ignored or "",
                                             op=op, ok=False,
                                             code=terr[1]["error"]["code"]))
            return JSONResponse(status_code=terr[0], content=terr[1])
        # ★ Step 3：定了哪家再定哪本账（X-Database 或 body.database，都不给就是默认库）
        db_name, derr = security.resolve_database(cfg, request, agent_name, body)
        if derr:
            _audit(cfg, security.audit_entry(request, client, claims, agent=agent_name, op=op,
                                             ok=False, code=derr[1]["error"]["code"],
                                             params=body.get("params"),
                                             note="账套越界，未转发"))
            return JSONResponse(status_code=derr[0], content=derr[1])
        timeout = min(max(int(body.get("timeout") or cfg.get("rpc_timeout", 60)), 5), 300)
        rl = cfg.get("rate_limit") or {}
        allowed_call, wait = limiter.allow(limiter.key_of(client, op),
                                            rl.get("rate"), rl.get("burst"))
        if not allowed_call:
            return JSONResponse(status_code=429, headers={"Retry-After": str(int(wait) + 1)},
                                content={"ok": False, "error": {
                                    "code": "RATE_LIMITED",
                                    "message": "请求太密集了，请 %d 秒后再试" % (int(wait) + 1)}})
        ts = time.time()
        try:
            resp = await reg.rpc(agent_name, op, body.get("params") or {}, timeout,
                                 database=db_name, actor=security.actor_of(claims))
            ok = bool(resp.get("ok"))
            status = 200 if ok else 502 if resp.get("error", {}).get("code") == "INTERNAL" else 400
            resp["agent"] = agent_name
            entry = security.audit_entry(request, client, claims, agent=agent_name, op=op,
                                         ok=ok, code=resp.get("error", {}).get("code"),
                                         params=body.get("params"), database=db_name,
                                         served_database=str(resp.get("database") or ""))
            entry["ts"] = ts                      # 用发起时刻，不是回包时刻
            entry["ms"] = int((time.time() - ts) * 1000)
            _audit(cfg, entry)
            return JSONResponse(resp, status_code=status)
        except AgentNoDbRouting:
            # ★ 点名了账套但那台客户机是不认识这功能的旧包 ⇒ 明确失败。
            #   绝不"降级成读默认库"：那会返回一份来自另一套账、看着完全正常的数字。
            entry = security.audit_entry(request, client, claims, agent=agent_name, op=op,
                                         ok=False, code="AGENT_NO_DB_ROUTING",
                                         params=body.get("params"), database=db_name,
                                         note="旧版本 Agent 不支持按请求选账套")
            entry["ts"] = ts
            _audit(cfg, entry)
            return JSONResponse(status_code=502, content={"ok": False, "error": {
                "code": "AGENT_NO_DB_ROUTING",
                "message": "该商户的本地服务还是旧版本，暂不支持按请求切换账套。"
                           "请联系平台更新本地 Agent 后再试"}})
        except AgentOffline:
            return JSONResponse(
                {"ok": False, "error": {"code": "AGENT_OFFLINE", "message": f"客户本地 Agent 不在线：{agent_name}"}},
                status_code=503,
            )
        except AgentBadOp as exc:
            return JSONResponse(
                {"ok": False, "error": {"code": "OP_NOT_PUBLISHED", "message": str(exc)}},
                status_code=403,
            )
        except AgentBusy as exc:
            return JSONResponse(
                {"ok": False, "error": {"code": "AGENT_BUSY",
                                        "message": busy_message(exc)}},
                status_code=429, headers={"Retry-After": str(exc.retry_after)},
            )
        except TimeoutError:
            return JSONResponse(
                {"ok": False, "error": {"code": "TIMEOUT", "message": f"Agent 响应超时：{agent_name}"}},
                status_code=504,
            )

    @app.websocket("/ws/agent")
    async def ws_agent(ws: WebSocket):
        await ws.accept()
        name = None
        try:
            while True:
                text = await ws.receive_text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "hello":
                    if name is not None:
                        continue
                    try:
                        await reg.register(ws, data)
                    except AgentBadSecret:
                        await ws.close(code=4001, reason="bad secret")
                        return
                    except AgentNameConflict as exc:
                        # ★ 4002 = 名字已被另一台安装占用，**在线那条不动**。
                        #   改造前这里是无条件顶号：拿同一份密钥在第二台机器装一次，
                        #   第一家就无限掉线（客户表现为"接口时好时坏"，实测 3 分钟重连 34 次）。
                        #   要合法换机，去 /admin 点「接管」再重连。
                        _audit(cfg, {"ts": time.time(), "client": "agent", "agent": exc.name,
                                     "op": "agent.connect", "ok": False,
                                     "code": "NAME_CONFLICT",
                                     "note": "另一台安装试图占用 %s（旧指纹 %s）"
                                             % (exc.name, exc.holder)})
                        await ws.close(code=4002, reason="name held by another install")
                        return
                    name = str(data.get("agent") or "")
                    continue
                if name is None:
                    continue
                await reg.handle_agent_message(name, ws, data)
        except WebSocketDisconnect:
            pass
        finally:
            if name:
                reg.unregister(name, ws)

    return app
