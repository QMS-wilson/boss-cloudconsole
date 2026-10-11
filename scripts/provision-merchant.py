#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一次开通一家商户（Phase 3）：商户+Agent 密钥 + 客户端 + 首个应用账号 → 一份装机包。

它只做"按顺序调后台接口"这件事，但把三条容易出事的地方钉死了：
  1. **同名存在即非零退出，绝不覆盖**（改造前 `POST /api/admin/agents` 遇同名会静默换密钥，
     结果就是现网那一家当场掉线 —— 见 P1-2）；
  2. 密钥/令牌/账号密码**只写进出包文件**，不出现在 stdout、不进仓库（默认落 `~/.jxc-provision/`）；
  3. 出包里直接带上 Agent 该用的 `config.json` 片段（商户码/账套提示/relay），
     装机时导入即可 —— 安装器里那些"默认值"是顶掉第一家的直接原因，不能靠手抄。

用法：
    # 后台密码优先读环境变量 JXC_ADMIN_PWD；没有就加 --from-ssh 从云端配置里读一次（不落盘）
    python scripts/provision-merchant.py --merchant yedian --title "也店" --access auth --dry-run
    python scripts/provision-merchant.py --merchant yedian --title "也店" --access auth --from-ssh
退出码：0 成功 / 2 名称已被占用 / 3 接口或网络失败 / 4 缺少后台凭据
"""
import argparse
import json
import os
import re
import secrets
import string
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

BASE_DEFAULT = "http://149.88.75.125:8000"
RELAY_DEFAULT = "ws://149.88.75.125:8000/ws/agent"
CODE_RE = re.compile(r"^[a-z][a-z0-9-]{1,29}$")     # 与 deploy.py / admin.py 同一套
OUT_DIR = os.path.expanduser("~/.jxc-provision")


def rand_pw(n=14):
    """应用账号初始密码：避开易混字符，长度够 PBKDF2 那套校验（≥6）。"""
    alph = (string.ascii_uppercase + string.ascii_lowercase + string.digits)
    pw = "".join(secrets.choice(alph) for _ in range(n))
    return pw + secrets.choice("#@") + secrets.choice(string.digits)


class Api:
    def __init__(self, base, cookie=None):
        self.base = base
        self.cookie = cookie

    def call(self, path, payload=None, method=None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        hdr = {"Content-Type": "application/json"}
        if self.cookie:
            hdr["Cookie"] = self.cookie
        req = urllib.request.Request(self.base + path, data=data, headers=hdr,
                                     method=method or ("POST" if payload is not None else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"detail": raw[:300]}
        except Exception as e:  # noqa: BLE001
            return 0, {"detail": "%s %s" % (type(e).__name__, str(e)[:160])}

    def login(self, password):
        """后台会话是 **HttpOnly cookie**（admin.py:125），不是 Authorization 头。
        urllib 没有 cookie jar，所以这里把 Set-Cookie 的那一段自己摘出来。"""
        req = urllib.request.Request(self.base + "/api/admin/login",
                                     data=json.dumps({"password": password}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                cookie = (r.headers.get("Set-Cookie") or "").split(";")[0]
                return (r.status == 200 and bool(cookie)), cookie
        except urllib.error.HTTPError as e:
            return False, ""
        except Exception:  # noqa: BLE001
            return False, ""


def admin_password(args):
    pw = (os.environ.get("JXC_ADMIN_PWD") or "").strip()
    if pw:
        return pw, "env JXC_ADMIN_PWD"
    if args.from_ssh:
        import sshtarget
        cli = sshtarget.connect()
        code, out, err = sshtarget.run(
            cli, "/opt/v3cloud/venv/bin/python -c "
                 "\"import json;print(json.load(open('/opt/v3cloud/cloud_config.json'))"
                 "['auth']['admin']['password'])\"")
        cli.close()
        got = (out or "").strip()
        if code == 0 and got:
            return got, "ssh 读云端配置（未落盘、未打印）"
        raise SystemExit("从云端读后台密码失败：%s" % ((err or out)[:160] or code))
    return "", ""


def state_of(api):
    st, body = api.call("/api/admin/state")
    if st != 200:
        raise SystemExit("读 /api/admin/state 失败 %s %s" % (st, str(body)[:200]))
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--merchant", required=True, help="商户码（== Agent 名 == H5 URL 前缀）")
    ap.add_argument("--title", default="", help="商户展示名，默认同商户码")
    ap.add_argument("--access", choices=["guest", "auth"], default="auth",
                    help="客户端访问模式：auth=必须登录（正式商户用这个）")
    ap.add_argument("--client-name", default="", help="客户端名，默认 <码>")
    ap.add_argument("--account-name", default="", help="首个应用账号，默认 <码>-admin")
    ap.add_argument("--account-label", default="", help="账号显示名（谁在用这个端）")
    ap.add_argument("--database-hint", default="", help="账套名提示（装机时照它填 sql.database）")
    ap.add_argument("--databases", default="",
                    help="该商户**允许读**的账套清单，逗号或空格分隔，第一本是默认库。"
                         "一台主机两本账的老板用它登记第二本；给了它就优先于 --database-hint")
    ap.add_argument("--base", default=BASE_DEFAULT)
    ap.add_argument("--relay", default=RELAY_DEFAULT)
    ap.add_argument("--out", default=OUT_DIR, help="出包目录（默认仓库外 ~/.jxc-provision）")
    ap.add_argument("--from-ssh", action="store_true", help="没有 JXC_ADMIN_PWD 时从云端配置读一次")
    ap.add_argument("--dry-run", action="store_true", help="只做冲突检查与打印，不写任何东西")
    args = ap.parse_args()

    code = args.merchant.strip().lower()
    if not CODE_RE.match(code):
        print("✗ 商户码不合法：%r（要小写字母开头，可含数字与 -，2-30 位）" % args.merchant)
        return 4
    title = (args.title or code).strip()
    client_name = (args.client_name or code).strip()
    account_name = (args.account_name or (code + "-admin")).strip()
    account_pw = rand_pw()

    pw, how = admin_password(args)
    if not pw:
        print("✗ 没有后台密码：设 JXC_ADMIN_PWD 环境变量，或加 --from-ssh（当前：%s）" % how)
        return 4
    api = Api(args.base)
    ok, cookie = api.login(pw)
    if not ok:
        print("✗ 后台登录失败（密码不对或被限流）")
        return 3
    api.cookie = cookie

    st = state_of(api)
    merchants = {m["code"]: m for m in (st.get("merchants") or [])}
    agents = {a["name"] for a in (st.get("agents") or [])}
    clients = {c.get("name") for c in (st.get("clients") or [])}
    accounts = {a.get("username") for a in (st.get("accounts") or [])}

    clash = ([("商户码", code, code in merchants)]
             + [("Agent 名", code, code in agents)]
             + [("客户端名", client_name, client_name in clients)]
             + [("账号", account_name, account_name in accounts)])
    clash = [(k, v) for k, v, hit in clash if hit]
    print("冲突检查：%s" % ("无占用，可以开通" if not clash else
                            "已被占用 " + "、".join("%s=%s" % c for c in clash)))
    if clash:
        print("★ 本脚本不覆盖已存在的同名条目（那正是把在线商户顶下线/锁死密钥的路径）。")
        print("  要给已有商户加东西，请直接去 /admin 或改用别的码。")
        return 2
    if args.dry_run:
        print("[dry-run] 将创建：商户 %s（%s）+ 同名 Agent + 客户端 %s（access=%s）+ 账号 %s"
              % (code, title, client_name, args.access, account_name))
        return 0

    print("\n[1/3] 建商户 + 同名 Agent")
    # databases 是**这家商户被允许读的账套清单**（第一本=默认库）。
    # 一台主机两本账的老板就传 --databases 一本,另一本；--database-hint 是它的单值写法。
    databases = ([d.strip() for d in (args.databases or "").replace("，", ",").split(",")
                  if d.strip()] or ([args.database_hint.strip()] if args.database_hint.strip() else []))
    st, body = api.call("/api/admin/merchants", {"code": code, "title": title,
                                                 "enabled": True,
                                                 "databases": databases})
    if st != 200 or not body.get("ok"):
        print("  ✗ %s %s" % (st, str(body)[:220]))
        return 3
    secret = body.get("agent_secret") or ""
    print("  ✓ 商户已建，Agent 密钥已生成（只回显这一次，只进出包文件）")

    print("[2/3] 建客户端")
    st, body = api.call("/api/admin/clients", {"name": client_name, "title": title,
                                               "agent": code, "access": args.access})
    if st != 200 or not body.get("ok"):
        print("  ✗ 客户端建失败 %s %s" % (st, str(body)[:220]))
        print("  ！此时商户与 Agent 已存在 —— 要么在后台删掉重来，要么手工补客户端")
        return 3
    client = body.get("client") or {}

    print("[3/3] 建首个应用账号")
    st, body = api.call("/api/admin/accounts", {
        "username": account_name, "password": account_pw,
        "name": args.account_label or (title + " 管理员"), "stores": [client_name],
        "all_stores": False})
    if st != 200:
        print("  ✗ 账号建失败 %s %s" % (st, str(body)[:220]))
        return 3

    bundle = {
        "code": code, "title": title, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        # 这一段直接给安装器/手工 config.json 用
        "agent_config": {"agent": {"name": code, "secret": secret, "relay_url": args.relay},
                         # 装机先给默认库；老板要看第二本由平台方切库（Step 2 之后按请求路由）
                         "sql": {"database": (databases or [""])[0]}},
        "client": {"name": client.get("name"), "token": client.get("token"),
                   "access": client.get("access")},
        "account": {"username": account_name, "password": account_pw,
                    "note": "首次登录后请在后台改密；这条口令是本脚本随机生成的"},
        "h5": {"url": "http://149.88.75.125/%s/" % code,
               "publish": "python 进销存-uniapp/scripts/deploy.py --merchant %s --no-build" % code},
    }
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "merchant-bundle-%s.json" % code)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        json.dump(bundle, fh, ensure_ascii=False, indent=2)

    print("\n开通完成 ✓")
    print("  出包（含密钥/令牌/口令，别提交、别外发）：%s" % path)
    print("  下一步（按顺序）：")
    print("    1) 把这家的 config.json 放到客户机 V3AgentGui.exe 同目录（用出包里 agent_config 那段）")
    print("    2) 看 发布程序/logs/agent.status.json 的 database_verified 是否 == 该账套")
    print("    3) /admin 确认**两家都在线**（一家在线不代表没事）")
    print("    4) 发布 H5 前缀：%s" % bundle["h5"]["publish"])
    print("    5) 跑负向测试：进销存-云端后台源码/scripts/check-tenancy-live.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
