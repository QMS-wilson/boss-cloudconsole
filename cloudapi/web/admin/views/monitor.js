/* 监控：Phase 5 那几道闸门的实况 + 合并时间线。
 * 闸门以前是"静默生效"的保护 —— 回 429 之后运营者什么都看不见，
 * 就会把"某家页面变慢"误判成"云端坏了"。这一页的存在就是为了消掉这个误判。 */
import { api, toast, confirmImpact } from "../api.js";
import { S, go } from "../app.js";
import * as U from "../ui.js";

export const title = "监控";
const esc = U.esc;
function $(id) { return document.getElementById(id); }

export async function mount(el) {
  const r = await api("/api/admin/monitor");
  const st = r.stats || {};
  const rows = r.merchants || [];
  el.innerHTML = `
    ${U.alertsBanner(st.alerts || [])}
    <div class="grid g6">
      ${card(st.online + " / " + st.merchants_total, "Agent 在线", "测试商户也算在总数里")}
      ${card(st.calls_24h, "近 24h 调用", `失败 ${st.fail_24h} 次`)}
      ${card(st.inflight, "此刻在途请求", `${st.merchants_total} 家 · 每家上限 ${st.max_inflight} 条`)}
      ${card(st.busy_rejects, "「忙」拒绝次数", "在途满了直接回忙，不排队堆积")}
      ${card((st.rate || {}).total || 0, "被限流次数", `稳态 8 次/秒 · 突发 20`)}
      ${card(Object.keys(st.takeover_open || {}).length, "放行中的接管窗口", "3 分钟一次性")}
    </div>

    <div class="grid g2">
      <div class="card"><h2>各家实况<span class="r">点行进详情</span></h2>
        ${U.table([["商户"], ["状态"], ["实测账套"], ["在途"], ["24h 调用"], ["24h 失败"]],
          rows.map(m => `<tr class="click" data-code="${esc(m.code)}">
            ${U.td(`<span class="light ${U.levelOf(m)}"></span>${esc(m.title)}`, "商户")}
            ${U.td(m.online ? U.tag("在线 " + U.dur(m.uptime_sec), "on") : U.tag("未接入", "off"), "状态")}
            ${U.td(U.dbTag(m), "实测账套")}
            ${U.td(String(m.inflight), "在途")}
            ${U.td(String(m.calls_24h), "24h 调用")}
            ${U.td(m.fail_24h ? U.tag(String(m.fail_24h), "warn") : "0", "24h 失败")}</tr>`).join(""))}
      </div>
      <div class="card"><h2>后台任务与限流热点<span class="r">近 24h 累计</span></h2>
        <div class="kv"><span class="k">任务表总数</span><span>${(st.tasks || {}).total || 0}</span></div>
        ${Object.keys((st.tasks || {}).per_agent || {}).length
          ? Object.entries(st.tasks.per_agent).map(([k, v]) =>
              `<div class="kv"><span class="k">${esc(k)}</span><span>${v.total} 条 · 在跑 ${v.running}</span></div>`).join("")
          : '<div class="muted small" style="padding:6px 0">还没有后台任务</div>'}
        <div style="margin-top:10px"><b class="small">被限流最多的入口 + 动作</b></div>
        ${((st.rate || {}).top || []).length
          ? U.table([["入口"], ["动作"], ["次数"]], (st.rate.top).map(t =>
              `<tr>${U.td(esc(t.client), "入口")}${U.td(esc(t.op), "动作")}${U.td(String(t.count), "次数")}</tr>`).join(""))
          : '<div class="muted small">一次都没限流过（说明现在没人刷接口）</div>'}
      </div>
    </div>

    <div class="card"><h2>H5 发布态<span class="r">读文件系统实况，快照只用来对时间</span></h2>
      ${h5Table(st.h5, st.h5_snapshot)}</div>

    <div class="card"><h2>链路与商户对账<span class="r">文件系统 vs 商户名单，不一致就摆出来</span></h2>
      ${reconcileBlock(st)}</div>

    <div class="card"><h2>时间线<span class="r">上下线 + 后台运营动作 + 同名冲突</span></h2>
      <div class="tl">${(r.events || []).map(e => `<div class="ev ${esc(e.level)}">
        <div class="tm">${esc(U.ts(e.ts))} · ${esc(label(e.kind))} · ${esc(e.code || "—")}</div>
        <div class="tx"><b>${esc(e.title)}</b> ${esc(e.detail || "")}</div></div>`).join("")
        || '<div class="muted small">还没有事件</div>'}</div>
    </div>`;

  el.querySelectorAll("tr[data-code]").forEach(tr =>
    tr.addEventListener("click", () => go("merchants", { code: tr.dataset.code })));
  el.querySelectorAll("[data-unpub]").forEach(b => b.addEventListener("click", async ev => {
    ev.stopPropagation();
    const code = b.dataset.unpub;
    const ok = await confirmImpact(code, "unpublish", `收掉孤儿链 /${code}/？`,
      { okLabel: "收链", hint: "商户名单里已经没有这一家了；收链只删这一条软链，别家的不动。" });
    if (!ok) return;
    try {
      const r = await api(`/api/admin/merchants/${encodeURIComponent(code)}/unpublish-prefix`, "POST", {});
      toast(r.message || "已收链");
      await mount($("view"));
    } catch (e) { toast(e.message, true); }
  }));
}

function label(k) {
  return { agent: "Agent", admin: "后台操作", conflict: "同名冲突" }[k] || k;
}

function card(num, lbl, sub) {
  return `<div class="stat"><div class="num">${esc(String(num))}</div>
    <div class="lbl">${esc(lbl)}</div><div class="sub">${esc(sub || "")}</div></div>`;
}

function h5Table(live, snap) {
  const t = (live && live.tenants) || {};
  const codes = Object.keys(t).sort();
  if (!codes.length && !((live || {}).builds || []).length) {
    return `<div class="muted small">这台机器上 <code>tenants/</code> 里一条链都没有。
      发布用 <code>python scripts/deploy.py --merchant &lt;码&gt;</code>；
      发布脚本每次都会把矩阵回写一份给后台。</div>`;
  }
  return `<div class="kv"><span class="k">根站 <code>/</code> 指向</span><span><code>${esc(live.current || "—")}</code></span></div>
    ${U.table([["商户"], ["指向构建"], ["地址"], ["链变更时间"]], codes.map(code => {
      const x = t[code] || {};
      return `<tr>${U.td(esc(code), "商户")}${U.td(`<code>${esc(x.build_id)}</code>${
        x.broken ? " " + U.tag("链指向不存在的构建", "off") : ""}`, "指向构建")}
        ${U.td(esc(x.url), "地址")}${U.td(esc(U.ts(x.linked_at)), "链变更时间")}</tr>`;
    }).join(""))}
    <div class="muted small" style="margin-top:8px">留存构建 ${((live.builds) || []).length} 份 ·
      这一页读的是**文件系统实况**${snap && snap.available
        ? `；发布脚本上次回写是 ${esc(U.ts(snap.updated_at))}${snap.stale ? U.tag("（已超过 24 小时）", "warn") : ""}`
        : "；还没有发布脚本回写过快照"}</div>`;
}

function reconcileBlock(st) {
  const orphans = st.orphans || [], missing = st.missing_prefix || [],
        broken = st.broken_links || [], plain = st.plain_links || [];
  if (!orphans.length && !missing.length && !broken.length && !plain.length) {
    return `<div class="muted small">对得上：${(st.merchants_total || 0)} 家商户与 ${
      Object.keys(((st.h5 || {}).tenants) || {}).length} 条前缀一一对应。</div>`;
  }
  const li = (arr, kind) => arr.map(c => `<li><code>${esc(c)}</code>
    ${kind === "orphan" ? `<button class="btn link" data-unpub="${esc(c)}">收链</button>` : ""}
    ${kind === "missing" ? `<button class="btn link" data-open="${esc(c)}">进详情看该跑的发布命令</button>` : ""}
    </li>`).join("");
  return `${orphans.length ? `<div class="banner bad"><b>${orphans.length} 条孤儿链</b>：nginx 上还能打开，
      但商户名单里已经没有这一家 —— 这就是"删了商户没收链"留下的东西。
      <ul>${li(orphans, "orphan")}</ul></div>` : ""}
    ${broken.length ? `<div class="banner"><b>${broken.length} 条链指向不存在的构建</b>
      <ul>${li(broken, "broken")}</ul></div>` : ""}
    ${plain.length ? `<div class="banner bad"><b>${plain.length} 个不是软链的站点目录</b>：
      出现在 tenants/ 下说明有人直接拷了一份进去 —— nginx 会服务它，但后台不删真目录，
      请用命令行处理：<ul>${li(plain, "plain")}</ul></div>` : ""}
    ${missing.length ? `<div class="muted small" style="margin-top:6px">还没发布前缀的商户：${
      missing.map(esc).join("、")}（新开通的商户本来就该是这样，装机完成后发布即可）</div>` : ""}`;
}

export async function onTick() {
  if ($("view") && document.querySelector(".tl")) await mount($("view"));
}
