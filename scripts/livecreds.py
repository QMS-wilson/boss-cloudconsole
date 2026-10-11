# -*- coding: utf-8 -*-
"""线上验收脚本用的应用账号（**不落仓库**）。

★ 2026-09-26：同一个应用账号口令此前被复制进 50 个脚本（含 40 多个线上门），
  任何一个文件被贴进群/问答/备份都是一次泄露。口令取值顺序：
  环境变量 JXC_APP_PWD → ~/.ssh/jxc_app_pw → 都没有就报清楚怎么补。
  轮换口令时只改那一处文件，脚本一行都不用动。
"""
import os

ACCOUNT = os.environ.get("JXC_APP_ACCOUNT", "system")   # 默认值别漂：shiwang 已在 2026-09-26 清场时删除
# ★ 三份 livecreds（云端/uniapp/Agent）此前默认值不一致，Agent 那份是 system、
#   这两份还写着已删的 shiwang ⇒ 用它的门（probe-api 等）当场报 ACCOUNT_INVALID，
#   看着像"线上账号又坏了"。改任何一份都要另两份对着看。


def app_password():
    pw = (os.environ.get("JXC_APP_PWD") or "").strip()
    if not pw:
        p = os.path.expanduser("~/.ssh/jxc_app_pw")
        try:
            pw = open(p, encoding="utf-8").read().strip()
        except OSError:
            pw = ""
    if not pw:
        raise SystemExit("缺应用账号口令：设 JXC_APP_PWD 环境变量，或写 %s"
                         % os.path.expanduser("~/.ssh/jxc_app_pw"))
    return pw


APP_PASSWORD = app_password()
