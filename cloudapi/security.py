# -*- coding: utf-8 -*-
"""多端统一安全层：账号密码、HMAC access/refresh token、店铺解析、旧令牌兼容。"""

import base64
import hashlib
import hmac
import json
import secrets
import time

from . import config as cfgmod

ACCESS_TTL = 12 * 3600
REFRESH_TTL = 30 * 24 * 3600


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def ok(data, **extra):
    out = {"ok": True, "data": data}
    out.update(extra)
    return out


def err(code, message, status=400):
    return status, {"ok": False, "error": {"code": code, "message": message}}


# ---------------- 账号密码 ----------------

def hash_password(password: str, salt: str = None, iterations: int = 120000):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return {"salt": salt, "password_hash": _b64e(dk), "iterations": iterations, "algo": "pbkdf2_sha256"}


def verify_password(password: str, rec: dict) -> bool:
    if not rec:
        return False
    try:
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            str(rec.get("salt")).encode("utf-8"), int(rec.get("iterations") or 120000))
        return hmac.compare_digest(_b64e(dk), str(rec.get("password_hash") or ""))
    except Exception:  # noqa: BLE001
        return False


def find_account(cfg, username):
    username = str(username or "").strip().lower()
    return next((a for a in cfg["auth"].get("accounts", [])
                 if str(a.get("username") or "").lower() == username), None)


def list_accounts(cfg):
    out = []
    for a in cfg["auth"].get("accounts", []):
        out.append({
            "username": a.get("username"), "name": a.get("name") or a.get("username"),
            "stores": a.get("stores") or [], "all_stores": bool(a.get("all_stores")),
            "disabled": bool(a.get("disabled")),
            "created_at": a.get("created_at"), "updated_at": a.get("updated_at"),
        })
    return out


def upsert_account(cfg, body):
    username = str(body.get("username") or "").strip().lower()
    if not username or len(username) < 3:
        raise ValueError("账号至少 3 位")
    name = str(body.get("name") or username).strip()
    stores = [str(x) for x in (body.get("stores") or []) if str(x).strip()]
    all_stores = bool(body.get("all_stores"))
    disabled = bool(body.get("disabled"))
    account = find_account(cfg, username)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if account is None:
        password = str(body.get("password") or "")
        if len(password) < 6:
            raise ValueError("密码至少 6 位")
        account = {"username": username, "created_at": now}
        account.update(hash_password(password))
        cfg["auth"].setdefault("accounts", []).append(account)
    elif body.get("password"):
        password = str(body["password"])
        if len(password) < 6:
            raise ValueError("密码至少 6 位")
        account.update(hash_password(password))
    account.update({"name": name, "stores": stores, "all_stores": all_stores,
                    "disabled": disabled, "updated_at": now})
    cfgmod.save(cfg)
    return list_accounts(cfg)


def delete_account(cfg, username):
    username = str(username or "").strip().lower()
    before = len(cfg["auth"].get("accounts", []))
    cfg["auth"]["accounts"] = [a for a in cfg["auth"].get("accounts", [])
                               if str(a.get("username") or "").lower() != username]
    cfgmod.save(cfg)
    return before != len(cfg["auth"]["accounts"])


def set_password(cfg, username, password):
    account = find_account(cfg, username)
    if not account:
        raise ValueError("账号不存在")
    if len(str(password or "")) < 6:
        raise ValueError("密码至少 6 位")
    account.update(hash_password(str(password)))
    account["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    cfgmod.save(cfg)


# ---------------- 令牌 ----------------

def ensure_token_secret(cfg):
    secret = str(cfg["auth"].get("token_secret") or "")
    if not secret:
        secret = secrets.token_urlsafe(32)
        cfg["auth"]["token_secret"] = secret
        cfgmod.save(cfg)
    return secret


def issue_token(cfg, claims, typ="access"):
    ttl_cfg = cfg["auth"].get("refresh_ttl" if typ == "refresh" else "access_ttl")
    ttl = int(ttl_cfg) if ttl_cfg else (REFRESH_TTL if typ == "refresh" else ACCESS_TTL)
    payload = dict(claims or {})
    payload.update({"typ": typ, "iat": int(time.time()), "exp": int(time.time()) + ttl,
                    "jti": secrets.token_hex(8)})
    body = _b64e(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    sig = _b64e(hmac.new(ensure_token_secret(cfg).encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    return body + "." + sig


def verify_token(cfg, token, typ=None):
    try:
        body, sig = str(token or "").split(".", 1)
        expect = _b64e(hmac.new(ensure_token_secret(cfg).encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(expect, sig):
            return None
        payload = json.loads(_b64d(body).decode("utf-8"))
        if int(payload.get("exp") or 0) < time.time():
            return None
        if typ and payload.get("typ") != typ:
            return None
        return payload
    except Exception:  # noqa: BLE001
        return None


def issue_token_pair(cfg, subject, name, stores, all_stores, kind="app", merchants=None,
                     extra=None):
    """签发 access/refresh 令牌。

    ★ Phase 2（2026-09-26）：令牌里带**商户归属**。
      可及商户唯一 → 只写 `mkt`（一家一个令牌，最紧）；
      平台/多商户账号 → 写 `mkts`（列表），请求仍必须用 X-Merchant 明确选一家。
      这样"能不能碰别家"由令牌决定，而不是由调用方随口报一个 store/agent 决定。
      没传 merchants 时按 stores/all_stores 换算，兼容既有调用点。

    ★ `extra`（2026-09-28，V3 操作员登录）：几个身份附加项原样进 claims。
      制单人不能再写死 `SYSTEM` —— 但把操作员身份塞进业务链路要改的地方比这多，
      所以第一版只把身份**签进令牌**，续期时照抄，等业务侧接它时不用再改契约。
    """
    codes = [str(x).strip() for x in
             (merchants if merchants is not None
              else merchants_for_names(cfg, stores, all_stores)) if str(x).strip()]
    claims = {"sub": subject, "name": name, "kind": kind,
              "stores": stores or [], "all": bool(all_stores)}
    for k, v in (extra or {}).items():
        claims[k] = v
    if len(codes) == 1:
        claims["mkt"] = codes[0]
    else:
        claims["mkts"] = codes
    return {
        "accessToken": issue_token(cfg, claims, "access"),
        "refreshToken": issue_token(cfg, claims, "refresh"),
        "expiresIn": int(cfg["auth"].get("access_ttl") or ACCESS_TTL),
        "tokenType": "Bearer",
    }


def actor_of(claims):
    """这条请求的**操作员代码** —— 要写进单据的制单人/审核人/作废人那几列。

    ★ 只认财鲸 V3 操作员登录签出来的令牌（`kind='v3'` 且带 `v3uid`）：那个代码是账套库
      `UserInfo.UserID` 里的真值，Agent 直接写库、打印时还能拿它换回姓名。
    ★ 平台应用账号 / 微信 openid 的令牌里没有 V3 身份 ⇒ 返回空串，由 Agent 回落它自己的
      兜底代码（`v3agent/actor.py` 的 `SYSDEV`）。云端不猜、也不替客户编一个人名。
    ★★ 绝不从请求体或查询参数里读"我是谁"：那样谁都能把单挂到别人名下。
      身份的唯一来源是**已经验过签的令牌**，而且它走的是中转帧的独立字段，不进业务 params。
    """
    c = claims or {}
    if str(c.get("kind") or "") != "v3":
        return ""
    return str(c.get("v3uid") or "").strip()[:40]


# ---------------- 店铺与鉴权 ----------------

def client_by_token(cfg, token):
    return next((c for c in cfg["auth"].get("clients", []) if c.get("token") == token), None)


def client_by_name(cfg, name):
    name = str(name or "").strip()
    for c in cfg["auth"].get("clients", []):
        if str(c.get("name") or "") == name or str(c.get("title") or "") == name:
            return c
    return None


def user_by_openid(cfg, openid):
    return next((u for u in cfg["auth"].get("users", []) if u.get("openid") == openid), None)


# ---------------- 商户归属（Phase 2） ----------------

def merchants_for_names(cfg, names, all_stores):
    """把"可及客户端"换算成"可及商户码"（约定：客户端的 agent 就是商户码）。"""
    if all_stores:
        return cfgmod.enabled_merchants(cfg)
    codes = set()
    for n in (names or []):
        c = client_by_name(cfg, n)
        if c and str(c.get("agent") or "").strip():
            codes.add(str(c["agent"]).strip())
    return sorted(codes)


def claims_merchants(cfg, claims):
    """令牌里可及的商户码。

    ★ 老令牌（没有 mkt/mkts）必须继续能用 —— 现网那批 access token 有效期 12 小时、
      refresh 30 天，云端一重启就把它们判死等于把所有客户踢下线。
      所以这里按老 claims 的 stores/all 换算，语义不变、只是补齐了新形状。
    """
    if not claims:
        return []
    mkt = str(claims.get("mkt") or "").strip()
    if mkt:
        return [mkt]
    if claims.get("mkts") is not None:
        return [str(x).strip() for x in (claims.get("mkts") or []) if str(x).strip()]
    return merchants_for_names(cfg, claims.get("stores") or [], bool(claims.get("all")))


def merchants_view(cfg, reg, names, all_stores):
    """给登录响应与 /me 用：这个身份能进哪几家商户、有没有在连、名下属哪些店铺。"""
    online = (reg.list_online() if reg is not None else None) or {}
    out = []
    for code in merchants_for_names(cfg, names, all_stores):
        m = cfgmod.merchant_of(cfg, code) or {}
        info = online.get(code) or {}
        # ★ 第一本在前端/后台就是"默认那本"（不带点名时读的那本），所以它必须是
        #   **客户机实际会在读的那本**。登记顺序由人填，填成 "B,A" 而客户机默认是 A 时，
        #   手机上会写着"当前账套 B"、不点名的请求却读 A —— 看着正常的读错账最坏。
        #   客户机在线并报了实测库 ⇒ 按它轮转；离线时没得更可靠的信息，保持登记顺序。
        dbs = cfgmod.merchant_databases(cfg, code)
        running = str(info.get("database_verified") or info.get("database") or "").strip().lower()
        if running and len(dbs) > 1 and dbs[0] != running and running in dbs:
            dbs = [running] + [d for d in dbs if d != running]
        out.append({
            "code": code, "title": m.get("title") or code,
            "enabled": bool(m.get("enabled", True)),
            "online": code in online,
            "stores": [{"name": c.get("name"), "title": c.get("title") or c.get("name")}
                       for c in cfgmod.clients_of_merchant(cfg, code)],
            # ★ Step 3 起把"这家有哪几本账"随登录/`/me` 一起给出（Step 4 的选择器就吃这个）。
            #   同时给出客户机实际服务过哪几本：前端要按"读过的那本"分缓存，不能猜。
            #   老前端不认识这些键，多带字段不影响它。
            "databases": dbs,
            "database_default": (dbs[0] if dbs else "") or code,
            "database_verified": str(info.get("database_verified") or ""),
            "served_databases": list(info.get("served_databases") or []),
        })
    return out


def _pick_client(cfg, code, want_store):
    """商户内挑一个客户端作为权限载体 —— 店铺从"路由依据"降级成"展示标签"。"""
    picks = cfgmod.clients_of_merchant(cfg, code)
    if not picks:
        return None
    if want_store:
        hit = next((c for c in picks
                    if str(c.get("name") or "") == want_store
                    or str(c.get("title") or "") == want_store), None)
        if hit:
            return hit
    return picks[0]


def stores_for(cfg, reg, names, all_stores):
    picks = cfg["auth"].get("clients", []) if all_stores else [
        c for c in cfg["auth"].get("clients", []) if c.get("name") in (names or [])]
    online = reg.list_online()
    out = []
    for c in picks:
        agent = c.get("agent") or ""
        on_at, off_at = reg.last_events_for(agent)
        out.append({
            "name": c.get("name"), "title": c.get("title") or c.get("name"),
            "agent": agent, "access": c.get("access", "guest"),
            "online": agent in online,
            # 最后一次上线 / 最后一次离线的时刻（unix **秒**；0 = 云端没这段历史）。
            # ★ 为什么要交给客户端：手机上的应用是采样的（90 秒一次），客户机只在线
            #   一两分钟就可能整段被跳过 —— 那样它自己永远不知道"曾经在线过"，
            #   那次掉线就报不出来（2026-10-05 现场就是这个形状）。云端一直在看着，
            #   把时刻给它就补上了。走 HTTP 轮询那一路没有成对事件，这里会是 0，
            #   客户端遇到 0 会退回自己采到的边沿，不会把陈年离线当成刚掉的。
            "last_online_at": on_at, "last_offline_at": off_at,
        })
    return out


def bearer(request):
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def _header(request, name, body=None, body_key=None):
    v = (request.headers.get(name) or "").strip()
    if not v and body is not None and body_key:
        v = str((body or {}).get(body_key) or "").strip()
    return v


def resolve_merchant_hint(cfg, request, body=None):
    """**没有令牌**时定商户：只能由前缀带来的 `X-Merchant`（或 body.merchant）决定。

    返回 (商户码, 错误)。错误为 (status, payload) 或 None。

    ★ 为什么单独一条而不复用 resolve_rpc_context：那条的第一句是"没有 Authorization 就 401"，
      而登录本身就发生在有令牌之前 —— 复用它会写成"先要有令牌才能拿令牌"。
    ★ 这里刻意**不做任何兜底**（不猜"就一家所以肯定是它"、不读 default_agent）：
      登录一旦能猜商户，一个口令就能顶开别家的账。宁可回一句"请用自家链接进入"。
    """
    code = _header(request, "x-merchant", body, "merchant")
    if not code:
        return "", err("MERCHANT_REQUIRED",
                       "缺少商户码：请从自家那条链接进入（形如 http://…/yedian/），"
                       "或在登录里填商户码", 400)
    m = cfgmod.merchant_of(cfg, code)
    if not m:
        return "", err("MERCHANT_NOT_FOUND", "未知商户码：%s" % code, 404)
    if not m.get("enabled", True):
        return "", err("MERCHANT_DISABLED", "商户「%s」已停用，请联系平台" % code, 403)
    return code, None


def resolve_rpc_context(cfg, request, body=None):
    """统一解析请求上下文。

    返回 (client, label, claims, error)；error 为 (status, payload) 或 None。
    - 多端 accessToken：可及商户由令牌的 `mkt`/`mkts` 限定，请求用 `X-Merchant` 在范围内选一家
    - 旧模式：店铺令牌 + X-Openid，商户就是该客户端绑定的 agent
    - 目标商户**不接受调用方自行点名**（body.agent 由 target_agent 忽略并留痕）

    ★ 两条刻意保留的向后兼容：
      1) 授权范围内只有一家时，不带 X-Merchant 也放行 —— 现网那份 H5/小程序还不会发这个头，
         不能因为它们没升级就把客户锁在门外；第二家放出来之前前端必须已经带上（闸门见接续文档）。
      2) X-Store 不再具备跨商户能力，只在"选中的商户内部"当展示标签用（老客户端还在发）。
    """
    token = bearer(request)
    if not token:
        return None, "", None, err("UNAUTHORIZED", "缺少 Authorization", 401)
    claims = verify_token(cfg, token, "access")
    if claims:
        allowed = [c for c in claims_merchants(cfg, claims) if cfgmod.merchant_of(cfg, c)]
        code = _header(request, "x-merchant", body, "merchant")
        if code:
            if not cfgmod.merchant_of(cfg, code):
                return None, "", claims, err("MERCHANT_NOT_FOUND", "未知商户码：%s" % code, 404)
            if code not in allowed:
                return None, "", claims, err(
                    "TENANT_MISMATCH",
                    "该登录身份不属于商户「%s」，请退出后从对应商户的入口重新进入" % code, 403)
        elif len(allowed) == 1:
            code = allowed[0]
        elif not allowed:
            return None, "", claims, err("NO_MERCHANT", "该账号没有可访问的商户，请联系平台开通", 403)
        else:
            return None, "", claims, err(
                "MERCHANT_REQUIRED", "该账号可访问多家商户，请在请求头带 X-Merchant 指定一家", 400)
        m = cfgmod.merchant_of(cfg, code) or {}
        if not m.get("enabled", True):
            return None, "", claims, err("MERCHANT_DISABLED",
                                        "商户「%s」已停用，请联系平台" % code, 403)
        client = _pick_client(cfg, code, _header(request, "x-store", body, "store"))
        if not client:
            return None, "", claims, err("MERCHANT_UNBOUND",
                                        "商户「%s」下没有可用客户端，请联系平台" % code, 403)
        return client, "app:" + str(claims.get("sub") or ""), claims, None

    client = client_by_token(cfg, token)
    if not client:
        return None, "", None, err("UNAUTHORIZED", "无效令牌", 401)
    code = str(client.get("agent") or "").strip()
    m = cfgmod.merchant_of(cfg, code)
    if m is not None and not m.get("enabled", True):
        return None, "", None, err("MERCHANT_DISABLED",
                                  "商户「%s」已停用，请联系平台" % code, 403)
    openid = (request.headers.get("x-openid") or "").strip()
    access = client.get("access", "guest")
    if access == "auth":
        user = user_by_openid(cfg, openid) if openid else None
        if not openid:
            return None, "", None, err("OPENID_REQUIRED", "该客户端需要微信登录", 401)
        if user is None:
            return None, "", None, err("USER_NOT_FOUND", "该微信未开通访问权限", 403)
        if not (user.get("all_clients") or client["name"] in user.get("clients", [])):
            return None, "", None, err("NO_ACCESS", "该微信无权访问此账套", 403)
    elif cfg["auth"].get("require_openid") and not openid:
        return None, "", None, err("OPENID_REQUIRED", "请先登录", 401)
    return client, "legacy", None, None


def resolve_database(cfg, request, code, body=None):
    """Step 3：在"已经定了哪家"之后，定"这一条读那家的哪本账"。

    返回 (账套名, 错误)。**账套名为空 = 不点名**，走该商户的默认库（清单第一本），
    与 Step 3 之前的行为逐字节一致 —— 现在线上的 H5/小程序都还不发这个头，
    所以"不点名"是常态，不是遗漏。

    ★ 为什么单独一步而不塞进 resolve_rpc_context：
      那一段回答的是"你是谁、能进哪家"（身份与租户），这一段是"进哪本账"（数据源）。
      两件事的错误码、可及性来源、演进速度都不同；混在一起的话，
      将来加第三种维度就得再改一次所有调用点的元组长度。

    ★ 判据只有一条：点名了就必须在这家登记的清单里（`config.database_state` 同一个来源）。
      越界一律 403 挡在云端，**不转给客户端机去拒** ——
      转下去的话，日志里只有一台客户机在抱怨，看不出是谁的请求，跨家排查会走偏。
    """
    name = ""
    if request is not None:
        name = (request.headers.get("x-database") or "").strip()
    if not name and isinstance(body, dict):
        name = str(body.get("database") or "").strip()
    if not name:
        return "", None
    allowed = cfgmod.allowed_databases(cfg, code)
    # ★ 比对不分大小写，命中后**一律换成登记里的那个写法**再往下转。
    #   两个理由：① SQL Server 默认排序规则下库名本来就不分大小写，判死会把同一本账
    #   说成越界；② 已经发出去的 H5/小程序/APK 都把库名 toLowerCase 过一轮（旧包收不回来），
    #   云端判死 ⇒ 客户一点第二本账就 403，而那正是我们要排除的假故障。
    #   安全性质没变：能匹配的仍然只有这家自己登记过的那几本，跨家照样挡在云端。
    hit = None
    low = name.lower()
    for d in allowed:
        if str(d).strip().lower() == low:
            hit = str(d).strip()
            break
    if hit is None:
        # ★ 被拒时**照样把试着点名的库返回出去**（只给审计用，调用方见 error 就不会转发）。
        #   留空的话，事后看审计只有一句"越界"，答不出"谁想读哪本"——
        #   而这正是排查跨家试探与误登记时唯一有用的那个信息。
        return name, err("DATABASE_NOT_ALLOWED",
                       "商户「%s」未登记账套「%s」。该商户已登记：%s。"
                       "如需查看其他账套，请从对应入口进入，或先联系平台登记。"
                       % (code, name, "、".join(allowed) or "(无登记清单)"), 403)
    return hit, None


# ---------------- 统一审计条目 ----------------

# ★ 口令/密钥这类值**按键名剔掉**再去算摘要。别小看"只留 12 位摘要"：
#   sha256(整个 params) 是个**离线撞库的靶子** —— 拿到审计文件的人对着某个操作员
#   试常见口令，一算就知道中没中。auth.v3check 走信封式 /api/v1/rpc 时就是这个形状。
SENSITIVE_KEYS = ("password", "passwd", "secret", "token", "apikey", "api_key")


def _strip_sensitive(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if any(p in str(k).lower() for p in SENSITIVE_KEYS):
                out[k] = "***"          # 留住"这个字段出现过"，值不给
            else:
                out[k] = _strip_sensitive(v)
        return out
    if isinstance(value, list):
        return [_strip_sensitive(v) for v in value]
    return value


def params_digest(params) -> str:
    """只留摘要不留原值：参数里全是客户名与单据金额，审计文件不该变成第二份业务库。"""
    try:
        raw = json.dumps(_strip_sensitive(params or {}), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raw = repr(params).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:12]


def audit_entry(request=None, client=None, claims=None, agent="", op="", ok=True,
                code=None, note="", params=None, database="", served_database=""):
    """补齐 mkt/sub/jti/ip/rid/params_digest 的审计条目。

    ★ 以前四个调用点各自手拼 dict，字段集三套并存（成功条目只有 6 个键），
      事后想回答"这条是谁、从哪个 IP、以什么身份干的"就没有依据。
    """
    entry = {
        "ts": time.time(),
        "client": (client or {}).get("name") or "",
        "agent": agent or "",
        "mkt": agent or "",          # 约定：商户码 == agent 名
        "op": op,
        "ok": bool(ok),
        "code": code,
    }
    if claims:
        entry["sub"] = str(claims.get("sub") or "")
        entry["jti"] = str(claims.get("jti") or "")
        entry["kind"] = str(claims.get("kind") or "")
    elif client:
        entry["sub"] = "legacy"
    ip = getattr(request, "client", None) if request is not None else None
    entry["ip"] = getattr(ip, "host", "") or ""
    if request is not None:
        entry["rid"] = str(request.headers.get("x-request-id") or "")
    if params is not None:
        entry["params_digest"] = params_digest(params)
    if note:
        entry["note"] = note
    # ★ Step 3：一条请求"要读哪本"与"实际读了哪本"都要留痕。
    #   没有这两个字段，热切/路由之后审计就只能看到 mkt=某家，
    #   而"那家有两本账"恰好是这批改造要支持的事实 —— 事后查账会查不定是谁的数字。
    if database:
        entry["database"] = str(database)
    if served_database and served_database != database:
        entry["database_served"] = str(served_database)
    return entry


# ---------------- 租户裁决：目标 Agent 只能由服务端决定 ----------------

def target_agent(cfg, client, body=None):
    """返回 (agent_name, error, ignored)。error 为 (status, payload) 或 None。

    ★★ 平台化第 1 阶段（2026-09-25）把"选哪家"这件事从调用方手里收回来。
      改造前三条取值链都是 `body.agent or client.agent or default_agent`：
        · 调用方自带 `agent` 就能点名读写**别家**的库，无 allowlist、不看令牌归属
          （server.py 旧 :268、tasks.py 旧 :60）；
        · 兜底的 `default_agent` 指向某一家具体商户，平台层于是存在"猜不到的那一家"，
          配置写错时流量会静默进别人家。
      现在：只认客户端记录自己的 `agent`，且必须已在 `auth.agents` 登记；
      请求体里再出现 `agent` 一律忽略并回第三个值供调用方记日志（用来看还有谁在发）。
      归属为真的前提是"建 client 时校验过 agent 已登记"（admin.py），
      所以那道校验是这一步的配套，不能省。
    """
    agents = (cfg.get("auth") or {}).get("agents") or {}
    supplied = str(((body or {}).get("agent") or "")).strip()
    agent = str((client or {}).get("agent") or "").strip()
    if not agent:
        return None, err("TENANT_UNBOUND",
                         "该客户端没有绑定商户 Agent，请联系平台在后台补齐", 403), supplied or None
    if agent not in agents:
        return None, err("AGENT_NOT_REGISTERED",
                         "客户端绑定的 Agent「%s」未在平台登记（后台里名字写错了？）" % agent,
                         403), supplied or None
    return agent, None, (supplied if supplied and supplied != agent else None)

