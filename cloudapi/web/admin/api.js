/* 网络与浮层。
 * 只在这里决定"未登录怎么办、错误怎么露出来"，视图不再各写一份 fetch。 */
import { esc } from "./ui.js";

export let expired = false;
const listeners = [];
export function onExpired(fn) { listeners.push(fn); }

export async function api(path, method = "GET", body) {
  const opt = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const resp = await fetch(path, opt);
  let data = {};
  try { data = await resp.json(); } catch (e) { /* 空响应体 */ }
  if (resp.status === 401) {
    expired = true;
    listeners.forEach(f => { try { f(); } catch (e) {} });
    throw new Error("登录已过期");
  }
  if (!resp.ok && !data.ok) {
    throw new Error(data.detail || (data.error && data.error.message) || JSON.stringify(data).slice(0, 200));
  }
  return data;
}

export function toast(msg, isErr = false) {
  const d = document.createElement("div");
  d.className = "toast" + (isErr ? " err" : "");
  d.textContent = msg;
  document.getElementById("toastWrap").appendChild(d);
  setTimeout(() => d.remove(), isErr ? 5200 : 3200);
}

export function modal(html) {
  const box = document.getElementById("modalBox");
  box.innerHTML = html;
  document.getElementById("modalMask").classList.add("show");
  box.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", closeModal));
  return box;
}
export function closeModal() { document.getElementById("modalMask").classList.remove("show"); }
document.addEventListener("click", e => { if (e.target.id === "modalMask") closeModal(); });

/** 危险动作的确认框：文案来自服务端算好的**活值**（GET /impact）。
 * 为什么不用写死的"确定吗？"：决定要不要点下去的是"这家现在在线吗、几个客户端在用、
 * 刚才还在被调用吗"，而这些只有服务端知道。 */
export async function confirmImpact(code, action, title, opts = {}) {
  let imp;
  try {
    imp = (await api(`/api/admin/impact?code=${encodeURIComponent(code)}&action=${action}`)).data;
  } catch (e) {
    imp = { lines: ["影响面暂时取不到：" + e.message], blocking: false };
  }
  const needType = action === "delete";
  const box = modal(`
    <h3>${esc(title)}</h3>
    <div class="fx">${imp.lines.map(esc).join("<br>")}</div>
    ${opts.hint ? `<div class="muted small" style="margin:10px 0 0">${esc(opts.hint)}</div>` : ""}
    ${needType ? `<input id="impType" placeholder="输入商户码以确认" style="width:100%;margin-top:12px">` : ""}
    <div class="row" style="margin-top:14px">
      <button class="btn ${action === "delete" ? "red" : ""}" id="impOk">${opts.okLabel || "确认"}</button>
      <button class="btn gray" data-close>取消</button>
    </div>`);
  return new Promise(resolve => {
    box.querySelector("#impOk").addEventListener("click", () => {
      if (needType && box.querySelector("#impType").value.trim() !== code) {
        toast("两次输入不一致，未执行", true);
        return;
      }
      closeModal();
      resolve(true);
    });
    box.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", () => resolve(false)));
  });
}

export async function copyText(text, label = "已复制") {
  const val = String(text == null ? "" : text);
  if (!val) return;
  try {
    await navigator.clipboard.writeText(val);
    toast(label);
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = val; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); toast(label); } catch (e2) { toast("复制失败，请长按选中", true); }
    ta.remove();
  }
}

/** 表单里有没有未提交的输入：自动刷新不能把它冲掉。 */
export function editing() {
  const el = document.activeElement;
  if (!el || !/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return false;
  return el.value !== "" && el.closest("#view") !== null;
}
