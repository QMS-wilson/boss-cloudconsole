# -*- coding: utf-8 -*-
"""只更新 /opt/v3cloud/cloudapi/business.py 并重启（带 md5 双边校验 + py_compile）。

用法：python scripts/deploy_business.py
"""
import hashlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sshtarget  # noqa: E402

HOST = sshtarget.HOST
USER = sshtarget.USER
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL = os.path.join(ROOT, "cloudapi", "business.py")
REMOTE = "/opt/v3cloud/cloudapi/business.py"


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cli, cmd, timeout=90):
    code, out, err = sshtarget.run(cli, cmd, timeout)
    return out.strip(), err.strip()


def main():
    local_md5 = md5(LOCAL)
    print("本地 business.py md5 =", local_md5)

    cli = sshtarget.connect(timeout=20)

    sftp = cli.open_sftp()
    sftp.put(LOCAL, REMOTE)
    sftp.close()
    print("上传完成")

    o, e = run(cli, "md5sum " + REMOTE)
    print("远端 md5 =", o.split()[0] if o else o)
    remote_md5 = o.split()[0] if o else ""

    o, e = run(cli, "/opt/v3cloud/venv/bin/python -m py_compile " + REMOTE + " && echo COMPILE_OK")
    print("py_compile:", o or e)

    o, e = run(cli, "systemctl restart v3cloud && sleep 3 && systemctl is-active v3cloud")
    print("service:", o or e)

    o, e = run(cli, "curl -s -m 10 http://127.0.0.1:8000/api/v1/health")
    print("health:", o or e)

    # 确认 504 分支已上线
    o, e = run(cli, "grep -c 'AGENT_TIMEOUT' " + REMOTE)
    print("远端含 AGENT_TIMEOUT 次数 =", o)

    cli.close()

    ok = (local_md5 == remote_md5)
    print()
    print("md5 双边一致:", "PASS ✓" if ok else "FAIL ✗")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
