/* 应用外壳：登录、路由、自动刷新。
 *
 * 为什么不再是一个巨型内联脚本：改造前加一页要同改三处（id 数组、标题表、loader），
 * 1183 行里互相踩。现在视图只导出 {title, mount}，路由表只有一处。
 * 用原生 ES module，不引入构建链 —— 后台是运维工具，不该为了它再维护一套打包。 */
import { api, toast, onExpired, editing } from "./api.js";
import * as merchants from "./views/merchants.js";
import * as monitor from "./views/monitor.js";
import * as audit from "./views/audit.js";
import * as settings from "./views/settings.js";

const VIEWS = { merchants, monitor, audit, settings };
const AUTO_SEC = 20;

let current = null;
let timer = null;
export const S = { health: [], alerts: [], h5: null, meta: null, loadedAt: 0 };

function $(id) { return document.getElementById(id); }

/* ---------------- 登录 ---------------- */
function loginView(msg) {
  document.body.innerHTML = `
  <div style="min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg)">
    <div class="card" style="width:min(360px,92vw)">
      <h2>V3 云端后台</h2>
      <label>后台密码</label>
      <input type="password" id="pwd" style="width:100%" autocomplete="current-password">
      <div class="muted small" id="loginErr" style="min-height:18px;color:#b91c1c">${msg || ""}</div>
      <button class="btn" id="btnLogin" style="width:100%;margin-top:6px">登录</button>
    </div>
  </div>`;
  const go = async () => {
    try {
      await api("/api/admin/login", "POST", { password: $("pwd").value });
      location.replace("/admin");
    } catch (e) { loginView(e.message); }
  };
  $("btnLogin").addEventListener("click", go);
  $("pwd").addEventListener("keydown", e => { if (e.key === "Enter") go(); });
  $("pwd").focus();
}

/* ---------------- 数据 ---------------- */
export async function loadHealth(force = false) {
  if (!force && Date.now() - S.loadedAt < 4000) return;   // 一次切页可能触发多处，合并掉
  const r = await api("/api/admin/merchant-health");
  S.health = r.merchants || [];
  S.alerts = r.alerts || [];
  S.h5 = r.h5 || null;
  S.meta = { generated_at: r.generated_at, days: r.days };
  S.loadedAt = Date.now();
  paintChrome();
}

function paintChrome() {
  const n = S.alerts.length;
  const badge = $("alertN");
  if (badge) {
    badge.style.display = n ? "" : "none";
    badge.textContent = String(n);
  }
  const online = S.health.filter(m => m.online).length;
  const dot = $("statusDot");
  if (dot) dot.style.background = online ? "#22c55e" : "#ef4444";
  const chip = $("chipOnline");
  if (chip) { chip.style.display = ""; chip.textContent = `在线 ${online} / ${S.health.length}`; }
  const foot = $("footOnline");
  if (foot) foot.textContent = `在线 ${online} / ${S.health.length} · 24h 失败 ${S.health.reduce((a, b) => a + (b.fail_24h || 0), 0)} 次`;
  const stale = $("chipStale");
  if (stale) {
    if (S.h5 && !S.h5.available) { stale.style.display = ""; stale.textContent = "发布状态未回写"; }
    else if (S.h5 && S.h5.stale) { stale.style.display = ""; stale.textContent = `发布状态 ${Math.round(S.h5.age_sec / 3600)} 小时前`; }
    else stale.style.display = "none";
  }
  const lu = $("lastUpd");
  if (lu) lu.textContent = new Date().toLocaleTimeString();
}

/* ---------------- 路由 ---------------- */
export function go(v, params) {
  v = VIEWS[v] ? v : "merchants";
  current = v;
  document.querySelectorAll("nav a").forEach(a => a.classList.toggle("active", a.dataset.v === v));
  $("pageTitle").textContent = VIEWS[v].title;
  if (location.hash.slice(1).split("?")[0] !== v) history.replaceState(null, "", "#" + v);
  mount(v, params || {});
}

async function mount(v, params) {
  const el = $("view");
  el.innerHTML = '<div class="muted small">加载中…</div>';
  try {
    await VIEWS[v].mount(el, params);
  } catch (e) {
    el.innerHTML = `<div class="banner bad"><b>这一页没加载出来</b><br>${e.message}</div>`;
  }
}

export function refresh(silent) {
  if (!current) return;
  if (!silent) toast("已刷新");
  mount(current, {});
}

async function tick() {
  if (document.hidden || editing()) return;      // 正在输入就不动界面
  try {
    await loadHealth(true);
    if (current && VIEWS[current].onTick) await VIEWS[current].onTick();
  } catch (e) { /* 网络抖一下不该弹错误条 */ }
}

function startTimer() {
  if (timer) clearInterval(timer);
  timer = setInterval(tick, AUTO_SEC * 1000);
}

/* ---------------- 启动 ---------------- */
async function boot() {
  try {
    await api("/api/admin/state");              // 只是探一下有没有登录态
  } catch (e) {
    loginView(String(e.message).indexOf("过期") >= 0 ? "登录已过期，请重新登录" : e.message);
    return;
  }
  try { await loadHealth(true); } catch (e) { toast(e.message, true); }
  document.querySelectorAll("nav a").forEach(a =>
    a.addEventListener("click", ev => { ev.preventDefault(); go(a.dataset.v); }));
  $("btnRefresh").addEventListener("click", () => refresh());
  $("btnLogout").addEventListener("click", async () => {
    try { await api("/api/admin/logout", "POST", {}); } catch (e) {}
    location.replace("/admin");
  });
  onExpired(() => { if (timer) { clearInterval(timer); timer = null; } loginView("登录已过期，请重新登录"); });
  const h = (location.hash || "#merchants").slice(1).split("?")[0];
  go(VIEWS[h] ? h : "merchants");
  startTimer();
}

boot();
