#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""云端 cloudapi 增量上线（多文件 + 先备份 + 失败自动还原）。

★ 为什么另起一支而不是用现成的：
  · `deploy_cloud.py` / `deploy_cloud_update.py` **绝不能跑** —— 它们会
    `secrets.token_*` 重新生成 ADMIN_PWD / AGENT_SECRET / CLIENT_TOKEN 并重写
    cloud_config.json ⇒ 现网密钥全废。
  · `deploy_business.py` 只认 business.py 一个文件，而 P1-1~P1-3 一次要发 7 个 .py。
  这支脚本只做"按 md5 差异上传指定文件"，**不碰配置文件、不生成任何密钥**。

用法：
    python scripts/deploy_cloudapi.py                 # 只报告差异（不写远端）
    python scripts/deploy_cloudapi.py --apply         # 备份→上传→py_compile→重启 v3cloud→校验
    python scripts/deploy_cloudapi.py --apply --files cloudapi/security.py cloudapi/web/admin.html
    python scripts/deploy_cloudapi.py --restore <备份目录名>   # 出事就整目录还原

★ 默认比对范围：cloudapi/*.py 与 cloudapi/web/admin.html。
  `cloud_config.json` / `logs/` 永远不在范围内（密钥与审计都在里面）。
"""
import argparse
import hashlib
import os
import posixpath
import re
import shlex
import sys
import time

import paramiko

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sshtarget  # noqa: E402

HOST, USER = sshtarget.HOST, sshtarget.USER
REMOTE_ROOT = "/opt/v3cloud"
REMOTE_API = REMOTE_ROOT + "/cloudapi"
VENV_PY = REMOTE_ROOT + "/venv/bin/python"
SERVICE = "v3cloud"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FORBIDDEN = ("cloud_config.json", "/logs/", ".bak", "backup")


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def connect():
    return sshtarget.connect()


def run(cli, cmd, timeout=120):
    _i, out, err = cli.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", "replace").strip()
    e = err.read().decode("utf-8", "replace").strip()
    return out.channel.recv_exit_status(), o, e


def candidates(only):
    """本地相对路径列表。只认 .py 与后台页面文件，且绝不碰密钥/审计。

    ★ 2026-09-26 后台从单个 admin.html 拆成了 `cloudapi/web/admin/` 多文件
      （原生 ES module），所以这里必须递归收目录 —— 只发 index.html 的话，
      浏览器会拿到 404 的 app.js，症状是"页面全白"，比不部署还难查。
    """
    rels = []
    if only:
        rels = list(only)
    else:
        base = os.path.join(ROOT, "cloudapi")
        for n in sorted(os.listdir(base)):
            if n.endswith(".py"):
                rels.append("cloudapi/" + n)
        # 旧版整页仍留在服务器上（/admin/legacy 是回退路径），本地已改名，不再比对
        legacy = os.path.join(base, "web", "admin_legacy.html")
        if os.path.isfile(legacy):
            rels.append("cloudapi/web/admin_legacy.html")
        admin_dir = os.path.join(base, "web", "admin")
        for cur, _dirs, files in os.walk(admin_dir):
            for fn in sorted(files):
                if fn.endswith((".html", ".js", ".css")):
                    rels.append(os.path.relpath(os.path.join(cur, fn), ROOT)
                                .replace("\\", "/"))
    out = []
    for r in rels:
        r = r.replace("\\", "/").lstrip("./")
        if any(b in r for b in FORBIDDEN):
            print("  ! 拒绝处理 %s（配置文件/日志/备份不在本脚本职责内）" % r)
            continue
        if not os.path.isfile(os.path.join(ROOT, r)):
            print("  ! 本地没有 %s" % r)
            continue
        out.append(r)
    return out


def remote_md5s(cli, rels):
    """一次 ssh 往返取回所有远端 md5（- 表示不存在）"""
    paths = " ".join(posixpath.join(REMOTE_ROOT, r) for r in rels)
    code, o, _ = run(cli, "for f in %s; do if [ -f $f ]; then md5sum $f; "
                          "else echo \"- $f\"; fi; done" % paths)
    got = {}
    for line in o.split("\n"):
        parts = line.split(None, 1)
        if len(parts) == 2:
            got[parts[1].replace(REMOTE_ROOT + "/", "")] = parts[0]
    return got


def diff_set(cli, rels):
    local = {r: md5(os.path.join(ROOT, r)) for r in rels}
    remote = remote_md5s(cli, rels)
    out = []
    for r in rels:
        rm = remote.get(r)
        if rm == local[r]:
            print("  = %-34s 远端已是这一份" % r)
        else:
            print("  ✗ %-34s 本地 %s / 远端 %s" % (r, local[r][:8], (rm or "(无)")[:8]))
            out.append((r, local[r]))
    return out


def backup(cli):
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = "%s.bak-%s" % (REMOTE_API, ts)
    code, o, e = run(cli, "cp -a %s %s && du -sh %s && echo BACKUP_OK" % (REMOTE_API, dst, dst))
    print("  备份：%s  %s" % (dst, (o.splitlines() or [""])[0]))
    return (dst, code == 0 and "BACKUP_OK" in o)


def upload(cli, rels_with_md5):
    stamp = str(int(time.time()))
    sftp = cli.open_sftp()
    ok = True
    for r, want in rels_with_md5:
        lp = os.path.join(ROOT, r.replace("/", os.sep))
        rp = posixpath.join(REMOTE_ROOT, r)
        tmp = "%s.new-%s" % (rp, stamp)
        # ★ 必须先建目录再 put：sftp 不会自动创建父目录。
        #   后台拆成 web/admin/views/*.js 之后这条从"可选"变成"必须" ——
        #   第一版就是在这里抛 FileNotFoundError，把上线拦腰截断在"部分文件已换、
        #   服务还没重启"的中间态（那种状态下一旦进程重启就会因缺模块起不来）。
        run(cli, "mkdir -p \"%s\"" % posixpath.dirname(rp))
        try:
            sftp.put(lp, tmp)
        except Exception as exc:  # noqa: BLE001
            print("  ✗ 上传 %s 失败：%s" % (r, exc))
            ok = False
            continue
        code, o, _ = run(cli, "mv -f %s %s && md5sum %s" % (tmp, rp, rp))
        got = o.split()[0] if o else ""
        hit = got == want
        ok = ok and hit
        print("  %s 上传 %s（md5 %s）" % ("✓" if hit else "✗", r, got[:8] or "取不到"))
    sftp.close()
    return ok


def gate(cli):
    """远端整包 py_compile —— 语法错的 .py 会让服务起不来，必须在重启前拦住"""
    code, o, e = run(cli, "cd %s && %s -m py_compile cloudapi/*.py && echo COMPILE_OK"
                     % (REMOTE_ROOT, VENV_PY))
    print("  py_compile:", o or e or "(无输出)")
    return code == 0 and "COMPILE_OK" in o


def agents_online(body):
    """兼容两种 health 形状：
       旧：{"ok":true,"agents":{"名":{"online":true,...}}}   （会泄露租户名单）
       新：{"ok":true,"agents":<在线数>}                    （P1-1 已收口）
    ★ 别再只找 '"online":true' —— 新形状里根本没有这个串，
      上一次上线就是因此把成功的部署判成失败、自动回滚了一遍。"""
    import json
    try:
        d = json.loads(body)
    except Exception:
        return -1
    if not d.get("ok"):
        return -1
    a = d.get("agents")
    if isinstance(a, int):
        return a
    if isinstance(a, dict):
        return sum(1 for v in a.values() if isinstance(v, dict) and v.get("online"))
    return -1


def restart_and_check(cli):
    # ★ 重启**前**先取一次在线数：它决定"重启成功"该怎么验。
    #   以前这里无条件要求"60 秒内有 Agent 重连"，而安静时段（客户机没开机、无人值守窗口）
    #   本来一家都不在线 —— 那次上线就是这么把一份没问题的改动自动回滚掉的（2026-10-05）。
    #   判据不能依赖一个当前合法地不存在的东西。
    _, hb, _ = run(cli, "curl -s -m 8 http://127.0.0.1:8000/api/v1/health")
    before = agents_online(hb)
    code, o, e = run(cli, "systemctl restart %s && sleep 3 && systemctl is-active %s"
                     % (SERVICE, SERVICE))
    active = (o or e).strip().splitlines()[-1] if (o or e) else ""
    print("  服务状态:", active or e)
    if active != "active":
        run(cli, "journalctl -u %s --no-pager -n 25 -o cat" % SERVICE)
        return False
    if before >= 1:
        # 重启前有人在用：Agent 是 5s 重连，给到 60s 确认它回来了（这条能挡住"进程活着但
        # 注册表坏了"这种半死状态，保留原样）
        for i in range(12):
            _, o2, _ = run(cli, "curl -s -m 8 http://127.0.0.1:8000/api/v1/health")
            n = agents_online(o2)
            if n >= 1:
                print("  ✓ 重启前在线 %d 家，%ds 后 health 报告在线 %d 家" % (before, 3 + i * 5, n))
                print("    " + o2[:200])
                return True
            time.sleep(5)
        print("  ✗ 重启前在线 %d 家，但 60s 内一家都没重连回来，最后一次的 health：" % before)
        print("    " + (o2 or "(空)")[:300])
        run(cli, "journalctl -u %s --no-pager -n 30 -o cat | grep -iE 'error|traceback|上线|离线' | tail -15"
            % SERVICE)
        return False

    # 重启前就是 0 家（或 health 读不出数）：改验"进程真的在处理请求"——
    # health 形状可读，且一个要鉴权的接口回 401（不是 500、不是连不上）。
    print("  i 重启前 0 家在线（无人时段/客户机没开机）：不要求 Agent 重连，改验服务本身在应答")
    for i in range(6):
        _, hb2, _ = run(cli, "curl -s -m 8 http://127.0.0.1:8000/api/v1/health")
        if agents_online(hb2) >= 0:
            _, st, _ = run(cli, "curl -s -o /dev/null -w '%{http_code}' -m 8 "
                                "http://127.0.0.1:8000/api/v1/auth/me")
            st = (st or "").strip()
            if st == "401":
                print("  ✓ health 可读（在线 %d 家）、未带令牌访问 /auth/me 回 401 —— 进程在服务"
                      % agents_online(hb2))
                return True
            print("  ✗ /auth/me 回 %s（期望 401）：服务没真正起来" % (st or "空"))
            break
        time.sleep(5)
    print("  ✗ 校验没过，最后一次的 health：\n    " + (hb2 or "(空)")[:300])
    run(cli, "journalctl -u %s --no-pager -n 30 -o cat | grep -iE 'error|traceback|上线|离线' | tail -15"
        % SERVICE)
    return False


def restore(cli, name):
    src = name if name.startswith("/") else posixpath.join(REMOTE_ROOT, name)
    # ★★ 这条命令以 **root** 跑 `rm -rf`，所以路径必须先"长对"再拼进命令（2026-10-12 修）。
    #   原来两处都不加引号：目录名里带空格会把参数拆断，带 `;` / `&&` 就是命令注入，
    #   而它前面正好是一条 rm -rf /opt/v3cloud/cloudapi。
    #   备份目录是本脚本自己建的（`cloudapi.bak-<时间戳>`），就按那个形状卡死 ——
    #   不匹配直接停手，别拿"运维大概不会乱填"当输入校验。
    base = posixpath.basename(src.rstrip("/"))
    ok_shape = (posixpath.dirname(src.rstrip("/")) == REMOTE_ROOT
                and re.match(r"^cloudapi\.bak-[0-9]{8}-[0-9]{6}$", base or ""))
    if not ok_shape:
        print("  ✗ 拒绝还原：%r 不是一个本脚本产出的备份目录"
              "\n    备份必须长成 %s/cloudapi.bak-YYYYMMDD-HHMMSS（先 --list-backups 看有哪些）"
              % (name, REMOTE_ROOT))
        return False
    src = posixpath.join(REMOTE_ROOT, base)
    q = shlex.quote
    code, o, e = run(cli, "test -d %s && rm -rf %s && cp -a %s %s && echo RESTORED"
                     % (q(src), q(REMOTE_API), q(src), q(REMOTE_API)))
    print("  还原:", o or e)
    if "RESTORED" in o:
        code, o, e = run(cli, "systemctl restart %s && sleep 3 && systemctl is-active %s"
                         % (SERVICE, SERVICE))
        print("  重启后:", o or e)
        return "active" in (o + e)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写远端（默认只报告差异）")
    ap.add_argument("--files", nargs="*", help="只发这几个相对路径（默认全量比对 cloudapi/*.py + admin.html）")
    ap.add_argument("--restore", metavar="BACKUP_DIR", help="用指定备份目录整份还原并重启")
    ap.add_argument("--no-restart", action="store_true", help="只上传不重启（配合手工分批验证）")
    args = ap.parse_args()

    cli = connect()
    print("✓ SSH %s@%s" % (USER, HOST))
    rc = 0
    try:
        if args.restore:
            return 0 if restore(cli, args.restore) else 1

        rels = candidates(args.files)
        print("比对范围 %d 个文件：" % len(rels))
        todo = diff_set(cli, rels)
        if not todo:
            print("\n远端与本地一致，无需上线（no-op）")
            return 0
        print("\n待上线 %d 个文件：%s" % (len(todo), [r for r, _ in todo]))
        if not args.apply:
            print("！未带 --apply：只报告，什么都没改")
            return 0

        print("\n[1] 备份")
        dst, ok = backup(cli)
        if not ok:
            print("✗ 备份失败，放弃上线（没有回退路径就不动现网）")
            return 1
        print("\n[2] 上传")
        if not upload(cli, todo):
            print("✗ 上传后 md5 不匹配，还原备份")
            restore(cli, dst)
            return 1
        print("\n[3] 远端语法门")
        if not gate(cli):
            print("✗ py_compile 没过，还原备份")
            restore(cli, dst)
            return 1
        if args.no_restart:
            print("\n[4] 跳过重启（--no-restart）。生效前请手工 systemctl restart %s" % SERVICE)
            print("    备份留在 %s" % dst)
            return 0
        print("\n[4] 重启并校验")
        if not restart_and_check(cli):
            print("✗ 重启后校验没过 → 还原备份并再重启")
            restore(cli, dst)
            return 1
        print("\n部署完成 ✓  备份留在 %s（要回退：--restore %s）" % (dst, dst))
    finally:
        cli.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
