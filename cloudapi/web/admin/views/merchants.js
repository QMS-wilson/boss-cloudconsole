/* 商户：健康一览（列表）+ 单商户工作台（详情）+ 开通向导。
 *
 * 为什么把这三件事放一个文件：它们是同一个问题的三种视图 —— "这家能不能用、
 * 坏在哪、怎么开一家新的"。拆成三个页签就会逼着运营者自己在三处做 join。
 */
import { api, toast, modal, closeModal, confirmImpact, copyText } from "../api.js";
import { S, go, loadHealth } from "../app.js";
import * as U from "../ui.js";

export const title = "商户";
let detailCode = null;

const esc = U.esc;
function $(id, root) { return (root || document).getElementById(id); }

export async function mount(el, params) {
  detailCode = params.code || (params.keep && detailCode) || null;
  if (params.code) { await loadHealth(true); return renderDetail(el, params.code); }
  return renderList(el);
}

export async function onTick() {
  // 详情页里有 #acc-prog（接入进度那一格），列表页里有 #m-list —— 用它们判断现在在哪一屏，
  // 自动刷新才不会把用户正在看的另一屏换掉
  if (detailCode && document.getElementById("acc-prog")) await renderDetail(document.getElementById("view"), detailCode);
  else if (document.getElementById("m-list")) await renderList(document.getElementById("view"));
}

/* ------------------------------------------------------------------ 列表 */

function dbCell(m) {
  if (m.db_state === "match")
    return `${U.tag("一致", "on")} <span class="small">${esc(m.database_verified)}</span>`;
  if (m.db_state === "allowed")
    // 一台主机多本账：读非默认库是合法状态，但必须看得见"现在在看哪一本"
    return `${U.tag("在读第二本", "gray")} <span class="small">${esc(m.database_verified)}
      <span class="muted"> / 默认 ${esc(m.database_default || m.code)}</span></span>`;
  if (m.db_state === "mismatch")
    return `${U.tag("越界", "off")} <span class="small">${esc(m.database_verified)}
      <span class="muted"> 不在 ${(m.databases || []).length ? (m.databases || []).join("、") : "登记清单（按商户码判）"} 里</span></span>`;
  return `${U.tag("未上报", "gray")} <span class="small muted">老包或还没连库</span>`;
}

function renderRow(m) {
  const lv = U.levelOf(m);
  const ver = m.version ? `<code>${esc(m.version)}</code>` : '<span class="muted">—</span>';
  return `<tr class="click" data-code="${esc(m.code)}">
    ${U.td(`<span class="light ${lv}"></span><b>${esc(m.title)}</b>
            <div class="muted small" style="margin-left:15px">${esc(m.code)}${m.enabled ? "" : " · 已停用"}</div>`, "商户")}
    ${U.td(dbCell(m), "实测账套")}
    ${U.td(m.online ? U.tag("在线", "on") + `<div class="muted small">已连 ${U.dur(m.uptime_sec)}</div>`
                    : U.tag("未接入", "off") + `<div class="muted small">${esc(U.ago(m.last_online_at))}在线过</div>`, "Agent")}
    ${U.td(`${ver}<div class="muted small">门槛 ${esc(m.min_agent_version || "不强制")}${m.version_too_old ? " · 偏旧" : ""}</div>`, "版本")}
    ${U.td(m.last_ok_at ? `${esc(U.ts(m.last_ok_at))}<div class="muted small">${esc(m.last_ok_op || "")}</div>`
                        : '<span class="muted">—</span>', "最近成功")}
    ${U.td(`${m.fail_24h ? U.tag(m.fail_24h + " 次", "warn") : U.tag("0 次", "on")}
            <div class="muted small">共 ${m.calls_24h} 次调用</div>`, "24h 失败")}
    ${U.td(`H5 ${m.prefix.published ? U.tag("已发布", "on") : U.tag("未发布", "gray")}
            <div class="muted small">${esc(m.prefix.build_id || "")} · 令牌 ${m.clients.length}</div>`, "入口")}
    ${U.td(`${m.accounts.length} 个<div class="muted small">微信 ${m.users_n} 个</div>`, "账号")}
    ${U.td(`<button class="btn link" data-open="${esc(m.code)}">进详情</button>`, "")}
  </tr>`;
}

async function renderList(el) {
  await loadHealth();
  const rows = S.health;
  el.innerHTML = `
    ${U.alertsBanner(S.alerts)}
    <div class="card" style="padding:12px 16px;display:flex;gap:10px;flex-wrap:wrap;align-items:center">
      <input id="q" placeholder="搜商户码 / 显示名 / 主机名" style="flex:1;min-width:180px">
      <select id="fAll"><option value="">全部商户</option><option value="bad">只看不正常</option>
        <option value="online">只看在线</option></select>
      <button class="btn" id="btnNew">开通新商户</button>
    </div>
    <div class="card flush" id="m-list"></div>
    <div class="grid g6" id="m-stats"></div>`;
  const paint = () => {
    const q = ($("q").value || "").trim().toLowerCase();
    const f = $("fAll").value;
    const list = rows.filter(m => {
      if (q && !(`${m.code} ${m.title} ${m.hostname}`.toLowerCase().indexOf(q) >= 0)) return false;
      if (f === "bad") return m.level !== "ok";
      if (f === "online") return m.online;
      return true;
    });
    $("m-list").innerHTML = list.length
      ? U.table([["商户"], ["实测账套"], ["Agent"], ["版本 / 门槛"], ["最近成功调用"], ["24h 失败"], ["入口"], ["账号"], [""]],
                list.map(renderRow).join(""))
      : '<div class="muted small" style="padding:14px">没有符合条件的商户</div>';
    $("m-list").querySelectorAll("[data-open]").forEach(b =>
      b.addEventListener("click", e => { e.stopPropagation(); go("merchants", { code: b.dataset.open }); }));
    $("m-list").querySelectorAll("tr[data-code]").forEach(tr =>
      tr.addEventListener("click", () => go("merchants", { code: tr.dataset.code })));
    const online = rows.filter(r => r.online).length;
    const calls = rows.reduce((a, b) => a + b.calls_24h, 0);
    const fails = rows.reduce((a, b) => a + b.fail_24h, 0);
    const usable = rows.filter(r => r.online && r.enabled && r.db_state !== "mismatch").length;
    $("m-stats").innerHTML = `
      ${stat(usable, "可正常使用的商户", "在线 · 已启用 · 账套一致")}
      ${stat(calls, "近 24h 调用", `失败 ${fails} 次`)}
      ${stat(rows.length ? U.esc(rows.filter(r => r.online).map(r => r.version).sort()[0] || "—") : "—", "最低在跑版本", "离线商户不计入")}
      ${stat(S.meta ? S.meta.days : 1 + " 天", "统计窗口", "审计按日落盘")}
      ${stat(S.h5 && S.h5.current ? esc(S.h5.current) : "—", "根站指向构建", S.h5 && S.h5.available ? `${(S.h5.builds || []).length} 份构建留存` : "发布脚本未回写")}
      ${stat(S.alerts.length, "待处理告警", S.alerts.length ? "见顶部黄条" : "没有异常")}`;
  };
  $("q").addEventListener("input", paint);
  $("fAll").addEventListener("change", paint);
  $("btnNew").addEventListener("click", openWizard);
  paint();
}

function stat(num, lbl, sub) {
  return `<div class="stat"><div class="num">${num}</div><div class="lbl">${esc(lbl)}</div>
          <div class="sub">${esc(sub || "")}</div></div>`;
}

/* ------------------------------------------------------------------ 详情 */

async function renderDetail(el, code) {
  let m;
  try {
    m = (await api(`/api/admin/merchants/${encodeURIComponent(code)}`)).data;
  } catch (e) {
    el.innerHTML = `<div class="banner bad">读不到商户「${esc(code)}」：${esc(e.message)}</div>
      <button class="btn ghost" id="back">返回列表</button>`;
    $("back").addEventListener("click", () => go("merchants"));
    return;
  }
  detailCode = code;
  const lv = U.levelOf(m);
  el.innerHTML = `
    <div class="topbar">
      <button class="btn ghost sm" id="back">‹ 商户</button>
      <h1><span class="light ${lv}"></span>${esc(m.title)}</h1>
      <span class="chip gray">商户码 ${esc(m.code)}</span>
      ${m.online ? `<span class="chip">在线 ${U.dur(m.uptime_sec)}</span>` : `<span class="chip gray">未接入</span>`}
      <span class="grow"></span>
      <button class="btn ghost sm" id="btnMin">改门槛</button>
      <button class="btn gray sm" id="btnToggle">${m.enabled ? "停用" : "启用"}</button>
      <button class="btn red sm" id="btnDrop">下线删除</button>
    </div>
    ${U.alertsBanner(m.alerts.map(a => Object.assign({ title: m.title }, a)))}
    <div class="grid g6">
      ${stat(m.db_state === "match" ? "一致" : m.db_state === "allowed" ? "在读第二本"
             : m.db_state === "mismatch" ? "越界" : "未上报",
             "实测账套", m.database_verified ? esc(m.database_verified) : "等 Agent 上报")}
      ${stat(esc(m.version || "—"), "Agent 版本", `门槛 ${esc(m.min_agent_version || "不强制")}`)}
      ${stat(m.calls_24h, "24h 调用", `失败 ${m.fail_24h} 次`)}
      ${stat(m.last_ok_at ? U.ago(m.last_ok_at) : "从未", "最近成功调用", esc(m.last_ok_op || "—"))}
      ${stat(`${m.clients.length} / ${m.accounts.length}`, "入口 / 账号", `微信授权 ${m.users_n} 个`)}
      ${stat(m.prefix.published ? "已发布" : "未发布", "H5 前缀", esc(m.prefix.build_id || m.prefix.url))}
    </div>

    <div class="acc" id="acc-prog"><div class="h">接入进度
      <span class="tag ${m.done_n === m.checklist.length ? "on" : "warn"}">${m.done_n} / ${m.checklist.length}</span>
      <span class="ar">收起 ‹</span></div>
      <div class="b">${m.checklist.map(c => `<div class="chk">
        <span class="i ${c.done ? "y" : (c.key === "database" && m.db_state === "mismatch" ? "n" : "w")}">${c.done ? "✓" : "!"}</span>
        <div><div class="t">${esc(c.label)}</div><div class="d">${esc(c.evidence || "")}</div></div></div>`).join("")}
        <div class="muted small" style="margin-top:8px">★ 每一步的完成与否都由事实推导（有没有登记、有没有连上、实测账套、前缀探活、有没有账号），
        不给人手工勾选 —— 勾了"已完成"但实际没做，比没有这一列更糟。</div>
      </div></div>

    <div class="acc" id="acc-agent"><div class="h">Agent 登记 <span class="ar">展开 ›</span></div><div class="b">
      <div class="grid g2">
        <div>
          ${U.kv("名称（= 商户码 = 前缀）", `<code>${esc(m.code)}</code>`)}
          ${U.kv("密钥", m.agent_secret
            ? `<code>${esc(m.agent_secret)}</code> <button class="btn link" data-cp="${esc(m.agent_secret)}">复制</button>
               <button class="btn link" id="btnRot">轮换</button>`
            : `${U.tag("未登记", "off")} <button class="btn link" id="btnRot">轮换</button>`)}
          ${U.kv("入口令牌", (m.entry_tokens || []).length
            ? m.entry_tokens.map(c => `<div><code>${esc(c.token)}</code>
                <button class="btn link" data-cp="${esc(c.token)}">复制</button>
                <span class="muted small">${esc(c.name)}</span></div>`).join("")
            : "这家还没有入口令牌")}
          ${U.kv("接入通道", esc(m.source || "—"))}
          ${U.kv("主机名", esc(m.hostname || "—"))}
          ${U.kv("装机指纹", `<code>${esc((m.install_id || "未上报").slice(0, 12))}</code>`)}
          ${U.kv("登记的账套清单", (m.databases || []).length
            ? (m.databases || []).map((d, i) => `<div><code>${esc(d)}</code>${i === 0
                ? ' <span class="small muted">默认库</span>' : ""}${d === m.database_verified
                ? ' <span class="small">← 在读</span>' : ""}</div>`).join("")
            : `<span class="muted">未登记（按「实测 == 商户码」判一致）；当前配置写的是 ${esc(m.database || "—")}</span>`)}
          ${U.kv("实测账套", m.database_verified ? `<code>${esc(m.database_verified)}</code>` : "未上报")}
          ${U.kv("客户机实际放行", (m.agent_allowed || []).length
            ? `<code>${esc(m.agent_allowed.join("、"))}</code>`
            : '<span class="muted">老版本包没上报这一项（不因此判故障）</span>')}
          ${U.kv("这台机器服务过的账套", (m.served_databases || []).length
            ? esc(m.served_databases.join("、")) : '<span class="muted">只有默认库</span>')}
          ${U.kv("最近上线", esc(U.ts(m.online ? m.connected_at : m.last_online_at) || "—"))}
        </div>
        <div>
          <div class="row">
            <button class="btn ghost" id="btnTake">放行换机接管</button>
            <button class="btn gray" id="btnDisc" ${m.online ? "" : "disabled"}>断开连接</button>
          </div>
          <div class="muted small" style="margin-top:8px;line-height:1.8">「放行接管」= 一次性 3 分钟窗口，
          让对方机器现在重连。自动放行等于回到"谁先连上算谁"，那正是会把在线那家顶掉的动作。</div>
          <div style="margin-top:12px"><b class="small">这家的动作能力（${m.ops.length} 个）</b>
            <div class="row" style="margin-top:6px">${m.ops.slice(0, 14).map(o =>
              `<span class="tag info" title="${esc((m.op_docs || {})[o] || {}).desc || ''}">${esc(o)}</span>`).join("")}
            ${m.ops.length > 14 ? U.tag("+" + (m.ops.length - 14), "gray") : ""}</div>
            <div class="muted small" style="margin-top:6px">白名单由 Agent 自报，没上报的动作云端一律拒（不猜）。
            鼠标停在上面能看到这个动作是做什么的。</div>
          </div>
        </div>
      </div>
    </div></div>

    <div class="acc" id="acc-entry"><div class="h">入口与账号
      <span class="tag gray">令牌 ${m.clients.length} · 应用账号 ${m.accounts.length} · 微信 ${m.users_n}</span><span class="ar">展开 ›</span></div>
      <div class="b">
        <div class="kv"><span class="k">H5 前缀</span><span>
          ${m.prefix.published
            ? `${m.prefix.plain ? U.tag("已发布（但不是软链，后台不删）", "off")
                                : U.tag("已发布 → " + (m.prefix.build_id || "?"), "on")}
               <code>${esc(m.prefix.url)}</code>
               ${m.prefix.plain ? "" : `<button class="btn link" id="btnUnpub">收链（下线这个前缀）</button>`}`
            : `${U.tag("未发布", "gray")} <button class="btn link" id="btnCmd">该跑的发布命令</button>`}</span></div>
        <div class="muted small" style="margin:2px 0 10px">收链只删 nginx 上这一条软链，不碰构建目录、不碰别家的链，
        也不需要 reload。发布仍然只能在电脑上跑（那边有逐文件校验与配置试跑两道门）。</div>
        <b class="small">客户端令牌</b>
        ${m.client_rows.length ? U.table([["名称"], ["显示名"], ["访问"]], m.client_rows.map(c =>
          `<tr>${U.td(esc(c.name), "名称")}${U.td(esc(c.title), "显示名")}${U.td(U.tag(c.access === "auth" ? "必须登录" : "免登录", c.access === "auth" ? "on" : "warn"), "访问")}</tr>`).join("")) : '<div class="muted small">还没有入口</div>'}
        <div style="margin-top:12px"><b class="small">应用账号</b></div>
        ${m.account_rows.length ? U.table([["账号"], ["姓名"], ["可及商户"], ["状态"]], m.account_rows.map(a =>
          `<tr>${U.td(esc(a.username), "账号")}${U.td(esc(a.name), "姓名")}
           ${U.td(a.all_stores ? `${U.tag("全平台可达", "warn")}` : esc((a.stores || []).join("、")), "可及商户")}
           ${U.td(a.disabled ? U.tag("已禁用", "off") : U.tag("正常", "on"), "状态")}</tr>`).join(""))
          : '<div class="muted small">还没有应用账号</div>'}
        <div class="muted small" style="margin-top:8px">★ 客户账号必须只绑自己那一家。「全平台可达」是给平台运营者的；
        给错了一个账号，裸 IP 入口上所有请求都会因为"没点名商户"被拒。</div>
        <div style="margin-top:12px"><b class="small">微信授权（${m.user_rows.length}）</b>
        <div class="muted small">${m.user_rows.length ? m.user_rows.map(u => esc(u.name || u.openid)).join("、") : "还没有"}</div></div>
      </div></div>

    <div class="acc" id="acc-fail"><div class="h">这家的异常与最近动作
      <span class="tag ${m.recent_failures.length ? "warn" : "on"}">24h 失败 ${m.recent_failures.length}</span><span class="ar">展开 ›</span></div>
      <div class="b">
        ${m.recent_failures.length ? U.table([["时间"], ["动作"], ["原因"], ["谁"], ["请求号"]], m.recent_failures.map(f =>
          `<tr>${U.td(esc(U.ts(f.ts)), "时间")}${U.td(esc(f.op), "动作")}${U.td(U.tag(f.code || "ERR", "off"), "原因")}
           ${U.td(esc(f.sub || f.client || "—"), "谁")}${U.td(`<code>${esc((f.rid || "").slice(0, 8))}</code>`, "请求号")}</tr>`).join(""))
          : '<div class="muted small">近 24 小时没有失败记录。</div>'}
        <div class="row" style="margin-top:10px"><button class="btn ghost sm" id="btnAudit">看这家完整审计</button></div>
      </div></div>

    <div class="card"><h2>上下线与运营动作<span class="r">这一家的时间线</span></h2>
      <div class="tl">${m.events.map(e => `<div class="ev ${esc(e.level)}">
        <div class="tm">${esc(U.ts(e.ts))} · ${esc(e.kind === "admin" ? "后台操作" : e.kind === "conflict" ? "同名冲突" : "Agent")}</div>
        <div class="tx"><b>${esc(e.title)}</b> ${esc(e.detail || "")}</div></div>`).join("")
        || '<div class="muted small">还没有事件</div>'}</div>
    </div>`;

  el.querySelectorAll(".acc > .h").forEach(h => h.addEventListener("click", () => {
    const box = h.parentElement;
    box.classList.toggle("closed");
    h.querySelector(".ar").textContent = box.classList.contains("closed") ? "展开 ›" : "收起 ‹";
  }));
  const bind = (id, fn) => { const b = $(id); if (b) b.addEventListener("click", fn); };
  el.querySelectorAll("[data-cp]").forEach(x => x.addEventListener("click", () => copyText(x.dataset.cp)));
  $("back").addEventListener("click", () => { detailCode = null; go("merchants"); });
  bind("btnAudit", () => go("audit", { code: m.code }));
  bind("btnMin", () => editMinVer(m));
  bind("btnToggle", () => toggleMerchant(m));
  bind("btnDrop", () => dropMerchant(m));
  bind("btnUnpub", () => unpublishPrefix(m));
  bind("btnCmd", () => modal(`<h3>发布 /${esc(m.code)}/</h3>
    <div class="muted small">后台不代跑发布 —— 发布带逐文件校验与 nginx 配置试跑两道门，代跑等于绕过它们。</div>
    <div class="secret" style="margin-top:10px">python scripts/deploy.py --merchant ${esc(m.code)} --no-build</div>
    <div class="row" style="margin-top:12px"><button class="btn" id="cpCmd">复制命令</button>
    <button class="btn gray" data-close>关闭</button></div>`));
  const cp = $("cpCmd");
  if (cp) cp.addEventListener("click", () => copyText(
    `python scripts/deploy.py --merchant ${m.code} --no-build`));
  bind("btnRot", () => rotateSecret(m));
  bind("btnTake", () => takeover(m));
  bind("btnDisc", () => disconnect(m));
}

/* ------------------------------------------------------------------ 动作 */

async function toggleMerchant(m) {
  if (m.enabled) {
    const ok = await confirmImpact(m.code, "disable", `停用商户「${m.title}」？`,
      { okLabel: "停用", hint: "停用后密钥与客户端都留着，随时可启用。" });
    if (!ok) return;
    await api("/api/admin/merchants", "POST", { code: m.code, enabled: false });
    toast("已停用");
  } else {
    await api("/api/admin/merchants", "POST", { code: m.code, enabled: true });
    toast("已启用");
  }
  await loadHealth(true);
  renderDetail($("view"), m.code);
}

async function editMinVer(m) {
  const v = prompt(`商户「${m.code}」的最低 Agent 版本（三段号，如 0.2.0；留空 = 不强制）\n`
    + "装机版本低于它时，商户列表那一行会标黄 —— 这就是灰度的抓手。", m.min_agent_version || "");
  if (v === null) return;
  try {
    await api("/api/admin/merchants", "POST", { code: m.code, min_agent_version: v.trim() });
    toast("已设置"); await loadHealth(true); renderDetail($("view"), m.code);
  } catch (e) { toast(e.message, true); }
}

async function unpublishPrefix(m) {
  const ok = await confirmImpact(m.code, "unpublish", `收掉访问前缀 /${m.code}/？`,
    { okLabel: "收链", hint: "只删 nginx 上这一条软链；构建目录与别家的链都不动，也不需要 reload。" });
  if (!ok) return;
  try {
    const r = await api(`/api/admin/merchants/${encodeURIComponent(m.code)}/unpublish-prefix`, "POST", {});
    toast(r.message || "已收链");
    await loadHealth(true);
    renderDetail($("view"), m.code);
  } catch (e) { toast(e.message, true); }
}

async function dropMerchant(m) {
  // 删除的闭环：前缀还开着就先收 —— 否则公网上会留一个"能打开、但永远停在登录页"的地址，
  // 而它对应的商户已经从平台名单里消失了，谁都想不起来它是谁的。
  if (m.prefix.published) {
    const ok = await confirmImpact(m.code, "unpublish",
      `删商户前要先收掉 /${m.code}/`,
      { okLabel: "先收链", hint: "收链只删这一条软链，不碰构建目录与别家。收完再继续删除。" });
    if (!ok) return;
    try {
      await api(`/api/admin/merchants/${encodeURIComponent(m.code)}/unpublish-prefix`, "POST", {});
      toast("前缀已下线");
      await loadHealth(true);
      m = (await api(`/api/admin/merchants/${encodeURIComponent(m.code)}`)).data;   // 重新取一遍，门的状态变了
    } catch (e) { toast(e.message, true); return; }
  }
  const ok2 = await confirmImpact(m.code, "delete", `下线删除商户「${m.code}」？`,
    { okLabel: "确认删除", hint: "只是临时停服请点「停用」。删除后客户机立刻连不上，且要重新发密钥。" });
  if (!ok2) return;
  try {
    await api(`/api/admin/merchants/${encodeURIComponent(m.code)}?confirm=delete`, "DELETE", {});
    toast("已删除"); detailCode = null; await loadHealth(true); go("merchants");
  } catch (e) { toast(e.message, true); }
}

async function rotateSecret(m) {
  const box = modal(`<h3>轮换「${esc(m.code)}」的 Agent 密钥</h3>
    <div class="fx">客户机上的 Agent 会在<b>下次重连时</b>连不上，必须把新密钥填进配置文件再启动。<br>
    正在跑的连接不受影响 —— 也就是说：换完密钥没重装，问题会拖到下次断线才暴露。</div>
    <div class="row" style="margin-top:12px"><input id="ns" placeholder="留空则自动生成" style="flex:1">
    <button class="btn" id="ok">生成并保存</button><button class="btn gray" data-close>取消</button></div>`);
  box.querySelector("#ok").addEventListener("click", async () => {
    try {
      const r = await api(`/api/admin/agents/${encodeURIComponent(m.code)}/secret`, "POST",
        { secret: box.querySelector("#ns").value.trim() });
      modal(`<h3>新密钥（只显示这一次）</h3><div class="secret">${esc(r.secret || "(已保存，值不再回显)")}</div>
        <div class="row" style="margin-top:12px"><button class="btn" id="cp">复制</button>
        <button class="btn gray" data-close>知道了</button></div>`);
      $("cp").addEventListener("click", () => copyText(r.secret || ""));
    } catch (e) { toast(e.message, true); }
  });
}

async function takeover(m) {
  const ok = await confirmImpact(m.code, "takeover", `放行「${m.code}」换机接管？`,
    { okLabel: "放行 3 分钟", hint: "确定后让对方机器现在重连；过了窗口作废，现网连接不受影响。" });
  if (!ok) return;
  try {
    const r = await api(`/api/admin/agents/${encodeURIComponent(m.code)}/takeover`, "POST", {});
    toast(r.message || "已放行 3 分钟");
    await loadHealth(true); renderDetail($("view"), m.code);
  } catch (e) { toast(e.message, true); }
}

async function disconnect(m) {
  try {
    await api(`/api/admin/agents/${encodeURIComponent(m.code)}/disconnect`, "POST", {});
    toast("已断开，客户机会自动重连"); renderDetail($("view"), m.code);
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ 开通向导 */

function randPw() {
  const a = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789#@%";
  return Array.from(crypto.getRandomValues(new Uint8Array(14))).map(x => a.charAt(x % a.length)).join("");
}

const W = { step: 1, code: "", title: "", access: "auth", minVer: "", dbHint: "", user: "", bundle: null, saved: {} };

function openWizard() {
  W.step = 1; W.code = ""; W.title = ""; W.access = "auth"; W.minVer = ""; W.dbHint = "";
  W.user = ""; W.bundle = null; W.saved = {};
  const box = modal(`<h3>开通新商户<span class="muted small" style="font-weight:400"> · 4 步</span></h3>
    <div id="wz"></div>`);
  paintWizard(box);
}

function stepsBar() {
  const names = ["基本信息", "生成凭据", "等首次接入", "发布前缀"];
  return `<div class="steps">${names.map((n, i) => {
    const k = i + 1;
    return `<div class="step ${k < W.step ? "done" : k === W.step ? "cur" : ""}">
      <div class="n">第 ${k} 步</div><div class="t">${n}</div>
      <div class="e">${k < W.step ? "已完成" : k === W.step ? "进行中" : "待做"}</div></div>`;
  }).join("")}</div>`;
}

function paintWizard(box) {
  box = box || $("modalBox");
  box.innerHTML = stepsBar() + `<div id="wzb" style="margin-top:14px"></div>`;
  const b = $("wzb");
  if (W.step === 1) {
    b.innerHTML = `
      <label>商户码（= 访问前缀 = 客户端机上的 Agent 名）</label>
      <input id="wCode" value="${esc(W.code)}" placeholder="如 hengjun" style="width:100%">
      <div class="small" id="wCodeMsg" style="min-height:20px;margin-top:4px"></div>
      <label>显示名</label><input id="wTitle" value="${esc(W.title)}" placeholder="如 恒钧五金" style="width:100%">
      <label>入口方式</label>
      <select id="wAccess" style="width:100%"><option value="auth">必须登录（推荐）</option><option value="guest">免登录访客</option></select>
      <label>账套清单（一行一本，<b>第一本是默认库</b>；一台主机两本账就填两行）</label>
      <textarea id="wDb" rows="2" style="width:100%;font-family:Consolas,monospace">${esc(W.dbHint)}</textarea>
      <div class="small muted">清单决定后台把"实测账套不等于商户码"判成故障还是判成正常多账套；
        留空则按老规矩「实测 == 商户码」判。</div>
      <label>最低版本门槛（留空不强制）</label><input id="wMin" value="${esc(W.minVer)}" placeholder="0.2.0" style="width:100%">
      <label>首个应用账号（留空 = 商户码-admin）</label><input id="wUser" value="${esc(W.user)}" style="width:100%">
      <div class="muted small" style="margin:10px 0">★ 商户码之后会同时出现在 URL 前缀、Agent 自报名、云端登记名三处，
      填错了要删掉重开（删商户会连带换密钥），所以这一步就把它验死。</div>
      <div class="row"><button class="btn" id="wNext">下一步：生成凭据</button>
      <button class="btn gray" data-close>取消</button></div>`;
    $("wAccess").value = W.access;
    let tid = null;
    const precheck = async () => {
      const code = $("wCode").value.trim();
      W.code = code; W.title = $("wTitle").value.trim(); W.access = $("wAccess").value;
      W.dbHint = $("wDb").value.trim(); W.minVer = $("wMin").value.trim(); W.user = $("wUser").value.trim();
      const msg = $("wCodeMsg");
      if (!code) { msg.innerHTML = ""; return; }
      clearTimeout(tid);
      msg.innerHTML = '<span class="muted">检查中…</span>';
      try {
        const r = (await api("/api/admin/merchant-check?code=" + encodeURIComponent(code))).data;
        if (!msg.isConnected) return;
        msg.innerHTML = r.ok
          ? `<span style="color:#15803d">✓ 格式可用，且没被占用</span>
             <div class="muted">访问地址 …${esc(r.url_prefix)} · 客户端机上要填的名称 ${esc(r.code)}</div>`
          : `<span style="color:#b91c1c">✗ ${esc((r.reasons.concat(r.taken_by.map(x => "已被" + x + "占用"))).join("；"))}</span>`;
      } catch (e) { if (msg.isConnected) msg.innerHTML = `<span style="color:#b91c1c">${esc(e.message)}</span>`; }
    };
    ["wCode", "wTitle", "wAccess", "wDb", "wMin", "wUser"].forEach(id => {
      $(id).addEventListener("input", () => {
        if (id === "wCode") { tid = setTimeout(precheck, 320); }
        else { W[{ wTitle: "title", wAccess: "access", wDb: "dbHint", wMin: "minVer", wUser: "user" }[id]] = $(id).value.trim(); }
      });
      if (id === "wCode") $(id).addEventListener("blur", precheck);
    });
    $("wNext").addEventListener("click", doCreate);
    if (W.code) precheck();
  } else if (W.step === 2) {
    const bd = W.bundle;
    b.innerHTML = `
      <div class="banner bad" style="margin-bottom:10px"><b>下面这些只显示这一次</b>，关掉页面就取不到。</div>
      ${[["Agent 密钥", "secret"], ["入口令牌", "token"], [`${bd.user} 的初始口令`, "pw"]].map(([l, k]) =>
        `<div class="kv"><span class="k">${esc(l)}</span><span><code>${esc(String(bd[k]).slice(0, 10))}…</code>
          <button class="btn link" data-cp="${k}">复制</button></span></div>`).join("")}
      <div class="secret" style="margin-top:10px">${esc(W.cfgSnippet)}</div>
      <div class="muted small" style="margin:8px 0 12px">账套名与本地库口令要现场填 —— <b>缺账套名时客户端机会直接拒绝启动</b>，
      这是刻意的：防止用 A 家的名字读 B 家的账。</div>
      <div class="card" style="background:#fffbeb;border-color:#fde68a;margin:0 0 10px">
        <b class="small">离开前确认已保存</b>
        ${[["secret", "密钥已存进密码管理器"], ["pw", "初始口令已交给客户"], ["cfg", "配置片段已发给装机人"]].map(([k, t]) =>
          `<label class="row" style="margin:6px 0;align-items:flex-start">
            <input type="checkbox" data-saved="${k}" ${W.saved[k] ? "checked" : ""}> <span>${t}</span></label>`).join("")}
      </div>
      <div class="row"><button class="btn" id="wNext2" disabled>下一步：等接入</button>
      <button class="btn gray" data-close>取消（已生成的密钥将取不回）</button></div>`;
    b.querySelectorAll("[data-cp]").forEach(x => x.addEventListener("click", () => {
      copyText(bd[x.dataset.cp] || ""); W.saved[x.dataset.cp === "secret" ? "secret" : x.dataset.cp] = true;
    }));
    b.querySelectorAll("[data-saved]").forEach(cb => cb.addEventListener("change", () => {
      W.saved[cb.dataset.saved] = cb.checked;
      $("wNext2").disabled = !(W.saved.secret && W.saved.pw && W.saved.cfg);
    }));
    $("wNext2").addEventListener("click", () => { W.step = 3; paintWizard(); pollJoin(); });
  } else if (W.step === 3) {
    b.innerHTML = `<div id="wJoin"><div class="banner">正在等客户端机连上来（每 5 秒查一次）…</div></div>
      <div class="card" style="margin:0 0 10px"><b class="small">连不上就按这个顺序查（从两次真实事故里排出来的）</b>
        <div class="muted small" style="line-height:1.9;margin-top:6px">
        ① 配置文件是不是<b>放在程序同目录</b> —— 放错目录会读到另一份，那是最难查的一种"看着连上了但数据是别家的"<br>
        ② 账套名填了没有（不填就是拒绝启动，界面上会写缺哪个键）<br>
        ③ 商户码是否与刚才生成的完全一致（差一个字母 = 登记名不存在）</div></div>
      <div class="row"><button class="btn" id="wSkip">跳过，稍后再看</button></div>`;
    $("wSkip").addEventListener("click", () => { W.step = 4; paintWizard(); });
    pollJoin();
  } else {
    b.innerHTML = `<div class="kv"><span class="k">前缀状态</span><span id="wPfx">…</span></div>
      <div class="muted small" style="margin:8px 0">发布由 uniapp 的发布脚本完成（带逐文件校验与 nginx 配置试跑两道门），
      <b>后台不代跑</b> —— 代跑等于把这两道门绕过。要发布就复制这条命令：</div>
      <div class="secret">python scripts/deploy.py --merchant ${esc(W.code)} --no-build</div>
      <div class="row" style="margin-top:12px"><button class="btn" id="wDone">完成，去看这家</button></div>`;
    const showPfx = async () => {
      try {
        await loadHealth(true);
        const m = S.health.find(x => x.code === W.code);
        $("wPfx").innerHTML = m && m.prefix.published
          ? U.tag("已发布 → " + m.prefix.build_id, "on") : U.tag("未发布", "gray");
      } catch (e) { $("wPfx").textContent = "读不到"; }
    };
    showPfx();
    $("wDone").addEventListener("click", () => { closeModal(); go("merchants", { code: W.code }); });
  }
}

async function doCreate() {
  const code = (W.code || "").trim();
  if (!/^[a-z][a-z0-9-]{1,29}$/.test(code)) {
    toast("商户码不合法：小写字母开头，可含数字与 -，2-30 位", true);
    return;
  }
  const btn = $("wNext"); if (btn) btn.disabled = true;
  try {
    const r = await api("/api/admin/merchants", "POST", {
      code, title: W.title || code, enabled: true, databases: W.dbHint, min_agent_version: W.minVer });
    const c = await api("/api/admin/clients", "POST",
      { name: code, title: W.title || code, agent: code, access: W.access });
    const user = W.user || (code + "-admin");
    const pw = randPw();
    await api("/api/admin/accounts", "POST", { username: user, password: pw,
      name: (W.title || code) + " 管理员", stores: [code], all_stores: false });
    W.bundle = { code, title: W.title || code, secret: r.agent_secret || "(同名 Agent 已存在，未新生成)",
                 token: (c.client || {}).token || "", user, pw };
    W.cfgSnippet = JSON.stringify({
      agent: { name: code, secret: W.bundle.secret, relay_url: location.origin.replace(/^http/, "ws") + "/ws/agent" },
      sql: { server: "127.0.0.1", port: 1433, user: "sa", password: "",
             // 装机时先给默认库（清单第一本）；老板要看第二本时由平台方切或走后面的账套路由
             database: (W.dbHint || "").split(/[\s,，、]+/).filter(Boolean)[0] || "" }}, null, 2);
    W.step = 2;
    await loadHealth(true);
    paintWizard();
  } catch (e) {
    toast(e.message, true);
    if (btn) btn.disabled = false;
  }
}

let joinTimer = null;
async function pollJoin() {
  if (joinTimer) clearInterval(joinTimer);
  const once = async () => {
    const box = $("wJoin");
    if (!box) { clearInterval(joinTimer); return; }
    try {
      const m = (await api(`/api/admin/merchants/${encodeURIComponent(W.code)}?days=1`)).data;
      box.innerHTML = m.online
        ? `<div class="banner ok"><b>已连上</b> · 版本 ${esc(m.version)} · 主机 ${esc(m.hostname)} ·
            指纹 <code>${esc((m.install_id || "").slice(0, 12))}</code> · 上报 ${m.ops.length} 个动作</div>
           <div class="kv"><span class="k">实测账套</span><span>${dbCell(m)}</span></div>`
        : `<div class="banner">还没连上（已查 ${esc(new Date().toLocaleTimeString())}）。
             登记 ${m.agent_registered ? "已就绪" : "缺失"}，历史上线 ${m.last_online_at ? U.ts(m.last_online_at) : "无"}。</div>`;
      if (m.online) {
        clearInterval(joinTimer); joinTimer = null;
        const row = document.createElement("div");
        row.className = "row"; row.style.marginTop = "10px";
        row.innerHTML = `<button class="btn" id="wGo4">下一步：前缀</button>`;
        box.parentElement.appendChild(row);
        $("wGo4").addEventListener("click", () => { W.step = 4; paintWizard(); });
      }
    } catch (e) { box.innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; }
  };
  once();
  joinTimer = setInterval(once, 5000);
}
