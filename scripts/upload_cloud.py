# -*- coding: utf-8 -*-
"""上传 cloudapi 到云端 /opt/v3cloud 并重启 v3cloud（保留 cloud_config.json）。

★ 路径跟随脚本自身位置，**不写死盘符**（项目约定）。
   本脚本位于 `<ROOT>/scripts/`，ROOT 即云端源码根。

用法：
  python scripts/upload_cloud.py                 # 上传全部 .py + web/admin.html 并重启
  python scripts/upload_cloud.py --only business.py   # 只传一个文件（快，调试用）
  python scripts/upload_cloud.py --no-restart     # 只传不重启
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLOUDAPI = os.path.join(ROOT, "cloudapi")
sys.path.insert(0, HERE)
import sshtarget  # noqa: E402

HOST = sshtarget.HOST
USER = sshtarget.USER
REMOTE = "/opt/v3cloud/cloudapi"


def run(cli, cmd, timeout=60):
    return sshtarget.run(cli, cmd, timeout)[1].strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="只上传指定文件名")
    ap.add_argument("--no-restart", action="store_true")
    args = ap.parse_args()

    cli = sshtarget.connect(timeout=15)
    sftp = cli.open_sftp()

    if args.only:
        names = [args.only]
    else:
        names = [n for n in sorted(os.listdir(CLOUDAPI)) if n.endswith(".py")]

    sent = []
    for name in names:
        local = os.path.join(CLOUDAPI, name)
        if not os.path.isfile(local):
            print("  ! 跳过（不存在）：%s" % name)
            continue
        sftp.put(local, "%s/%s" % (REMOTE, name))
        sent.append(name)
        print("  ↑ %s" % name)

    # web/admin.html（有则一起更新）
    admin_local = os.path.join(CLOUDAPI, "web", "admin.html")
    if not args.only and os.path.isfile(admin_local):
        sftp.put(admin_local, "%s/web/admin.html" % REMOTE)
        sent.append("web/admin.html")
        print("  ↑ web/admin.html")

    sftp.close()
    print("已上传 %d 个文件" % len(sent))

    if not args.no_restart:
        print(run(cli, "systemctl restart v3cloud"))
        time.sleep(3)
        print("health: %s" % run(cli, "curl -s http://127.0.0.1:8000/api/v1/health"))
        print("purchases 路由: %s" % run(
            cli, "curl -s -o /dev/null -w '%{http_code}' "
                 "-X POST http://127.0.0.1:8000/api/v1/purchases -H 'Content-Type: application/json' -d '{}'"))

    # ★ 反向校验：线上文件 md5 必须与本地产物一致
    print("--- md5 对照 ---")
    for name in sent:
        if "/" in name:
            continue
        out = run(cli, "md5sum %s/%s | awk '{print $1}'" % (REMOTE, name))
        import hashlib
        local_md5 = hashlib.md5(open(os.path.join(CLOUDAPI, name), "rb").read()).hexdigest()
        flag = "✓" if out == local_md5 else "✗ 不一致"
        print("  %-24s 线上=%s 本地=%s %s" % (name, out[:12], local_md5[:12], flag))

    cli.close()


if __name__ == "__main__":
    main()
