/* 设置 + 批量视图。
 * 这里放的是"跨家的、一次配好就不太动"的东西：平台参数、Agent 登记、客户端令牌、
 * 应用账号、老板授权、手动发一条 op。日常按商户的操作都在「商户」详情里。 */
import { api, toast, modal, closeModal, copyText } from "../api.js";
import { S, loadHealth } from "../app.js";
import * as U from "../ui.js";

export const title = "设置";
const esc = U.esc;
let tab = "params";

function $(id) { return document.getElementById(id); }

export async function mount(el) {
  el.innerHTML = `<div class="tabs" id="tabs"></div><div id="tabBody"></div>`;
  const tabs = [["params", "平台参数"], ["agents", "Agent 登记"], ["tokens", "客户端令牌"],
                ["accounts", "应用账号"], ["v3op", "V3 操作员"], ["users", "老板授权"],
                ["rpc", "调试工具"]];
  $("tabs").innerHTML = tabs.map(([k, n]) => `<div data-t="${k}" class="${k === tab ? "on" : ""}">${n}</div>`).join("");
  $("tabs").querySelectorAll("[data-t]").forEach(d =>
    d.addEventListener("click", () => { tab = d.dataset.t; mount(el); }));
  await renderTab();
}

async function renderTab() {
  const b = $("tabBody");
  b.innerHTML = '<div class="muted small">加载中…</div>';
  try {
    if (tab === "params") return await params(b);
    if (tab === "agents") return await agents(b);
    if (tab === "tokens") return await tokens(b);
    if (tab === "accounts") return await accounts(b);
    if (tab === "v3op") return await v3ops(b);
    if (tab === "users") return await users(b);
    return await rpc(b);
  } catch (e) { b.innerHTML = `<div class="banner bad">${esc(e.message)}</div>`; }
}

/* ---------------- 平台参数 ---------------- */
async function params(b) {
  const st = await api("/api/admin/settings");
  b.innerHTML = `<div class="grid g2">
    <div class="card"><h2>运行参数</h2>
      <label>请求超时（秒，5-300）</label><input id="pTimeout" value="${esc(st.rpc_timeout || 60)}" style="width:100%">
      <label>全局最低 Agent 版本（留空不强制）</label>
      <input id="pMinVer" value="${esc(st.min_agent_version || "")}" placeholder="0.2.0" style="width:100%">
      <div class="muted small" style="margin-top:6px">灰度抓手：先给个别商户单独设门槛，全局值兜底。</div>
      <label class="row" style="margin-top:10px"><input type="checkbox" id="pOpenid" ${st.require_openid ? "checked" : ""}>
        <span>连免登录入口也要求微信授权（微信通道就绪后再开）</span></label>
      <button class="btn" id="pSave" style="margin-top:12px">保存参数</button>
    </div>
    <div class="card"><h2>凭据与通道<span class="r">明文直出，抄走前确认没人看屏</span></h2>
      ${U.kv("后台密码", (st.admin_password || st.admin_password_set)
        ? `<code>${esc(st.admin_password || "")}</code> <button class="btn link" data-cp="${esc(st.admin_password || "")}">复制</button>`
        : U.tag("未设置", "off"))}
      <label>改后台密码（留空不改）</label><input id="pPwd" type="password" style="width:100%" autocomplete="new-password">
      <div class="muted small" style="margin-top:6px">★ 后台口令与下面这些密钥是<b>明文接口 + 明文页面</b>（2026-09-26 拍板：
      审查时查不到密钥的代价更大 —— 那天为了查一把密钥把现网顶下线 50 分钟）。
      口令本身仍明文存在配置文件里；改哈希要一次带保护窗口的迁移，失败就把自己锁在外面，得有人盯着做。</div>
      ${U.kv("令牌签名密钥", st.token_secret
        ? `<code>${esc(st.token_secret)}</code> <button class="btn link" data-cp="${esc(st.token_secret)}">复制</button>`
        : U.tag("未设置", "off"))}
      <div class="muted small" style="margin-top:2px">换它 = 已发出的 accessToken/refreshToken 当场全部作废，
      所有手机端都要重新登录。只在怀疑泄露时才动。</div>
      <label>入口令牌（仅退役的原生小程序通道用）</label><input id="pPortal" value="${esc(st.portal_token || "")}" style="width:100%">
      <div style="margin-top:12px"><b class="small">微信登录</b></div>
      ${U.kv("启用", st.wechat && st.wechat.enabled ? U.tag("是", "on") : U.tag("否", "gray"))}
      ${U.kv("AppID", esc((st.wechat || {}).appid || "未填"))}
      ${U.kv("AppSecret", (st.wechat || {}).secret
        ? `<code>${esc(st.wechat.secret)}</code> <button class="btn link" data-cp="${esc(st.wechat.secret)}">复制</button>`
        : (st.wechat || {}).secret_set ? U.tag("已配置（值未回显）", "on") : U.tag("未填", "off"))}
      <label>AppID</label><input id="wAppid" value="${esc((st.wechat || {}).appid || "")}" style="width:100%">
      <label>AppSecret（留空不改）</label><input id="wSecret" style="width:100%">
      <label class="row" style="margin-top:8px"><input type="checkbox" id="wEnabled" ${st.wechat && st.wechat.enabled ? "checked" : ""}>
        <span>启用微信登录</span></label>
      <button class="btn" id="wSave" style="margin-top:12px">保存微信配置</button>
      ${U.kv("日志目录", `<code>${esc(st.log_dir || "")}</code>`)}
    </div></div>`;
  $("pSave").addEventListener("click", async () => {
    try {
      await api("/api/admin/settings", "POST", {
        rpc_timeout: parseInt($("pTimeout").value, 10) || 60,
        min_agent_version: $("pMinVer").value.trim(),
        require_openid: $("pOpenid").checked });
      toast("已保存"); await loadHealth(true);
    } catch (e) { toast(e.message, true); }
  });
  $("wSave").addEventListener("click", async () => {
    const body = { wechat: { appid: $("wAppid").value.trim(), enabled: $("wEnabled").checked,
                             secret: $("wSecret").value.trim() },
                   portal_token: $("pPortal").value.trim() };
    if ($("pPwd").value) body.password = $("pPwd").value;
    try { await api("/api/admin/settings", "POST", body); toast("已保存"); renderTab(); }
    catch (e) { toast(e.message, true); }
  });
  b.querySelectorAll("[data-cp]").forEach(x => x.addEventListener("click", () => copyText(x.dataset.cp)));
}

/* ---------------- Agent 登记 ---------------- */
async function agents(b) {
  const st = await api("/api/admin/state");
  const rows = st.agents || [];
  b.innerHTML = `<div class="card"><h2>Agent 登记<span class="r">${rows.length} 条</span></h2>
    <div class="muted small" style="margin-bottom:10px">名称必须等于商户码（一家多分店要真隔离就再发一个码）。
    新建商户请走「商户 → 开通新商户」，那里会把密钥、入口令牌、首个账号一次配齐。</div>
    ${U.table([["名称"], ["Agent 密钥"], ["在线"], ["实测账套"], ["版本"], ["主机名 / 指纹"], ["动作"], ["最后上线"], [""]],
      rows.map(a => `<tr>
        ${U.td(`<code>${esc(a.name)}</code>`, "名称")}
        ${U.td(`<code>${esc(a.secret || "（未登记）")}</code>
                <button class="btn link" data-cp="${esc(a.secret || "")}">复制</button>`, "Agent 密钥")}
        ${U.td(a.online ? U.tag("在线", "on") : U.tag("离线", "gray"), "在线")}
        ${U.td(U.dbTag(a), "实测账套")}
        ${U.td(esc(a.version || "—") + (a.version_too_old ? " " + U.tag("偏旧", "warn") : ""), "版本")}
        ${U.td(`${esc(a.hostname || "—")}<div class="muted small"><code>${esc((a.install_id || "").slice(0, 12))}</code></div>`, "主机名 / 指纹")}
        ${U.td(String((a.ops || []).length), "动作")}
        ${U.td(esc(U.ts(a.last_online_at) || "—"), "最后上线")}
        ${U.td(`<button class="btn link" data-rot="${esc(a.name)}">轮换密钥</button>
                <button class="btn link" data-tk="${esc(a.name)}">接管</button>`, "")}</tr>`).join(""))}
    <div class="muted small" style="margin-top:8px">★ 密钥这里是明文直出（2026-09-26 拍板：查密钥不该靠"轮换"，
    轮换＝现网当场掉线）。代价：F12、截图、远程协助投屏都看得到 —— 有人站在屏幕前时别开这一页。</div>
  </div>
  <div class="card"><h2>新增登记</h2><div class="row">
    <input id="agName" placeholder="名称（= 商户码）" style="min-width:200px">
    <input id="agSecret" placeholder="密钥（留空自动生成）" style="min-width:200px">
    <button class="btn" id="agAdd">保存登记</button></div>
    <div class="muted small" style="margin-top:8px">同名提交会被拒（409）—— 以前这里会静默换掉在线那家的密钥，
    表现为"客户接口突然全挂"。要换密钥请用上面的「轮换密钥」。</div></div>`;
  $("agAdd").addEventListener("click", async () => {
    try {
      const r = await api("/api/admin/agents", "POST",
        { name: $("agName").value.trim(), secret: $("agSecret").value.trim() });
      modal(`<h3>已登记</h3><div class="secret">${esc(r.secret || "")}</div>
        <div class="muted small" style="margin-top:8px">这一行的密钥在上面的表格里也一直看得到（明文直出），
        忘了就不用重登记，直接回来抄。</div>
        <button class="btn" data-close style="margin-top:10px">知道了</button>`);
      renderTab();
    } catch (e) { toast(e.message, true); }
  });
  b.querySelectorAll("[data-cp]").forEach(x => x.addEventListener("click", () => copyText(x.dataset.cp)));
  b.querySelectorAll("[data-rot]").forEach(x => x.addEventListener("click", () => rotAgent(x.dataset.rot)));
  b.querySelectorAll("[data-tk]").forEach(x => x.addEventListener("click", async () => {
    try {
      const r = await api(`/api/admin/agents/${encodeURIComponent(x.dataset.tk)}/takeover`, "POST", {});
      toast(r.message || "已放行 3 分钟");
    } catch (e) { toast(e.message, true); }
  }));
}

async function rotAgent(name) {
  const box = modal(`<h3>轮换「${esc(name)}」的密钥</h3>
    <div class="fx">客户机下次重连时才会用新密钥 —— 换完不重装，问题会拖到下次断线才暴露。</div>
    <div class="row" style="margin-top:12px"><input id="ns" placeholder="留空自动生成" style="flex:1">
    <button class="btn" id="ok">生成</button><button class="btn gray" data-close>取消</button></div>`);
  box.querySelector("#ok").addEventListener("click", async () => {
    try {
      const r = await api(`/api/admin/agents/${encodeURIComponent(name)}/secret`, "POST",
        { secret: box.querySelector("#ns").value.trim() });
      closeModal();
      modal(`<h3>新密钥（只显示这一次）</h3><div class="secret">${esc(r.secret || "已保存")}</div>
        <div class="row" style="margin-top:12px"><button class="btn" id="cp">复制</button>
        <button class="btn gray" data-close>知道了</button></div>`);
      $("cp").addEventListener("click", () => copyText(r.secret || ""));
    } catch (e) { toast(e.message, true); }
  });
}

/* ---------------- 客户端令牌 ---------------- */
async function tokens(b) {
  const st = await api("/api/admin/state");
  const agents = (st.agents || []).map(a => a.name);
  b.innerHTML = `<div class="card"><h2>客户端令牌<span class="r">${(st.clients || []).length} 个入口</span></h2>
    ${U.table([["名称"], ["显示名"], ["归属商户"], ["访问"], ["令牌"], [""]],
      (st.clients || []).map(c => `<tr>
        ${U.td(esc(c.name), "名称")}${U.td(esc(c.title || ""), "显示名")}
        ${U.td(esc(c.agent || "") + (c.agent ? "" : " " + U.tag("未归属", "off")), "归属商户")}
        ${U.td(U.tag(c.access === "auth" ? "必须登录" : "免登录", c.access === "auth" ? "on" : "warn"), "访问")}
        ${U.td(`<code>${esc(c.token || "")}</code>
                <button class="btn link" data-cp="${esc(c.token)}">复制</button>`, "令牌")}
        ${U.td(`<button class="btn link" data-del="${esc(c.token)}">删除</button>`, "")}</tr>`).join(""))}
  </div>
  <div class="card"><h2>新增入口</h2><div class="row">
    <input id="cName" placeholder="名称，如 hengjun-h5" style="min-width:180px">
    <input id="cTitle" placeholder="显示名" style="min-width:150px">
    <select id="cAgent">${agents.map(a => `<option>${esc(a)}</option>`).join("")}</select>
    <select id="cAccess"><option value="auth">必须登录</option><option value="guest">免登录访客</option></select>
    <button class="btn" id="cAdd">新增</button></div>
    <div class="muted small" style="margin-top:8px">归属必须指向已登记的商户，打错字会被当场拒 ——
    以前这里会兜到某一家，后果是"整家商户的流量静默进别家的库，数字全都正常，只是不是这家的"。</div></div>`;
  $("cAdd").addEventListener("click", async () => {
    try {
      const r = await api("/api/admin/clients", "POST", { name: $("cName").value.trim(),
        title: $("cTitle").value.trim(), agent: $("cAgent").value, access: $("cAccess").value });
      modal(`<h3>新令牌（只显示这一次）</h3><div class="secret">${esc((r.client || {}).token || "")}</div>
        <button class="btn" data-close style="margin-top:10px">知道了</button>`);
      await loadHealth(true); renderTab();
    } catch (e) { toast(e.message, true); }
  });
  b.querySelectorAll("[data-cp]").forEach(x => x.addEventListener("click", () => copyText(x.dataset.cp)));
  b.querySelectorAll("[data-del]").forEach(x => x.addEventListener("click", async () => {
    if (!confirm("删除这个入口令牌？用它登录的端会立刻 401。")) return;
    try { await api("/api/admin/clients/" + encodeURIComponent(x.dataset.del), "DELETE", {});
      await loadHealth(true); renderTab(); } catch (e) { toast(e.message, true); }
  }));
}

/* ---------------- 应用账号 ---------------- */

// 可分配的权限位。★ 真源在云端 security.PERMS，这里只是把它列出来给人勾 ——
// 接口那边还会再校验一次，所以界面只是入口，不是安全边界。
const PERMS = [
  ["sessions.view", "成员管理（看在线）"],
  ["sessions.kick", "踢出成员"],
];

function permBoxes(a) {
  const has = a.perms || [];
  return PERMS.map(([k, label]) =>
    `<label class="row" style="margin:0 14px 0 0;white-space:nowrap">` +
    `<input type="checkbox" class="aPerm" data-user="${esc(a.username)}" value="${esc(k)}"` +
    `${has.indexOf(k) >= 0 ? " checked" : ""}> ${esc(label)}</label>`).join("");
}

async function accounts(b) {
  const st = await api("/api/admin/state");
  const codes = (S.health || []).map(m => m.code);
  b.innerHTML = `<div class="card"><h2>应用账号<span class="r">${(st.accounts || []).length} 个</span></h2>
    ${U.table([["账号"], ["姓名"], ["可及商户"], ["权限"], ["状态"], [""]],
      (st.accounts || []).map(a => `<tr>
        ${U.td(esc(a.username), "账号")}${U.td(esc(a.name || ""), "姓名")}
        ${U.td((a.all_stores ? U.tag("全平台可达", "warn") : esc((a.stores || []).join("、"))), "可及商户")}
        ${U.td(permBoxes(a), "权限")}
        ${U.td(a.disabled ? U.tag("已禁用", "off") : U.tag("正常", "on"), "状态")}
        ${U.td(`<button class="btn link" data-pw="${esc(a.username)}">重置口令</button>
                <button class="btn link" data-adel="${esc(a.username)}">删除</button>`, "")}</tr>`).join(""))}
    <div class="muted small" style="margin-top:8px">★ 客户账号必须只绑自己那一家。开了「全平台可达」，
    在裸 IP 入口（不带商户前缀）上所有请求都会因为"没点名商户"被拒 —— 客户的老书签当场进不去，这事真发生过。<br>
    ★ 权限勾选**改完立刻生效**：勾上「成员管理」后，该账号在 App 的「我的」页会出现入口
    （已经登录的需重新登录一次才拿到新权限）；云端会话接口那边还会再校验一次，双保险。</div>
  </div>
  <div class="card"><h2>新增 / 修改账号</h2><div class="row">
    <input id="aUser" placeholder="账号名" style="min-width:140px">
    <input id="aPw" placeholder="口令（新建必填；已存在则视为重置）" style="min-width:220px">
    <input id="aName" placeholder="姓名" style="min-width:120px"></div>
    <div class="row" style="margin-top:8px">${codes.map(c =>
      `<label class="row" style="margin:0 10px 0 0"><input type="checkbox" class="aCode" value="${esc(c)}"> ${esc(c)}</label>`).join("")}
      ${PERMS.map(([k, label]) =>
        `<label class="row" style="margin:0 0 0 14px;white-space:nowrap"><input type="checkbox" class="aPermNew" value="${esc(k)}"> ${esc(label)}</label>`).join("")}
      </div>
    <div class="row" style="margin-top:8px"><button class="btn" id="aSave">保存</button></div></div>`;
  $("aSave").addEventListener("click", async () => {
    try {
      await api("/api/admin/accounts", "POST", { username: $("aUser").value.trim(),
        password: $("aPw").value, name: $("aName").value.trim(),
        stores: Array.from(document.querySelectorAll(".aCode:checked")).map(x => x.value),
        all_stores: false,
        perms: Array.from(document.querySelectorAll(".aPermNew:checked")).map(x => x.value) });
      toast("已保存"); await loadHealth(true); renderTab();
    } catch (e) { toast(e.message, true); }
  });
  // ★ 行内权限勾选：勾一下立刻保存。
  //   为什么不做成"填整表单再保存"：那个表单是整条覆盖，改权限还得把商户重新勾对，
  //   漏勾一次就把账号的可及商户清空了 —— 让改权限只需要改权限。
  //   ★ 失败时把勾**回到原样**，不让界面显示一个没存上的状态。
  b.querySelectorAll(".aPerm").forEach(x => x.addEventListener("change", async () => {
    const user = x.dataset.user;
    const boxes = Array.from(b.querySelectorAll(".aPerm"))
      .filter(y => y.dataset.user === user);
    const perms = boxes.filter(y => y.checked).map(y => y.value);
    try {
      await api("/api/admin/accounts/" + encodeURIComponent(user) + "/perms",
        "POST", { perms });
      toast("权限已保存");
      renderTab();
    } catch (e) {
      toast(e.message, true);
      x.checked = !x.checked;
    }
  }));
  b.querySelectorAll("[data-pw]").forEach(x => x.addEventListener("click", async () => {
    const pw = prompt(`为「${x.dataset.pw}」设置新口令（至少 8 位）：`);
    if (!pw) return;
    try { await api(`/api/admin/accounts/${encodeURIComponent(x.dataset.pw)}/password`, "POST", { password: pw }); toast("已重置"); }
    catch (e) { toast(e.message, true); }
  }));
  b.querySelectorAll("[data-adel]").forEach(x => x.addEventListener("click", async () => {
    if (!confirm(`删除账号「${x.dataset.adel}」？该账号的登录态会立刻失效。`)) return;
    try { await api("/api/admin/accounts/" + encodeURIComponent(x.dataset.adel), "DELETE", {}); renderTab(); }
    catch (e) { toast(e.message, true); }
  }));
}

/* ---------------- V3 操作员权限 ---------------- */
//
// ★ 为什么单独一页、不能并进「应用账号」：那张表是**平台账号**（账号+口令登录），
//   而 V3 操作员是**账套库 UserInfo 里的人**（`v3:<商户>:<编号>`）——
//   云端读不到那本库，也不能往人家的表里加字段。所以这里只记"云端侧附加属性"。
// ★ 键是 `<商户码>:<编号>`：同一编号在两家商户是**两个不同的人**。
// ★ 这里只能管"云端能管的事"（App 里能不能看/踢在线成员）。
//   要**停用这个人本身**，得去财鲸 V3 的「操作员」里停 —— 云端管不着那个库。

function v3PermBoxes(o) {
  const has = o.perms || [];
  return PERMS.map(([k, label]) =>
    `<label class="row" style="margin:0 14px 0 0;white-space:nowrap">` +
    `<input type="checkbox" class="v3Perm" data-code="${esc(o.code)}" data-uid="${esc(o.uid)}"` +
    ` value="${esc(k)}"${has.indexOf(k) >= 0 ? " checked" : ""}> ${esc(label)}</label>`).join("");
}

async function v3ops(b) {
  const r = await api("/api/admin/v3operators");
  const rows = r.operators || [];
  const codes = (S.health || []).map(m => m.code);
  b.innerHTML = `<div class="card"><h2>已授权的 V3 操作员<span class="r">${rows.length} 个</span></h2>
    ${rows.length ? U.table([["商户"], ["操作员编号"], ["权限"], ["更新时间"], [""]],
      rows.map(o => `<tr>
        ${U.td(esc(o.code), "商户")}${U.td(esc(o.uid), "操作员编号")}
        ${U.td(v3PermBoxes(o), "权限")}
        ${U.td(esc(o.updated_at || "—"), "更新时间")}
        ${U.td(`<button class="btn link" data-vrevoke="${esc(o.code)}|${esc(o.uid)}">踢出全部会话</button>`, "")}
      </tr>`).join(""))
      : '<div class="muted small" style="padding:12px">还没有给任何 V3 操作员配权限。用下面那个表单加一个。</div>'}
    <div class="muted small" style="margin-top:8px">★ 云端拿不到账套库的操作员名单，所以这里**手填编号**
    （编号就是他在财鲸 V3 里的操作员编号，例如 101）。<br>
    ★ **编号不区分大小写**：填 `system` 或 `SYSTEM` 会落到同一条上（以前会存成两条，
    其中一条永远不生效）。<br>
    ★ 权限改完立刻生效，但**已登录的人要重新登录一次**才拿到 —— 权限是签在令牌里的。</div>
  </div>
  <div class="card"><h2>添加 / 修改</h2><div class="row">
    <select id="vCode">${codes.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("")}</select>
    <input id="vUid" placeholder="操作员编号（如 101，不区分大小写）" style="min-width:210px">
    ${PERMS.map(([k, label]) =>
      `<label class="row" style="margin:0 12px;white-space:nowrap"><input type="checkbox" class="vPermNew" value="${esc(k)}"> ${esc(label)}</label>`).join("")}
    <button class="btn" id="vSave">保存</button></div></div>`;

  const save = async (code, uid, perms) => {
    await api("/api/admin/v3operators/perms", "POST", { code, uid, perms });
    toast("已保存");
    renderTab();
  };
  $("vSave").addEventListener("click", async () => {
    const code = $("vCode").value, uid = $("vUid").value.trim();
    if (!uid) return toast("要填操作员编号", true);
    try {
      await save(code, uid,
        Array.from(document.querySelectorAll(".vPermNew:checked")).map(x => x.value));
    } catch (e) { toast(e.message, true); }
  });
  // 行内勾选：勾一下立刻保存（失败把勾回原样，不让界面撒谎）
  b.querySelectorAll(".v3Perm").forEach(x => x.addEventListener("change", async () => {
    const code = x.dataset.code, uid = x.dataset.uid;
    const boxes = Array.from(b.querySelectorAll(".v3Perm"))
      .filter(y => y.dataset.code === code && y.dataset.uid === uid);
    const perms = boxes.filter(y => y.checked).map(y => y.value);
    try { await save(code, uid, perms); }
    catch (e) { toast(e.message, true); x.checked = !x.checked; }
  }));
  b.querySelectorAll("[data-vrevoke]").forEach(x => x.addEventListener("click", async () => {
    const [code, uid] = String(x.dataset.vrevoke).split("|");
    if (!confirm(`踢出「${code} / ${uid}」的全部会话？\n\n他所有设备会立刻掉线，需要重新登录。\n`
      + "注意：这只是让他下线，**不是**停用他的操作员身份（那要去财鲸 V3 里停）。")) return;
    try {
      const rr = await api("/api/admin/v3operators/revoke", "POST", { code, uid });
      toast(`已踢出全部会话（清 ${rr.dropped || 0} 条）`);
      renderTab();
    } catch (e) { toast(e.message, true); }
  }));
}

/* ---------------- 老板授权 ---------------- */
async function users(b) {
  const st = await api("/api/admin/state");
  const clients = st.clients || [];
  b.innerHTML = `<div class="card"><h2>老板微信授权<span class="r">${(st.users || []).length} 个</span></h2>
    ${U.table([["姓名"], ["openid"], ["可及入口"], ["最近登录"], [""]],
      (st.users || []).map(u => `<tr>
        ${U.td(esc(u.name), "姓名")}${U.td(`<code>${esc(String(u.openid || "").slice(0, 16))}…</code>`, "openid")}
        ${U.td(u.all_clients ? U.tag("全部入口", "warn") : esc((u.clients || []).join("、") || "无"), "可及入口")}
        ${U.td(esc(U.ts(u.last_seen) || "—"), "最近登录")}
        ${U.td(`<button class="btn link" data-udel="${esc(u.openid)}">删除</button>`, "")}</tr>`).join(""))}
  </div>
  <div class="card"><h2>新增授权</h2><div class="row">
    <input id="uName" placeholder="姓名" style="min-width:120px">
    <input id="uOpenid" placeholder="openid" style="min-width:220px"></div>
    <div class="row" style="margin-top:8px">${clients.map(c =>
      `<label class="row" style="margin:0 10px 0 0"><input type="checkbox" class="uCli" value="${esc(c.name)}"> ${esc(c.name)}</label>`).join("")}
      <button class="btn" id="uAdd">保存</button></div>
    <div class="muted small" style="margin-top:8px">微信通道现在还没就绪（缺 AppID/AppSecret），
    这一页先保持能配置，等通道打开时不用再改界面。</div></div>`;
  $("uAdd").addEventListener("click", async () => {
    try {
      await api("/api/admin/users", "POST", { name: $("uName").value.trim(), openid: $("uOpenid").value.trim(),
        clients: Array.from(document.querySelectorAll(".uCli:checked")).map(x => x.value), all_clients: false });
      toast("已保存"); renderTab();
    } catch (e) { toast(e.message, true); }
  });
  b.querySelectorAll("[data-udel]").forEach(x => x.addEventListener("click", async () => {
    if (!confirm("删除这个微信授权？")) return;
    try { await api("/api/admin/users/" + encodeURIComponent(x.dataset.udel), "DELETE", {}); renderTab(); }
    catch (e) { toast(e.message, true); }
  }));
}

/* ---------------- 调试工具 ---------------- */
async function rpc(b) {
  const st = await api("/api/admin/agents");
  const rows = (st.agents || []).filter(a => a.online);
  b.innerHTML = `<div class="card"><h2>手动发一条动作<span class="r">运营者的工具，必须显式点名商户</span></h2>
    <div class="row">
      <select id="rAgent">${rows.length ? rows.map(a =>
        `<option value="${esc(a.name)}">${esc(a.name)}（${(a.ops || []).length} 个动作）</option>`).join("")
        : '<option value="">（现在没有在线商户）</option>'}</select>
      <select id="rOp"><option value="">选一个动作</option></select>
      <select id="rDb" title="读哪本账"></select>
      <input id="rTimeout" type="number" value="60" min="5" max="300" style="width:90px" title="超时秒">
      <button class="btn" id="rRun">执行</button>
    </div>
    <div id="rHelp" class="muted small" style="margin:8px 0 0;min-height:20px"></div>
    <label>参数（JSON）</label><textarea id="rParams" rows="7" placeholder="选一个动作，这里会自动填上示例参数"></textarea>
    <div class="muted small" style="margin-top:6px">说明与示例都来自 Agent 自己上报的那份（与动作清单同源），
      云端不另写一份 —— 否则两边一漂移，症状就是"某个动作选了没反应"。</div>
    <pre id="rOut" style="display:none"></pre></div>`;

  const docsFor = () => {
    const a = rows.find(x => x.name === $("rAgent").value);
    return (a && a.op_docs) || {};
  };
  /* 账套下拉：一家一台主机两本账时，调试台必须能点到第二本。
     ★ 只列**这家在云端登记过的**那几本（越界的点了也会被云端挡），
       并把客户机自己放行的清单标出来 —— 否则运营会以为是后台坏了。 */
  const fillDbs = () => {
    const a = rows.find(x => x.name === $("rAgent").value) || {};
    const dbs = a.databases || [];
    const allowed = a.allowed_databases || [];
    if (!dbs.length) {
      $("rDb").innerHTML = '<option value="">默认账套</option>';
      $("rDb").disabled = true;
      $("rDb").title = allowed.length > 1
        ? `客户机放行了 ${allowed.length} 本，但云端还没给这家登记清单（商户页补一下）`
        : "这家只登记了一本账（默认）";
      return;
    }
    $("rDb").disabled = false;
    $("rDb").innerHTML = dbs.map((d, i) => `<option value="${esc(d)}">${esc(d)}${
      i === 0 ? "（默认）" : ""}${allowed.length && allowed.indexOf(d) < 0 ? "（客户机未放行）" : ""
    }</option>`).join("");
  };
  fillDbs();
  const fillOps = () => {
    const docs = docsFor();
    const cats = {};
    Object.keys(docs).sort().forEach(k => {
      const c = docs[k].cat || "未分类";
      (cats[c] = cats[c] || []).push(k);
    });
    $("rOp").innerHTML = '<option value="">选一个动作</option>'
      + Object.keys(cats).sort().map(c => `<optgroup label="${esc(c)}">`
          + cats[c].map(k => {
              const d = docs[k] || {};
              return `<option value="${esc(k)}" data-desc="${esc(d.desc || "")}">${esc(d.label || k)}　${esc(k)}</option>`;
            }).join("") + "</optgroup>").join("");
    $("rHelp").textContent = Object.keys(docs).length
      ? `这家能执行 ${Object.keys(docs).length} 个动作。` : "这个通道没上报动作说明。";
  };
  const showHelp = () => {
    const op = $("rOp").value;
    const d = docsFor()[op];
    if (!d) { $("rHelp").textContent = op ? "这个动作没有上报说明。" : ""; return; }
    const write = (d.desc || "").indexOf("写库动作") === 0;
    $("rHelp").innerHTML = `<b>${esc(d.label)}</b> · ${esc(d.desc)}`
      + (d.note ? `<div style="margin-top:4px">${esc(d.note)}</div>` : "")
      + (write ? '<div style="margin-top:4px;color:#b91c1c">★ 这会真的改这家账套的数据。</div>' : "");
    // 选完动作就把示例参数填进去（用户要求：不用自己猜字段名）
    $("rParams").value = JSON.stringify(d.example || {}, null, 2);
  };
  $("rAgent").addEventListener("change", () => { fillOps(); fillDbs(); });
  $("rOp").addEventListener("change", showHelp);
  if (rows.length) { fillOps(); }
  $("rRun").addEventListener("click", async () => {
    let params = {};
    const raw = $("rParams").value.trim();
    if (raw) { try { params = JSON.parse(raw); } catch (e) { toast("参数不是合法 JSON", true); return; } }
    const out = $("rOut");
    out.style.display = ""; out.textContent = "执行中…";
    try {
      const db = $("rDb").value || "";
      const r = await api("/api/admin/rpc", "POST", { agent: $("rAgent").value,
        op: $("rOp").value.trim(), params, timeout: parseInt($("rTimeout").value, 10) || 60,
        database: db });
      const got = (r.response || {}).database || "";
      out.textContent = (db || got
        ? `【账套】点名 ${db || "默认"}｜实际读到 ${got || db || "默认"}
` : "")
        + JSON.stringify(r.response, null, 2);
    } catch (e) { out.textContent = "✗ " + e.message; }
  });
}
