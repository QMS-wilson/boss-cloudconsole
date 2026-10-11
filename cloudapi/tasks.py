# -*- coding: utf-8 -*-
"""异步任务：把耗时的 Agent op 放到后台执行，返回 taskId 供轮询。"""

import asyncio
import logging
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import security
from .registry import AgentBadOp, AgentNoDbRouting

router = APIRouter()
TASK_LOG = logging.getLogger("cloud.tasks")

TASKS = {}
MAX_TASKS = 200
# 每商户同时在跑/排队的任务上限。没有它，一家脚本连发 50 条报表就能把
# 云端协程和那台客户机全占住，别家的请求跟着一起等。
MAX_RUNNING_PER_AGENT = 3
MAX_TASKS_PER_AGENT = 20


def _check(request: Request):
    """返回 (client, agent, error)。agent 由服务端裁决，调用方给的 agent 一律不算。"""
    cfg = request.app.state.cfg
    body = {"store": request.query_params.get("store"),
            "agent": request.query_params.get("agent")}
    client, label, claims, auth_err = security.resolve_rpc_context(cfg, request, body)
    if auth_err:
        return None, None, JSONResponse(status_code=auth_err[0], content=auth_err[1])
    agent, terr, ignored = security.target_agent(cfg, client, body)
    if ignored:
        TASK_LOG.warning("忽略调用方自带的 agent=%s（真实租户=%s）", ignored, agent)
    if terr:
        return None, None, JSONResponse(status_code=terr[0], content=terr[1])
    return client, agent, None


def prune(agent, cap_keep=MAX_TASKS_PER_AGENT, cap_total=MAX_TASKS):
    """超额时**只淘汰本商户自己的**最旧任务；总量再超限则从攒得最多的那家里丢。

    ★ 原来是一份全局 dict 按 createdAt 裁 —— 一家跑批量导出就能把别家刚查完的
      结果挤掉（用户表现为"我明明查过了又没了"）。
    """
    mine = [k for k, t in TASKS.items() if t.get("agent") == agent]
    if len(mine) > cap_keep:
        for k in sorted(mine, key=lambda x: TASKS[x].get("createdAt", 0))[:len(mine) - cap_keep]:
            TASKS.pop(k, None)
    while len(TASKS) > cap_total:
        by = {}
        for k, t in TASKS.items():
            by.setdefault(t.get("agent"), []).append(k)
        worst = max(by, key=lambda a: len(by[a]))
        if len(by[worst]) <= 2:
            break                       # 已经很公平了，再丢就是无差别误伤
        oldest = min(by[worst], key=lambda k: TASKS[k].get("createdAt", 0))
        TASKS.pop(oldest, None)


async def _run(app, task_id, agent, op, params, timeout, database=""):
    reg = app.state.registry
    TASKS[task_id].update({"status": "running", "startedAt": time.time()})
    try:
        resp = await reg.rpc(agent, op, params or {}, timeout, database=database)
        # ★ 实际读了哪本要落进任务记录：一台主机两本账时，
        #   "这个任务的结果是哪本账的"决定前端把缓存写到哪个桶里（Step 4 依赖这一字段）
        served = str((resp or {}).get("database") or "")
        if served:
            TASKS[task_id]["database_served"] = served
        TASKS[task_id].update({
            "status": "success" if resp.get("ok") else "failed",
            "result": resp,
        })
    except AgentNoDbRouting:
        # 任务记录里留下这个码：否则前端只会看到一个 failed，不知道是"该换包了"
        TASKS[task_id].update({
            "status": "failed", "updatedAt": time.time(),
            "result": {"ok": False, "error": {
                "code": "AGENT_NO_DB_ROUTING",
                "message": "本地服务还是旧版本，暂不支持按请求切换账套，请联系平台更新"}}})
    except Exception as exc:  # noqa: BLE001
        TASKS[task_id].update({
            "status": "failed",
            "result": {"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)}},
        })
    finally:
        TASKS[task_id]["updatedAt"] = time.time()


@router.post("/api/v1/tasks")
async def task_create(request: Request):
    client, agent, err = _check(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    op = str(body.get("op") or "").strip()
    if not op:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": {"code": "BAD_REQUEST", "message": "缺少 op"}})
    cfg = request.app.state.cfg
    # ★ Step 3：任务也要能指定读哪本账（跑第二本账的报表是异步的，最容易漏这一维）
    db_name, derr = security.resolve_database(cfg, request, agent, body)
    if derr:
        return JSONResponse(status_code=derr[0], content=derr[1])
    timeout = min(max(int(body.get("timeout") or cfg.get("rpc_timeout", 60)), 5), 300)
    # ★ 每商户在途配额（Phase 5）：先拒新的，而不是悄悄排队把别人挤在后面
    running = [t for t in TASKS.values()
               if t.get("agent") == agent and t.get("status") in ("pending", "running")]
    cap_run = int(cfg.get("max_running_per_agent") or MAX_RUNNING_PER_AGENT)
    if len(running) >= cap_run:
        return JSONResponse(status_code=429, headers={"Retry-After": "5"}, content={
            "ok": False, "error": {
                "code": "AGENT_BUSY",
                "message": "本地服务已有 %d 个任务在跑，请等前面的完成后再提交" % len(running)}})
    task_id = uuid.uuid4().hex
    TASKS[task_id] = {"taskId": task_id, "op": op, "agent": agent, "status": "pending",
                      "params": body.get("params") or {}, "createdAt": time.time(),
                      "updatedAt": time.time(), "result": None,
                      "database": db_name}
    # ★ 超额只淘汰本商户自己的最旧任务（见 prune 的说明）
    prune(agent, int(cfg.get("max_tasks_per_agent") or MAX_TASKS_PER_AGENT), MAX_TASKS)
    asyncio.create_task(_run(request.app, task_id, agent, op, body.get("params") or {},
                             timeout, database=db_name))
    data = {"taskId": task_id, "status": "pending", "op": op, "agent": agent}
    if db_name:
        data["database"] = db_name
    return {"ok": True, "data": data}


@router.get("/api/v1/tasks")
async def task_list(request: Request):
    _, agent, err = _check(request)
    if err:
        return err
    limit = min(max(int(request.query_params.get("limit") or 20), 1), 100)
    # ★ 只回本租户的任务：以前这里不过滤，A 家能翻出 B 家任务的 params（含单号、客户名）
    mine = [t for t in TASKS.values() if t.get("agent") == agent]
    items = sorted(mine, key=lambda x: x.get("createdAt", 0), reverse=True)[:limit]
    return {"ok": True, "data": {"items": items, "count": len(items)}}


@router.get("/api/v1/tasks/{task_id}")
async def task_get(task_id: str, request: Request):
    _, agent, err = _check(request)
    if err:
        return err
    task = TASKS.get(task_id)
    if not task or task.get("agent") != agent:
        # 别人的任务一律报"不存在"，不用 403 —— 403 等于承认它存在
        return JSONResponse(status_code=404,
                            content={"ok": False, "error": {"code": "TASK_NOT_FOUND", "message": "任务不存在或已过期"}})
    out = dict(task)
    # 取结果时把"实际读过的那本"也顶上 database，调用方只需要认一个字段
    if out.get("database_served"):
        out["database"] = out["database_served"]
    return {"ok": True, "data": out}
