# -*- coding: utf-8 -*-
"""按 (客户端, op) 的令牌桶限流（Phase 5）。

★ 为什么必须有：整个云端此前**零限流**。多商户共用一个云端之后，一家脚本
  （或一个狂点的用户）可以把 uvicorn 的协程、Agent 的连接和 SQL Server 的
  并发一起打满，表现就是"另一家打开转圈转半天"。平台侧必须能明确回
  429 并给出 Retry-After，而不是让所有人一起慢。

默认值取的是"正常人手点不会碰到、脚本刷立刻撞上"的量：稳态 8 次/秒、
突发 20 次。要按商户放宽就在配置里写 `rate_limit: {rate, burst}`。
"""
import time

BUCKETS = {}          # (client, op) -> [tokens, last_ts]
REJECTS = {}          # (client, op) -> 累计被拒次数（监控页要看得见，不能只回 429 就完事）
DEFAULT_RATE = 8.0    # 每秒补充多少
DEFAULT_BURST = 20    # 桶容量（突发上限）
MAX_KEYS = 5000       # 键数上限，防止有人用海量 op 名把内存吃了


def allow(key, rate=None, burst=None):
    """返回 (是否放行, 建议等待秒数)。"""
    rate = float(rate or DEFAULT_RATE)
    burst = float(burst or DEFAULT_BURST)
    now = time.time()
    tk, ts = BUCKETS.get(key, (burst, now))
    tk = min(burst, tk + (now - ts) * rate)
    if tk < 1:
        BUCKETS[key] = (tk, now)
        REJECTS[key] = REJECTS.get(key, 0) + 1
        return False, max(0.1, (1 - tk) / rate)
    BUCKETS[key] = (tk - 1, now)
    if len(BUCKETS) > MAX_KEYS:
        # 只留最近还在动的键；老键留着没意义（下次请求会重新按满桶起算）
        cutoff = now - 300
        for k in [k for k, (t, s) in BUCKETS.items() if s < cutoff][:MAX_KEYS // 2]:
            BUCKETS.pop(k, None)
        # ★ REJECTS 必须跟着清（2026-10-12）：原先只有 BUCKETS 有上限，
        #   REJECTS 是**只增不减**的 —— 每一个被拒过的 (客户端, op) 组合都永久留在内存里。
        #   它唯一的用处是让监控页看见"这家被拒过几次"，不值得用无界内存去换。
        #   先算列表再删，不许在迭代 dict 的同时改它。
        for k in [k for k in REJECTS if k not in BUCKETS]:
            REJECTS.pop(k, None)
    return True, 0.0


def key_of(client, op, params=None):
    """桶按 (客户端, op) 分。params 不进键 —— 否则每次改日期就换一个桶，形同不限。"""
    return (str((client or {}).get("name") or "?"), str(op or "?"))


def snapshot(top=10):
    """给后台监控页：累计被限流次数 + 最热的几个 (客户端, op)。

    ★ 为什么值得记：限流是"静默生效"的保护 —— 回 429 之后前端只是重试或转圈，
      运营者如果看不见"谁家被限了几次"，就会把"某家页面变慢"错判成"云端坏了"。
    """
    items = sorted(REJECTS.items(), key=lambda kv: -kv[1])[:int(top)]
    return {"total": sum(REJECTS.values()),
            "top": [{"client": k[0], "op": k[1], "count": v} for k, v in items]}
