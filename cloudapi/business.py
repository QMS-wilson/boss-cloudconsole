# -*- coding: utf-8 -*-
"""业务 REST 接口层：按 API 文档把 RPC op 包装成 /api/v1/... 业务接口。

鉴权与 /api/v1/rpc 一致：Authorization: Bearer <店铺token> + X-Openid。
返回统一 envelope：{"ok": true, "data": ..., "op": ..., "agent": ...}。
"""

import asyncio
import base64
import json
import os
import re
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .registry import AgentBadOp, AgentBusy, AgentNoDbRouting, AgentOffline, busy_message
from . import audit as auditmod
from . import limit as limiter
from . import security

router = APIRouter()


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def _client_by_token(cfg, token):
    return next((c for c in cfg["auth"].get("clients", []) if c.get("token") == token), None)


def _user_by_openid(cfg, openid):
    return next((u for u in cfg["auth"].get("users", []) if u.get("openid") == openid), None)


def _auth_error(cfg, client, openid):
    access = client.get("access", "guest")
    if access == "auth":
        if not openid:
            return (401, {"ok": False, "error": {"code": "OPENID_REQUIRED", "message": "该客户端需要微信登录"}})
        user = _user_by_openid(cfg, openid)
        if user is None:
            return (403, {"ok": False, "error": {"code": "USER_NOT_FOUND", "message": "该微信未开通访问权限"}})
        if not (user.get("all_clients") or client["name"] in user.get("clients", [])):
            return (403, {"ok": False, "error": {"code": "NO_ACCESS", "message": "该微信无权访问此账套"}})
    elif cfg["auth"].get("require_openid") and not openid:
        return (401, {"ok": False, "error": {"code": "OPENID_REQUIRED", "message": "请先微信登录"}})
    return None


async def _rpc(request: Request, op: str, params: dict = None, timeout: int = 60):
    """REST → op 的唯一出口，**每次调用都落一条审计**。

    ★ 以前这条线上没有审计：`_audit` 是 server.py create_app 里的闭包，本模块调不到，
      于是"客户在 H5 里查了什么、开了哪张单"完全无痕（只有信封式 /api/v1/rpc 有记录）。
      多商户平台上"谁在什么时候动了哪家的数据"是事后唯一能自证清白的东西，必须补上。
    """
    cfg = request.app.state.cfg
    reg = request.app.state.registry
    db_name = ""            # 点名了才非空；审计里区分"这家有两本"与"这一条读了哪本"
    hint = {"store": request.query_params.get("store")}
    client, label, claims, auth_err = security.resolve_rpc_context(cfg, request, hint)

    def _audit_it(status, ok, code=None, note="", started=None, served=""):
        entry = security.audit_entry(request, client, claims,
                                     agent=(client or {}).get("agent") or "",
                                     op=op, ok=ok, code=code, note=note, params=params,
                                     database=db_name, served_database=served)
        entry["via"] = "rest"
        entry["path"] = request.url.path
        entry["http"] = status
        if started:
            entry["ms"] = int((time.time() - started) * 1000)
        auditmod.write(cfg, entry)

    if auth_err:
        code = ((auth_err[1] or {}).get("error") or {}).get("code") or "AUTH_ERROR"
        _audit_it(auth_err[0], False, code, "上下文未通过 label=%s" % (label or ""))
        return JSONResponse(status_code=auth_err[0], content=auth_err[1])
    agent, terr, _ignored = security.target_agent(cfg, client)
    if terr:
        _audit_it(terr[0], False, terr[1]["error"]["code"])
        return JSONResponse(status_code=terr[0], content=terr[1])
    # ★ Step 3：定了"哪家"之后再定"哪本账"。不点名 ⇒ 空串 ⇒ 默认库，行为与改造前一致。
    db_name, derr = security.resolve_database(cfg, request, agent)
    if derr:
        _audit_it(derr[0], False, derr[1]["error"]["code"], "账套越界 request=%s" % db_name)
        return JSONResponse(status_code=derr[0], content=derr[1])
    ts = time.time()
    # ★ 限流（Phase 5）：按 (客户端, op) 记令牌桶。放在转发之前拦，否则一条慢报表
    #   会把云端协程与那家客户的 SQL Server 一起占满，别家跟着一起转圈。
    rl = cfg.get("rate_limit") or {}
    allowed, wait = limiter.allow(limiter.key_of(client, op), rl.get("rate"), rl.get("burst"))
    if not allowed:
        _audit_it(429, False, "RATE_LIMITED", started=ts)
        return JSONResponse(status_code=429, headers={"Retry-After": str(int(wait) + 1)},
                            content={"ok": False, "error": {
                                "code": "RATE_LIMITED",
                                "message": "请求太密集了，请 %d 秒后再试" % (int(wait) + 1)}})
    try:
        # ★ 操作员身份从**已验签的令牌**里取，作为帧字段下去 —— Agent 建单时写进制单人列。
        #   不给（应用账号/微信会话）就留空，由 Agent 回落它自己的兜底代码。
        resp = await reg.rpc(agent, op, params or {}, timeout,
                             database=db_name, actor=security.actor_of(claims))
    except AgentBusy as exc:
        _audit_it(429, False, "AGENT_BUSY", started=ts)
        return JSONResponse(status_code=429, headers={"Retry-After": str(exc.retry_after)},
                            content={"ok": False, "error": {
                                "code": "AGENT_BUSY",
                                "message": busy_message(exc)}})
    except AgentOffline:
        _audit_it(502, False, "AGENT_OFFLINE", started=ts)
        return JSONResponse(status_code=502, content={
            "ok": False, "error": {"code": "AGENT_OFFLINE", "message": "客户本地 Agent 不在线：" + agent}})
    except AgentBadOp as exc:
        _audit_it(403, False, "OP_NOT_PUBLISHED", started=ts)
        return JSONResponse(status_code=403, content={
            "ok": False, "error": {"code": "OP_NOT_PUBLISHED", "message": str(exc)}})
    except (asyncio.TimeoutError, TimeoutError):
        # ★ 超时必须与「内部错误」分开（2026-09-22）：
        #   以前 TimeoutError 被下面的 `except Exception` 兜成 502 INTERNAL，
        #   前端只能看到「服务异常」，既不知道是超时、也不知道该重试还是该缩小范围。
        #   现在明确回 **504 AGENT_TIMEOUT** + 实际超时秒数，前端可据此提示
        #   「查询超时，请缩小日期范围」而不是笼统报错。
        _audit_it(504, False, "AGENT_TIMEOUT", started=ts)
        return JSONResponse(status_code=504, content={
            "ok": False, "error": {
                "code": "AGENT_TIMEOUT",
                "message": "本地 Agent 响应超时（%ds），请缩小日期范围或稍后重试" % timeout,
                "hint": "缩小日期范围、减少 limit，或确认本地 Agent 未被其他重查询占用",
                "timeout": timeout, "op": op}})
    except Exception as exc:  # noqa: BLE001
        _audit_it(502, False, "INTERNAL", started=ts)
        return JSONResponse(status_code=502, content={
            "ok": False, "error": {"code": "INTERNAL", "message": str(exc)}})
    served = str(resp.get("database") or "") if isinstance(resp, dict) else ""
    if resp.get("ok") is False:
        code = (resp.get("error") or {}).get("code")
        status = 502 if code == "INTERNAL" else 400
        _audit_it(status, False, code, started=ts, served=served)
        return JSONResponse(status_code=status, content=resp)
    _audit_it(200, True, started=ts, served=served)
    out = {"ok": True, "data": resp.get("data"), "op": op, "agent": agent}
    # 前端要按"实际读了哪本"分缓存（Step 4），所以这里也得回给它，不能只留在审计里
    if served:
        out["database"] = served
    return out


async def _body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return {}


def _q(request: Request, key: str, default=None):
    v = request.query_params.get(key)
    return default if v in (None, "") else v


# ---------------- 销售单 ----------------
@router.get("/api/v1/sales")
async def sales_list(request: Request):
    # ★ keyword / offset 以前漏了转发 —— 前端传了也被静默丢弃：
    #   表现为「搜索框没反应」「加载更多翻不出第二页」。
    #   转发层丢参数是最难查的一类 bug：三层都没有报错。
    params = {
        "date_from": _q(request, "date_from"), "date_to": _q(request, "date_to"),
        "customer": _q(request, "customer"), "state": _q(request, "state"),
        "keyword": _q(request, "keyword"),
        "limit": _q(request, "limit", 100), "offset": _q(request, "offset", 0),
    }
    return await _rpc(request, "bill.list", params)


@router.get("/api/v1/sales/{bill_no}")
async def sales_detail(bill_no: str, request: Request):
    return await _rpc(request, "bill.get", {"bill_no": bill_no})


@router.post("/api/v1/sales")
async def sales_create(request: Request):
    return await _rpc(request, "salesbill.create", await _body(request), timeout=120)


@router.put("/api/v1/sales/{bill_no}")
async def sales_update(bill_no: str, request: Request):
    body = await _body(request)
    body["bill_no"] = bill_no
    return await _rpc(request, "salesbill.update", body, timeout=120)


# ★ 审核 / 反审 / 复制（2026-09-22 第十一轮）
@router.post("/api/v1/sales/{bill_no}/audit")
async def sales_audit(bill_no: str, request: Request):
    body = await _body(request)
    body["bill_no"] = bill_no
    return await _rpc(request, "salesbill.audit", body, timeout=120)


@router.post("/api/v1/sales/{bill_no}/unaudit")
async def sales_unaudit(bill_no: str, request: Request):
    body = await _body(request)
    body["bill_no"] = bill_no
    return await _rpc(request, "salesbill.unaudit", body, timeout=120)


@router.post("/api/v1/sales/{bill_no}/void")
async def sales_void(bill_no: str, request: Request):
    body = await _body(request)
    body["bill_no"] = bill_no
    return await _rpc(request, "salesbill.void", body, timeout=120)


@router.post("/api/v1/sales/{bill_no}/copy")
async def sales_copy(bill_no: str, request: Request):
    body = await _body(request)
    body["bill_no"] = bill_no
    return await _rpc(request, "salesbill.copy", body, timeout=120)


# ---------------- 采购(进货)单 ----------------
# ★ 采购在 V3 里叫「进货」，Agent 侧走 purchase.* op（表 JH/JHMX）。
#   之前这两个路径是 404，前端被迫走示例数据；2026-09-20 打通。
@router.get("/api/v1/purchases")
async def purchases(request: Request):
    params = {
        "keyword": _q(request, "keyword", ""),
        "date_from": _q(request, "date_from"),
        "date_to": _q(request, "date_to"),
        "supplier": _q(request, "supplier"),
        "state": _q(request, "state"),
        "limit": _q(request, "limit", 100),
        "offset": _q(request, "offset", 0),
    }
    return await _rpc(request, "purchase.list", params)


@router.get("/api/v1/purchases/{bill_no}")
async def purchase_detail(bill_no: str, request: Request):
    return await _rpc(request, "purchase.get", {"bill_no": bill_no})


@router.post("/api/v1/purchases")
async def purchase_create(request: Request):
    """新建进货单。★ 表头时间 YYYY-MM-DD HH:mm:ss、制单人 ZDR=SYSTEM 由 Agent 侧负责。"""
    return await _rpc(request, "purchase.create", await _body(request), timeout=120)


@router.put("/api/v1/purchases/{bill_no}")
async def purchase_update(bill_no: str, request: Request):
    """改进货单。与 `PUT /sales/{bill_no}` 同一形状：单号从路径取，body 是 {header, lines}。

    ★ timeout=120：改单是"删行+插行+触发器联动库存与应付"，和建单同量级，
      默认那档超时会把已经落库的改动报成失败（用户再点一次就成了第二次改动）。
    """
    body = await _body(request)
    body["bill_no"] = bill_no
    return await _rpc(request, "purchase.update", body, timeout=120)


# ---------------- 供应商 ----------------
# ★ 路径顺序要紧：`/supplier-types` 是固定段，必须排在 `/suppliers/{code}` 之前
#   之外的独立前缀上，这里前缀不同（supplier-types vs suppliers），不会互相吃掉。
@router.get("/api/v1/supplier-types")
async def supplier_types(request: Request):
    return await _rpc(request, "supplier.types", {})


@router.get("/api/v1/suppliers")
async def suppliers(request: Request):
    params = {
        "keyword": _q(request, "keyword", ""),
        "limit": _q(request, "limit", 120),
        "offset": _q(request, "offset", 0),
        # ★ 档案页那个「显示已停用」开关（默认 0 = 暂停往来的不回）。
        #   这一类"前端传了、转发层没列出来"的参数是静默丢的三层 bug（见 sales_list 的教训），
        #   所以参数名与 Agent 侧 ops.py 完全同名，并由 check-stopped-archive.py 三端对齐钉住。
        "show_stopped": _q(request, "show_stopped", 0),
    }
    return await _rpc(request, "supplier.search", params)


@router.get("/api/v1/suppliers/{code}")
async def supplier_detail(code: str, request: Request):
    return await _rpc(request, "supplier.get", {"code": code})


@router.post("/api/v1/suppliers")
async def supplier_create(request: Request):
    """新增供应商（Gysdan）。编号缺省由 Agent 侧 G+5 位自增。"""
    return await _rpc(request, "supplier.save", await _body(request), timeout=60)


@router.put("/api/v1/suppliers/{code}")
async def supplier_update(code: str, request: Request):
    body = await _body(request)
    body["code"] = code
    return await _rpc(request, "supplier.save", body, timeout=60)


@router.get("/api/v1/suppliers/{code}/prices")
async def supplier_price_list(code: str, request: Request):
    """供应商进货价格历史（Price_Gys）。供编辑页带出最近进价。"""
    params = {"code": code, "limit": _q(request, "limit", 200)}
    return await _rpc(request, "supplier.prices", params)


# ---------------- 客户 ----------------
@router.get("/api/v1/customers")
async def customers(request: Request):
    params = {
        "keyword": _q(request, "keyword", ""),
        "limit": _q(request, "limit", 120),
        "offset": _q(request, "offset", 0),
        "show_stopped": _q(request, "show_stopped", 0),
    }
    return await _rpc(request, "customer.search", params)


@router.get("/api/v1/customers/{code}")
async def customer_detail(code: str, request: Request):
    return await _rpc(request, "customer.get", {"code": code})


@router.post("/api/v1/customers")
async def customer_create(request: Request):
    return await _rpc(request, "customer.save", await _body(request), timeout=120)


@router.put("/api/v1/customers/{code}")
async def customer_update(code: str, request: Request):
    body = await _body(request)
    body["code"] = code
    return await _rpc(request, "customer.save", body, timeout=120)


@router.get("/api/v1/customer-types")
async def customer_types(request: Request):
    return await _rpc(request, "customer.types", {})


# ---------------- 商品 ----------------
@router.get("/api/v1/product-types")
async def product_types(request: Request):
    """货品类别清单。★ 路径用 `product-types`（与 `/products/{hpbh}` 不同段），
    不会被通配段吃掉 —— 这类"新路由被已有路径参数抢走"的坑 supplier-types 也踩过。"""
    return await _rpc(request, "product.types", {})


@router.get("/api/v1/product-next-code")
async def product_next_code(request: Request):
    """按这台机器 V3 自己的「货品编号规则」带出下一个货品编号（新增货品页用）。

    ★ 规则不在云端也不在前端写死：Agent 每次现读 HxAccountOption，
      技术员在 V3 里改了档位或流水长度，下一个请求出的号就跟着变。
    ★ 同样放 `product-xxx` 独立段，不进 `/products/{hpbh}` 的通配（那会被抢走）。
    """
    return await _rpc(request, "product.nextcode",
                      {"category": _q(request, "category", "")})


@router.get("/api/v1/archive-stop-status")
async def archive_stop_status(request: Request):
    """停用的档案清单（客户/供应商/货品）⇒ 界面上每枚「停用」标签的唯一数据源。

    ★ 一次取回、前端查表：渲染档案名的地方有 30 来处（单据/库存/报表/草稿/首页…），
      逐条 SQL 加一列改不完、以后新增页面必漏。
    ★ 走 quick 那档的并发位（读的是三张档案表，不 JOIN 明细），别占报表的位。
    """
    return await _rpc(request, "archive.stopStatus",
                      {"cap": _q(request, "cap", 2000)}, timeout=30)


@router.get("/api/v1/products")
async def products(request: Request):
    params = {
        "keyword": _q(request, "keyword", ""),
        "limit": _q(request, "limit", 120),
        "offset": _q(request, "offset", 0),
        # show_stopped：档案页「显示已停用」开关；usage：开单方向 sale/purchase
        #（这时 show_stopped 在 Agent 侧失效 —— 单上不该出现停掉的东西）。
        "show_stopped": _q(request, "show_stopped", 0),
        "usage": _q(request, "usage", ""),
    }
    return await _rpc(request, "product.search", params)


@router.get("/api/v1/products/{hpbh}")
async def product_detail(hpbh: str, request: Request):
    return await _rpc(request, "product.get", {"hpbh": hpbh})


@router.get("/api/v1/products/{hpbh}/sales-history")
async def product_sales_history(hpbh: str, request: Request):
    """某货品的最近销售明细行（新增销售单的「最近销售」面板）。

    ★ 不能用 GET /sales（销售单列表）反查 —— 列表项不含明细行，
      那样永远查不到，前端会一直显示「暂无历史销售记录」。
    """
    params = {
        "hpbh": hpbh,
        "limit": _q(request, "limit", 8),
        "customer": _q(request, "customer", ""),
    }
    return await _rpc(request, "product.salesHistory", params)


@router.post("/api/v1/products")
async def product_create(request: Request):
    body = await _body(request)
    # ★ POST = 新增。带上 create_only，服务端就不会在"编号已存在"时退化成编辑别人的货品
    #   （编号现在能自动带出，预测与保存之间可能撞号；静默改名比报错严重得多）。
    body["create_only"] = True
    return await _rpc(request, "product.save", body, timeout=120)


@router.put("/api/v1/products/{hpbh}")
async def product_update(hpbh: str, request: Request):
    body = await _body(request)
    body["code"] = hpbh
    return await _rpc(request, "product.save", body, timeout=120)


# ---------------- 价格 / 库存 ----------------
@router.get("/api/v1/price")
async def price(request: Request):
    # ★ 2026-09-22：新增 priceTrace / priceTypeXs（单据页面「价格方案」）。
    #   缺省传 "" —— Agent 侧 ops._price_resolve 把 "" 与 None 同等对待（不覆盖），
    #   所以老前端不传这两个参数时行为完全不变。
    #   ★ 不能在这里给默认值 0：0 是合法方案（零售价），会把「没传」变成「明确选零售价」。
    params = {"customer": _q(request, "customerId"), "hpbh": _q(request, "productId"),
              "unit": _q(request, "unit", ""),
              "price_trace": _q(request, "priceTrace", ""),
              "price_type_xs": _q(request, "priceTypeXs", "")}
    return await _rpc(request, "price.resolve", params)


@router.get("/api/v1/inventory")
async def inventory(request: Request):
    # 2026-09-25：库存页改版。返回体从「裸数组」换成 {items,total,truncated}，
    # 所以这里必须把 flag/warehouse/offset 透下去（合计要跟随筛选口径）。
    params = {"keyword": _q(request, "keyword", ""), "hpbh": _q(request, "productId"),
              "warehouse": _q(request, "warehouse", ""), "flag": _q(request, "flag", "all"),
              "limit": _q(request, "limit", 200), "offset": _q(request, "offset", 0)}
    return await _rpc(request, "inventory.query", params)


@router.get("/api/v1/inventory/flow")
async def inventory_flow(request: Request):
    """收发存：某货品的逐笔出入库与结存（结存由 Agent 递推，不取 V3 的快照列）。"""
    params = {"hpbh": _q(request, "productId"),
              "date_from": _q(request, "startDate"), "date_to": _q(request, "endDate"),
              "limit": _q(request, "limit", 500)}
    return await _rpc(request, "inventory.flow", params)


# ---------------- 看板 / 报表 ----------------
@router.get("/api/v1/dashboard/summary")
async def dashboard(request: Request):
    params = {"month_start": _q(request, "month_start"), "today": _q(request, "today")}
    return await _rpc(request, "system.dashboard", params)


@router.get("/api/v1/reports/sales-summary")
async def sales_summary(request: Request):
    params = {"date_from": _q(request, "startDate"), "date_to": _q(request, "endDate"),
              "customer": _q(request, "customerId")}
    return await _rpc(request, "report.sales.summary", params, timeout=180)


@router.get("/api/v1/reports/sales-trend")
async def sales_trend(request: Request):
    params = {"date_from": _q(request, "startDate"), "date_to": _q(request, "endDate"),
              "granularity": _q(request, "granularity", "day")}
    return await _rpc(request, "report.sales.trend", params, timeout=180)


@router.get("/api/v1/reports/customer-ranking")
async def customer_ranking(request: Request):
    params = {"date_from": _q(request, "startDate"), "date_to": _q(request, "endDate"),
              "limit": _q(request, "limit", 20)}
    return await _rpc(request, "report.customer.ranking", params, timeout=180)


@router.get("/api/v1/reports/product-ranking")
async def product_ranking(request: Request):
    params = {"date_from": _q(request, "startDate"), "date_to": _q(request, "endDate"),
              "limit": _q(request, "limit", 20)}
    return await _rpc(request, "report.product.ranking", params, timeout=180)


@router.get("/api/v1/reports/receivables/summary")
async def receivables(request: Request):
    params = {"keyword": _q(request, "keyword", ""),
              "only_receivable": _q(request, "onlyReceivable", "true") not in ("false", "0", False),
              "limit": _q(request, "pageSize", 200),
              # 账龄分段筛选：'' | 0-30 | 31-60 | 61-90 | 90+（Agent 侧按客户最后出货日算）
              "aging": _q(request, "aging", ""),
              # 只决定"列不列暂停往来的客户"；合计与账龄始终是全量
              "show_stopped": _q(request, "show_stopped", 0)}
    return await _rpc(request, "report.receivables.summary", params, timeout=180)


@router.get("/api/v1/reports/customer-product-detail")
async def customer_product_detail(request: Request):
    params = {"customer": _q(request, "customerId"), "date_from": _q(request, "startDate"),
              "date_to": _q(request, "endDate"), "limit": _q(request, "limit", 200)}
    return await _rpc(request, "report.customer.product.detail", params, timeout=180)


@router.get("/api/v1/reports/customer-statement")
async def customer_statement(request: Request):
    params = {"customer": _q(request, "customerId"), "date_from": _q(request, "startDate"),
              "date_to": _q(request, "endDate"),
              # ★ 货品层（"月份对账单"那张一行一个货品的版式）默认**不取**：
              #   这一层比流水行多一个数量级，只有导出点名要时才透传。
              #   不点名 ⇒ Agent 走改造前那条路径，返回体逐字节与老版一致。
              "with_lines": _q(request, "withLines") or _q(request, "with_lines"),
              "line_limit": _q(request, "lineLimit")}
    return await _rpc(request, "report.customer.statement", params, timeout=180)


# ---------------- 采购报表 / 毛利 ----------------
@router.get("/api/v1/reports/purchase-summary")
async def purchase_summary(request: Request):
    params = {"date_from": _q(request, "startDate"), "date_to": _q(request, "endDate"),
              "supplier": _q(request, "supplier")}
    return await _rpc(request, "report.purchase.summary", params, timeout=180)


@router.get("/api/v1/reports/purchase-trend")
async def purchase_trend(request: Request):
    params = {"date_from": _q(request, "startDate"), "date_to": _q(request, "endDate"),
              "granularity": _q(request, "granularity", "day")}
    return await _rpc(request, "report.purchase.trend", params, timeout=180)


@router.get("/api/v1/reports/supplier-ranking")
async def supplier_ranking(request: Request):
    params = {"date_from": _q(request, "startDate"), "date_to": _q(request, "endDate"),
              "limit": _q(request, "limit", 20)}
    return await _rpc(request, "report.supplier.ranking", params, timeout=180)


@router.get("/api/v1/reports/profit-summary")
async def profit_summary(request: Request):
    params = {"date_from": _q(request, "startDate"), "date_to": _q(request, "endDate")}
    return await _rpc(request, "report.profit.summary", params, timeout=180)


# ---------------- 只读诊断（定位大数据量报表崩溃；无副作用） ----------------
@router.get("/api/v1/diag/report-gradient")
async def diag_report_gradient(request: Request):
    params = {"only": _q(request, "only")}
    return await _rpc(request, "diag.report.gradient", params, timeout=300)


# ---------------- 打印 ----------------
@router.post("/api/v1/print/salesbill")
async def print_salesbill(request: Request):
    body = await _body(request)
    params = {"bill_no": body.get("bill_no") or body.get("billNo"),
              "mode": body.get("mode") or "system",
              "copies": body.get("copies") or 1,
              # ★ 2026-09-23：打印页可选「打印机」与「打印格式」，透传给 Agent。
              #   printer 为空串时 Agent 走系统默认打印机（老行为，向后兼容）。
              "printer": body.get("printer"),
              "format": body.get("format")}
    return await _rpc(request, "print.salesbill", params, timeout=120)


# -------- 版式预览（远程看单）：Agent 出 PNG → 落盘 → 返回可被 <image> 直载的 URL --------
_PRINT_TTL = 86400       # 预览图保留 24h
# 内联 base64 的总量上限：小程序 setData 单次上限 1MB，留足余量取 700KB。
# （Agent 侧已按页数自适应 dpi，这是第二道保险。）
_INLINE_LIMIT = 700 * 1024


def _print_cache_dir(cfg):
    d = (cfg or {}).get("print_cache_dir") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "print_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _print_cache_gc(d, ttl=_PRINT_TTL):
    """清掉过期预览图。

    ★ 为什么用「随机文件名 + TTL」当凭证而不是常规鉴权：
      小程序的 `<image>` 组件**无法携带 Authorization 头**，图片 URL 必须自带访问资格。
      所以文件名用 uuid（不可猜），并设 24h TTL —— 过期即删，等于一次性凭证。
    """
    now = time.time()
    try:
        for f in os.listdir(d):
            p = os.path.join(d, f)
            if os.path.isfile(p) and now - os.path.getmtime(p) > ttl:
                os.remove(p)
    except Exception:  # noqa: BLE001
        pass


@router.post("/api/v1/print/salesbill/preview")
async def print_salesbill_preview(request: Request):
    """按 V3 版式生成预览图（Agent 端 GDI 直绘 PNG）。

    ★★★ 为什么**默认内联返回 base64**（`data:image/png;base64,...`）而不是只给 URL：
      真机调试时 `<image src="http://IP:端口/...">` 加载**不可靠** —— 明文 http +
      裸 IP + 非标端口，在真机上常被**静默拦掉**：图一片空白、**不报错**、控制台也干净，
      极易误判成「接口没数据」。base64 是**零网络依赖**，必然能显示。
      同时保留 `url`（H5 场景、以及「另存/转发」用）。

    ★ 内联总量守卫：小程序 setData 单次上限 1MB。Agent 侧已按页数自适应 dpi，
      这里再兜一道 —— 超过 `_INLINE_LIMIT` 就退化成只给 URL（前端会给出下载入口）。
    """
    body = await _body(request)
    bill_no = body.get("bill_no") or body.get("billNo")
    params = {"bill_no": bill_no, "mode": "image"}
    if body.get("dpi"):
        params["dpi"] = body.get("dpi")
    resp = await _rpc(request, "print.salesbill", params, timeout=180)
    if isinstance(resp, JSONResponse):
        return resp
    data = resp.get("data") or {}
    pages = data.get("pages") or []
    d = _print_cache_dir(request.app.state.cfg)
    _print_cache_gc(d)
    total_b64 = sum(len(p.get("png") or "") for p in pages)
    can_inline = body.get("inline") is not False and total_b64 <= _INLINE_LIMIT
    stamp = time.strftime("%Y%m%d%H%M%S")
    images = []
    for p in pages:
        b64 = p.get("png") or ""
        raw = base64.b64decode(b64)
        name = "p_%s_%s_%d.png" % (stamp, uuid.uuid4().hex[:12], p.get("page") or 1)
        with open(os.path.join(d, name), "wb") as fh:
            fh.write(raw)
        it = {"page": p.get("page"), "width": p.get("w"), "height": p.get("h"),
              "bytes": len(raw), "url": "/api/v1/print/preview/" + name}
        if can_inline:
            it["dataUrl"] = "data:image/png;base64," + b64
        images.append(it)
    return {"ok": True, "data": {"billNo": data.get("billNo") or bill_no,
                                 "totalPages": data.get("totalPages") or len(images),
                                 "dpi": data.get("dpi"), "inlined": can_inline,
                                 "inlineBytes": total_b64, "images": images}}


@router.get("/api/v1/print/preview/{name}")
async def print_preview_image(name: str, request: Request):
    """返回预览图二进制。**故意不鉴权**（见 _print_cache_gc 的说明）。"""
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,80}", name or "") or ".." in name:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": {"code": "BAD_NAME", "message": "非法文件名"}})
    p = os.path.join(_print_cache_dir(request.app.state.cfg), name)
    if not os.path.isfile(p):
        return JSONResponse(status_code=404, content={
            "ok": False, "error": {"code": "NOT_FOUND", "message": "预览图不存在或已过期"}})
    with open(p, "rb") as fh:
        return Response(content=fh.read(), media_type="image/png",
                        headers={"Cache-Control": "private, max-age=86400"})


# ---------------- 审计日志（已下线） ----------------
@router.get("/api/v1/audit-logs")
async def audit_logs(request: Request):
    """★ 2026-09-25 下线：整份 audit.jsonl 无过滤地回给任何一家持令牌者
      —— 多商户之后这就是一家能翻出**所有商户**的操作史。前端与原生小程序都零调用方，
      直接 410。运营侧看审计走 /api/admin/audit（带会话 + 可按商户过滤）。"""
    return JSONResponse(status_code=410, content={
        "ok": False, "error": {"code": "GONE",
                               "message": "该接口会跨商户泄露操作史，已下线；请改用后台 /api/admin/audit"}})
