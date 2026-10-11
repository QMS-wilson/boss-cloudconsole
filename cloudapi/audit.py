# -*- coding: utf-8 -*-
"""审计落盘与读取（Phase 2/5 交汇处的公共件）。

★ 为什么要单独一个模块：以前 `_audit` 是 server.py create_app 里的闭包，
  结果 business.py（H5/小程序真正走的 30 多个 REST 端点）**根本没法调用它** ——
  于是"客户在 App 里干了什么"这条线一条都不留，只有信封式 /api/v1/rpc 有记录。
  审计补齐（Phase 2）与按日切分（Phase 5）都得先把写入口公共化。

文件：`<log_dir>/audit-YYYY-MM-DD.jsonl`（本地日期，运营排障按"今天谁动了"读，
不再每次全量重读一份只增不减的大文件）；历史遗留的 `audit.jsonl` 仍然可读。
"""
import json
import os
import time

DAY_FMT = "%Y-%m-%d"


def log_dir(cfg):
    return (cfg or {}).get("log_dir") or "logs"


def day_file(cfg, day=None):
    return os.path.join(log_dir(cfg), "audit-%s.jsonl" % (day or time.strftime(DAY_FMT)))


LEGACY_FILE = "audit.jsonl"


def write(cfg, entry):
    """追加一条审计。**写失败绝不阻断业务**（磁盘满/只读挂载时宁可少一条记录）。"""
    d = log_dir(cfg)
    try:
        os.makedirs(d, exist_ok=True)
        with open(day_file(cfg), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _iter_lines(paths):
    for p in paths:
        if not p or not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue        # 半行/坏行跳过，不要让一条脏数据毁掉整个看板
        except OSError:
            continue


def read(cfg, days=1, include_legacy=False):
    """读最近 N 天（默认今天）。include_legacy=True 时把切分前的 audit.jsonl 也带上。"""
    names = [time.strftime(DAY_FMT, time.localtime(time.time() - i * 86400))
             for i in range(max(1, int(days or 1)))]
    paths = [day_file(cfg, n) for n in names]
    if include_legacy:
        paths.append(os.path.join(log_dir(cfg), LEGACY_FILE))
    return list(_iter_lines(paths))


def count_day(cfg, day=None):
    """某一天的条数（看板分桶用；不解析 JSON，坏行也计数，图不会空）。"""
    p = day_file(cfg, day)
    if not os.path.isfile(p):
        return 0
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


def latest(cfg, limit=300, days=7):
    """最近 limit 条（按天从新到旧读够了就停，避免整天文件都扫一遍）。"""
    out = []
    for i in range(max(1, int(days or 1))):
        day = time.strftime(DAY_FMT, time.localtime(time.time() - i * 86400))
        rows = list(_iter_lines([day_file(cfg, day)]))
        out.extend(rows)
        if len(out) >= limit:
            break
    if len(out) < limit:
        p = os.path.join(log_dir(cfg), LEGACY_FILE)
        if os.path.isfile(p):
            out = list(_iter_lines([p])) + out
    return out[-limit:]
