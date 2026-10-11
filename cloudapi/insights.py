# -*- coding: utf-8 -*-
"""后台「以商户为中心」的聚合**读**接口（B1/B2/B4/B6/B7/B8 + 监控与时间线）。

为什么单独一个模块：`admin.py` 已经 740 行 / 30 个路由，再把聚合逻辑塞进去，
它就会变成第二个"什么都往一个文件里堆"的地方。本模块**只读**：
不碰配置、不写盘、不改状态 —— 出错最多是看板数字不对，不会把线上改坏。
写操作仍然全部留在 admin.py，这样"哪些接口会改变世界"只有一处可查。
"""

import json
import os
import re
import time

from . import audit as auditmod
from . import config as cfgmod
from . import limit as limitmod
from . import security

# 商户码规则：**与前端读 URL 前缀的那份、以及发布脚本 nginx 正则的那份同一套**。
# 三处任何一处放宽，都会在"第二家"身上出事（前缀进 nginx 正则、进令牌 claims、进存储键）。
CODE_RE = re.compile(r"^[a-z][a-z0-9-]{1,29}$")
# 这些字符串不能作为商户码：它们已经是 URL 上的目录名/入口文件名，撞了就会互相遮蔽。
RESERVED = ("", "h5", "web", "index", "static", "assets", "admin", "api", "ws", "logs")

OFFLINE_ALERT_SEC = 900        # 在线过但已失联超过 15 分钟 → 告警
H5_STALE_SEC = 86400           # 发布状态超过 24 小时没更新 → 标"可能已过期"
VERSION_FLOOR_FAIL = 3         # 连续失败达到这个数 → 告警


# ------------------------------------------------------------------ B8 预检

def check_code(cfg, code):
    """开通向导第 1 步的即时校验：格式 / 保留字 / 是否已被占用。

    ★ 为什么要在提交前问：商户码之后会同时出现在 **URL 前缀、Agent 名、云端登记名**
      三处，填错了不是改个显示名能救的 —— 得删掉重开，而删商户会连带换密钥。
    ★ 这里**不做静默转小写**：真正建商户的那条接口是按原样校验的，
      预检如果偷偷改了大小写，就会出现"预检说可用、提交被拒"这种更让人困惑的结果。
    """
    raw = str(code or "").strip()
    reasons = []
    if not raw:
        reasons.append("还没填")
    else:
        if raw != raw.lower():
            reasons.append("要用小写字母（大写会被拒）")
        if not CODE_RE.match(raw.lower()):
            reasons.append("小写字母开头，可含数字与 -，长度 2-30")
        elif raw in RESERVED:
            reasons.append("这个字符串是访问路径的保留字，不能当商户码")
    ms = cfgmod.merchants(cfg) or {}
    agents = (cfg["auth"].get("agents") or {})
    clients = cfg["auth"].get("clients") or []
    taken_by = []
    if raw in ms:
        taken_by.append("商户条目")
    if raw in agents:
        taken_by.append("Agent 登记")
    if any(str(c.get("agent") or "") == raw for c in clients):
        taken_by.append("客户端归属")
    return {
        "code": raw,
        "ok": not reasons and not taken_by,
        "reasons": reasons,
        "taken_by": taken_by,
        "url_prefix": "/%s/" % raw if raw else "",
        "relay_ws": "/ws/agent",
        "hint": ("名字已被占用时整条开通会被拒，不会换掉在线那家的密钥"
                 if taken_by else "可用"),
    }


# ------------------------------------------------------------------ B4 H5 发布态

def h5_state(cfg):
    """读 `logs/h5_state.json`（由 uniapp 的 deploy.py 每次发布后原子写）。

    ★ 为什么不让云端自己去问 nginx / SSH 看软链：那样这个只读看板就依赖了
      一条带凭据的通道，而且 nginx 挂了的时候恰好什么都查不到 ——
      最需要看状态的时刻正是它不可用的时刻。读"最后一次已知状态 + 陈旧标记"更诚实。
    """
    path = os.path.join((cfg or {}).get("log_dir") or "logs", "h5_state.json")
    now = time.time()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"available": False, "tenants": {}, "builds": [], "updated_at": 0,
                "stale": False, "age_sec": 0}
    updated = float(data.get("updated_at") or 0)
    return {
        "available": True,
        "tenants": data.get("tenants") or {},
        "current": data.get("current") or "",
        "builds": data.get("builds") or [],
        "updated_at": updated,
        "age_sec": int(now - updated) if updated else 0,
        "stale": bool(updated and now - updated > H5_STALE_SEC),
        "base_url": data.get("base_url") or "",
    }


# ------------------------------------------------------------------ 审计小工具

def _recent(cfg, days=1):
    return auditmod.read(cfg, days=max(1, min(int(days or 1), 90)))


def _per_merchant(entries):
    """{mkt: {calls, fail, last_ok_at, last_ok_op, codes:{code:count}}}

    ★ 只算客户侧流量：`admin.*` 是运营者在后台做的动作，混进来的话
      "这家 24 小时被调用几次"就变成了"我刚才点了几下"，判读会错。
    """
    out = {}
    for e in entries:
        op = str(e.get("op") or "")
        if op.startswith("admin."):
            continue
        code = str(e.get("mkt") or e.get("agent") or "")
        if not code:
            continue
        d = out.setdefault(code, {"calls": 0, "fail": 0, "last_ok_at": 0,
                                  "last_ok_op": "", "codes": {}})
        d["calls"] += 1
        if not e.get("ok"):
            d["fail"] += 1
            key = str(e.get("code") or "ERR")
            d["codes"][key] = d["codes"].get(key, 0) + 1
        elif e.get("ok") and (e.get("ts") or 0) >= d["last_ok_at"]:
            d["last_ok_at"] = e.get("ts") or 0
            d["last_ok_op"] = str(e.get("op") or "")
    return out


def h5_root(cfg):
    return str((cfg or {}).get("h5_root") or "/var/www/jxc-h5")


def scan_h5(cfg):
    """直接看文件系统：`tenants/<码>` 每条软链就是一个已发布的前缀。

    与 `h5_state.json`（发布脚本回写的那份）互为对照：快照可能过期，
    而"收链"这种动作必须作用在真实文件上，所以**动手前一律以文件系统为准**。
    """
    root = h5_root(cfg)
    tdir = os.path.join(root, "tenants")
    tenants = {}
    plain = []
    try:
        for name in sorted(os.listdir(tdir)):
            p = os.path.join(tdir, name)
            if not os.path.islink(p):
                # 不是软链却出现在这里 = 有人直接拷了一份站点进去。
                # nginx 照样会服务它，所以**不能当没看见**；但也绝不能由这里去删
                # （它不是链，删错了就是删真数据），只报出来让人用命令行处理。
                plain.append(name)
                continue
            tgt = os.path.realpath(p)
            tenants[name] = {"build_id": os.path.basename(tgt), "url": "/%s/" % name,
                             "linked_at": os.path.getmtime(p),
                             "broken": not os.path.isdir(tgt)}
    except OSError:
        pass
    try:
        current = os.path.basename(os.path.realpath(os.path.join(root, "current")))
    except OSError:
        current = ""
    try:
        builds = sorted(os.listdir(os.path.join(root, "builds")))
    except OSError:
        builds = []
    return {"available": bool(tenants) or bool(builds) or bool(plain), "tenants": tenants,
            "plain": plain, "current": current, "builds": builds,
            "updated_at": 0, "stale": False, "age_sec": 0, "source": "filesystem"}


def published_codes(live):
    """前缀"算不算已发布"的唯一判据：软链或真目录都算（nginx 都会服务）。"""
    return set((live or {}).get("tenants") or {}) | set((live or {}).get("plain") or [])


def write_h5_snapshot(cfg):
    """重扫文件系统并原子回写 `logs/h5_state.json`，让界面立刻自洽。"""
    data = scan_h5(cfg)
    data["updated_at"] = time.time()
    data["stale"] = False
    data["age_sec"] = 0
    data["written_by"] = "cloudapi（后台收链后重扫）"
    path = os.path.join((cfg or {}).get("log_dir") or "logs", "h5_state.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=1))
        os.replace(tmp, path)
    except OSError:
        pass
    return data


def unpublish_prefix(cfg, code):
    """收掉一个商户的 H5 前缀：只删 `tenants/<码>` 这一条**软链**。

    ★ 为什么这条可以让云端动手，而"发布"不行：发布要逐文件校验 sha256、要先在
      独立配置里 `nginx -t` 试跑，那些门都在发布脚本里 —— 后台代跑等于绕过它们。
      收链只是删一条符号链接，nginx 按路径解析，删完立刻 404，不需要 reload、
      不碰构建目录、不碰别家的链。所以它是可以安全收进后台的那一半。
    ★ 三道自证：码必须过 CODE_RE（不可能带 / 或 ..）、目标必须是软链（绝不动真目录）、
      realpath 必须仍在 tenants 里（防有人把链指向 /etc）。
    """
    code = str(code or "").strip()
    if not CODE_RE.match(code):
        raise ValueError("商户码不合法，不能拿去拼路径：%s" % code)
    root = h5_root(cfg)
    tdir = os.path.join(root, "tenants")
    link = os.path.join(tdir, code)
    if os.path.exists(link) and not os.path.islink(link):
        # 是**真目录/真文件**而不是软链：nginx 同样会服务它，所以这一家确实"还发布着"，
        # 但删它就不是"收一条链"而是"删一份拷贝"了 —— 那是发布脚本的活，这里坚决不动。
        raise ValueError("tenants/%s 不是软链（像是直接拷进去的一份站点），后台不删真目录，"
                         "请用 deploy.py --merchant %s --unpublish 人工处理" % (code, code))
    if not os.path.islink(link):
        return {"removed": False, "reason": "本来就没有这条链（已是未发布状态）", "code": code}
    target = os.path.realpath(link)
    # 链本来就拼在 tenants/<码> 里（码过 CODE_RE，不可能带 / 或 ..），这里再验一次
    # **指向**必须落在站点根内：正常链指的是 ../builds/<id>（在 h5_root 里、但在 tenants 外），
    # 所以判据是 h5_root 而不是 tenants —— 第一版写成 tenants 会把每条正常链都判成越界。
    if not target.startswith(os.path.realpath(root) + os.sep):
        raise ValueError("这条链指向站点目录之外（%s），拒绝删除" % target)
    build_id = os.path.basename(target)
    os.unlink(link)
    snap = write_h5_snapshot(cfg)
    probe = None
    try:                                    # 收完自己看一眼 nginx，别只信文件系统
        import urllib.error
        import urllib.request

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open("http://127.0.0.1/%s/" % code, timeout=8) as r:
                probe = r.status
        except urllib.error.HTTPError as exc:
            probe = exc.code
    except Exception:  # noqa: BLE001 探不到不影响"链已删"这个事实
        probe = None
    return {"removed": True, "code": code, "build_id": build_id, "target": target,
            "probe_status": probe, "still_published": code in (snap.get("tenants") or {})}


def reconcile(cfg, rows):
    """对账：链与商户两个清单不一致的两种情况都摆出来。

    ★ 为什么必须有：删商户以前不碰 nginx，于是留下"前缀还能打开、但点进去永远停在
      登录页"的孤儿链 —— 对外这是个能访问的陌生地址，对内谁都想不起来它为什么在。
      反过来（有商户没链）也值得提一句，否则新开通的商户会一直 404 而没人知道下一步。
    """
    live = scan_h5(cfg)
    codes = {r["code"] for r in rows}
    published = published_codes(live)
    return {
        "orphans": sorted(published - codes),
        "missing": sorted(codes - published),
        "broken": sorted(c for c, t in (live.get("tenants") or {}).items() if t.get("broken")),
        "plain": sorted(live.get("plain") or []),
        "live": live,
    }


# ------------------------------------------------------------------ 上下线账

def agent_events(cfg, code=None, limit=40):
    """`logs/agents.jsonl` 尾部（上线/下线事实流）。"""
    path = os.path.join((cfg or {}).get("log_dir") or "logs", "agents.jsonl")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-800:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):        # 新的在前
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if code and e.get("agent") != code:
            continue
        out.append(e)
        if len(out) >= int(limit):
            break
    return out


def timeline(cfg, reg, code=None, limit=30):
    """把「上下线账 + 后台运营动作 + 同名冲突」并成一根线，给监控页。

    ★ 为什么合并：这三类事件回答的是同一个问题 —— "刚才发生了什么"。
      分成三页看，运营者就得自己在三个时间戳之间对齐，而事故恰恰发生在对齐失败时。
    """
    ev = []
    for e in agent_events(cfg, code=code, limit=limit):
        ev.append({
            "ts": e.get("ts"), "kind": "agent", "code": e.get("agent"),
            "level": "ok" if e.get("event") == "online" else "muted",
            "title": ("Agent 上线 " + str(e.get("version") or ""))
                     if e.get("event") == "online" else "Agent 下线",
            "detail": "主机 %s · 指纹 %s · 动作 %s 个 · 通道 %s" % (
                e.get("hostname") or "?", (e.get("install_id") or "(未上报)")[:12],
                e.get("ops") if e.get("ops") is not None else "?",
                e.get("source") or "ws"),
            "database_verified": e.get("database_verified") or "",
        })
    for e in _recent(cfg, days=7):
        op = str(e.get("op") or "")
        if not op.startswith("admin."):
            continue
        if code and str(e.get("mkt") or e.get("agent") or "") != code:
            continue
        ev.append({"ts": e.get("ts"), "kind": "admin", "code": e.get("mkt") or e.get("agent") or "",
                   "level": "ok" if e.get("ok") else "bad",
                   "title": op.replace("admin.", ""), "detail": str(e.get("note") or "")})
    for c in list(getattr(reg, "conflicts", []) or [])[-10:]:
        if code and c.get("agent") != code:
            continue
        ev.append({"ts": c.get("ts"), "kind": "conflict", "code": c.get("agent"),
                   "level": "bad", "title": "同名接入被拦",
                   "detail": "在用的是 %s，后来者 %s（主机 %s）—— 要换机请点「放行接管」" % (
                       (c.get("held_by") or "?")[:12], (c.get("rejected") or "?")[:12],
                       c.get("rejected_host") or "?")})
    ev.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return ev[:int(limit)]


# ------------------------------------------------------------------ B1 健康一览

def _accounts_of(cfg, code):
    rows = []
    for a in cfg["auth"].get("accounts", []) or []:
        if code in security.merchants_for_names(cfg, a.get("stores"), a.get("all_stores")):
            rows.append(a)
    return rows


def _db_evidence(row):
    """账套那一格的证据文案，按 db_state 分四支。

    ★ 必须是函数而不是一坨内联字典：字典字面量会把所有分支的值先算出来，
      而 allowed 那支要在清单里 index() 找"第几本"，实测值为空时直接 ValueError
      ⇒ 整个健康聚合接口 500（2026-09-26 本地门当场抓到过一次）。
    """
    st = row.get("db_state") or "unknown"
    verified = row.get("database_verified") or ""
    dbs = row.get("databases") or []
    if st == "match":
        return "实测 %s（默认库）" % verified
    if st == "allowed":
        n = (dbs.index(verified) + 1) if verified in dbs else 0
        return ("实测 %s —— 该商户登记的第 %d 个账套，非默认账套（合法，但需确认当前查看的是哪一个）"
                % (verified, n or 2))
    if st == "mismatch":
        return "实测 %s，不在登记的清单里（%s）" % (
            verified, "、".join(dbs) or "未登记，按商户码判")
    return "Agent 未上报实测值（老版本包，或还没连库）"


def _checklist(row, m, h5):
    """接入进度：**全部由事实推导**，不给人手工勾选。

    勾了"已完成"但实际没做，比没有这一列更糟 —— 它会让人停止核对。
    """
    ev_online = row.get("last_online_at") or (row.get("connected_at") if row.get("online") else 0)
    return [
        {"key": "registered", "label": "商户与 Agent 密钥已建立",
         "done": bool(row.get("agent_registered")),
         "evidence": "已登记" if row.get("agent_registered") else "还没有 Agent 登记"},
        {"key": "connected", "label": "客户端机连上过",
         "done": bool(ev_online or row.get("online")),
         "evidence": (("%s · 主机 %s · 指纹 %s · 上报 %d 个动作") % (
             _ts(ev_online or row.get("connected_at")), row.get("hostname") or "?",
             (row.get("install_id") or "(未上报)")[:12], len(row.get("ops") or [])))
         if (ev_online or row.get("online")) else "从未接入"},
        {"key": "database", "label": "实测账套属于这家商户",
         "done": row.get("db_state") in ("match", "allowed"),
         # ★ 这里不能用"字典字面量 + .get(state)"：字面量会**先把所有分支的值算出来**，
         #   而 allowed 那支要对清单做 index() —— 未上报（verified=""）时当场 ValueError，
         #   整个 /api/admin/merchant-health 回 500（本地门就是这么抓到的）。
         "evidence": _db_evidence(row)},
        {"key": "prefix", "label": "访问前缀已发布",
         "done": bool(h5.get("published")),
         "evidence": ("指向构建 %s" % h5.get("build_id")) if h5.get("published")
         else "未发布（发布用 uniapp 的 deploy.py --merchant %s）" % row.get("code")},
        {"key": "accounts", "label": "有可用账号",
         "done": bool(row.get("accounts")),
         "evidence": ("%d 个应用账号 · %d 个微信授权" % (
             len(row.get("accounts") or []), row.get("users_n") or 0))},
    ]


def _ts(v):
    if not v:
        return ""
    return time.strftime("%m-%d %H:%M:%S", time.localtime(float(v)))


def merchant_rows(cfg, reg, days=1):
    """B1：每家一行的健康数据。概览、列表、详情头部都用它。"""
    ms = cfgmod.merchants(cfg) or {}
    agents = cfg["auth"].get("agents") or {}
    online = reg.list_online() if reg is not None else {}
    seen = reg.last_seen_versions() if hasattr(reg, "last_seen_versions") else {}
    per = _per_merchant(_recent(cfg, days=days))
    h5 = h5_state(cfg)
    # ★ 前缀状态以**文件系统**为准，快照只当兜底：v3cloud 与站点在同一台机器上（都是 root），
    #   直接看 tenants/ 就是真相；而快照是发布脚本"上一次告诉我们"的状态 —— 手工 ln -s / rm
    #   之后它是滞后的。判断"能不能删、要不要收链"用滞后数据，就会出现
    #   "影响面说可以直接删、删除接口却回 409"这种自相矛盾（验收当场抓到过一次）。
    live = scan_h5(cfg)
    published = published_codes(live)
    now = time.time()
    rows = []
    for code in sorted(set(ms) | set(online) | set(agents)):
        m = ms.get(code) or {}
        info = online.get(code) or {}
        last = seen.get(code) or {}
        version = info.get("version") or last.get("version") or ""
        floor = str(m.get("min_agent_version") or cfg.get("min_agent_version") or "").strip()
        verified = str(info.get("database_verified") or last.get("database_verified") or "")
        dbs = cfgmod.merchant_databases(cfg, code)
        configured = str(info.get("database") or last.get("database")
                         or (dbs[0] if dbs else "") or "")
        # ★ 判据从「实测 == 商户码」换成「实测属于这家」（config.database_state）：
        #   一台主机两本账的老板，第二本的库名不可能等于商户码，旧判据会让他在后台永久标红。
        #   空清单仍按老规矩（实测 == 商户码）判，所以单账套商户不登记也不会被误判成故障。
        db_state = cfgmod.database_state(cfg, code, verified)
        st = per.get(code, {})
        # 文件系统读得到就**完全以它为准**（包括"没有这条链"这个否定结论）；
        # 只在读不到站点目录时（比如开发机在 Windows 上跑云端）才退回快照。
        # 混着读会出现"影响面说已发布、删除接口说没发布"这种自相矛盾。
        live_ok = bool(live.get("available"))
        src = ((live.get("tenants") or {}).get(code) if live_ok
               else (h5.get("tenants") or {}).get(code)) or {}
        prefix = {"published": (code in published) if live_ok
                  else bool((h5.get("tenants") or {}).get(code, {}).get("build_id")),
                  "build_id": src.get("build_id") or "",
                  "url": src.get("url") or ("/%s/" % code),
                  "source": "filesystem" if live_ok else "snapshot",
                  "broken": bool(src.get("broken")),
                  # 不是软链却出现在 tenants/ 下：算已发布，但后台不会去删它
                  "plain": code in (live.get("plain") or []) if live_ok else False}
        row = {
            "code": code, "title": m.get("title") or code,
            "enabled": bool(m.get("enabled", True)),
            "databases": dbs,
            "database_default": (dbs[0] if dbs else ""),
            # ★ Step 2：客户机自己放行的清单与实际服务过的账套。
            #   云端这份是"平台允许这家有哪几本"，客户机那份是"那个进程允许被读到哪几本"，
            #   **两边都放行才真读得到** —— 差集就是装机现场最常见的卡点。
            "agent_allowed": [str(x) for x in (info.get("allowed_databases") or [])],
            "served_databases": [str(x) for x in (info.get("served_databases") or [])],
            "database": configured, "database_verified": verified, "db_state": db_state,
            "agent_registered": code in agents,
            "online": code in online, "source": info.get("source") or "",
            "version": version, "build_id": version.split("+")[-1] if "+" in version else "",
            "ops": info.get("ops") or [],
            "connected_at": info.get("connected_at"),
            "uptime_sec": info.get("uptime_sec") or 0,
            "status_at": info.get("status_at") or 0,
            "install_id": info.get("install_id") or last.get("install_id") or "",
            "hostname": info.get("hostname") or last.get("hostname") or "",
            "last_online_at": last.get("last_online"),
            "last_offline_at": last.get("last_offline"),
            "min_agent_version": floor,
            "version_too_old": bool(floor and _ver(version) < _ver(floor)),
            "clients": [c.get("name") for c in cfgmod.clients_of_merchant(cfg, code)],
            "accounts": [a.get("username") for a in _accounts_of(cfg, code)],
            "users_n": len([u for u in (cfg["auth"].get("users") or [])
                            if code in security.merchants_for_names(
                                cfg, [x for x in (u.get("clients") or [])],
                                bool(u.get("all_clients")))]),
            "calls_24h": st.get("calls", 0), "fail_24h": st.get("fail", 0),
            "codes": st.get("codes", {}),
            "last_ok_at": st.get("last_ok_at") or 0, "last_ok_op": st.get("last_ok_op") or "",
            "inflight": (getattr(reg, "inflight", {}) or {}).get(code, 0),
            # 分级闸门之后，"在途 5 条"这种数字单独看没有信息量：
            # 是 5 条轻查询还是 2 条报表 + 3 条在排队，处置完全不同。后台按类摊开给人看。
            "inflight_by_class": (getattr(reg, "inflight_detail", lambda _n: {}) or (lambda _n: {}))(code),
            "busy_rejects": (getattr(reg, "busy_rejects", {}) or {}).get(code, 0),
            "prefix": prefix,
            "h5_stale": h5.get("stale"), "h5_available": h5.get("available"),
            "takeover_open": bool((getattr(reg, "takeover_until", {}) or {}).get(code)
                                  and reg.takeover_until[code] > now),
        }
        row["checklist"] = _checklist(row, m, row["prefix"])
        row["done_n"] = sum(1 for c in row["checklist"] if c["done"])
        row["alerts"] = _row_alerts(row)
        row["level"] = ("bad" if any(a["level"] == "bad" for a in row["alerts"])
                        else "warn" if row["alerts"] else "ok")
        rows.append(row)
    return rows


def _ver(v):
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def _row_alerts(row):
    """告警文案：**必须随数据变**（写死的解释性文案会让人以为界面在说自己的情况）。"""
    out = []
    if not row["enabled"]:
        out.append({"level": "warn", "text": "已停用：这家所有请求现在都回「商户已停用」"})
    if row["db_state"] == "mismatch":
        out.append({"level": "bad", "text": "实测账套是「%s」，不属于这家登记的账套（%s）"
                    " —— 客户看到的数字可能来自另一套账"
                    % (row["database_verified"],
                       "、".join(row.get("databases") or []) or "未登记清单，按商户码判")})
    # ★ db_state == "allowed"（在读这家登记过的第二本账）**不进告警**：
    #   一台主机多本账是合法形态，而告警条是按条计数的"N 项需要你处理"，
    #   把正常状态塞进去就等于制造新的永久噪音 —— 它只在该商户那一列里如实显示。
    # ★ 云端登记了、客户机却没放行 ⇒ 请求会被 Agent 挡在门外（DATABASE_NOT_ALLOWED）。
    #   老版本包不上报 allowed_databases，那份清单是空的 —— **空就跳过这条判断**，
    #   否则每一家老包都会凭空多一条告警（告警一多就没人看了，这是这轮改动的前提）。
    agent_allowed = row.get("agent_allowed") or []
    if agent_allowed and row.get("online"):
        missing = [d for d in (row.get("databases") or []) if d not in agent_allowed]
        if missing:
            out.append({"level": "warn",
                        "text": "云端允许该商户读取「%s」，但该客户机仅放行「%s」—— 未放行的账套"
                                "需在客户机上执行：scripts\\switch-db.py --allow \"%s\""
                                % ("、".join(row["databases"]), "、".join(agent_allowed),
                                   ",".join(row["databases"]))})
    if row["db_state"] == "unknown" and row["online"]:
        out.append({"level": "warn", "text": "在线但没上报实测账套（客户机上的包版本偏老，重打包后会有）"})
    if not row["online"] and row["agent_registered"]:
        gap = (time.time() - (row["last_online_at"] or 0)) if row["last_online_at"] else None
        if gap and gap > OFFLINE_ALERT_SEC:
            out.append({"level": "bad", "text": "已失联 %s（最后一次上线 %s）"
                        % (_dur(gap), _ts(row["last_online_at"]))})
        elif not row["last_online_at"]:
            out.append({"level": "warn", "text": "登记了但从未接入：客户机还没装，或配置里的名称/密钥不一致"})
    if row["version_too_old"]:
        out.append({"level": "warn", "text": "版本 %s 低于该商户门槛 %s"
                    % (row["version"] or "未上报", row["min_agent_version"])})
    if row["fail_24h"] and row["calls_24h"] and row["fail_24h"] >= VERSION_FLOOR_FAIL:
        top = sorted(row["codes"].items(), key=lambda kv: -kv[1])[:2]
        out.append({"level": "warn", "text": "24 小时内 %d 次失败（主要是 %s）"
                    % (row["fail_24h"], "、".join("%s×%d" % (k, v) for k, v in top))})
    if row["prefix"]["published"] and not row["online"] and row["calls_24h"] == 0:
        out.append({"level": "warn", "text": "前缀已发布但这家没有可用客户端 —— 打开只会停在登录页"})
    return out


def _dur(sec):
    sec = int(sec or 0)
    if sec < 90:
        return "%d 秒" % sec
    if sec < 5400:
        return "%d 分钟" % round(sec / 60.0)
    if sec < 172800:
        return "%.1f 小时" % (sec / 3600.0)
    return "%d 天" % (sec / 86400)


def alerts(rows):
    """全局告警条：只汇总有事的行，一条都没有就返回空（界面不显示黄条）。"""
    out = []
    for r in rows:
        for a in r["alerts"]:
            out.append({"code": r["code"], "title": r["title"], **a})
    out.sort(key=lambda x: 0 if x["level"] == "bad" else 1)
    return out


# ------------------------------------------------------------------ B2 详情

def merchant_detail(cfg, reg, code, days=1):
    rows = merchant_rows(cfg, reg, days=days)
    row = next((r for r in rows if r["code"] == code), None)
    if row is None:
        return None
    clients = [c for c in cfgmod.clients_of_merchant(cfg, code)]
    accounts = []
    for a in _accounts_of(cfg, code):
        accounts.append({"username": a.get("username"), "name": a.get("name") or "",
                         "disabled": bool(a.get("disabled")),
                         "stores": security.merchants_for_names(cfg, a.get("stores"),
                                                                a.get("all_stores")),
                         "all_stores": bool(a.get("all_stores"))})
    users = []
    for u in cfg["auth"].get("users") or []:
        got = security.merchants_for_names(cfg, [x for x in (u.get("clients") or [])],
                                           bool(u.get("all_clients")))
        if code in got:
            users.append({"openid": u.get("openid"), "name": u.get("name") or ""})
    row["client_rows"] = [{"name": c.get("name"), "title": c.get("title") or c.get("name"),
                           "access": c.get("access") or "auth"} for c in clients]
    # ★ 凭据明文随详情一起回（2026-09-26 用户拍板：后台只有他一人能进，审查时不该
    #   为了查一个密钥去重置密钥 —— 重置＝现网当场掉线，那天真出过 50 分钟事故）。
    #   代价说清楚：值出现在接口响应与页面上，F12 / 截图 / 远程协助投屏都能看到，
    #   所以别在有人看着屏幕时开这一页。应用账号口令不在这里，那些是 pbkdf2 哈希，
    #   服务端也还原不出来（要口令只能重置，那本来就是设计如此）。
    row["agent_secret"] = str(((cfg["auth"] or {}).get("agents") or {}).get(code) or "")
    row["entry_tokens"] = [{"name": c.get("name"), "token": str(c.get("token") or "")}
                           for c in clients]
    # 调试台要的说明与示例：只在详情这一层回（一份约 8KB，列表层带上会把接口撑肥）
    row["op_docs"] = ((reg.list_online() if reg is not None else {}) or {}).get(code, {}) \
        .get("op_docs") or {}
    row["account_rows"] = accounts
    row["user_rows"] = users
    row["events"] = timeline(cfg, reg, code=code, limit=20)
    row["recent_failures"] = [
        {"ts": e.get("ts"), "op": e.get("op"), "code": e.get("code"),
         "sub": e.get("sub") or e.get("client"), "ip": e.get("ip"),
         "rid": e.get("rid"), "note": e.get("note") or ""}
        for e in _recent(cfg, days=days)
        if not e.get("ok") and str(e.get("mkt") or e.get("agent") or "") == code][:20]
    return row


# ------------------------------------------------------------------ B7 影响面

def impact(cfg, reg, code, action="disable"):
    """危险动作的**活值**影响面，供确认弹窗。

    ★ 为什么单独一个接口：确认框里写"确定吗"没有价值 —— 决定要不要点下去的是
      "这家现在在线吗、有几个客户端在用、刚才还在被调用吗"。这些只有服务端知道。
    """
    rows = merchant_rows(cfg, reg, days=1)
    row = next((r for r in rows if r["code"] == code), None)
    if row is None:
        return None
    bound_clients = len(row["clients"])
    lines = []
    if action == "delete":
        lines.append("会一起删掉：%d 个客户端归属、同名的 Agent 登记" % bound_clients)
        if row["accounts"]:
            lines.append("名下还有 %d 个应用账号（%s）—— 要先处理它，否则拒绝删除"
                         % (len(row["accounts"]), "、".join(row["accounts"])))
        lines.append("H5 前缀：%s" % (
            "已发布 —— 删商户不会自动收链，要先点「收链」把 /%s/ 下线，"
            "否则留下一个谁都能打开、却永远停在登录页的地址" % code
            if row["prefix"]["published"] else "未发布，可以直接删"))
    elif action == "disable":
        lines.append("该商户的 %d 个客户端、%d 个应用账号的请求会立刻回「商户已停用」"
                     % (bound_clients, len(row["accounts"])))
        lines.append("密钥与客户端都留着，随时可启用")
    elif action == "rotate":
        lines.append("客户机上的 Agent 会在下次重连时连不上，要重新把新密钥填进配置文件")
    elif action == "unpublish":
        live = scan_h5(cfg).get("tenants", {}).get(code) or {}
        lines.append("要删的只是 nginx 上这一条软链：tenants/%s → builds/%s"
                     % (code, live.get("build_id") or "?"))
        lines.append("构建目录、根站 current 与别家的链都不动；nginx 按路径解析，删完立刻 404，不需要 reload")
        if row["online"]:
            lines.append("注意：这家 Agent 还在线，收链后客户机仍能连云端，只是网页入口打不开")
    lines.append("当前状态：%s" % ("在线，已连 %s" % _dur(row["uptime_sec"])
                                if row["online"] else "未接入"))
    lines.append("近 24 小时：调用 %d 次、失败 %d 次%s"
                 % (row["calls_24h"], row["fail_24h"],
                    "" if not row["last_ok_at"] else "，最近成功 %s" % _ts(row["last_ok_at"])))
    return {"code": code, "action": action, "lines": lines,
            "clients": bound_clients, "accounts": len(row["accounts"]),
            "online": row["online"], "calls_24h": row["calls_24h"],
            "prefix_published": bool(row["prefix"]["published"]),
            # 删除的四道门：在线 / 应用账号 / 前缀未收（客户端可以随 confirm 一起解绑）
            "blocking": bool(action == "delete" and (row["accounts"] or bound_clients
                                                     or row["prefix"]["published"] or row["online"]))}


# ------------------------------------------------------------------ 监控页

def monitor(cfg, reg, rows=None):
    rows = rows if rows is not None else merchant_rows(cfg, reg, days=1)
    try:
        from . import tasks as tasksmod
        task_total = len(getattr(tasksmod, "TASKS", {}) or {})
        per_agent = {}
        for t in (getattr(tasksmod, "TASKS", {}) or {}).values():
            key = t.get("agent") or "-"
            d = per_agent.setdefault(key, {"total": 0, "running": 0})
            d["total"] += 1
            if t.get("status") in ("pending", "running"):
                d["running"] += 1
    except Exception:  # noqa: BLE001 任务表读不到不该影响监控页
        task_total, per_agent = 0, {}
    online = [r for r in rows if r["online"]]
    rec = reconcile(cfg, rows)
    return {
        "orphans": rec["orphans"], "missing_prefix": rec["missing"], "broken_links": rec["broken"],
        "plain_links": rec["plain"],
        "merchants_total": len(rows), "online": len(online),
        "enabled": sum(1 for r in rows if r["enabled"]),
        "calls_24h": sum(r["calls_24h"] for r in rows),
        "fail_24h": sum(r["fail_24h"] for r in rows),
        "inflight": sum(r["inflight"] for r in rows),
        "busy_rejects": sum(r["busy_rejects"] for r in rows),
        "max_inflight": int((cfg or {}).get("max_inflight") or 24),
        "inflight_by_class": (cfg or {}).get("inflight_by_class") or {},
        "rate": limitmod.snapshot(),
        "tasks": {"total": task_total, "per_agent": per_agent,
                  "quota": int((cfg or {}).get("tasks_per_agent") or 20)},
        "takeover_open": {k: v for k, v in
                          (getattr(reg, "takeover_until", {}) or {}).items()
                          if v > time.time()},
        "h5": rec["live"], "h5_snapshot": h5_state(cfg),
        "lowest_version": min([r["version"] for r in online if r["version"]] or ["—"]),
        "alerts": alerts(rows),
    }
