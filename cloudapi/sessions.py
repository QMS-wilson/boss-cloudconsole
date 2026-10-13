# -*- coding: utf-8 -*-
"""会话表（2026-10-12）：让"谁在线"和"把谁踢下线"第一次成为可能。

★ 为什么需要它 —— 三个既有事实：
    1. access/refresh token 是 HMAC **无状态**的（security.py L133/L144）：
       签发之后**撤不掉**，没有黑名单、没有版本号；
    2. `/api/v1/auth/logout` 一直是**空壳**（auth_routes.py L460）：服务端什么都不做，
       登出只是让客户端自己丢掉 token —— 那条 token 依然能用满 12 小时（ACCESS_TTL）；
    3. 没有任何"谁在线"的记录。
   ⇒ 云端答不出"现在谁在用"，也踢不掉任何人。

★ 两张数据，各管一件事：
    · **会话表**（本模块）：jti → 谁 / 何时登录 / 最后活跃 / IP。用来「看」。
    · **账号版本号**（在 config 的 accounts 里，由 security.verify_token 校验）：
      踢某人**全部**会话 = 版本号 +1 ⇒ 他手上所有 token 立刻失效。
      一次写操作、O(1)，不用遍历会话表。
    ⇒ 所以：踢「这一条」用本表的 revoked 标记（精确）；
      踢「这个人的全部」用版本号（粗暴但快）。两者都要 —— 界面上也必须分开给，
      否则会出现"我只想踢他下线，结果把他账号禁了"。

★ 存哪：与 cloud_config.json **同目录**的 sessions.json。
  **刻意不写进 config.json** —— 那份是"平台登记"（商户/账号/客户端），
  会话是运行时状态；混在一起会让每次登录都重写整份配置，还有并发写风险。

★ 在线判定：last_seen 在 ONLINE_WINDOW 秒内。App 活跃时每个请求都会过
  security.verify_token，那里顺带 touch 一下，所以这个判据够用。

★ ⚠️ 与 admin.py 里的 `SESSIONS` 是**两回事**：那个是后台管理界面自己的登录态
  （cookie → 过期时间）；本模块管的是 App / Agent 侧的**访问令牌会话**。
"""

import json
import os
import re
import threading
import time

ONLINE_WINDOW = 300          # 5 分钟内有动静 = 在线
MAX_SESSIONS = 2000          # 上限，防内存无限涨
SAVE_THROTTLE = 5.0          # 最多 5 秒落一次盘（touch 每次请求都会调，必须节流）
UA_KEEP = 200                # UA 存多少字符
#
# ★ UA 截断长度从 120 提到 200（2026-10-12）：120 会在 `Chrome/…` 之后断掉，
#   而「是不是微信/小程序」（要看到 MicroMessenger）恰好在更后面 ——
#   截短了就等于把判定凭据扔了。存 200 已经足够覆盖常见 UA。

# ---------------- 设备识别（从 UA 认，只为了"谁在线"这行看得懂） ----------------
#
# ★ 定位：**不是指纹、不做精确识别**。认不出就给空串，绝不猜 ——
#   这一栏的用途是"老板问是哪台手机登的"，认错比认不出更糟。
# ★ 为什么从 UA 认而不是让客户端上报：App/H5/小程序三条链路都要改代码，
#   而 UA 是现成的、且对**存量登录**也立刻有效（不用等用户重新登录）。

_RE_ANDROID = re.compile(r"Android\s+([\d.]+)")
_RE_MODEL = re.compile(r";\s*([^;()]+?)\s+Build/([^;)]+)")
_RE_IOS = re.compile(r"(iPhone|iPad|iPod)[^)]*?OS\s+(\d+(?:_\d+)*)")
_RE_WIN = re.compile(r"Windows NT\s+([\d.]+)")
_RE_MAC = re.compile(r"Macintosh;.*?Mac OS X\s+([\d_.]+)")


def parse_ua(ua):
    """把 UA 解析成 {platform, brand, model, client}，认不出的留空串。

    client 是这四条链路里的一条：App / 小程序 / 微信 / H5 / 程序。
    ★ 判定依据（按优先级，都取自真实 UA）：
      · `miniProgram`             → 微信小程序
      · `MicroMessenger`          → 微信内置浏览器（H5 但被微信包着）
      · `Html5Plus` / `StreamApp` → uni-app 打包的 App
      · `; wv)`（Android WebView）且无微信标识 → 打包 App 的内嵌浏览器
      · 其余有内容的 UA            → 普通浏览器 H5
    """
    s = str(ua or "").strip()
    low = s.lower()
    out = {"platform": "", "brand": "", "model": "", "client": ""}
    if not s:
        return out

    # ---- 客户端类型 ----
    if "miniprogram" in low:
        out["client"] = "小程序"
    elif "micromessenger" in low:
        out["client"] = "微信"
    elif "html5plus" in low or "streamapp" in low:
        out["client"] = "App"
    elif "; wv)" in low:
        # Android WebView 且已被上面几条排除 ⇒ 是打包 App 的内嵌浏览器
        out["client"] = "App"
    elif "curl/" in low or "python" in low or "okhttp" in low or "wget" in low:
        out["client"] = "程序"
    else:
        out["client"] = "H5"

    # ---- 平台 / 型号 / 品牌 ----
    m = _RE_ANDROID.search(s)
    if m:
        out["platform"] = "Android " + m.group(1)
        mm = _RE_MODEL.search(s)
        if mm:
            model = mm.group(1).strip()
            build = mm.group(2).strip()
            out["model"] = model
            # `Build/<品牌><型号>` —— 品牌 = 这串去掉型号尾巴
            if model and build.lower().endswith(model.lower()):
                out["brand"] = build[: len(build) - len(model)].strip()
            elif build and build.lower() != model.lower():
                out["brand"] = build
        return out

    mi = _RE_IOS.search(s)
    if mi:
        out["platform"] = "iOS " + mi.group(2).replace("_", ".")
        out["brand"] = "Apple"
        out["model"] = mi.group(1)
        return out

    mw = _RE_WIN.search(s)
    if mw:
        out["platform"] = "Windows"
        return out
    if _RE_MAC.search(s):
        out["platform"] = "macOS"
    return out

_LOCK = threading.RLock()
_TABLE = {}                  # jti -> dict
_PATH = {"p": ""}
_STATE = {"dirty": False, "last_save": 0.0}


def configure(cfg):
    """定下会话文件的落盘位置（跟 cloud_config.json 放一起，运维好找）。"""
    if _PATH["p"]:
        return _PATH["p"]
    base = ""
    try:
        base = os.path.dirname(os.path.abspath(str(cfg.get("_path") or "")))
    except Exception:  # noqa: BLE001
        base = ""
    _PATH["p"] = os.path.join(base, "sessions.json") if base else "sessions.json"
    load()
    return _PATH["p"]


def path():
    return _PATH["p"]


def load():
    """从磁盘载入（过期的不载）。返回载入条数。"""
    p = _PATH["p"]
    if not p or not os.path.isfile(p):
        return 0
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except Exception:  # noqa: BLE001
        return 0
    items = data.get("sessions") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return 0
    now = time.time()
    with _LOCK:
        _TABLE.clear()
        for it in items:
            if not isinstance(it, dict):
                continue
            jti = str(it.get("jti") or "")
            if not jti or float(it.get("exp") or 0) < now:
                continue
            _TABLE[jti] = it
    return len(_TABLE)


def _save(force=False):
    with _LOCK:
        now = time.time()
        if not force and (not _STATE["dirty"]
                          or now - _STATE["last_save"] < SAVE_THROTTLE):
            return
        items = [dict(v) for v in _TABLE.values()]
        _STATE["dirty"] = False
        _STATE["last_save"] = now
    p = _PATH["p"]
    if not p:
        return
    try:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"saved_at": int(time.time()), "sessions": items},
                      fh, ensure_ascii=False, indent=2)
        os.replace(tmp, p)          # 原子替换：读不到"半个文件"
    except Exception:  # noqa: BLE001
        pass


def _evict_locked():
    """表满了先清场。调用方必须持锁。

    ★★ 次序是修过的（2026-10-12）：原先第一句就把 `revoked` 的行直接丢掉，
      而撤销判定读的正是"表里那条是不是 revoked"（`is_revoked`）⇒
      **被踢的那条记录自己先没了**，旧 access 再打一次反而通过校验（复活）。
      已撤销的行必须活到它自己的 exp：那条行唯一的用处就是"继续拒绝这个 jti"。
      ① 过期的（含"已撤销且已过期"——那种令牌本来就到点，留着没意义）先丢；
      ② 还不够就在**没被撤销**的行里挑最久没动静的；撤销行不进 LRU 候选。
    """
    now = time.time()
    for k in [k for k, v in _TABLE.items() if float(v.get("exp") or 0) < now]:
        _TABLE.pop(k, None)
    if len(_TABLE) < MAX_SESSIONS:
        return
    live = sorted(((k, v) for k, v in _TABLE.items() if not v.get("revoked")),
                  key=lambda kv: kv[1].get("last_seen") or 0)
    for k, _v in live[:len(_TABLE) - MAX_SESSIONS + 1]:
        _TABLE.pop(k, None)


def register(claims, ip="", ua="", database=""):
    """登记一条新会话。claims 直接来自刚签发的 access token 的 payload。

    ★ `database`（账套）是 2026-10-14 用户指出必须有的维度：
      **每本账有自己的操作员集合**（A 本有 101、B 本可能只有 202），
      所以"谁在线"必须能回答「**哪个账套**的哪个操作员、在哪台设备、哪一端」。
      不记账套的话，一家两本账时只能看到一个光秃秃的操作员编号。
    """
    jti = str((claims or {}).get("jti") or "")
    if not jti:
        return None
    now = time.time()
    ua_text = str(ua or "")[:UA_KEEP]
    rec = {
        "jti": jti,
        "sub": str(claims.get("sub") or ""),
        "name": str(claims.get("name") or ""),
        "kind": str(claims.get("kind") or ""),
        "mkt": str(claims.get("mkt") or ""),
        "mkts": list(claims.get("mkts") or []),
        # 这条会话读的是哪本账。空串 = 该商户没登记账套清单（单本商户的常态）
        "database": str(database or (claims or {}).get("db") or "").strip(),
        "login_at": now,
        "last_seen": now,
        "exp": float(claims.get("exp") or 0),
        "ip": str(ip or "")[:64],
        "ua": ua_text,
        "revoked": False,
        "revoked_at": 0.0,
        "revoked_by": "",
    }
    # 设备识别结果**摊成字段**，不让前端各自去解析 UA ——
    # 三处页面（云端后台 / 本地控制台 / App）共用同一份口径，才不会出现
    # "同一台手机在这页显示 HONOR、在那页显示 Android"。
    rec.update(parse_ua(ua_text))
    ua_key = str(rec.get("ua") or "")
    sub_key = str(rec.get("sub") or "")
    db_key = str(rec.get("database") or "")
    with _LOCK:
        # ★ 「同一身份 + 同一设备 + **同一账套**」只留一条（账套维度 2026-10-14 加）
        #
        #   为什么必须带账套：**每本账有自己的操作员集合**，同一个人可能在
        #   A 本和 B 本各登过 —— 那**是两条该并存的记录**。旧的那条不再被 touch
        #   ⇒ 自然显示"离线"，正好回答「他此刻在哪本账上」。
        #   不带账套的话后登的会把前一条顶掉，"他到底在哪本"就看不出来了。
        #
        #   仍要合并的情况（不合并就是脏数据）：
        #     ① 前端连点两次登录 / 超时重试 ⇒ 同一秒同一 IP 留两条；
        #     ② `/auth/refresh` 也走 issue_token_pair ⇒ 每次续期新增一条，
        #        而前端 onShow 会静默续期 ⇒ 会话数一路涨。
        #
        #   ★★ 但**已撤销的行不清**（2026-10-12）：清掉它 = 把"这个 jti 已被踢"的证据抹了，
        #   同一台设备重新登录一次，那条还没过期的旧 access 就又能过校验（顶号白做）。
        #   墓碑留到它自己的 exp 自然过期，由 _evict_locked 收。
        for k in [k for k, v in _TABLE.items()
                  if str(v.get("sub") or "") == sub_key
                  and str(v.get("ua") or "") == ua_key
                  and str(v.get("database") or "") == db_key
                  and not v.get("revoked")]:
            _TABLE.pop(k, None)
        if len(_TABLE) >= MAX_SESSIONS:
            _evict_locked()
        _TABLE[jti] = rec
        _STATE["dirty"] = True
    _save()
    return rec


def touch(jti):
    """刷新最后活跃时间（每个带 token 的请求都会走到这里）。"""
    if not jti:
        return
    with _LOCK:
        rec = _TABLE.get(str(jti))
        if rec is None:
            return
        rec["last_seen"] = time.time()
        _STATE["dirty"] = True
    _save()


def get(jti):
    with _LOCK:
        rec = _TABLE.get(str(jti))
        return dict(rec) if rec else None


def is_revoked(jti):
    if not jti:
        return False
    with _LOCK:
        rec = _TABLE.get(str(jti))
        return bool(rec and rec.get("revoked"))


def revoke(jti, by="admin"):
    """踢掉单条会话。返回是否真的改动了（幂等：重复踢返回 False）。"""
    with _LOCK:
        rec = _TABLE.get(str(jti))
        if rec is None or rec.get("revoked"):
            return False
        rec["revoked"] = True
        rec["revoked_at"] = time.time()
        rec["revoked_by"] = str(by or "")
        _STATE["dirty"] = True
    _save(force=True)
    return True


def drop_subject(subject):
    """清掉某人全部会话记录（版本号已经 +1 了，这里只是把表也扫干净）。

    返回清掉的条数。★ 真正让 token 失效的是**版本号**，不是这张表 ——
    这里只是让"在线列表"立刻不再显示他。
    """
    sub = str(subject or "")
    if not sub:
        return 0
    with _LOCK:
        dead = [k for k, v in _TABLE.items() if str(v.get("sub") or "") == sub]
        for k in dead:
            _TABLE.pop(k, None)
        if dead:
            _STATE["dirty"] = True
    _save(force=True)
    return len(dead)


def online(rec):
    return (not rec.get("revoked")
            and time.time() - float(rec.get("last_seen") or 0) <= ONLINE_WINDOW)


def list_sessions(merchant="", only_online=False, kinds=None):
    """列出会话（最近活跃的在前）。

    merchant 非空时只返回与该商户相关的；
    ★ kinds 非空时只返回这些 kind（2026-10-14）—— 本地控制台只要 V3 操作员，
      不能把**平台账号**的登录状态露到客户机上：平台账号是云端层的东西，
      客户机上的 Agent 只服务一家账套，看到别层的东西既没用又是泄露面。
    """
    now = time.time()
    with _LOCK:
        items = [dict(v) for v in _TABLE.values()]
    out = []
    want = set(kinds) if kinds else None
    for rec in items:
        if float(rec.get("exp") or 0) < now:
            continue
        if want is not None and str(rec.get("kind") or "") not in want:
            continue
        # ★ 存量会话（设备识别上线**之前**登记的）没有 brand/model 等字段 ——
        #   返回时现场补一次，不用等用户重新登录才看得到设备。
        if "brand" not in rec:
            rec.update(parse_ua(rec.get("ua")))
        if merchant:
            mkts = list(rec.get("mkts") or [])
            if merchant not in mkts and str(rec.get("mkt") or "") != merchant:
                continue
        on = online(rec)
        if only_online and not on:
            continue
        rec["online"] = on
        rec["idle_sec"] = int(now - float(rec.get("last_seen") or now))
        out.append(rec)
    out.sort(key=lambda r: r.get("last_seen") or 0, reverse=True)
    return out


def stats():
    with _LOCK:
        items = list(_TABLE.values())
    now = time.time()
    live = [r for r in items if float(r.get("exp") or 0) >= now and not r.get("revoked")]
    return {
        "total": len(items),
        "live": len(live),
        "online": sum(1 for r in live if online(r)),
        "window": ONLINE_WINDOW,      # 前端要显示"最近 N 分钟内有请求算在线"，别让它猜
        "path": _PATH["p"],
    }


def reset_for_test(path_=""):
    """测试用：清空内存表并（可选）重设路径。"""
    with _LOCK:
        _TABLE.clear()
        _STATE["dirty"] = False
        _STATE["last_save"] = 0.0
        _PATH["p"] = str(path_ or "")
