/* 纯展示助手：不做任何网络请求。
 * 单独一个文件是因为"时间怎么显示、状态灯怎么选颜色"这类判断，
 * 以前散在十几个渲染函数里，同一个含义有四种写法。 */

export const esc = v => String(v == null ? "" : v).replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function ts(v, withSec = true) {
  if (!v) return "";
  const d = new Date(v * 1000);
  const p = n => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
    + (withSec ? `:${p(d.getSeconds())}` : "");
}

export function ago(v) {
  if (!v) return "从未";
  const s = Math.max(0, Date.now() / 1000 - v);
  if (s < 90) return Math.round(s) + " 秒前";
  if (s < 5400) return Math.round(s / 60) + " 分钟前";
  if (s < 172800) return (s / 3600).toFixed(1) + " 小时前";
  return Math.round(s / 86400) + " 天前";
}

export function dur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 90) return sec + " 秒";
  if (sec < 5400) return Math.round(sec / 60) + " 分钟";
  if (sec < 172800) return (sec / 3600).toFixed(1) + " 小时";
  return Math.round(sec / 86400) + " 天";
}

export const light = level => `<span class="light ${level}"></span>`;

/** 状态灯：ok/warn/bad/unknown → 绿/黄/红/灰。界面只出现这一套颜色语义。 */
export function levelOf(row) {
  if (!row.enabled) return "y";
  if (row.db_state === "mismatch") return "r";
  if (row.online) return row.version_too_old || row.fail_24h >= 3 ? "y" : "g";
  return row.last_online_at ? "r" : "n";
}

export function tag(text, cls) { return `<span class="tag ${cls || "gray"}">${esc(text)}</span>`; }

/** 实测账套的标记。三处视图（商户/监控/设置）必须同一个说法，
 *  否则同一家在不同页一个显示"一致"一个显示"越界"，操作员就不知道该信哪页。
 *  allowed = 一台主机多本账里在读非默认库，是**合法状态**，不能画成故障色。 */
export function dbTag(m) {
  if (m.db_state === "match") return tag("一致", "on");
  if (m.db_state === "allowed") return tag("在读 " + (m.database_verified || ""), "gray");
  if (m.db_state === "mismatch") return tag("越界 " + (m.database_verified || ""), "off");
  return tag("未上报", "gray");
}

export function money(v) {
  if (v == null || isNaN(v)) return "—";
  return Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function kv(k, v) { return `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`; }

/** 表头 + 每列的"手机上的字段名"。移动端 td 用 data-l 显示标签，否则卡片里读不出列意。 */
export function table(cols, rowsHtml) {
  return `<table><thead><tr>${cols.map(c => `<th>${esc(c[0])}</th>`).join("")}</tr></thead>`
    + `<tbody>${rowsHtml}</tbody></table>`;
}
export function td(html, label) { return `<td data-l="${esc(label || "")}">${html}</td>`; }

export function alertsBanner(alerts) {
  if (!alerts || !alerts.length) return "";
  const bad = alerts.filter(a => a.level === "bad");
  return `<div class="banner ${bad.length ? "bad" : ""}"><b>${alerts.length} 项需要你处理</b><ul>`
    + alerts.map(a => `<li><b>${esc(a.title || a.code)}</b> ${esc(a.text)}</li>`).join("")
    + `</ul></div>`;
}
