/* 审计：先回答"有没有异常、我刚做了什么"，再谈全量检索。
 * 出事先翻运营动作这本账，而不是在上万条数据请求里找。 */
import { api } from "../api.js";
import { S, go } from "../app.js";
import * as U from "../ui.js";

export const title = "审计";
const esc = U.esc;
const PAGE = 40;
let F = { code: "", kind: "", ok: "", op: "", sub: "", rid: "", days: 3, offset: 0 };

export async function mount(el, params) {
  if (params && params.code) F = Object.assign(F, { code: params.code, offset: 0 });
  el.innerHTML = `<div class="card" style="padding:12px 14px"><div class="row">
      <select id="fCode"><option value="">全部商户</option>
        ${S.health.map(m => `<option value="${esc(m.code)}">${esc(m.title)}</option>`).join("")}</select>
      <select id="fKind"><option value="">数据请求 + 运营动作</option>
        <option value="data">只看数据请求</option><option value="admin">只看我做过什么</option></select>
      <select id="fOk"><option value="">全部结果</option><option value="false">只看失败</option>
        <option value="true">只看成功</option></select>
      <input id="fOp" placeholder="动作名，可只填片段" style="width:170px">
      <input id="fSub" placeholder="账号 / 客户端名" style="width:150px">
      <select id="fDays"><option value="1">今天</option><option value="3">近 3 天</option>
        <option value="7">近 7 天</option><option value="30">近 30 天</option></select>
      <button class="btn sm" id="fGo">查询</button>
      <button class="btn gray sm" id="fReset">清空条件</button>
      <span class="grow"></span>
      <span class="muted small" id="aMeta"></span>
    </div>
    <div class="muted small" id="aRid" style="margin-top:6px"></div></div>
    <div class="card flush" id="aBody"></div>
    <div class="row" id="aPager"></div>`;
  $("fCode").value = F.code; $("fKind").value = F.kind; $("fOk").value = F.ok;
  $("fOp").value = F.op; $("fSub").value = F.sub; $("fDays").value = String(F.days);
  $("fGo").addEventListener("click", () => {
    F = { code: $("fCode").value, kind: $("fKind").value, ok: $("fOk").value,
          op: $("fOp").value.trim(), sub: $("fSub").value.trim(), rid: F.rid,
          days: parseInt($("fDays").value, 10) || 3, offset: 0 };
    load();
  });
  $("fReset").addEventListener("click", () => { F = { code: "", kind: "", ok: "", op: "", sub: "", rid: "", days: 3, offset: 0 }; mount(el); });
  await load();
}

function $(id) { return document.getElementById(id); }

async function load() {
  const q = Object.entries({ mkt: F.code, kind: F.kind, ok: F.ok, op: F.op, sub: F.sub,
                             rid: F.rid, days: F.days, limit: PAGE, offset: F.offset })
    .filter(([k, v]) => v !== "" && v != null)
    .map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
  let r;
  try { r = await api("/api/admin/audit?" + q); } catch (e) { $("aBody").innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; return; }
  const rows = r.entries || [];
  $("aMeta").textContent = `命中 ${r.total} 条 · 第 ${F.offset + 1}-${F.offset + rows.length} 条 · 窗口 ${r.window_days} 天`;
  $("aRid").innerHTML = F.rid
    ? `正在按请求号 <code>${esc(F.rid)}</code> 看一条请求的完整链路 · <a href="#" id="clrRid">取消</a>` : "";
  if (F.rid) $("clrRid").addEventListener("click", e => { e.preventDefault(); F.rid = ""; F.offset = 0; load(); });
  $("aBody").innerHTML = rows.length ? U.table(
    [["时间"], ["商户"], ["谁"], ["动作"], ["结果"], ["耗时"], ["来源"], ["请求号"]],
    rows.map(e => `<tr>
      ${U.td(esc(U.ts(e.ts)), "时间")}
      ${U.td(esc(e.mkt || e.agent || "—"), "商户")}
      ${U.td(esc(e.sub || e.client || "—"), "谁")}
      ${U.td(esc(e.op || ""), "动作")}
      ${U.td(e.ok ? U.tag("成功", "on") : U.tag(e.code || "失败", "off"), "结果")}
      ${U.td(e.ms != null ? Math.round(e.ms) + "ms" : "—", "耗时")}
      ${U.td(esc(e.ip || "—"), "来源")}
      ${U.td(e.rid ? `<a href="#" data-rid="${esc(e.rid)}"><code>${esc(String(e.rid).slice(0, 8))}</code></a>` : "—", "请求号")}
    </tr>`).join("")) : '<div class="muted small" style="padding:14px">这个条件下没有记录</div>';
  $("aBody").querySelectorAll("[data-rid]").forEach(a => a.addEventListener("click", ev => {
    ev.preventDefault(); F.rid = a.dataset.rid; F.offset = 0; load();
  }));
  $("aPager").innerHTML = `${F.offset > 0 ? '<button class="btn ghost sm" id="pPrev">上一页</button>' : ""}
    ${r.has_more ? '<button class="btn ghost sm" id="pNext">下一页</button>' : ""}`;
  if ($("pPrev")) $("pPrev").addEventListener("click", () => { F.offset = Math.max(0, F.offset - PAGE); load(); });
  if ($("pNext")) $("pNext").addEventListener("click", () => { F.offset += PAGE; load(); });
}

export async function onTick() { if ($("aBody")) await load(); }
