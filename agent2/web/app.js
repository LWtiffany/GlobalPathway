"use strict";

const $app = document.getElementById("app");
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;
const GRID = ["role", "beneficiary", "destination_country", "school", "course", "offer_status",
  "planned_start_date", "payer", "visa_category"];
const GRID_LABEL = { destination_country: "目的地", offer_status: "录取状态", planned_start_date: "入学日期" };
const STATUS = {
  answered: { label: "已确认", cls: "ok", why: "" },
  needs_input: { label: "待补充", cls: "", why: "还缺少关键信息，补充后再给出确定答案。" },
  needs_verification: { label: "待核实", cls: "warn", why: "部分材料是否需要取决于尚未确认的条件，已逐项标出。" },
  cannot_answer: { label: "无法回答", cls: "warn", why: "已发布的官方资料不足以支持确定回答，不做猜测。" },
  out_of_scope: { label: "范围外", cls: "", why: "该问题不在留学准备顾问的服务范围内。" },
};
const STEPS = ["识别问题", "必要时追问", "查询已发布知识", "核对答案", "保存正式回答", "展示结果"];
const TOPICS = [
  { key: "remittance", no: "01", title: "汇款建议", rows: ["用途", "金额", "材料", "时间点"] },
  { key: "account", no: "02", title: "开户建议", rows: ["地区", "产品", "材料", "门槛"] },
  { key: "visa", no: "03", title: "签证建议" },
  { key: "credit_card", no: "04", title: "信用卡建议", rows: ["产品", "费用 / 限制", "申请条件"] },
];
const AVAIL = { available: "有效", stale: "待复核（超过核验时效）", withdrawn: "已撤回", superseded: "已被新版本取代",
  missing_evidence: "证据缺失" };
const TAG = { required: "必需", conditional: "视情况", unverified: "待核实" };
const PRESETS = [
  { user: "王女士", tag: "家长 · 英国 + 香港",
    desc: "两个孩子：王小明（英国 UCL 硕士，签证类别未填）、王小悦（香港 HKU 本科）。",
    profiles: [
      { fields: { role: "parent", beneficiary: "王小明", destination_country: "UK", school: "UCL",
        course: "MSc Computer Science", offer_status: "conditional", planned_start_date: "2026-09-21", payer: "parent" },
        todos: ["与学校确认 CAS 发放时间", "预约签证中心递交时间"] },
      { fields: { role: "parent", beneficiary: "王小悦", destination_country: "CN-HK", school: "HKU",
        course: "BSc 本科", offer_status: "none" } },
    ] },
  { user: "李同学", tag: "学生 · 美国",
    desc: "本人申请：李华，美国 NYU 数据科学硕士，无条件录取，学生签证。",
    profiles: [
      { fields: { role: "student", beneficiary: "李华", destination_country: "US", school: "NYU",
        course: "MS Data Science", offer_status: "unconditional", planned_start_date: "2026-09-02",
        visa_category: "student_visa", payer: "self" } },
    ] },
  { user: "陈同学", tag: "学生 · 香港",
    desc: "本人申请：陈思，香港中文大学 MPhil，内地居民，学生签证。",
    profiles: [
      { fields: { role: "student", beneficiary: "陈思", destination_country: "CN-HK", school: "CUHK",
        course: "MPhil", offer_status: "unconditional", planned_start_date: "2026-09-01",
        visa_category: "student_visa", applicant_residence: "CN" } },
    ] },
];

const S = {
  token: localStorage.getItem("gp_token"), user: null, fields: null, profiles: [], pid: localStorage.getItem("gp_pid"),
  profile: null, conv: null, messages: [], advice: {}, plan: [], proposals: [], missing: new Set(),
  highlight: new Set(), flash: [], busy: false, pending: null, sheetH: null, collapsed: false, fb: {}, el: {},
  // One-question-at-a-time follow-ups; trail lets the user undo a wrong tap.
  followSeed: null, followTrail: [], suppressFields: [],
};

// ---------- helpers ----------

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "checked" || k === "disabled" || k === "selected") el[k] = true;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}

const pad = (n) => String(n).padStart(2, "0");

function fmtFull(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}（${TZ}）`;
}

function fmtVerified(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "核验时间未知" : `${d.getMonth() + 1}月${d.getDate()}日核验`;
}

function fmtClock(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  const t = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return d.toDateString() === new Date().toDateString() ? t : `${d.getMonth() + 1}月${d.getDate()}日 ${t}`;
}

function fmtDate(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || "");
  return m ? `${m[1]}年${+m[2]}月${+m[3]}日` : s;
}

function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return "c-" + Date.now().toString(36) + Math.random().toString(36).slice(2);
}

const label = (code) => (S.fields && S.fields[code] ? S.fields[code].label : code);

function valueLabel(code, v) {
  if (v === undefined || v === null) return "—";
  const f = S.fields && S.fields[code];
  const opt = f && f.options.find((o) => o.value === v);
  if (opt) return opt.label;
  if (f && f.kind === "date") return fmtDate(v);
  return String(v);
}

function val(profile, code) {
  const f = profile && profile.fields[code];
  return f && f.status === "confirmed" ? f.value : undefined;
}

function safeLink(url) {
  return /^https?:\/\//i.test(url || "") ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, url) : "—";
}

function toast(msg, bad) {
  const t = h("div", { class: "toast" + (bad ? " bad" : "") }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), 3600);
}

function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

class ApiError extends Error {
  constructor(code, message, trace) { super(message); this.code = code; this.trace = trace; }
}

async function api(method, path, body, { timeout = 20000 } = {}) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeout);
  let res;
  try {
    res = await fetch("/v1" + path, {
      method, signal: ctl.signal,
      headers: { "Content-Type": "application/json", ...(S.token ? { Authorization: "Bearer " + S.token } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw e.name === "AbortError" ? new ApiError("TIMEOUT", "请求超时，可以重试（同一条消息不会重复生成）")
      : new ApiError("NETWORK", "无法连接服务，请确认 agent2 serve 正在运行");
  } finally {
    clearTimeout(timer);
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 401 && path !== "/demo/login") logout("登录已失效，请重新选择身份");
    throw new ApiError(data.code || `HTTP_${res.status}`, data.message || "请求失败", data.trace_id);
  }
  return data;
}

function overlay(panel) {
  closeOverlay();
  const scrim = h("div", { class: "scrim", onclick: closeOverlay });
  scrim.dataset.overlay = "1";
  panel.dataset.overlay = "1";
  document.body.append(scrim, panel);
}

function closeOverlay() {
  document.querySelectorAll("[data-overlay]").forEach((n) => n.remove());
}

// ---------- shell / routing ----------

function topBar(cur) {
  const right = [];
  if (S.user) {
    right.push(
      h("a", { class: "nav" + (cur === "chat" ? " cur" : ""), href: "#/chat" }, "我的方案"),
      h("a", { class: "nav" + (cur === "feedback" ? " cur" : ""), href: "#/feedback" }, "纠错记录"),
      h("div", { class: "rm" }, h("div", { class: "av sf" }, S.user.display_name.slice(0, 1)), S.user.display_name),
      h("button", { class: "linkbtn", onclick: () => logout() }, "切换身份"));
  }
  return h("div", { class: "top" },
    h("div", { class: "logo", onclick: () => go(S.user ? "#/chat" : "#/login") },
      h("div", { class: "mk sf" }, "留"), h("b", { class: "sf" }, "留学准备顾问"),
      h("small", null, "PRIVATE BANKING · EDUCATION")),
    h("div", { class: "r" }, right));
}

function shell(content, cur) {
  $app.replaceChildren(topBar(cur), h("div", { class: "gold" }), content);
}

function logout(message) {
  S.token = null; S.user = null; S.pid = null; S.profiles = []; S.fb = {}; S.pending = null;
  S.followSeed = null; S.followTrail = []; S.suppressFields = [];
  localStorage.removeItem("gp_token");
  localStorage.removeItem("gp_pid");
  if (message) toast(message, true);
  go("#/login");
}

async function route() {
  closeOverlay();
  const [page, arg] = location.hash.replace(/^#\/?/, "").split("/");
  try {
    if (!S.fields) S.fields = (await api("GET", "/fields")).fields;
    if (!S.token || page === "login") return renderLogin();
    if (!S.user) S.user = await api("GET", "/me");
    if (page === "onboarding") return await renderOnboarding();
    if (page === "report") return await renderReport(arg);
    if (page === "feedback") return await renderFeedback();
    return await renderChat(page === "chat" ? arg : null);
  } catch (e) {
    if (!S.token) return;
    shell(h("div", { class: "page" }, h("div", { class: "empty-state" },
      h("p", null, `页面加载失败：${e.message}`),
      e.trace ? h("p", { class: "mono" }, `trace_id: ${e.trace}`) : null,
      h("button", { class: "btn", onclick: route }, "重试"))), page);
  }
}

// ---------- /login ----------

function renderLogin() {
  const name = h("input", { name: "n", placeholder: "或输入其他用户名（进入后手动建档）", maxlength: "20" });
  shell(h("div", { class: "page" },
    h("h1", { class: "sf" }, "选择身份"),
    h("p", { class: "lead" }, "选择一位预设客户进入，体验「上方案表 · 下对话」的留学准备顾问。"),
    h("div", { class: "presets" }, PRESETS.map((p) => h("button", { class: "preset", onclick: () => login(p) },
      h("span", { class: "who" }, p.tag), h("b", { class: "sf" }, p.user), h("span", { class: "kids" }, p.desc)))),
    h("form", { class: "custom", onsubmit: (e) => {
      e.preventDefault();
      if (name.value.trim()) login({ user: name.value.trim(), profiles: [] });
    } }, name, h("button", { class: "btn" }, "进入"))), "login");
}

async function login(preset) {
  try {
    const res = await api("POST", "/demo/login", { user: preset.user });
    S.token = res.token;
    localStorage.setItem("gp_token", res.token);
    S.user = await api("GET", "/me");
    let { profiles } = await api("GET", "/profiles");
    if (!profiles.length && preset.profiles.length) {
      for (const p of preset.profiles) {
        const created = await api("POST", "/profiles", { fields: p.fields, synthetic: true });
        for (const text of p.todos || []) await api("POST", `/profiles/${created.profile_id}/plan`, { text });
      }
      profiles = (await api("GET", "/profiles")).profiles;
    }
    S.pid = null;
    localStorage.removeItem("gp_pid");
    go(profiles.length ? "#/chat" : "#/onboarding");
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------- field inputs ----------

function fieldInput(code, value) {
  const f = S.fields[code] || { kind: "text", options: [] };
  if (f.options.length) {
    return h("select", { name: code }, h("option", { value: "" }, "未填写"),
      f.options.map((o) => h("option", { value: JSON.stringify(o.value), selected: value !== undefined && o.value === value },
        o.label)));
  }
  if (f.kind === "date") return h("input", { type: "date", name: code, value: value || "" });
  if (f.kind === "age") return h("input", { type: "number", min: "5", max: "99", name: code, value: value ?? "" });
  return h("input", { type: "text", name: code, value: value ?? "", maxlength: "80" });
}

function readInput(el, code) {
  const f = S.fields[code] || { kind: "text", options: [] };
  const raw = (el.value || "").trim();
  if (raw === "") return undefined;
  if (f.options.length) return JSON.parse(raw);
  if (f.kind === "age") return parseInt(raw, 10);
  if (f.kind === "amount") return Number(raw);
  return raw;
}

// ---------- /onboarding ----------

async function renderOnboarding() {
  const codes = ["role", "beneficiary", "destination_country", "school", "course", "offer_status", "planned_start_date"];
  const required = ["role", "beneficiary", "destination_country"];
  const { profiles } = await api("GET", "/profiles");
  const err = h("div", { class: "formerr" });
  const form = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    const fields = {};
    for (const c of codes) {
      const v = readInput(form.elements[c], c);
      if (v !== undefined) fields[c] = v;
    }
    if (required.some((c) => fields[c] === undefined)) {
      err.textContent = "请至少填写角色、服务对象和目的地。";
      return;
    }
    try {
      const p = await api("POST", "/profiles", { fields, synthetic: true });
      go("#/chat/" + p.profile_id);
    } catch (x) {
      err.textContent = x.message;
    }
  } },
  h("div", { class: "form" }, codes.map((c) => h("label", null,
    (GRID_LABEL[c] || label(c)) + (required.includes(c) ? " *" : ""), fieldInput(c)))),
  err,
  h("div", { class: "btns" },
    profiles.length ? h("button", { class: "btn ghost", type: "button", onclick: () => go("#/chat") }, "返回") : null,
    h("button", { class: "btn" }, "创建并进入方案")));
  shell(h("div", { class: "page" },
    h("h1", { class: "sf" }, "新建留学档案"),
    h("p", { class: "lead" }, "只需最少信息即可开始；其余信息会在对话中按需追问。档案事实只有经您确认才会保存。"),
    form), "onboarding");
}

// ---------- /chat ----------

async function loadMessages() {
  S.messages = (await api("GET", `/sessions/${S.conv.session_id || S.conv.conversation_id}/messages?limit=200`)).messages;
}
async function loadAdvice() { S.advice = (await api("GET", `/profiles/${S.pid}/advice`)).advice; }
async function loadPlan() { S.plan = (await api("GET", `/profiles/${S.pid}/plan`)).items; }
async function loadProposals() { S.proposals = (await api("GET", `/profiles/${S.pid}/proposals`)).items; }

async function refreshProfile() {
  S.profile = await api("GET", `/profiles/${S.pid}`);
  syncProfile();
}

function syncProfile() {
  S.profiles = S.profiles.map((p) => (p.profile_id === S.profile.profile_id ? S.profile : p));
}

function lastAnswer() {
  for (let i = S.messages.length - 1; i >= 0; i--) if (S.messages[i].role === "assistant") return S.messages[i].body;
  return null;
}

function deriveMissing() {
  const last = lastAnswer();
  const codes = new Set();
  if (last) {
    (last.missing_fields || []).forEach((c) => codes.add(c));
    (last.follow_up_questions || []).forEach((q) => codes.add(q.field));
    (last.answer.materials || []).forEach((m) => (m.pending_fields || []).forEach((c) => codes.add(c)));
  }
  S.missing = new Set([...codes].filter((c) => val(S.profile, c) === undefined));
}

async function renderChat(pidArg) {
  shell(h("div", { class: "loading" }, "加载中…"), "chat");
  S.profiles = (await api("GET", "/profiles")).profiles;
  if (!S.profiles.length) return go("#/onboarding");
  const pid = [pidArg, S.pid].find((id) => id && S.profiles.some((p) => p.profile_id === id)) || S.profiles[0].profile_id;
  if (pid !== S.pid) {
    S.highlight = new Set(); S.pending = null;
    S.followSeed = null; S.followTrail = []; S.suppressFields = [];
  }
  S.pid = pid;
  localStorage.setItem("gp_pid", pid);
  S.profile = S.profiles.find((p) => p.profile_id === pid);
  S.conv = await api("POST", `/profiles/${pid}/session`);
  await Promise.all([loadMessages(), loadAdvice(), loadPlan(), loadProposals()]);
  deriveMissing();
  drawChatPage();
}

function drawChatPage() {
  const side = h("div", { class: "side" });
  const sheet = h("div", { class: "sheet" + (S.collapsed ? " collapsed" : "") });
  if (S.sheetH) sheet.style.height = S.sheetH;
  const chat = h("div", { class: "chat" });
  const bar = h("div", { class: "bar", title: "拖动调整方案表高度，双击折叠 / 展开" }, h("i"));
  setupDrag(bar, sheet);
  const input = h("textarea", { placeholder: "请输入您的问题…（Enter 发送，Shift + Enter 换行）", maxlength: "1000" });
  const submit = () => {
    const text = input.value.trim();
    if (!text || S.busy) return;
    input.value = "";
    send(text);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });
  const sendBtn = h("button", { class: "btn", onclick: submit }, "发送");
  S.el = { side, sheet, chat, input, sendBtn };
  shell(h("div", { class: "wrap" }, side, h("div", { class: "main" }, sheet, bar, chat,
    h("div", { class: "in" }, input, sendBtn))), "chat");
  drawSide();
  drawSheet();
  drawChat();
}

function setupDrag(bar, sheet) {
  bar.addEventListener("dblclick", () => {
    S.collapsed = !S.collapsed;
    sheet.classList.toggle("collapsed", S.collapsed);
  });
  bar.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const startY = e.clientY;
    const startH = sheet.getBoundingClientRect().height;
    const move = (ev) => {
      S.collapsed = false;
      sheet.classList.remove("collapsed");
      sheet.style.height = Math.max(80, Math.min(window.innerHeight - 260, startH + ev.clientY - startY)) + "px";
      S.sheetH = sheet.style.height;
    };
    const up = () => { removeEventListener("mousemove", move); removeEventListener("mouseup", up); };
    addEventListener("mousemove", move);
    addEventListener("mouseup", up);
  });
}

function profileSub(p) {
  const start = val(p, "planned_start_date");
  const m = /^(\d{4})-(\d{2})/.exec(start || "");
  const parts = [val(p, "school") || valueLabel("destination_country", val(p, "destination_country")),
    val(p, "course"), m ? `${m[1]}年${+m[2]}月` : "入学待定"];
  return parts.filter((x) => x && x !== "—").join(" · ");
}

function drawSide() {
  S.el.side.replaceChildren(
    h("div", { class: "lab" }, "我的 PROFILE"),
    ...S.profiles.map((p) => {
      const name = val(p, "beneficiary") || "未命名档案";
      return h("div", { class: "pf" + (p.profile_id === S.pid ? " on2" : ""), onclick: () => go("#/chat/" + p.profile_id) },
        h("div", { class: "av2 sf" }, name.slice(-1)),
        h("div", null, h("b", { class: "sf" }, name), h("div", { class: "sub" }, profileSub(p))));
    }),
    h("button", { class: "new", onclick: () => go("#/onboarding") }, "＋ 添加 profile"));
}

function drawSheet() {
  const p = S.profile;
  const name = val(p, "beneficiary") || "未命名档案";
  const filled = GRID.filter((c) => val(p, c) !== undefined).length;
  const done = S.plan.filter((i) => i.done).length;
  const updated = Object.values(S.advice).map((a) => a.updated_at).sort().pop();
  S.el.sheet.replaceChildren(
    h("div", { class: "sh" },
      h("h2", { class: "sf" }, `${name} · 留学准备方案`),
      h("span", { class: "meta" },
        h("span", null, "基本信息 ", h("em", { class: "sf" }, filled), ` / ${GRID.length}`),
        h("span", null, "待办已完成 ", h("em", { class: "sf" }, done), ` / ${S.plan.length}`),
        h("span", null, updated ? `方案更新于 ${fmtClock(updated)}` : "方案尚未生成"),
        h("button", { class: "btn", onclick: () => go("#/report/" + S.pid) }, "导出报告"))),
    h("div", { class: "mt" }, "一 · 留学基本信息", h("button", { class: "ed", onclick: () => openEdit() }, "✎ 编辑信息")),
    h("div", { class: "grid" }, GRID.map(cell)),
    h("div", { class: "mt" }, "二 · 为您定制的方案"),
    h("div", { class: "plan" }, TOPICS.map(planCard)),
    h("div", { class: "mt" }, "三 · 我的待办"),
    todoBox());
  if (S.flash.length) {
    const flashed = S.flash;
    S.flash = [];
    setTimeout(() => flashed.forEach((k) => document.getElementById("card-" + k)?.classList.remove("flash")), 2400);
  }
}

function cell(code) {
  const v = val(S.profile, code);
  const miss = v === undefined && S.missing.has(code);
  return h("div", { class: "c" + (miss ? " miss" : "") + (S.highlight.has(code) ? " hl" : ""), title: "点击编辑",
    onclick: () => openEdit(code) },
  h("i", null, GRID_LABEL[code] || label(code)), h("span", { class: "pen" }, "✎"),
  v === undefined ? h("b", { class: miss ? null : "e" }, miss ? "待确认" : "—")
    : h("b", { title: valueLabel(code, v) }, valueLabel(code, v)));
}

function cardHead(t, status) {
  return h("h4", { class: "sf" }, h("span", null, h("small", null, t.no), t.title), status);
}

function row(k, ...v) {
  return h("div", { class: "row" }, h("span", null, k), h("span", null, ...v));
}

const empty = () => h("span", { class: "e" }, "—");

function planCard(t) {
  if (t.key === "visa") return visaCard(t);
  return h("div", { class: "p", id: "card-" + t.key }, cardHead(t, h("span", { class: "st" }, "待补充")),
    t.rows.map((r) => row(r, empty())));
}

function sourceChips(citations) {
  const seen = new Map();
  (citations || []).forEach((c) => { if (!seen.has(c.source_name)) seen.set(c.source_name, c); });
  const sources = [...seen.values()];
  return [sources.slice(0, 2).map((c) => h("button", { class: "cite", title: c.source_name, onclick: () => openCitation(c) },
    `${c.source_name.length > 14 ? c.source_name.slice(0, 13) + "…" : c.source_name} · ${fmtVerified(c.verified_at)}`)),
  sources.length > 2 ? h("span", { class: "e" }, ` 等 ${sources.length} 处`) : null];
}

function visaCard(t) {
  const a = S.advice.visa;
  const flash = S.flash.includes("visa") ? " flash" : "";
  const visa = val(S.profile, "visa_category");
  if (!a) {
    return h("div", { class: "p" + flash, id: "card-visa" }, cardHead(t, h("span", { class: "st" }, "未开始")),
      row("类别", visa ? valueLabel("visa_category", visa) : empty()), row("材料", empty()), row("待确认", empty()));
  }
  const pl = a.payload;
  const mats = pl.materials || [];
  const req = mats.filter((m) => m.applicability === "required");
  const cond = mats.filter((m) => m.applicability !== "required");
  const unconfirmed = pl.unconfirmed_fields || [];
  const warn = unconfirmed.length || mats.some((m) => m.applicability === "unverified");
  const names = req.slice(0, 2).map((m) => m.name_zh).join("、");
  return h("div", { class: "p" + flash, id: "card-visa" },
    cardHead(t, h("span", { class: "st " + (warn ? "warn" : "ok") }, warn ? "待核实" : "已确认")),
    row("类别", `${valueLabel("destination_country", val(S.profile, "destination_country"))} · ${valueLabel("visa_category", visa)}`),
    row("必备材料", req.length ? `${names}${req.length > 2 ? " 等" : ""} ${req.length} 项` : empty()),
    row("视情况", cond.length ? `${cond.length} 项` : "无"),
    row("待确认", unconfirmed.length ? h("span", { class: "warn" }, unconfirmed.map(label).join("、")) : "无"),
    row("来源", sourceChips(pl.citations)),
    h("div", { class: "why" }, `v${a.version} · 更新于 ${fmtClock(a.updated_at)}`));
}

function todoBox() {
  const input = h("input", { placeholder: "添加一条待办，例如「预约签证中心」", maxlength: "200" });
  return h("div", { class: "todo" },
    S.plan.length ? h("ul", null, S.plan.map((i) => h("li", { class: i.done ? "done" : "" },
      h("input", { type: "checkbox", checked: i.done, onchange: (e) => updatePlan(i, { done: e.target.checked }) }),
      h("span", { title: "双击修改", ondblclick: () => {
        const text = prompt("修改待办", i.text);
        if (text && text.trim() && text.trim() !== i.text) updatePlan(i, { text: text.trim() });
      } }, i.text),
      h("button", { class: "x", title: "删除", onclick: () => deletePlan(i) }, "×"))))
      : h("div", { class: "empty" }, "暂无待办。可在回答的材料旁点「+ 待办」，或在下方直接添加。"),
    h("form", { onsubmit: (e) => {
      e.preventDefault();
      if (input.value.trim()) addPlan(input.value.trim());
    } }, input, h("button", null, "添加")));
}

async function addPlan(text) {
  try {
    await api("POST", `/profiles/${S.pid}/plan`, { text });
    await loadPlan();
    drawSheet();
    drawChat({ keep: true });
  } catch (e) { toast(e.message, true); }
}

async function updatePlan(item, change) {
  try {
    await api("PATCH", `/plan-items/${item.item_id}`, change);
    await loadPlan();
    drawSheet();
  } catch (e) { toast(e.message, true); }
}

async function deletePlan(item) {
  try {
    await api("DELETE", `/plan-items/${item.item_id}`);
    await loadPlan();
    drawSheet();
    drawChat({ keep: true });
  } catch (e) { toast(e.message, true); }
}

// ---------- chat stream ----------

function stepsLine(states) {
  return h("div", { class: "steps" }, states.map(([text, cls]) => h("span", { class: cls }, text)));
}

function finalSteps(status) {
  const s = STEPS.map((text) => [text, "d"]);
  if (status === "needs_input") {
    s[1] = ["追问 · 等待您补充", "a"];
    for (let i = 2; i < s.length; i++) s[i][1] = "";
  } else if (status === "out_of_scope") {
    s[1] = ["范围外 · 未查询资料", "w"];
    for (let i = 2; i < s.length; i++) s[i][1] = "";
  } else {
    s[1] = ["无需追问", "d"];
    if (status !== "answered") s[3] = [status === "needs_verification" ? "核对 · 待核实" : "核对 · 无法确定", "w"];
  }
  return s;
}

function pendingSteps(k) {
  return STEPS.map((text, i) => [text, i < k ? "d" : i === k ? "a pulse" : ""]);
}

function suggestions() {
  const dest = valueLabel("destination_country", val(S.profile, "destination_country"));
  const place = dest === "—" ? "留学" : dest;
  return [`去${place}读书，签证需要准备哪些材料？`, "学费怎么汇？", "需要先开户吗？",
    `去${place}留学，应该办哪张信用卡？`];
}

function drawChat({ keep } = {}) {
  const box = S.el.chat;
  if (!box) return;
  const scroll = box.scrollTop;
  const items = [];
  if (!S.messages.length && !S.pending) {
    const name = val(S.profile, "beneficiary") || "您";
    items.push(h("div", { class: "m welcome" },
      h("h3", { class: "sf" }, `您好，我是${name}的留学准备顾问`),
      h("p", null, "可以直接问签证材料等问题。我只根据已发布的官方资料作答：资料不足会明确标注「待核实」或「无法回答」，不做猜测。"),
      h("div", { class: "chips" }, suggestions().map((q) => h("button", { onclick: () => send(q) }, q)))));
  }
  let lastIdx = -1;
  S.messages.forEach((m, i) => { if (m.role === "assistant") lastIdx = i; });
  S.messages.forEach((m, i) => {
    if (m.role === "user") {
      items.push(h("div", { class: "m u" }, m.body.text));
      return;
    }
    const latest = i === lastIdx && !S.pending;
    // During follow-ups, skip the process strip — only the question should show.
    if (latest && m.body.status !== "needs_input") items.push(stepsLine(finalSteps(m.body.status)));
    const bubble = answerBubble(m.body, latest);
    if (bubble) items.push(bubble);
  });
  if (S.pending) {
    items.push(h("div", { class: "m u pending" }, S.pending.text));
    if (S.pending.error) {
      const p = S.pending;
      items.push(h("div", { class: "m err" }, h("p", null, p.error.message),
        h("div", { class: "chips" },
          h("button", { class: "s", onclick: () => {
            S.pending = null;
            send(p.text, p.cid, p.changed, p.confirmedFields, p.expectedProfileVersion);
          } }, "重试"),
          h("button", { onclick: () => { S.pending = null; drawChat(); } }, "放弃"))));
    } else {
      items.push(stepsLine(pendingSteps(S.pending.k)));
    }
  }
  box.replaceChildren(...items);
  box.scrollTop = keep ? scroll : box.scrollHeight;
}

function isCardRecommendationSteps(steps) {
  return steps.some((s) => s.startsWith("年费："))
    && steps.some((s) => /主推荐：|汇丰.+发行的卡/.test(s));
}

function renderAnswerSteps(steps) {
  if (!isCardRecommendationSteps(steps)) {
    return [h("div", { class: "sub-h" }, "步骤"),
      h("ol", { class: "lim" }, steps.map((s) => h("li", null, s)))];
  }
  // Card copy is not a procedure — do not number section titles as 1. 2. 3.
  const box = h("div", { class: "card-rec" });
  for (const s of steps) {
    if (/^(优先|备用)?[：:]?汇丰.+发行的卡/.test(s)) {
      const market = s.includes("英国") ? "英国" : s.includes("美国") ? "美国" : "香港";
      box.append(h("div", { class: "card-sec primary" },
        h("span", { class: "market-tag" }, "申请地区"),
        h("span", null, `${market}发行的卡`)));
    }
    else if (/^(主推荐|场景搭配|其他选择)：/.test(s)) {
      const i = s.indexOf("：");
      box.append(h("div", { class: "card-name" },
        h("span", { class: "role-tag" }, s.slice(0, i)),
        h("span", null, s.slice(i + 1))));
    }
    else if (/^(推荐理由|为什么适合你|年费|主要权益|你可享|申请限制|申请前提)：/.test(s)) {
      const i = s.indexOf("：");
      box.append(h("div", { class: `card-detail ${/^(?:推荐理由|为什么适合你)/.test(s) ? "reason" : ""}` },
        h("span", { class: "k" }, s.slice(0, i + 1)), s.slice(i + 1)));
    } else if (s.startsWith("免年费选择：")) box.append(h("div", { class: "card-alt" }, s));
    else if (/（(?:Visa|Mastercard|UnionPay|银联卡|威士卡|万事达卡)/.test(s) || /（[A-Za-z]+）$/.test(s)) {
      box.append(h("div", { class: "card-name" }, s));
    } else box.append(h("div", { class: "card-note" }, s));
  }
  const preview = steps.some((s) => s.startsWith("提前了解："));
  const compare = steps.some((s) => s.startsWith("对比参考："));
  return [h("div", { class: "sub-h" }, preview ? "提前了解的信用卡" :
    compare ? "两张卡怎么选" : "为你挑选的信用卡"), box];
}

function answerBubble(b, latest) {
  // Follow-ups: chat only ever shows the current question + answer controls.
  // Past needs_input turns are dropped so the cluttered status/steps/proposal card
  // cannot reappear above the latest ask.
  if (b.status === "needs_input") {
    if (!latest) return null;
    const question = (b.follow_up_questions || []).find((q) => val(S.profile, q.field) === undefined)
      || (b.follow_up_questions || [])[0];
    if (!question) return null;
    const kids = [followUp(question)];
    if (S.followTrail.length) {
      kids.push(h("div", { class: "fq-nav" },
        h("button", { class: "btn sm ghost", disabled: S.busy, onclick: goBackFollowUp },
          "上一步")));
    }
    return h("div", { class: "m s-needs_input ask-only" }, kids);
  }

  const st = STATUS[b.status] || STATUS.cannot_answer;
  const cmap = {};
  (b.citations || []).forEach((c, i) => { cmap[c.citation_id] = { ...c, n: i + 1 }; });
  const mats = b.answer.materials || [];
  const kids = [];

  if (b.status !== "answered") {
    kids.push(h("div", { class: "status" }, h("span", { class: "st " + st.cls }, st.label), h("span", { class: "why" }, st.why)));
  }
  const cardAnswer = isCardRecommendationSteps(b.answer.steps || []);
  if (!cardAnswer) kids.push(h("p", null, b.answer.summary));
  if (mats.length) kids.push(h("ul", { class: "mat" }, mats.map((m) => materialItem(m, cmap))));
  if ((b.answer.steps || []).length) {
    kids.push(...renderAnswerSteps(b.answer.steps));
  }
  if ((b.conflicts || []).length) {
    kids.push(h("div", { class: "sub-h" }, "不同来源说法不一致（并列展示，未替您取舍）"));
    b.conflicts.forEach((cf) => kids.push(h("div", null, cf.name_zh), h("div", { class: "conf" }, cf.variants.map((v) =>
      h("div", null, h("b", null, v.source_name), v.required ? "需要" : "不需要", v.condition ? `（${v.condition}）` : "",
        citeChip(cmap[v.citation_id]))))));
  }
  if ((b.answer.limitations || []).length) {
    kids.push(h("div", { class: "sub-h" }, "说明与限制"), h("ul", { class: "lim" }, b.answer.limitations.map((s) => h("li", null, s))));
  }
  if (b.status !== "answered" && (b.next_actions || []).length) {
    kids.push(h("div", { class: "sub-h" }, "下一步"), h("ul", { class: "lim" }, b.next_actions.map((s) => h("li", null, s))));
  }
  (b.notices || []).forEach((n) => kids.push(h("div", { class: "notice" }, n.message)));
  const open = new Set(S.proposals.map((p) => p.proposal_id));
  (b.profile_change_proposals || []).filter((p) => open.has(p.proposal_id)).forEach((p) => kids.push(proposalCard(p)));
  kids.push(actsRow(b));
  return h("div", { class: "m s-" + b.status }, kids);
}

function citeChip(c) {
  return c ? h("button", { class: "cite", title: c.source_name, onclick: () => openCitation(c) },
    `来源${c.n} · ${fmtVerified(c.verified_at)}`) : null;
}

function materialItem(m, cmap) {
  const todo = `准备材料：${m.name_zh}`;
  const exists = S.plan.some((i) => i.text === todo);
  return h("li", null,
    h("span", null, m.name_zh),
    m.name_en && m.name_en !== m.name_zh ? h("span", { class: "en" }, m.name_en) : null,
    h("span", { class: "tagx " + m.applicability }, TAG[m.applicability] || m.applicability),
    citeChip(cmap[m.citation_id]),
    h("button", { class: "addtodo", disabled: exists, onclick: () => addPlan(todo) }, exists ? "✓ 已在待办" : "+ 待办"),
    m.condition_text ? h("span", { class: "cond" }, `条件：${m.condition_text}`) : null,
    (m.pending_fields || []).length ? h("span", { class: "pend" }, `待确认：${m.pending_fields.map(label).join("、")}`) : null);
}

function followUp(q) {
  const f = S.fields[q.field] || { kind: "text", options: [], label: q.field };
  const options = (q.options && q.options.length) ? q.options : f.options;
  const chips = h("div", { class: "chips" });
  if (q.input_type === "multi_select") {
    const selected = new Map();
    const confirm = h("button", { class: "s", disabled: true }, "确认并保存到画像");
    options.forEach((o) => {
      const button = h("button", { onclick: () => {
        if (selected.has(o.value)) { selected.delete(o.value); button.classList.remove("on"); }
        else if (!q.max_selections || selected.size < q.max_selections) {
          selected.set(o.value, o.label); button.classList.add("on");
        } else {
          // At the cap: flash the button so the rejected click is not silent.
          button.classList.add("deny");
          setTimeout(() => button.classList.remove("deny"), 350);
        }
        confirm.disabled = selected.size === 0;
      } }, o.label);
      chips.append(button);
    });
    confirm.addEventListener("click", () => answerField(
      q.field, [...selected.keys()], [...selected.values()].join("、"), q.persist_mode));
    chips.append(confirm);
  } else if (options.length) {
    options.forEach((o) => chips.append(h("button", {
      onclick: () => answerField(q.field, o.value, o.label, q.persist_mode)
    }, o.label)));
  } else {
    const input = h("input", { type: f.kind === "date" ? "date" : f.kind === "age" ? "number" : "text", placeholder: f.label });
    input.dataset.field = q.field;
    const ok = () => {
      const v = readInput(input, q.field);
      if (v !== undefined) answerField(q.field, v, valueLabel(q.field, v));
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") ok(); });
    chips.append(input, h("button", { class: "s", onclick: ok }, "确定"));
  }
  return h("div", { class: "fq" }, h("div", { class: "q" }, q.text), chips);
}

// Values typed into follow-up inputs but not confirmed with Enter / 确定 are submitted
// together with the answer the user clicked, so they are never silently dropped.
function typedAnswers(except) {
  const out = {};
  document.querySelectorAll(".fq input[data-field]").forEach((el) => {
    const code = el.dataset.field;
    if (code === except || code in out) return;
    const v = readInput(el, code);
    if (v !== undefined && !Number.isNaN(v)) out[code] = v;
  });
  return out;
}

async function answerField(code, value, text, persistMode) {
  if (S.busy) { toast("上一条还在处理中，请稍候再提交"); return; }
  // Always confirm the answered follow-up. Date/text fields used to use
  // proposal-only mode, so the value never reached conversation facts and the
  // same question was asked again (display text is Chinese, e.g. 2027年1月1日).
  const confirmed = { [code]: value };
  S.suppressFields = S.suppressFields.filter((item) => item !== code);
  S.followTrail.push({ field: code, value, label: text, persistMode: persistMode || "save_on_select" });
  await send(`${label(code)}：${text}`, null, Object.keys(confirmed), confirmed, S.profile.version);
}

async function goBackFollowUp() {
  if (S.busy) { toast("上一条还在处理中，请稍候"); return; }
  if (!S.followTrail.length) { toast("没有可返回的步骤"); return; }
  const last = S.followTrail.pop();
  if (!S.suppressFields.includes(last.field)) S.suppressFields.push(last.field);
  // One request undoes the profile value and session fact, then re-asks.
  const res = await send("（返回上一步）", null, [last.field], {}, S.profile.version, {
    clearedFields: S.suppressFields.slice(),
    isBack: true,
  });
  if (!res) {
    S.followTrail.push(last);
    S.suppressFields = S.suppressFields.filter((item) => item !== last.field);
    return;
  }
  toast(`已返回，请重新选择「${label(last.field)}」`);
}

function proposalCard(p) {
  return h("div", { class: "prop" },
    h("div", null, "档案变更提议：", h("b", null, label(p.field_code)), "　",
      p.old_value !== null && p.old_value !== undefined ? valueLabel(p.field_code, p.old_value) : "（空）", " → ",
      h("b", null, valueLabel(p.field_code, p.new_value))),
    h("div", { class: "why" }, `${p.basis}。确认后才会写入档案。`),
    h("div", { class: "btns" },
      h("button", { class: "btn sm", onclick: () => decide(p, true) }, "确认更新"),
      h("button", { class: "btn sm ghost", onclick: () => decide(p, false) }, "暂不")));
}

async function decide(p, confirm) {
  try {
    if (confirm) {
      S.profile = await api("POST", `/profile-proposals/${p.proposal_id}/confirm`, { expected_version: S.profile.version });
      syncProfile();
      S.highlight = new Set([p.field_code]);
    } else {
      await api("POST", `/profile-proposals/${p.proposal_id}/dismiss`, {});
    }
    await loadProposals();
    deriveMissing();
    drawSheet();
    drawSide();
    drawChat({ keep: true });
    toast(confirm ? "已写入档案" : "已忽略该提议");
  } catch (e) {
    if (e.code === "VERSION_CONFLICT") await refreshProfile();
    toast(e.message, true);
  }
}

function actsRow(b) {
  const st = STATUS[b.status] || STATUS.cannot_answer;
  const state = S.fb[b.message_id] || (S.fb[b.message_id] = {});
  const parts = [h("div", { class: "acts" },
    h("span", { class: "fb" },
      h("span", { class: "st " + st.cls }, st.label), h("span", { class: "sep" }, "｜"),
      h("button", { class: state.kind === "helpful" ? "on" : "", onclick: () => feedback(b, "helpful") },
        state.kind === "helpful" ? "已标记有帮助" : "有帮助"), "·",
      h("button", { class: state.kind === "wrong" ? "on" : "", onclick: () => {
        state.open = !state.open;
        drawChat({ keep: true });
      } }, "信息有误"), "·",
      h("button", { onclick: () => { S.el.input.placeholder = "继续补充您的情况或追问…"; S.el.input.focus(); } }, "继续补充")),
    h("span", { class: "trace", title: `trace_id：${b.trace_id}` },
      `${(b.citations || []).length} 条来源 · ${b.trace_id.slice(0, 8)}`))];
  if (state.feedbackId) {
    parts.push(h("div", { class: "fbok" }, `已提交纠错，编号 ${state.feedbackId}。可在「纠错记录」查看处理状态。`));
  } else if (state.open) {
    const note = h("textarea", { placeholder: "哪里有误？例如：材料清单缺少某项 / 来源已过期（可选）", maxlength: "500" });
    parts.push(h("div", { class: "fbform" }, note, h("button", { class: "btn sm", onclick: () => feedback(b, "wrong", note.value) },
      "提交纠错")));
  }
  return parts;
}

async function feedback(b, kind, note) {
  try {
    const res = await api("POST", `/messages/${b.message_id}/feedback`, { kind, note: note || "" });
    const state = S.fb[b.message_id];
    state.kind = kind;
    state.open = false;
    if (res.feedback_id) state.feedbackId = res.feedback_id;
    drawChat({ keep: true });
  } catch (e) { toast(e.message, true); }
}

async function send(text, retryCid, changed, confirmedFields, expectedProfileVersion, opts = {}) {
  if (S.busy) {
    toast("上一条还在处理中，请稍候再提交");
    return null;
  }
  S.busy = true;
  if (S.el.sendBtn) S.el.sendBtn.disabled = true;
  const confirmed = confirmedFields || {};
  const clearedFields = opts.clearedFields || [];
  const isStructured = Object.keys(confirmed).length > 0;
  // Fresh free-text starts a new chain. Structured answers and "上一步" keep it.
  if (!isStructured && !opts.isBack && !retryCid) {
    S.followTrail = [];
    S.followSeed = text;
    S.suppressFields = [];
  }
  S.highlight = new Set(changed || []);
  S.pending = { text, cid: retryCid || uuid(), k: 0, error: null, changed,
    confirmedFields: confirmed, expectedProfileVersion, clearedFields };
  drawChat();
  const ticker = setInterval(() => {
    if (S.pending && !S.pending.error && S.pending.k < 3) { S.pending.k += 1; drawChat(); }
  }, 600);
  try {
    const res = await api("POST", `/sessions/${S.conv.session_id || S.conv.conversation_id}/messages`,
      { text, client_message_id: S.pending.cid, confirmed_fields: S.pending.confirmedFields,
        expected_profile_version: S.pending.expectedProfileVersion,
        cleared_fields: S.pending.clearedFields }, { timeout: 90000 });
    clearInterval(ticker);
    S.pending = null;
    await Promise.all([loadMessages(), refreshProfile(), loadAdvice(), loadProposals()]);
    deriveMissing();
    S.flash = res.advice_updates || [];
    if (res.status && res.status !== "needs_input") {
      S.followTrail = [];
      S.followSeed = null;
      S.suppressFields = [];
    }
    drawSheet();
    drawSide();
    drawChat();
    return res;
  } catch (e) {
    clearInterval(ticker);
    if (S.pending) { S.pending.error = e; drawChat(); }
    return null;
  } finally {
    S.busy = false;
    if (S.el.sendBtn) S.el.sendBtn.disabled = false;
  }
}

// ---------- overlays ----------

async function openCitation(c) {
  const snap = h("div", { class: "note" }, "正在读取资料快照…");
  overlay(h("div", { class: "drawer" },
    h("button", { class: "close", onclick: closeOverlay, title: "关闭" }, "×"),
    h("div", { class: "lab" }, "引用来源"),
    h("h3", { class: "sf" }, c.source_name),
    c.synthetic ? h("span", { class: "mock" }, "示例资料") : null,
    h("div", { class: "kv" },
      h("span", null, "官方链接"), h("span", null, safeLink(c.source_url)),
      h("span", null, "核验时间"), h("span", null, fmtFull(c.verified_at)),
      h("span", null, "数据版本"), h("span", { class: "mono" }, c.revision_id),
      h("span", null, "记录"), h("span", { class: "mono" }, c.record_id),
      h("span", null, "证据编号"), h("span", { class: "mono" }, c.evidence_id)),
    h("div", { class: "sub-h" }, "原文引用"), h("blockquote", null, c.quote),
    c.condition_quote ? [h("div", { class: "sub-h" }, "适用条件原文"), h("blockquote", null, c.condition_quote)] : null,
    snap));
  try {
    const s = await api("GET", `/knowledge/snapshots/${encodeURIComponent(c.revision_id)}`);
    snap.textContent = `资料状态：${AVAIL[s.availability] || s.availability} · 本版本共 ${(s.evidence || []).length} 条证据 · `
      + `核验于 ${fmtFull(s.verified_at)}`;
  } catch (e) {
    snap.textContent = `资料快照暂不可读：${e.message}`;
  }
}

function openEdit(focus) {
  const p = S.profile;
  const extra = Object.keys(p.fields).filter((c) => !GRID.includes(c) && S.fields[c] && val(p, c) !== undefined);
  const missing = [...S.missing].filter((c) => !GRID.includes(c) && !extra.includes(c) && S.fields[c]);
  const codes = [...GRID, ...extra, ...missing];
  const form = h("form", { class: "form", onsubmit: (e) => e.preventDefault() },
    codes.map((c) => h("label", { class: c === focus ? "focus" : "" }, GRID_LABEL[c] || label(c), fieldInput(c, val(p, c)))));
  const err = h("div", { class: "formerr" });
  const save = async () => {
    const set = {};
    const clear = [];
    for (const c of codes) {
      let v;
      try { v = readInput(form.elements[c], c); } catch (_) { v = undefined; }
      const old = val(p, c);
      if (v === undefined) { if (old !== undefined) clear.push(c); } else if (v !== old) set[c] = v;
    }
    if (!Object.keys(set).length && !clear.length) return closeOverlay();
    try {
      S.profile = await api("PATCH", `/profiles/${S.pid}`, { expected_version: p.version, fields: set, clear_fields: clear });
      syncProfile();
      S.highlight = new Set([...Object.keys(set), ...clear]);
      deriveMissing();
      closeOverlay();
      drawSheet();
      drawSide();
      drawChat({ keep: true });
      toast(`档案已保存（v${S.profile.version}）。`);
    } catch (e) {
      err.textContent = e.message;
      if (e.code === "VERSION_CONFLICT") refreshProfile();
    }
  };
  overlay(h("div", { class: "modal" },
    h("h3", { class: "sf" }, "编辑留学基本信息"),
    h("p", { class: "note" }, "在这里修改由您直接确认，立即写入档案；对话里识别到的变化只会以「变更提议」出现，需要您确认。"),
    form, err,
    h("div", { class: "btns" },
      h("button", { class: "btn ghost", type: "button", onclick: closeOverlay }, "取消"),
      h("button", { class: "btn", type: "button", onclick: save }, "保存"))));
  form.querySelector(".focus input, .focus select")?.focus();
}

// ---------- /report/:id ----------

async function renderReport(pid) {
  shell(h("div", { class: "loading" }, "正在生成报告…"), "report");
  const [profile, adv, plan] = await Promise.all([api("GET", `/profiles/${pid}`), api("GET", `/profiles/${pid}/advice`),
    api("GET", `/profiles/${pid}/plan`)]);
  const now = new Date().toISOString();
  const name = val(profile, "beneficiary") || "未命名档案";
  const visa = adv.advice.visa;
  const cites = visa ? visa.payload.citations || [] : [];
  const cn = {};
  cites.forEach((c, i) => { cn[c.citation_id] = i + 1; });
  const pairs = [];
  for (let i = 0; i < GRID.length; i += 2) pairs.push(GRID.slice(i, i + 2));
  const topicBlock = (t) => {
    if (t.key !== "visa") return [h("h3", { class: "sf" }, `${t.no} ${t.title} · 待补充`)];
    if (!visa) return [h("h3", { class: "sf" }, `${t.no} ${t.title} · 未开始`)];
    const pl = visa.payload;
    return [h("h3", { class: "sf" }, `${t.no} ${t.title} · v${visa.version}（${fmtFull(visa.updated_at)}）`),
      h("p", null, pl.summary),
      h("ol", null, (pl.materials || []).map((m) => h("li", null, m.name_en ? `${m.name_zh}（${m.name_en}）` : m.name_zh,
        h("span", { class: "tagx " + m.applicability }, TAG[m.applicability]),
        cn[m.citation_id] ? ` [${cn[m.citation_id]}]` : "",
        m.condition_text ? h("div", { class: "note" }, `条件：${m.condition_text}`) : null,
        (m.pending_fields || []).length ? h("div", { class: "warn" }, `待确认：${m.pending_fields.map(label).join("、")}`) : null))),
      (pl.unconfirmed_fields || []).length ? h("p", { class: "warn" }, `待确认信息：${pl.unconfirmed_fields.map(label).join("、")}`) : null];
  };
  shell(h("div", { class: "report" },
    h("div", { class: "tools" },
      h("button", { class: "btn ghost", onclick: () => go("#/chat/" + pid) }, "返回方案"),
      h("button", { class: "btn", onclick: () => window.print() }, "打印 / 导出 PDF")),
    h("h1", { class: "sf" }, `${name} · 留学准备方案报告`),
    h("div", { class: "rmeta" }, h("span", null, `生成于 ${fmtFull(now)} · 档案版本 v${profile.version}`)),
    h("h2", null, "一 · 留学基本信息"),
    h("table", null, pairs.map((pair) => h("tr", null, pair.map((c) => [h("th", null, GRID_LABEL[c] || label(c)),
      h("td", null, val(profile, c) === undefined ? "待确认" : valueLabel(c, val(profile, c)))])))),
    h("h2", null, "二 · 定制方案"),
    TOPICS.map(topicBlock),
    h("h2", null, "三 · 我的待办"),
    plan.items.length ? h("ol", null, plan.items.map((i) => h("li", null, `${i.done ? "☑" : "☐"} ${i.text}`)))
      : h("p", { class: "note" }, "暂无待办。"),
    h("h2", null, "四 · 资料来源"),
    cites.length ? h("ol", null, cites.map((c) => h("li", null, h("b", null, c.source_name), " ", safeLink(c.source_url),
      h("div", { class: "note" }, `核验 ${fmtFull(c.verified_at)} · 数据版本 ${c.revision_id}`),
      h("div", { class: "note" }, `「${c.quote.length > 160 ? c.quote.slice(0, 160) + "…" : c.quote}」`))))
      : h("p", { class: "note" }, "暂无引用来源。"),
    h("div", { class: "disc" },
      `本报告是 ${fmtFull(now)} 的方案快照，内容仅基于当时已发布的官方资料；政策可能变化，请以官方最新公布为准。`,
      "标注「待核实 / 待确认」的项目需补充信息或人工确认后再使用。")), "report");
}

// ---------- /feedback ----------

async function renderFeedback() {
  shell(h("div", { class: "loading" }, "加载中…"), "feedback");
  const [{ tickets }, { profiles }] = await Promise.all([api("GET", "/feedback"), api("GET", "/profiles")]);
  const names = Object.fromEntries(profiles.map((p) => [p.profile_id, val(p, "beneficiary") || "未命名档案"]));
  shell(h("div", { class: "page" },
    h("h1", { class: "sf" }, "纠错记录"),
    h("p", { class: "lead" }, "在任意回答下点「信息有误」即可提交纠错，系统会生成编号；运营复核后会更新处理状态。"),
    tickets.length ? h("table", { class: "tickets" },
      h("tr", null, ["编号", "提交时间", "档案", "原回答", "您的说明", "状态"].map((t) => h("th", null, t))),
      tickets.map((t) => h("tr", null,
        h("td", { class: "mono" }, t.feedback_id),
        h("td", null, fmtFull(t.created_at)),
        h("td", null, t.profile_id ? h("a", { href: "#/chat/" + t.profile_id }, names[t.profile_id] || "—") : "—"),
        h("td", null, h("span", { class: "st " + (STATUS[t.answer_status] || {}).cls }, (STATUS[t.answer_status] || {}).label || ""),
          h("div", null, t.answer_summary.length > 60 ? t.answer_summary.slice(0, 60) + "…" : t.answer_summary)),
        h("td", null, t.note),
        h("td", null, t.status === "open" ? "待处理" : "已处理"))))
      : h("div", { class: "empty-state" }, "还没有纠错记录。")), "feedback");
}

addEventListener("hashchange", route);
addEventListener("keydown", (e) => { if (e.key === "Escape") closeOverlay(); });
route();
