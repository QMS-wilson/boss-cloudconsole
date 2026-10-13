/* 会话：谁在用 App，以及把谁踢下线（2026-10-12）
 *
 * ★ 为什么有这一页：在这之前云端连"谁在线"都答不出来 ——
 *   令牌是 HMAC 无状态的（签发后撤不掉）、/auth/logout 是空壳、没有会话表。
 *   现在地基补上了，这一页负责把它摆出来。
 *
 * ★ 三个动作必须分开给，不能合成一个按钮：
 *     「踢出这条」    只断这一台设备（精确）
 *     「踢出该账号全部」他所有设备一起断（粗暴，但一按就干净）
 *     「禁用账号」    这是另一件事 —— 禁用是"以后不能再登录"，
 *                     不是"现在断线"。合成一个就会出现
 *                     "我只想踢他下线，结果把人账号禁了"。
 */
import { api, toast } from "../api.js";

export const title = "会话";
const esc = s => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

let onlyOnline = false;

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtIdle(sec) {
  if (sec == null) return "—";
  if (sec < 60) return sec + " 秒前";
  if (sec < 3600) return Math.floor(sec / 60) + " 分钟前";
  if (sec < 86400) return Math.floor(sec / 3600) + " 小时前";
  return Math.floor(sec / 86400) + " 天前";
}

function rowHtml(r) {
  const dot = r.revoked
    ? '<span style="color:#b91c1c">●</span> 已踢出'
    : (r.online ? '<span style="color:#22c55e">●</span> 在线'
                : '<span style="color:#94a3b8">●</span> 离线');
  const sub = String(r.sub || "");
  const user = sub.indexOf("account:") === 0 ? sub.slice(8)
             : (sub.indexOf("v3:") === 0 ? sub.split(":")[2] || sub : sub);
  const kinds = { app: "App 账号", v3: "V3 操作员", wechat: "微信" };
  const kind = kinds[r.kind] || r.kind || "—";
  // ★ 设备：品牌·型号·端 拼一行（服务端解析好的，三处页面共用同一份口径）
  const dev = [r.brand, r.model, r.client].filter(Boolean).join(" · ")
    || r.platform || "—";
  const mkt = r.mkt ? esc(r.mkt) : ((r.mkts || []).map(esc).join("、") || "—");
  // ★ 账套必须跟商户一起显示（2026-10-14）：**每本账有自己的操作员集合**
  //   （A 本有 101、B 本可能只有 202），只看到商户答不出"他现在在哪本账上"。
  const scope = r.database
    ? `${mkt} <span class="muted">/ ${esc(r.database)}</span>`
    : `${mkt} <span class="muted">/ 默认</span>`;
  return `<tr>
    <td>${dot}</td>
    <td><b>${esc(r.name || user)}</b><div class="muted small">${esc(user)}</div></td>
    <td>${esc(kind)}</td>
    <td>${esc(dev)}${r.platform && r.client ? `<div class="muted small">${esc(r.platform)}</div>` : ""}</td>
    <td>${scope}</td>
    <td class="muted small">${esc(r.ip || "—")}</td>
    <td class="muted small">${fmtTime(r.login_at)}</td>
    <td class="muted small">${fmtIdle(r.idle_sec)}</td>
    <td>
      <button class="btn gray sm" data-act="one" data-jti="${esc(r.jti)}"
        ${r.revoked ? "disabled" : ""}>踢出这条</button>
      <button class="btn gray sm" data-act="all" data-user="${esc(user)}">踢出该账号全部</button>
      <button class="btn sm" data-act="disable" data-user="${esc(user)}">禁用账号</button>
    </td>
  </tr>`;
}

async function act(btn) {
  const kind = btn.dataset.act;
  const user = btn.dataset.user || "";
  try {
    if (kind === "one") {
      if (!confirm("踢出这一条会话？\n\n只断这一台设备，该账号在其他设备上的登录不受影响。")) return;
      await api(`/api/admin/sessions/${encodeURIComponent(btn.dataset.jti)}/revoke`, "POST", {});
      toast("已踢出这一条会话");
    } else if (kind === "all") {
      if (!confirm(`踢出「${user}」的全部会话？\n\n`
        + "他所有设备会立刻掉线，需要重新登录。\n"
        + "账号本身不受影响 —— 之后还能正常登录。")) return;
      const r = await api(`/api/admin/accounts/${encodeURIComponent(user)}/revoke`, "POST", {});
      toast(`已踢出全部会话（清掉 ${r.dropped || 0} 条）`);
    } else {
      if (!confirm(`禁用账号「${user}」？\n\n`
        + "禁用 = 以后不能再登录，同时现有会话立刻失效。\n"
        + "如果只想让他下线、之后还能登，请点左边的「踢出」。\n\n"
        + "账号不会被删除，之后可以在「设置」里重新启用。")) return;
      const r = await api(`/api/admin/accounts/${encodeURIComponent(user)}/disable`,
        "POST", { disabled: true });
      toast(`已禁用，并清掉 ${r.dropped || 0} 条会话`);
    }
  } catch (e) {
    toast(e.message, true);
  }
}

export async function mount(el) {
  const r = await api(`/api/admin/sessions${onlyOnline ? "?online=1" : ""}`);
  const rows = r.sessions || [];
  const st = r.stats || {};
  const mins = Math.round((st.window || 300) / 60);

  el.innerHTML = `
  <div class="card" style="padding:12px 14px">
    <div class="row">
      <label class="muted small"><input type="checkbox" id="sOnline"
        ${onlyOnline ? "checked" : ""}> 只看在线</label>
      <button class="btn sm" id="sRefresh">刷新</button>
      <span class="grow"></span>
      <span class="muted small">共 ${st.total || 0} 条 · 有效 ${st.live || 0} ·
        <b style="color:#22c55e">在线 ${st.online || 0}</b></span>
    </div>
    <div class="muted small" style="margin-top:6px">
      「在线」= 最近 ${mins} 分钟内有请求（App 活跃时会不断刷新最后活跃时间）。
      「踢出」= 立刻断线，对方需重新登录；「禁用」= 以后不能再登录。
    </div>
  </div>
  <div class="card" style="margin-top:10px;overflow:hidden">
    <table>
      <thead><tr>
        <th style="width:76px">状态</th><th>账号</th><th style="width:88px">类型</th>
        <th style="width:172px">设备 / 端</th><th style="width:132px">商户 / 账套</th>
        <th style="width:118px">来源 IP</th>
        <th style="width:104px">登录于</th><th style="width:96px">最后活跃</th>
        <th style="width:236px">操作</th>
      </tr></thead>
      <tbody>${rows.length ? rows.map(rowHtml).join("")
        : `<tr><td colspan="9" class="muted" style="padding:18px;text-align:center">
             ${onlyOnline ? "当前没有在线会话" : "还没有任何会话记录"}</td></tr>`}</tbody>
    </table>
  </div>`;

  el.querySelector("#sOnline").addEventListener("change", ev => {
    onlyOnline = ev.target.checked;
    mount(el);
  });
  el.querySelector("#sRefresh").addEventListener("click", () => mount(el));
  el.querySelectorAll("[data-act]").forEach(b =>
    b.addEventListener("click", () => act(b)));
}
