"use strict";

const $app = document.getElementById("app");
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;
const DOMAIN_LABEL = {
  VISA_POLICY: "签证政策", FX_POLICY: "购汇政策", TUITION: "学费", SCHOOL: "学校", PROGRAM: "项目",
  ACCOUNT_THRESHOLD: "开户门槛", CREDIT_CARD: "信用卡",
};
const ACTION_LABEL = { CREATE: "新增", UPDATE: "更新" };

const A = {
  token: sessionStorage.getItem("gp_admin_token"), admin: sessionStorage.getItem("gp_admin_name"),
  domain: "", data: null, detail: null, busy: false,
};

// ---------- helpers ----------

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "disabled" || k === "selected") el[k] = true;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}

const pad = (n) => String(n).padStart(2, "0");

function fmtTime(iso) {
  const d = new Date(iso);
  if (!iso || isNaN(d)) return "—";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtVal(v) {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function toast(msg, bad) {
  const t = h("div", { class: "toast" + (bad ? " bad" : "") }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), 3600);
}

class ApiError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

async function api(method, path, body) {
  let res;
  try {
    res = await fetch("/v1/admin" + path, {
      method,
      headers: { "Content-Type": "application/json", ...(A.token ? { Authorization: "Bearer " + A.token } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw new ApiError("NETWORK", "无法连接服务，请确认 agent2 serve 正在运行");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (data.code === "ADMIN_UNAUTHORIZED") logout();
    throw new ApiError(data.code || "HTTP_" + res.status, data.message || "请求失败");
  }
  return data;
}

function logout() {
  A.token = A.admin = null;
  A.data = A.detail = null;
  sessionStorage.removeItem("gp_admin_token");
  sessionStorage.removeItem("gp_admin_name");
  render();
}

function topBar() {
  const right = [h("a", { class: "nav", href: "/" }, "返回前台")];
  if (A.token) {
    right.push(
      h("div", { class: "rm" }, h("div", { class: "av sf" }, (A.admin || "管").slice(0, 1)), A.admin),
      h("button", { class: "linkbtn", onclick: logout }, "退出"));
  }
  return h("div", { class: "top" },
    h("div", { class: "logo" },
      h("div", { class: "mk sf" }, "审"), h("b", { class: "sf" }, "数据审核后台"),
      h("small", null, "AGENT1 · REVIEW QUEUE")),
    h("div", { class: "r" }, right));
}

// ---------- login ----------

function renderLogin(err) {
  const name = h("input", { name: "username", autocomplete: "username", value: A.admin || "" });
  const pass = h("input", { name: "password", type: "password", autocomplete: "current-password" });
  const errEl = h("div", { class: "formerr" }, err || "");
  const submit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("POST", "/login", { username: name.value, password: pass.value });
      A.token = r.token;
      A.admin = r.admin;
      sessionStorage.setItem("gp_admin_token", r.token);
      sessionStorage.setItem("gp_admin_name", r.admin);
      render();
    } catch (ex) {
      errEl.textContent = ex.message;
    }
  };
  $app.replaceChildren(topBar(), h("div", { class: "gold" }),
    h("div", { class: "page" }, h("form", { class: "card login", onsubmit: submit },
      h("h1", { class: "sf" }, "管理员登录"),
      h("p", { class: "lead" }, "审核 Agent1 抓取后未通过自动质量检查的数据，决定是否写入正式数据库。"),
      h("label", null, "用户名", name), h("label", null, "密码", pass),
      h("button", { class: "btn", type: "submit" }, "登录"), errEl)));
  (A.admin ? pass : name).focus();
}

// ---------- list ----------

async function load() {
  const q = A.domain ? "?" + new URLSearchParams({ domain: A.domain }) : "";
  try {
    A.data = await api("GET", "/reviews" + q);
  } catch (e) {
    if (A.token) toast(e.message, true);
    return;
  }
  renderList();
}

function renderList() {
  const d = A.data;
  const domains = new Set([...d.domains, A.domain].filter(Boolean));
  const tabs = h("div", { class: "tabs" },
    h("button", { class: "on" }, "待审核", h("em", null, d.reviews.length)),
    h("span", { class: "sp" }),
    h("select", { onchange: (e) => { A.domain = e.target.value; load(); } },
      h("option", { value: "", selected: !A.domain }, "全部域表"),
      [...domains].map((key) => h("option", { value: key, selected: A.domain === key }, DOMAIN_LABEL[key] || key))));

  const body = d.reviews.length ? h("table", { class: "tickets" },
    h("thead", null, h("tr", null,
      h("th", null, "提交时间"), h("th", null, "域表"), h("th", null, "动作"), h("th", null, "内容"),
      h("th", null, "进审核原因"))),
    h("tbody", null, d.reviews.map((r) => h("tr", { class: "pick", onclick: () => openDetail(r.review_id) },
      h("td", { class: "mono" }, fmtTime(r.created_at)),
      h("td", null, DOMAIN_LABEL[r.domain_table] || r.domain_table),
      h("td", null, h("span", { class: "badge " + r.action }, ACTION_LABEL[r.action] || r.action)),
      h("td", { class: "sum" }, h("div", { class: "clip" }, r.summary || r.record_key)),
      h("td", { class: "why" }, h("div", { class: "clip" }, r.reason || "—"))))))
    : h("div", { class: "empty-state" }, "暂无待审核数据");

  $app.replaceChildren(topBar(), h("div", { class: "gold" }), h("div", { class: "page adm" },
    h("h1", { class: "sf" }, "待审核数据"),
    h("p", { class: "lead" }, `Agent1 写库时，跨页面冲突、字段变更、证据不足或材料移除的数据不会直接入库，而是进入审核队列。`
      + `“批准入库”会把拟写入内容写进正式数据库；“驳回”则丢弃，正式库保持原样。处理完的审核项会从队列删除。时间按 ${TZ} 显示。`),
    tabs, body));
  if (A.detail) renderDrawer();
}

// ---------- detail drawer ----------

async function openDetail(id) {
  try {
    A.detail = await api("GET", "/reviews/" + encodeURIComponent(id));
  } catch (e) {
    toast(e.message, true);
    return;
  }
  renderDrawer();
}

function closeDrawer() {
  A.detail = null;
  document.querySelectorAll(".scrim,.drawer").forEach((el) => el.remove());
}

function fieldTable(obj, current, skip) {
  const keys = Object.keys(obj).filter((k) => !(skip || []).includes(k));
  if (!keys.length) return h("div", { class: "note" }, "无字段变更");
  return h("table", { class: "fields" }, h("tbody", null, keys.map((k) => {
    const changed = current && k in current && fmtVal(current[k]) !== fmtVal(obj[k]);
    return h("tr", null, h("td", null, k), h("td", { class: changed ? "chg" : null },
      fmtVal(obj[k]), changed ? h("div", { class: "note" }, "库内现值：" + fmtVal(current[k])) : null));
  })));
}

function docList(docs, gone) {
  return h("ul", { class: "docs" + (gone ? " gone" : "") }, docs.map((doc) => h("li", null,
    h("b", null, doc.name_zh || doc.code), doc.required === false ? "（视情况）" : "",
    doc.still_quoted ? h("span", { class: "q" }, "注意：原引用仍在页面上，可能是漏抽") : null,
    doc.evidence_quote ? h("span", { class: "q" }, "“" + doc.evidence_quote + "”") : null,
    doc.condition_quote ? h("span", { class: "q" }, "条件：" + doc.condition_quote) : null)));
}

function proposedView(r) {
  const p = r.proposed_value || {};
  const parts = [];
  if (p.tuition) {
    parts.push(h("div", { class: "sec" }, "拟写入学费"),
      h("div", { class: "note" }, `学校 ${p.school_key || "—"} · 项目 ${p.program_key || "—"}`),
      fieldTable(p.tuition, r.current));
  } else if (p.policy) {
    parts.push(h("div", { class: "sec" }, "拟写入字段"),
      fieldTable(p.policy, r.current, ["policy_id", "country", "policy_type", "title", "purpose_code"]));
    if (p.documents && p.documents.length) parts.push(h("div", { class: "sec" }, "新增 / 变更材料"), docList(p.documents));
    if (p.removed_documents && p.removed_documents.length) {
      parts.push(h("div", { class: "sec" }, "批准后移除的材料"), docList(p.removed_documents, true));
    }
  } else {
    parts.push(h("div", { class: "sec" }, "拟写入内容"), h("pre", { class: "raw" }, JSON.stringify(p, null, 2)));
  }
  return parts;
}

function evidenceView(list) {
  if (!list.length) return h("div", { class: "note" }, "无证据");
  return h("ul", { class: "ev" }, list.map((e) => h("li", null,
    h("span", { class: "mono" }, e.field || e.program_name || ""),
    e.amount !== undefined ? ` ${fmtVal(e.amount)} ${e.currency || ""}` : "",
    e.value !== undefined && e.value !== null ? " = " + fmtVal(e.value) : "",
    h("blockquote", null, e.evidence_quote || e.quote || "—"),
    h("div", { class: "src" }, [e.source_id, e.snapshot_id].filter(Boolean).join(" · ")))));
}

function decideView(r) {
  const note = h("textarea", { placeholder: "审核备注（驳回必填，批准可选）", maxlength: "500" });
  const decide = async (verb) => {
    const text = note.value.trim();
    if (verb === "reject" && !text) { toast("驳回必须填写原因", true); note.focus(); return; }
    if (verb === "approve" && !confirm("确认把这条数据写入正式数据库？")) return;
    if (A.busy) return;
    A.busy = true;
    try {
      await api("POST", `/reviews/${encodeURIComponent(r.review_id)}/${verb}`, { note: text || null });
      closeDrawer();
      toast(verb === "approve" ? "已批准并写入正式库" : "已驳回，数据未入库");
      await load();
    } catch (e) {
      toast(e.message, true);
    } finally {
      A.busy = false;
    }
  };
  return h("div", { class: "decide" }, note,
    r.applicable ? null : h("div", { class: "hint" }, "该域表暂不支持自动写入，只能驳回。"),
    h("div", { class: "btns" },
      h("button", { class: "btn ghost", onclick: () => decide("reject") }, "驳回"),
      h("button", { class: "btn", disabled: !r.applicable, onclick: () => decide("approve") }, "批准入库")));
}

function renderDrawer() {
  document.querySelectorAll(".scrim,.drawer").forEach((el) => el.remove());
  const r = A.detail;
  const kv = [["域表", DOMAIN_LABEL[r.domain_table] || r.domain_table], ["动作", ACTION_LABEL[r.action] || r.action],
    ["记录键", r.record_key], ["审核编号", r.review_id], ["提交时间", fmtTime(r.created_at)]];
  document.body.append(
    h("div", { class: "scrim", onclick: closeDrawer }),
    h("div", { class: "drawer wide" },
      h("button", { class: "close", onclick: closeDrawer }, "×"),
      h("h3", { class: "sf" }, r.summary || r.record_key),
      h("div", { class: "kv" }, kv.map(([k, v]) => [h("span", null, k), h("span", { class: "mono" }, v)])),
      h("div", { class: "sec" }, "进审核原因"), h("blockquote", null, r.reason || "—"),
      proposedView(r),
      r.current ? [h("div", { class: "sec" }, "库内现有记录"), fieldTable(r.current)]
        : h("div", { class: "note" }, "库内尚无该记录，批准后新增。"),
      h("div", { class: "sec" }, `证据（${r.evidence.length}）`), evidenceView(r.evidence),
      decideView(r)));
}

// ---------- boot ----------

function render() {
  if (!A.token) return renderLogin();
  $app.replaceChildren(topBar(), h("div", { class: "gold" }), h("div", { class: "loading" }, "加载中…"));
  load();
}

document.addEventListener("keydown", (e) => { if (e.key === "Escape" && A.detail) closeDrawer(); });
render();
