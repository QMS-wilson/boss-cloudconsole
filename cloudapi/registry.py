# -*- coding: utf-8 -*-
"""本地 Agent 连接注册表与 RPC 转发。

每个客户的本地 Agent 主动连上来（hello），云端把小程序请求按 agent 名转发，
收到响应后按 msg id 匹配回给 HTTP 调用方。
"""

import asyncio
import collections
import json
import logging
import os
import time
import uuid

from . import config as cfgmod

log = logging.getLogger("cloud.registry")


class AgentOffline(Exception):
    pass


class AgentBadSecret(Exception):
    pass


class AgentNameConflict(Exception):
    """同名但来自另一台安装：先到的连接继续服务，拒绝后来者（见 register）。"""

    def __init__(self, name, holder=""):
        Exception.__init__(self, "agent name %s is held by another install" % name)
        self.name = name
        self.holder = holder


class AgentBusy(Exception):
    """某一家某一类的在途已满、且等过 `wait_sec` 仍没让出位来。回 429。

    ★ 带 `cls` 是为了把话说到点上：被打印挡住和被报表挡住，处置完全不同
      （前者等一下就好，后者是"你同时在查太多张大表"）。以前只有一句"本地服务正忙"，
      客户以为服务坏了来找平台方，而真相只是排队。
    """

    def __init__(self, name, inflight=0, cls="quick", waited_ms=0, retry_after=2):
        Exception.__init__(self, "agent %s busy in class %s (%d in flight)" % (name, cls, inflight))
        self.name = name
        self.inflight = inflight
        self.cls = cls
        self.waited_ms = waited_ms
        self.retry_after = max(1, int(retry_after))


def busy_message(exc):
    """429 的人话 —— **按类说**，别再说成同一句"本地服务正忙"。

    ★ 三种"忙"的处置完全不同：报表在跑（等一下就好）、打印机没吐完（看是不是卡纸）、
      在途真的满了（多半是某个页面在刷屏）。以前一句混着，客户只会认为"服务坏了"，
      平台方也拿不到方向（2026-09-26 那轮误伤就是这么来的）。
    """
    wait = max(1, int(getattr(exc, "retry_after", 2) or 2))
    cls = getattr(exc, "cls", "quick")
    if cls == "report":
        return ("上一张报表还在算（本地数据库同时只扛得住几张宽表查询），"
                "请 %d 秒后再点" % wait)
    if cls == "print":
        return ("上一个打印任务还没跑完（在等打印机/渲染），请 %d 秒后再试" % wait)
    return "本地服务正忙（在途 %d 条），请 %d 秒后再试" % (getattr(exc, "inflight", 0), wait)


# 兜底默认值：真源是 cfg 的 `max_inflight` 与 `inflight_by_class`
# （见 config.admission_for —— 分级并发的理由写在那边）。这里只在配置被删空时兜住，
# 绝不再当第二处"实际生效的上限"，否则改配置不生效、排查全白干。
MAX_INFLIGHT_PER_AGENT = 24


# P1-3 同名准入已落地；verify-tenancy.py 靠这个开关决定是否跑第 8 组断言
SUPPORTS_INSTALL_ID = True

# 离线超过这么久，指纹自动作废：换机/重装可以不经批准直接接管
# （不然客户机器硬盘坏了重装一次就要等运营者点按钮，那是人造停机）
STALE_INSTALL_SEC = 24 * 3600


class AgentConn:
    def __init__(self, ws, hello, source="ws"):
        self.ws = ws
        self.name = str(hello.get("agent") or "")
        self.version = str(hello.get("version") or "")
        self.ops = set(hello.get("ops") or [])
        self.source = source
        # 装机指纹：Agent 首次启动时生成一次、写进自己的 config.json。
        # 有了它才能区分"这台机器网络抖了一下重连"（同名同指纹，放行）
        # 和"另一台机器拿着同一份密钥上线"（同名不同指纹，会把在线那家顶掉）。
        self.install_id = str(hello.get("install_id") or "").strip()
        self.hostname = str(hello.get("hostname") or "").strip()
        # ★ B3（2026-09-26）：账套名与**实测**账套名。
        #   09-26 00:38 那次事故的形态是"名字叫 A、实际读 B"，而云端两边都看不见 ——
        #   实测值当时只写在客户机本地的 status.json 里。从这一版起它随握手与心跳上报，
        #   后台能直接把"不一致"标红，不必登客户机查。
        self.database = str(hello.get("database") or "").strip()
        self.database_verified = str(hello.get("database_verified") or "").strip()
        # ★ Step 2：客户机**自己放行**的账套清单与实际服务过的账套。
        #   云端 merchants[码].databases 是"平台允许这家有哪几本"，
        #   这一份是"那台机器上的进程允许被读到哪几本"。两边都放行才真的读得到，
        #   差集就是装机现场最容易卡住的地方（后台看着登记好了、请求却被 Agent 拒）。
        self.allowed_databases = [str(x).strip() for x in (hello.get("allowed_databases") or [])
                                  if str(x or "").strip()]
        self.served_databases = [str(x).strip() for x in (hello.get("served_databases") or [])
                                 if str(x or "").strip()]
        self.status_at = time.time() if self.database_verified else 0.0
        self.uptime_sec = hello.get("uptime_sec") or 0
        # 后台调试台用：每个动作的中文说明与示例参数（Agent 自报，与 ops 同源，不会漂移）
        self.op_docs = hello.get("op_docs") if isinstance(hello.get("op_docs"), dict) else {}
        self.connected_at = time.time()


class _Gate:
    """一家一个在途闸门：**先等一会儿，等不到才拒**。

    ★ 为什么不是 asyncio.Semaphore：Semaphore 会**先占位再等**（FIFO 排队），
      一条跑 90s 的报表能让后面 15 条轻查询全挂在队里 —— 那正是用户抱怨的
      "点什么都转圈"。这里改成"只在有余量时才占位"：满就先回 `Retry-After`，
      让调用方自己决定要不要重来，云端不留排队大队。
    ★ 为什么分级：打印/报表这类长 op 与几十毫秒的轻 op 抢同一组位，
      结果是一条打印就能把一家"看起来弄坏了"（2026-09-26 实测就是这现象）。
    ★ 为什么轮询而不是 Condition/Event：等待者数量本来就个位数，
      0.15s + 抖动的粒度换来的是"任何时刻读 counts 都等于真实在途数"，
      不依赖谁忘了 notify（那是另一种"看着正常其实卡死"的故障）。
    """

    TICK = 0.15

    def __init__(self, table):
        self.table = table or {}          # {"report": {"cap":3,"wait_sec":8}, ..., "_total": {...}}
        self.counts = {}                  # 类 -> 当前在途数（含 "_total"）
        self.cond = asyncio.Condition()

    def _cap(self, cls):
        info = self.table.get(cls) or self.table.get("_total") or {"cap": 1}
        return max(1, int(info.get("cap") or 1))

    def _wait(self, cls):
        info = self.table.get(cls) or {}
        return max(0.0, float(info.get("wait_sec") or 0))

    async def acquire(self, cls):
        """拿到位返回 True；等不到返回 False（调用方负责计一次拒绝）。"""
        waited = 0.0
        limit = self._wait(cls)
        async with self.cond:
            while True:
                if (self.counts.get(cls, 0) < self._cap(cls)
                        and self.counts.get("_total", 0) < self._cap("_total")):
                    self.counts[cls] = self.counts.get(cls, 0) + 1
                    self.counts["_total"] = self.counts.get("_total", 0) + 1
                    return True
                if waited >= limit:
                    return False
                nap = min(self.TICK + (hash(cls) % 7) / 100.0, limit - waited)
                try:
                    await asyncio.wait_for(self.cond.wait(), timeout=max(0.01, nap))
                except asyncio.TimeoutError:
                    pass
                waited += max(0.01, nap)

    async def release(self, cls):
        async with self.cond:
            self.counts[cls] = max(0, self.counts.get(cls, 1) - 1)
            self.counts["_total"] = max(0, self.counts.get("_total", 1) - 1)
            self.cond.notify_all()

    def snapshot(self):
        return dict(self.counts)


class AgentNoDbRouting(Exception):
    """要点名账套，但那台客户机是**不认识这个功能的旧包**。

    ★★ 为什么必须有这道检查：老 Agent 收到带 `database` 的帧会**直接忽略这个键**，
      于是"读第二本账"的请求静默读了默认那本 —— 数字看着全正常，
      而这正是本项目反复出事的那一类（名字叫 A、连的是 B）。
      宁可回一个明确错误让人去换包，也绝不返回一份来自另一套账的"正确答案"。
    ★ 能力标志就是 hello/status 里上报的 `allowed_databases`（Step 2 起才有）。
    """

    def __init__(self, agent):
        super().__init__(f"agent {agent} 不支持按请求选账套")
        self.agent = agent


class AgentBadOp(Exception):
    """调用方请求了一个该 Agent 从未声明支持的 op。

    ★ 2026-09-25：Agent 上线时在 hello 里自报了 ops 清单（AgentConn.ops），
      但中转层以前是**盲转发** —— 任何 op 字符串都照发，云端等于对能力边界一无所知。
      现在把它变成双向白名单：没声明过的 op 直接拒，既挡住拼写错误，
      也让"新云端发了老 Agent 不认识的 op"这种版本漂移在第一时间暴露成明确错误。
    """


class AgentRegistry:
    def __init__(self, cfg):
        self.cfg = cfg
        self.agents = {}     # name -> AgentConn
        self.pending = {}    # msg_id -> (agent_name, asyncio.Future)
        self.inflight = {}   # name -> 正在飞的总条数（监控页读它；由闸门同步维护）
        self.gates = {}      # name -> _Gate（分级在途，见 config.admission_for）
        self.busy_rejects = {}   # name -> 因在途已满被拒的次数（监控页用，重启清零）
        self.last_seen = {}  # name -> unix（最近上线/离线/活跃时间）
        # name -> {"online": unix, "offline": unix}：最后一次上线/离线的**分开的**时刻。
        # ★ 为什么不能拿 last_seen 代替它：last_seen 在每条消息（含心跳）上都会刷，
        #   它回答的是"最后一次说话"，答不了"它是什么时候没的"。客户端要判
        #   「半小时前还在、现在没了」这种掉线，必须有两个分开的时刻。
        # ★ 为什么放内存而不是现读 agents.jsonl：`last_seen_versions()` 一次读 4000 行，
        #   给后台一页一刷没问题，但 /auth/me 是每个客户端 90 秒一次的轮询，不能那么花。
        self.last_events = {}
        # 运营者点了「接管」后，给这个名字一个短暂时窗允许换机上线
        self.takeover_until = {}   # name -> unix 截止
        self.conflicts = []        # 最近的同名冲突记录，供后台显示
        # HTTP 轮询模式（网络对 WS 不稳定时的备选传输）
        self.http_last = {}      # name -> unix 最近一次 poll
        self.http_meta = {}      # name -> {version, ops, connected_at}
        # ★ maxlen：Agent 长时间不 poll 时，待发队列必须有上界，否则一家断线
        #   就能让云端无限攒请求（内存无界 + 恢复后一次性灌回去）。
        self.http_queue = collections.defaultdict(lambda: collections.deque(maxlen=100))
        self.http_waiters = {}   # msg_id -> (agent_name, asyncio.Future)
        self.seed_last_events()

    def _check_secret(self, name, secret):
        agents = self.cfg["auth"].get("agents", {})
        return agents.get(name) is not None and str(agents.get(name)) == str(secret)

    def allow_takeover(self, name, seconds=180):
        """运营者在后台点「接管」→ 这个名字在时窗内允许换指纹上线（用一次即消耗）。"""
        self.takeover_until[name] = time.time() + max(30, int(seconds))
        return self.takeover_until[name]

    def _takeover_open(self, name):
        until = self.takeover_until.get(name)
        if not until:
            return False
        if time.time() > until:
            self.takeover_until.pop(name, None)
            return False
        return True

    def _note_conflict(self, rec):
        self.conflicts.append(rec)
        if len(self.conflicts) > 50:
            del self.conflicts[:-50]

    def _log_event(self, name, event, version="", install_id="", hostname="",
                   ops=0, source="ws", database="", database_verified=""):
        """把上下线事实落到 `logs/agents.jsonl`，专门用来回答**之后**的问题。

        ★ 为什么需要这本账：版本/op 数以前只存在于"此刻在线"的连接对象里，
          Agent 一离线，/admin 那行就变空白 —— 于是"灰度发了三家，还剩哪家没换包"
          这种最基本的问题都没法回答（Phase 4）。事件流留下 last-known，
          离线也看得见最后版本与最后时间。
        """
        entry = {"ts": time.time(), "agent": name, "event": event,
                 "version": version or "", "install_id": install_id or "",
                 "hostname": hostname or "", "ops": ops, "source": source,
                 "database": database or "", "database_verified": database_verified or ""}
        # ★ 内存这本账写在前面：agents.jsonl 写失败（磁盘满、权限漂了）是"记账丢了"，
        #   但不该顺带把 /auth/me 的在线时刻也掏空 —— 那会让客户端退回旧判据、
        #   把刚发生的这次掉线漏掉。两件事的后果不对等。
        if event in ("online", "offline"):
            self.last_events.setdefault(name, {})[event] = entry["ts"]
        d = (self.cfg or {}).get("log_dir") or "logs"
        try:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "agents.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 记账失败不能影响连接本身
            log.warning("agents.jsonl 写入失败（%s/%s）", event, name)

    def last_seen_versions(self, limit_lines=4000):
        """{agent: {version, build_id, install_id, hostname, ts, online}}，读尾部若干行。"""
        d = (self.cfg or {}).get("log_dir") or "logs"
        path = os.path.join(d, "agents.jsonl")
        out = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[-int(limit_lines):]
        except OSError:
            return out
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            name = e.get("agent")
            if not name:
                continue
            cur = out.get(name) or {}
            if e.get("event") == "online":
                cur.update({"version": e.get("version") or "",
                            "build_id": (e.get("version") or "").split("+")[-1],
                            "install_id": e.get("install_id") or "",
                            "hostname": e.get("hostname") or "",
                            "ops": e.get("ops") or 0,
                            "database": e.get("database") or "",
                            "database_verified": e.get("database_verified") or "",
                            "last_online": e.get("ts"), "online": True})
            else:
                cur.setdefault("version", "")
                cur["last_offline"] = e.get("ts")
                cur["online"] = False
            out[name] = cur
        return out

    def seed_last_events(self, limit_lines=4000):
        """启动时从事件流把 `last_events` 兜一次。

        ★ 不兜的话会有个具体的坏结果：云端一重启（发版、机器维护）所有家的
          "最后一次在线"全变空白，客户端就判不出"它重启前几分钟还在、现在没了"，
          白漏一条掉线通知 —— 而这段正是最容易掉链子的时刻。
        """
        for name, ev in self.last_seen_versions(limit_lines).items():
            self.last_events[name] = {"online": ev.get("last_online") or 0,
                                      "offline": ev.get("last_offline") or 0}
        return len(self.last_events)

    def last_events_for(self, name):
        """(最后一次上线, 最后一次离线)，unix 秒；没见过的事件给 0（不是 None，
        0 在两端都表示"没有这段历史"，省得每处判两种空）。"""
        ev = self.last_events.get(name) or {}
        return float(ev.get("online") or 0), float(ev.get("offline") or 0)

    async def register(self, ws, hello):
        name = str(hello.get("agent") or "").strip()
        if not self._check_secret(name, hello.get("secret") or ""):
            raise AgentBadSecret(name)
        old = self.agents.get(name)
        new_install = str(hello.get("install_id") or "").strip()
        if old is not None and old.ws is not ws:
            old_install = old.install_id or ""
            # ★ 两边任一没有指纹 = 老版本 Agent（0.1.0 不上报 install_id）。
            #   这时必须保持旧行为（新连接顶掉旧连接），否则云端一升级，
            #   现网所有没重装的客户立刻连不上 —— 那是比误顶号大得多的事故。
            #   只有在"双方都上报了指纹、且确实不同"时，才拦后来者。
            if old_install and new_install and old_install != new_install:
                if self._takeover_open(name):
                    self.takeover_until.pop(name, None)      # 一次性
                    log.warning("Agent %s 按运营者授权接管：旧指纹 %s → 新指纹 %s",
                                name, old_install, new_install)
                elif time.time() - self.last_seen.get(name, 0) > STALE_INSTALL_SEC:
                    # 离线超过 24h：视为原机器已经不在了，放行，免得换机重装变成人造停机
                    log.warning("Agent %s 旧指纹 %s 超过 24h 未上线，放行新指纹 %s",
                                name, old_install, new_install)
                else:
                    self._note_conflict({
                        "ts": time.time(), "agent": name,
                        "held_by": old_install, "rejected": new_install,
                        "rejected_host": str(hello.get("hostname") or ""),
                        "held_host": old.hostname or ""})
                    log.error("★ 拒绝同名接入：%s 已被另一台安装（%s）占用，后来者指纹 %s",
                              name, old_install, new_install)
                    raise AgentNameConflict(name, holder=old_install)
            try:
                await old.ws.close(code=4001, reason="replaced by new connection")
            except Exception:
                pass
        self.agents[name] = AgentConn(ws, hello)
        self.last_seen[name] = time.time()
        log.info("Agent 上线：%s v%s ops=%d install=%s host=%s 账套=%s/实测=%s", name,
                 self.agents[name].version, len(self.agents[name].ops),
                 self.agents[name].install_id or "(未上报)",
                 self.agents[name].hostname or "?",
                 self.agents[name].database or "(未上报)",
                 self.agents[name].database_verified or "(未实测)")
        verified = self.agents[name].database_verified
        if verified and not cfgmod.database_allowed(self.cfg, name, verified):
            # 只在"实测账套不属于这家"时按事故提醒（判据见 config.database_state）。
            # ★ 一台主机登记了多本账的老板，读非默认库是合法状态 —— 旧判据（必须等于商户码）
            #   会让他每次上线都打一条 error，噪音会把真事故淹掉。
            log.error("★ 账套越界：%s 的 Agent 实际在读「%s」，不在这家登记的清单（%s）。"
                      "请核对客户机 config.json 的 sql.database",
                      name, verified,
                      "、".join(cfgmod.merchant_databases(self.cfg, name)) or "未登记，按商户码判")
        self._log_event(name, "online", self.agents[name].version,
                        self.agents[name].install_id, self.agents[name].hostname,
                        len(self.agents[name].ops),
                        database=self.agents[name].database,
                        database_verified=self.agents[name].database_verified)
        return True

    def unregister(self, name, ws):
        cur = self.agents.get(name)
        if cur is None or cur.ws is not ws:
            return
        del self.agents[name]
        self.last_seen[name] = time.time()
        log.warning("Agent 离线：%s", name)
        self._log_event(name, "offline")
        # 该 Agent 在途请求全部失败，避免 HTTP 侧一直等待
        dead = [mid for mid, (an, _) in self.pending.items() if an == name]
        for mid in dead:
            _, fut = self.pending.pop(mid)
            if not fut.done():
                fut.set_exception(AgentOffline("agent offline"))

    async def disconnect(self, name):
        conn = self.agents.get(name)
        if conn is None:
            return False
        try:
            await conn.ws.close(code=4000, reason="admin disconnect")
        except Exception:
            pass
        return True

    async def handle_agent_message(self, name, ws, data):
        self.last_seen[name] = time.time()
        if data.get("type") == "ping":
            await ws.send_text(json.dumps({"type": "pong"}))
            return
        if data.get("type") == "status":
            # ★ B3：Agent 每 30 秒、以及**账套热切换之后**主动上报一次实测账套。
            #   热切是必须立刻报的时机 —— 切完还显示旧账套，等于把"哪家在读哪套账"
            #   又变回一个只有客户机本地知道的事实。
            conn = self.agents.get(name)
            if conn is not None:
                for key in ("database", "database_verified"):
                    val = str(data.get(key) or "").strip()
                    if val:
                        setattr(conn, key, val)
                # Step 2：两份清单是列表，单独收（空列表也要收 —— "把第二本撤掉"是合法动作）
                for key in ("allowed_databases", "served_databases"):
                    if isinstance(data.get(key), list):
                        setattr(conn, key,
                                [str(x).strip() for x in data[key] if str(x or "").strip()])
                if data.get("database_verified"):
                    conn.status_at = time.time()
                conn.uptime_sec = data.get("uptime_sec") or conn.uptime_sec
            return
        if data.get("type") != "response":
            return
        mid = str(data.get("id") or "")
        item = self.pending.get(mid)
        if item is None or item[0] != name:
            return
        _, fut = self.pending.pop(mid)
        if not fut.done():
            fut.set_result(data)

    def _require_published(self, published, agent_name, op):
        """op 必须在该 Agent 自报的能力清单里。清单为空 = 它没上报过，不猜，直接拒。"""
        if not published:
            raise AgentBadOp("Agent %s 未上报能力清单，拒绝转发 op=%s" % (agent_name, op))
        if op not in set(published):
            raise AgentBadOp("Agent %s 不支持 op=%s（其自报清单里没有这一项）" % (agent_name, op))

    async def rpc(self, agent_name, op, params, timeout, database="", actor=""):
        """中转一条请求给某家的本地 Agent。

        `database`（Step 3，2026-09-26）：这台客户机被允许读的另一本账。
        ★ 空字符串 ⇒ 帧里**不带这个键**，老版本 Agent 与默认路径完全不受影响；
        ★ 越界与否在云端（security.resolve_database）就已挡下，转下来之前先判，
          否则日志里只剩一台客户机在抱怨，看不出是谁的请求。

        `actor`（2026-09-29）：这条请求的操作员代码（单据的制单人就写它）。
        ★ 与 database 同一条纪律：**空串就不带键**，老版本 Agent 不受影响；
        ★ 它是**帧字段**，不是业务 params 里的键 —— 客户端自己塞的 "actor" 到不了这里，
          Agent 那边也只认信封上的（v3agent/agent.py:handle_request）。

        ★ 在途闸门（2026-09-26 起分级）：按 op 性质分 report / print / quick 三类，
          每类各有上限并可"先等一会儿再拒"，另有一个总闸兜底（见 config.admission_for）。
          改的原因：原来"一家总共 2 条、超了当场 429"，一条几十秒的打印就能让老板
          随手一次点击看到"本地服务正在处理其他查询"，体验上等于"这家坏了"。
        """
        if database:
            conn = self.agents.get(agent_name)
            reported = (conn.allowed_databases if conn is not None
                        else ((self.http_meta.get(agent_name) or {}).get("allowed_databases")))
            if not reported:
                raise AgentNoDbRouting(agent_name)

        table = cfgmod.admission_for(self.cfg, agent_name)
        gate = self.gates.get(agent_name)
        if gate is None or gate.table != table:
            # 配置改过（含商户级覆盖）就整块换新闸门：计数与上限是一体的，
            # 去动一个正在计数的闸门比换掉它更容易出错。换的瞬间在途数偏一次，
            # 不迁移 —— 宁可多放几条，也不能把正在跑的请求弄丢。
            gate = _Gate(table)
            self.gates[agent_name] = gate
        cls = cfgmod.op_class(op)
        t0 = time.time()
        if not await gate.acquire(cls):
            # 记一笔：闸门是保护，但运营者看不见次数就会把"这家变慢"误判成"云端坏了"
            self.busy_rejects[agent_name] = self.busy_rejects.get(agent_name, 0) + 1
            raise AgentBusy(agent_name, gate.snapshot().get("_total", 0), cls,
                            int((time.time() - t0) * 1000),
                            retry_after=int(gate._wait(cls)) or 2)
        self.inflight[agent_name] = gate.snapshot().get("_total", 1)
        try:
            return await self._rpc_inner(agent_name, op, params, timeout, database, actor)
        finally:
            await gate.release(cls)
            self.inflight[agent_name] = max(0, gate.snapshot().get("_total", 0))

    def inflight_detail(self, agent_name):
        """这一家此刻各类在途几条（后台监控/商户详情用）。没有闸门就是全 0。"""
        gate = self.gates.get(agent_name)
        return gate.snapshot() if gate is not None else {}

    async def _rpc_inner(self, agent_name, op, params, timeout, database="", actor=""):
        """真正拼帧发出去的那一段（闸门在 `rpc` 里，那层管"能不能发"）。

        ★ `database` / `actor` 都是从 `rpc` 透传下来的**帧字段**，空串就不带键；
          两条通道（WS 直连 / HTTP 轮询）都要带 —— 只带一条，换种连法身份就丢了。

        ★★ 业务 params 里夹带的 `database` 一律剥掉（2026-10-12）：
          Agent 那侧读账套名是"信封优先、params 兜底"（`v3agent/agent.py:handle_request`），
          于是客户端在业务体里塞一个 `"database": "另一本账"` 就能**绕过云端
          `security.resolve_database` 的 403 和"点名"审计**，直接把那台机器切到别的账上读。
          越界既然只在云端判一次（判完才转下去），转下去之前就得保证
          下面拿到的那本账确实是云端定下的那一本。
          真要点名另一本账，走上面的 `database` 帧字段 —— 六个调用点都已经显式在传它。
        """
        params = {k: v for k, v in (params or {}).items() if k != "database"}
        conn = self.agents.get(agent_name)
        if conn is not None:
            self._require_published(conn.ops, agent_name, op)
            mid = uuid.uuid4().hex
            fut = asyncio.get_running_loop().create_future()
            self.pending[mid] = (agent_name, fut)
            try:
                frame = {"type": "request", "id": mid, "op": op, "params": params or {}}
                if database:
                    frame["database"] = database
                if actor:
                    frame["actor"] = actor
                await conn.ws.send_text(json.dumps(frame, ensure_ascii=False))
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(f"agent {agent_name} 响应超时") from None
            finally:
                self.pending.pop(mid, None)

        # HTTP 轮询模式
        now = time.time()
        last = self.http_last.get(agent_name)
        if last is None or now - last > 25:
            raise AgentOffline(f"agent {agent_name} 不在线")
        self._require_published((self.http_meta.get(agent_name) or {}).get("ops"), agent_name, op)
        mid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self.http_waiters[mid] = (agent_name, fut)
        req = {"type": "request", "id": mid, "op": op, "params": params or {}}
        if database:
            req["database"] = database
        if actor:
            req["actor"] = actor
        self.http_queue[agent_name].append(req)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"agent {agent_name} 响应超时") from None
        finally:
            self.http_waiters.pop(mid, None)
            try:
                q = self.http_queue.get(agent_name)
                if q:
                    q = collections.deque(x for x in q if x.get("id") != mid)
                    self.http_queue[agent_name] = q
            except Exception:
                pass

    def http_poll(self, name, version="", ops=None, install_id="", hostname="",
                  database="", database_verified="", op_docs=None,
                  allowed_databases=None, served_databases=None):
        """Agent 轮询取请求；返回 (requests, meta)。"""
        now = time.time()
        install_id = str(install_id or "").strip()
        meta = self.http_meta.get(name)
        if meta is None:
            self.http_meta[name] = {"version": version, "ops": ops or [],
                                    "connected_at": now, "install_id": install_id,
                                    "hostname": str(hostname or "").strip(),
                                    "database": str(database or "").strip(),
                                    "database_verified": str(database_verified or "").strip(),
                                    "op_docs": op_docs or {},
                                    # Step 2/3：HTTP 通道也要存放行清单，
                                    # 否则走 http 传输的客户机永远被当成"不支持选账套的老包"
                                    "allowed_databases": [str(x).strip() for x in
                                                          (allowed_databases or [])
                                                          if str(x or "").strip()],
                                    "served_databases": [str(x).strip() for x in
                                                         (served_databases or [])
                                                         if str(x or "").strip()],
                                    "status_at": now if database_verified else 0.0}
        else:
            if version:
                meta["version"] = version
            if ops:
                meta["ops"] = ops
            if op_docs:
                # 这条通道只在第一帧带说明，之后不再重复推
                meta["op_docs"] = op_docs
            if database:
                meta["database"] = str(database).strip()
            if database_verified:
                meta["database_verified"] = str(database_verified).strip()
                meta["status_at"] = now
            if isinstance(allowed_databases, list):
                meta["allowed_databases"] = [str(x).strip() for x in allowed_databases
                                             if str(x or "").strip()]
            if isinstance(served_databases, list):
                meta["served_databases"] = [str(x).strip() for x in served_databases
                                            if str(x or "").strip()]
            if install_id:
                old = meta.get("install_id") or ""
                if old and old != install_id:
                    # HTTP 备选通道没有"连接"可以拒，只能记下冲突给后台看：
                    # 两条通道都在顶同一家的话，症状是接口时好时坏，光看日志查不出来
                    self._note_conflict({"ts": now, "agent": name, "channel": "http",
                                         "held_by": old, "rejected": install_id,
                                         "rejected_host": str(hostname or "")})
                meta["install_id"] = install_id
                meta["hostname"] = str(hostname or "").strip()
        self.http_last[name] = now
        q = self.http_queue.get(name)
        out = []
        while q and len(out) < 5:
            out.append(q.popleft())
        return out

    def http_result(self, agent_name, data):
        mid = str(data.get("id") or "")
        item = self.http_waiters.get(mid)
        if item is None or item[0] != agent_name:
            return False
        _, fut = self.http_waiters.pop(mid)
        if not fut.done():
            fut.set_result(data)
        return True

    def list_online(self, age=25):
        out = {}
        for name, c in self.agents.items():
            out[name] = {
                "source": "ws", "version": c.version, "ops": sorted(c.ops),
                "connected_at": c.connected_at,
                "install_id": c.install_id, "hostname": c.hostname,
                "database": c.database, "database_verified": c.database_verified,
                # Step 2：客户机放行的清单 / 实际服务过的账套（两本账的老板要看这个才知道切没切过去）
                "allowed_databases": list(c.allowed_databases),
                "served_databases": list(c.served_databases),
                "status_at": c.status_at, "uptime_sec": c.uptime_sec,
                "op_docs": c.op_docs,
            }
        now = time.time()
        for name, ts in self.http_last.items():
            if name in out or now - ts > age:
                continue
            m = self.http_meta.get(name, {})
            out[name] = {
                "source": "http",
                "version": m.get("version", ""),
                "ops": sorted(m.get("ops") or []),
                "connected_at": m.get("connected_at", ts),
                "install_id": m.get("install_id", ""),
                "hostname": m.get("hostname", ""),
                "database": m.get("database", ""),
                "database_verified": m.get("database_verified", ""),
                "op_docs": m.get("op_docs") or {},
                "status_at": m.get("status_at", 0.0), "uptime_sec": 0,
            }
        return out
