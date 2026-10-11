#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""下线一家商户（provision-merchant.py 的反向）：收前缀 → 删应用账号 → 删客户端 → 删 Agent 登记 → 删商户。

★ 为什么要脚本而不是手点：删一家的正确顺序是后台那四道门反过来走
  （在线 → 应用账号 → 客户端 → 前缀）。手点最容易留三种半成品：
    · 删了商户没收链 ⇒ 公网多一个"能打开但永远停在登录页"的地址；
    · 删了商户没删账号 ⇒ 账号还能登录，可及商户是空集（症状是"进去什么都没有"）；
    · 删了客户端没删账号归属 ⇒ 同上，且审计里一条异常都查不到。
  所以这个脚本**每一步都复核自己没留下上面任何一种**。

★ 三条硬规矩
  1. 默认 dry-run：只打印将要删什么，加 `--yes` 才动手；
  2. 该商户的 Agent 还在线 ⇒ 直接停手（先停客户机那份，别拿接口硬断，
     否则你会看到"删除成功、机器还在报 4002"这种两头对不上的现场）；
  3. 账号若还绑着**别家**商户 ⇒ 停手让人改绑，不越权删别人的登录口。

用法：
    python scripts/deprovision-merchant.py --merchant shiwantongda --from-ssh
    python scripts/deprovision-merchant.py --merchant shiwantongda --from-ssh --yes
退出码：0 成功（含"本来就没有这家"）/ 2 前置不满足（在线、跨家账号）/ 3 接口失败 / 4 缺后台凭据
"""
import argparse
import importlib.util
import json
import os
import sys
import types
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def _load_provision():
    """复用 provision-merchant.py 的登录/取密/商户码规则（同一个后台，规则不许抄两份）。"""
    spec = importlib.util.spec_from_file_location(
        "_prov", os.path.join(HERE, "provision-merchant.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def http_get(url, timeout=20):
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001
        return 0


def _h5_tenants(api):
    """前缀实况。**必须读 `live`**（此刻直接看文件系统），`data` 是发布脚本回写的那份快照，
    手删过软链/收过链之后它会滞后 —— 拿快照判断"还在发布中"会把已下线的一家算成没下线。"""
    _s, body = api.call("/api/admin/h5")
    body = body or {}
    live = body.get("live") or {}
    return (live.get("tenants") or {}) if live else (body.get("data") or {}).get("tenants") or {}


def brief(body, n=200):
    return json.dumps(body, ensure_ascii=False)[:n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--merchant", required=True, help="商户码（== Agent 名 == H5 前缀）")
    ap.add_argument("--base", default="http://149.88.75.125:8000")
    ap.add_argument("--from-ssh", action="store_true", help="没有 JXC_ADMIN_PWD 时从云端配置读一次")
    ap.add_argument("--yes", action="store_true", help="真删（默认只 dry-run 打印计划）")
    args = ap.parse_args()

    prov = _load_provision()
    code = str(args.merchant or "").strip().lower()
    if not prov.CODE_RE.match(code):
        print("✗ 商户码不合法：%r" % args.merchant)
        return 4
    pw, how = prov.admin_password(types.SimpleNamespace(from_ssh=args.from_ssh))
    if not pw:
        print("✗ 没有后台密码：设 JXC_ADMIN_PWD 或加 --from-ssh（当前：%s）" % how)
        return 4
    api = prov.Api(args.base)
    ok, cookie = api.login(pw)
    if not ok:
        print("✗ 后台登录失败（密码不对或被限流）")
        return 4
    api.cookie = cookie

    st, state = api.call("/api/admin/state")
    if st != 200:
        print("✗ 读 /api/admin/state 失败 %s %s" % (st, brief(state)))
        return 3
    rows = state.get("merchants") or []
    row = next((r for r in rows if r.get("code") == code), None)
    if row is None:
        print("云端没有商户「%s」的登记 —— 没什么要清的。" % code)
        # 但半成品前缀仍可能挂着（删商户失败过一次就会），所以照样查一遍并提示
        live = _h5_tenants(api)
        if code in live:
            print("★ 注意：前缀 /%s/ 还发布着（指向 %s），商户却没了 ⇒ 用 "
                  "deploy.py --unpublish 或后台「收链」补一刀。" % (
                      code, (live.get(code) or {}).get("build_id") or "?"))
        return 0

    # ---- 前置：那份 Agent 必须已经下线 ----------------------------------------
    view = state.get("agents") or []
    if isinstance(view, dict):
        entries = [dict(v or {}, agent=k) for k, v in view.items()]
    else:
        entries = list(view)
    online = [a for a in entries
              if str(a.get("agent") or a.get("name") or "") == code and a.get("online")]
    if online:
        print("✗ %s 的 Agent 还在线（%s）。先把客户机那份停掉再跑本脚本 —— "
              "用接口硬断，删完之后那台机器还会每 13 秒报一次接入被拒，"
              "现场看起来就像「脚本没删干净」。" % (code, brief(online[0], 120)))
        return 2

    clients = [c for c in (state.get("clients") or [])
               if str(c.get("agent") or "").strip() == code]
    acc_names = set(row.get("accounts") or [])
    accs = [a for a in (state.get("accounts") or []) if a.get("username") in acc_names]
    # 一个账号可以进多家时不能顺手删（删了别人的登录口）
    multi = [a.get("username") for a in accs
             if len({code} | set(_codes_of(state, a))) > 1]
    if multi:
        print("✗ 这些应用账号还绑着别家商户：%s。请先在「设置 → 应用账号」里改绑，"
              "本脚本不越权删。" % "、".join(x for x in multi if x))
        return 2

    published = code in _h5_tenants(api)

    print("=" * 62)
    print(" 下线商户「%s」计划（%s）" % (code, "DRY-RUN：什么都不动" if not args.yes else "将真删"))
    print("=" * 62)
    print("  1. H5 前缀 /%s/        %s" % (code, "收链（只删软链，构建目录留着）" if published else "未发布，跳过"))
    print("  2. 应用账号            %s" % ("、".join(a.get("username") for a in accs) or "（无）"))
    print("  3. 客户端              %s" % ("、".join(c.get("name") for c in clients) or "（无）"))
    print("  4. Agent 登记          %s" % code)
    print("  5. 商户本体 + 同名密钥  %s（title=%s，账套清单=%s）" % (
        code, row.get("title"), "、".join(row.get("databases") or []) or "未登记"))
    print("  不碰：客户机 config.json、SQL Server 里的账套数据、审计与 agents.jsonl")
    if not args.yes:
        print("\n确认无误后加 --yes 再跑一次。")
        return 0

    # ---- 1 收链 --------------------------------------------------------------
    if published:
        st, body = api.call("/api/admin/merchants/%s/unpublish-prefix" % code, {})
        if st != 200:
            print("✗ 收链失败 %s %s" % (st, brief(body)))
            return 3
        print("  ✓ 前缀已收：%s" % brief(body, 120))
    # ---- 2 账号 --------------------------------------------------------------
    for a in accs:
        st, body = api.call("/api/admin/accounts/%s" % a.get("username"), None, method="DELETE")
        if st != 200:
            print("✗ 删应用账号 %s 失败 %s %s" % (a.get("username"), st, brief(body)))
            return 3
        print("  ✓ 已删应用账号 %s" % a.get("username"))
    # ---- 3 客户端 ------------------------------------------------------------
    for c in clients:
        st, body = api.call("/api/admin/clients/%s" % c.get("token"), None, method="DELETE")
        if st != 200:
            print("✗ 删客户端 %s 失败 %s %s" % (c.get("name"), st, brief(body)))
            return 3
        print("  ✓ 已删客户端 %s" % c.get("name"))
    # ---- 4 Agent 登记（商户那一步也会带走同名，这里显式删是为了看到 404/409）--
    st, body = api.call("/api/admin/agents/%s" % code, None, method="DELETE")
    if st == 200:
        print("  ✓ 已删 Agent 登记")
    elif st != 404:
        print("✗ 删 Agent 登记失败 %s %s" % (st, brief(body)))
        return 3
    else:
        print("  · Agent 登记本来就没有（同名那条会随商户一起清）")
    # ---- 5 商户 --------------------------------------------------------------
    st, body = api.call("/api/admin/merchants/%s?confirm=delete" % code, None, method="DELETE")
    if st != 200:
        print("✗ 删商户失败 %s %s" % (st, brief(body)))
        print("   ★ 后台这道门的拒绝理由是明确的处置指引，照它补：多半是还有账号/客户端/前缀没清。")
        return 3
    print("  ✓ 已删商户与同名密钥登记")

    # ---- 复核：一家下线之后，四个地方都必须查不到它 --------------------------
    _s2, after = api.call("/api/admin/state")
    bad = []
    rows2 = (after or {}).get("merchants") or []
    if any(r.get("code") == code for r in rows2):
        bad.append("merchants 里还在")
    if any(str(c.get("agent") or "") == code for c in ((after or {}).get("clients") or [])):
        bad.append("还有客户端归属指向它")
    if any(code in _codes_of(after, a) for a in ((after or {}).get("accounts") or [])):
        bad.append("还有应用账号能进它")
    if code in _h5_tenants(api):
        bad.append("前缀还在发布中")
    probe = http_get(args.base + "/" + code + "/")
    if probe not in (404, 0):
        bad.append("/%s/ 探测回 %s（期望 404）" % (code, probe))
    if bad:
        print("\n✗ 复核没通过：" + "；".join(bad))
        print("   ⇒ 这就是「删一半」的状态，按上面那条补完再复核一次。")
        return 3
    print("\n✓ 已全部下线并复核：商户 / 密钥 / 客户端 / 账号 / 前缀 都查不到「%s」了。" % code)
    print("  要从零重开：python scripts/provision-merchant.py --merchant %s "
          "--title \"<展示名>\" --access auth --databases \"<账套名>\" --from-ssh" % code)
    print("  ★ 重开之后密钥是新的：必须同步写进客户机 config.json 的 agent.secret 并重启 Agent。")
    return 0


def _codes_of(state, account):
    """这个账号能进哪几家（后台算的是 merchants_for_names，这里用同一份口径的近似：
    账号 stores 里那些客户端的 agent 字段）。"""
    by_name = {c.get("name"): str(c.get("agent") or "") for c in (state.get("clients") or [])}
    out = set()
    for n in (account.get("stores") or []):
        a = by_name.get(n)
        if a:
            out.add(a)
    if account.get("all_stores"):
        out |= {r.get("code") for r in (state.get("merchants") or [])}
    return out


if __name__ == "__main__":
    raise SystemExit(main())
