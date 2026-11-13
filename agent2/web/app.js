"use strict";

const $app = document.getElementById("app");
const GRID = ["role", "beneficiary", "destination_country", "school", "course", "offer_status",
  "planned_start_date", "fee_status", "payment_deadline", "payer", "remittance_purpose", "visa_category"];
const GRID_LABEL = { destination_country: "目的地", offer_status: "录取状态", planned_start_date: "入学日期",
  fee_status: "学费档位", payment_deadline: "缴费截止日", remittance_purpose: "汇款用途" };
const STATUS = {
  answered: { label: "已确认", cls: "ok", why: "" },
  needs_input: { label: "待补充", cls: "", why: "还缺少关键信息，补充后再给出确定答案。" },
  needs_verification: { label: "待核实", cls: "warn", why: "部分材料是否需要取决于尚未确认的条件，已逐项标出。" },
  cannot_answer: { label: "无法回答", cls: "warn", why: "已发布的官方资料不足以支持确定回答，不做猜测。" },
  out_of_scope: { label: "范围外", cls: "", why: "该问题不在留学准备顾问的服务范围内。" },
};
const TOPICS = [
  { key: "remittance", no: "01", title: "汇款建议", rows: ["用途", "金额", "材料", "时间点"] },
  { key: "account", no: "02", title: "开户建议", rows: ["地区", "产品", "材料", "门槛"] },
  { key: "visa", no: "03", title: "签证建议" },
  { key: "credit_card", no: "04", title: "信用卡建议", rows: ["产品", "费用 / 限制", "申请条件"] },
];
const AVAIL = { available: "有效", stale: "待复核（超过核验时效）", withdrawn: "已撤回", superseded: "已被新版本取代",
  missing_evidence: "证据缺失" };
const TAG = { required: "必需", conditional: "视情况", unverified: "待核实" };

const S = {
  token: null, csrf: null, user: null, fields: null, profiles: [], pid: localStorage.getItem("gp_pid"),
  profile: null, conv: null, messages: [], advice: {}, adviceScopes: {}, plan: [], proposals: [],
  relationships: [], relationshipProposals: [], missing: new Set(),
  highlight: new Set(), flash: [], busy: false, refreshingTopics: new Set(), pending: null, sheetH: null, collapsed: true, fb: {}, el: {},
  // One-question-at-a-time follow-ups; trail lets the user undo a wrong tap.
  followSeed: null, followTrail: [], suppressFields: [],
  schools: [], programs: [], schoolById: {}, programById: {}, catalogReady: false,
};

// 追问撤销顺序（靠前的可先被「上一步」清掉）。trail 丢失时仍可按档案回退。
// remittance_purpose 不在此列：它常由消息文本自动解析入档（如「学费怎么汇」），
// 并非用户回答过的追问步骤，撤销它只会得到一张莫名的变更提案卡。
const FOLLOW_UNDO_ORDER = [
  "payment_deadline", "fee_status",
  "want_credit_card", "application_region", "applicant_age_band", "applicant_age",
  "us_hsbc_account_path", "uk_hsbc_account_path", "annual_income_band_hkd", "annual_income_hkd",
  "hk_resident", "hsbc_premier_path", "premier_relationship", "hsbc_relationship_balance_band",
  "full_time_student", "student_institution_eligible", "repayment_mode", "spending_scenarios",
  "study_card_focus", "reward_preference", "monthly_card_spend_band_hkd", "mainland_payment_method",
  "asset_band",
];

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

function profileActionIcon(kind) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "16");
  svg.setAttribute("height", "16");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.7");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const shapes = kind === "view"
    ? [["path", { d: "M2.2 12s3.5-6 9.8-6 9.8 6 9.8 6-3.5 6-9.8 6-9.8-6-9.8-6Z" }],
      ["circle", { cx: "12", cy: "12", r: "2.5" }]]
    : [["path", { d: "M4 7h16M9 7V4h6v3M7 7l1 14h8l1-14M10 11v6M14 11v6" }]];
  shapes.forEach(([tag, attrs]) => {
    const shape = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attrs).forEach(([key, value]) => shape.setAttribute(key, value));
    svg.append(shape);
  });
  return svg;
}

const pad = (n) => String(n).padStart(2, "0");

function fmtFull(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
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

const label = (code) => (S.fields && S.fields[code] ? S.fields[code].label : "相关信息");

function valueLabel(code, v) {
  if (v === undefined || v === null) return "—";
  if (Array.isArray(v)) return v.map((item) => valueLabel(code, item)).join("、");
  if (code === "school") {
    if (S.schoolById[v]) return S.schoolById[v].name;
    // Prefer a readable placeholder over raw UUIDs when catalog miss/orphan.
    if (looksLikeId(v)) return S.catalogReady ? "未知学校" : "学校加载中…";
  }
  if (code === "course") {
    if (S.programById[v]) return S.programById[v].label;
    if (looksLikeId(v)) return S.catalogReady ? "未知课程" : "课程加载中…";
  }
  const f = S.fields && S.fields[code];
  const opt = f && f.options.find((o) => o.value === v);
  if (opt) return opt.label;
  if (f && f.kind === "date") return fmtDate(v);
  return String(v);
}

async function ensureSchools(country) {
  const q = country ? `?country=${encodeURIComponent(country)}` : "";
  const { schools } = await api("GET", `/schools${q}`);
  S.schools = schools || [];
  for (const s of S.schools) S.schoolById[s.school_id] = s;
  return S.schools;
}

async function ensurePrograms(schoolId) {
  if (!schoolId) { S.programs = []; return []; }
  const { programs } = await api("GET", `/schools/${encodeURIComponent(schoolId)}/programs`);
  S.programs = programs || [];
  for (const p of S.programs) S.programById[p.program_id] = p;
  return S.programs;
}

function looksLikeId(v) {
  return typeof v === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v);
}

async function ensureCatalogForProfiles(profiles) {
  // Sidebar shows every profile: load the full school list once, then programs
  // for each school_id still stored as a UUID on any profile.
  await ensureSchools();
  const schoolIds = [...new Set((profiles || []).map((p) => val(p, "school")).filter(looksLikeId))];
  await Promise.all(schoolIds.map((id) => ensurePrograms(id)));
  S.catalogReady = true;
}

function schoolCourseInputs(initial = {}) {
  const dest = initial.destination_country;
  const schoolSel = h("select", { name: "school" }, h("option", { value: "" }, "请先选择目的地"));
  const courseSel = h("select", { name: "course" }, h("option", { value: "" }, "请先选择学校"));
  const fillSchools = async (country, keepSchool) => {
    schoolSel.replaceChildren(h("option", { value: "" }, country ? "加载学校…" : "请先选择目的地"));
    courseSel.replaceChildren(h("option", { value: "" }, "请先选择学校"));
    if (!country) return;
    let schools = [];
    try {
      schools = await ensureSchools(country);
    } catch (e) {
      schoolSel.replaceChildren(h("option", { value: "" }, "学校列表暂不可用"));
      toast(userFacingError(e, "学校列表暂时无法加载，请稍后重试。"), true);
      return;
    }
    schoolSel.replaceChildren(h("option", { value: "" }, schools.length ? "未填写" : "暂无该地区学校"));
    for (const s of schools) {
      schoolSel.append(h("option", {
        value: s.school_id,
        selected: keepSchool !== undefined && keepSchool === s.school_id,
      }, s.name));
    }
  };
  const fillPrograms = async (schoolId, keepCourse) => {
    courseSel.replaceChildren(h("option", { value: "" }, schoolId ? "加载课程…" : "请先选择学校"));
    if (!schoolId) return;
    let programs = [];
    try {
      programs = await ensurePrograms(schoolId);
    } catch (e) {
      courseSel.replaceChildren(h("option", { value: "" }, "课程列表暂不可用"));
      toast(userFacingError(e, "课程列表暂时无法加载，请稍后重试。"), true);
      return;
    }
    courseSel.replaceChildren(h("option", { value: "" }, programs.length ? "未填写" : "暂无该校课程"));
    for (const p of programs) {
      courseSel.append(h("option", {
        value: p.program_id,
        selected: keepCourse !== undefined && keepCourse === p.program_id,
      }, p.label));
    }
  };
  schoolSel.addEventListener("change", () => {
    fillPrograms(schoolSel.value || undefined);
  });
  return { schoolSel, courseSel, fillSchools, fillPrograms };
}

function val(profile, code) {
  const f = profile && profile.fields[code];
  return f && f.status === "confirmed" ? f.value : undefined;
}

function safeHttpUrl(raw) {
  try {
    const url = new URL(raw);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : "";
  } catch (_) { return ""; }
}

function safeLink(url) {
  const href = safeHttpUrl(url);
  return href ? h("a", { class: "text-link", href, target: "_blank", rel: "noopener noreferrer" }, href) : "—";
}

/** Turn http(s) URLs inside plain text into clickable anchors (keeps surrounding copy). */
function linkify(text) {
  const s = String(text ?? "");
  if (!s) return [""];
  // Stop before whitespace, quotes, and CJK / full-width punctuation so
  // 「https://a.com；https://b.com」and 「（https://a.com）。」split correctly.
  const re = /https?:\/\/[^\s<>"'`\u3000-\u303F\u4E00-\u9FFF\uFF00-\uFFEF）】》」』]+/gi;
  const out = [];
  let last = 0;
  let match;
  while ((match = re.exec(s))) {
    let raw = match[0];
    let trail = "";
    const trailMatch = raw.match(/[),.;:!?]+$/);
    if (trailMatch) {
      trail = trailMatch[0];
      raw = raw.slice(0, -trail.length);
    }
    if (!/^https?:\/\//i.test(raw)) continue;
    if (match.index > last) out.push(s.slice(last, match.index));
    out.push(h("a", {
      class: "text-link", href: raw, target: "_blank", rel: "noopener noreferrer",
    }, raw));
    if (trail) out.push(trail);
    last = match.index + match[0].length;
  }
  if (last < s.length) out.push(s.slice(last));
  return out.length ? out : [s];
}

function toast(msg, bad) {
  const t = h("div", { class: "toast" + (bad ? " bad" : ""), role: bad ? "alert" : "status", "aria-live": bad ? "assertive" : "polite" },
    customerText(msg, "操作暂未完成，请稍后再试。"));
  document.body.append(t);
  setTimeout(() => t.remove(), 3600);
}

function isInternalOutput(text) {
  const value = String(text || "");
  return /\b(?:prompt|completion)\s*\d+|\b(?:evidence_ids?|source_ids?|field_code|trace_id|prompt_version)\b|\b(?:llm|gateway|model|tokens?|timeout|fallback|deterministic|routing)\b|\banswered\s+(?:result|results)\b|\bmust\s+(?:include|have|contain)\s+evidence\b|\bmissing\s+evidence\b|结果必须带证据|缺少可审计证据|(?:^|\W)(?:fx_mode|selected_lifestyle|spending_scenarios|application_region|applicant_age_band|annual_income_band_hkd)(?:$|\W)|只在\s*(?:evidence|证据)|内部(?:字段|指令|提示)|不要向用户|不可展示|模型.{0,10}(?:服务|未完成|调用|响应|回退|降级|超时)|确定性规则|模板结果|服务未完成本轮处理|耗时\s*\d|\bs\d+(?:\s*[,，、;；]\s*s\d+)*\b|(?:system|developer|assistant)\s*(?:prompt|message|instruction)|<\|[^|]+\|>|(?:ignore|忽略).{0,24}(?:previous|此前|指令)|assistant\s+to=|traceback|stack\s*trace|sqlstate|psycopg\w*|keyerror|typeerror|valueerror|attributeerror|connectionrefusederror|httperror|http_\d{3}|\b(?:select|insert|update|delete)\s+.{0,30}\bfrom\b/i.test(value)
    || /^\s*[\[{][\s\S]{20,}[\]}]\s*$/.test(value);
}

function customerText(text, fallback = "") {
  const value = String(text || "").trim();
  return value && !isInternalOutput(value) ? value : fallback;
}

function userFacingError(error, fallback = "操作暂未完成，请稍后重试。") {
  const code = error && error.code;
  const known = {
    NETWORK: "暂时无法连接服务，请稍后重试。",
    TIMEOUT: "处理时间比预期长，您可以稍后重试。",
    VERSION_CONFLICT: "档案刚刚有更新，请刷新后再试。",
    STALE_FOLLOW_UP: "这道追问已更新，请回答页面上最新的问题。",
    VALIDATION_ERROR: "填写内容暂时无法保存，请检查后重试。",
    INVALID_CREDENTIALS: "用户名或密码不正确，请检查后重试。",
    ACCOUNT_LOCKED: "登录尝试次数较多，请稍后再试。",
    LOGIN_UNAVAILABLE: "登录服务暂时不可用，请稍后重试。",
  };
  return Object.prototype.hasOwnProperty.call(known, code) ? known[code] : fallback;
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
  const headers = { "Content-Type": "application/json" };
  if (S.csrf && method !== "GET") headers["X-CSRF-Token"] = S.csrf;
  try {
    res = await fetch("/v1" + path, {
      method, signal: ctl.signal, credentials: "same-origin",
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw e.name === "AbortError" ? new ApiError("TIMEOUT", "请求超时，可以重试（同一条消息不会重复生成）")
      : new ApiError("NETWORK", "无法连接服务，请确认 agent2 serve 正在运行");
  } finally {
    clearTimeout(timer);
  }
  const data = await res.json().catch(() => ({}));
  if (data.csrf_token) S.csrf = data.csrf_token;
  if (!res.ok) {
    if (res.status === 401 && path !== "/login" && path !== "/demo/login" && path !== "/logout") {
      logout("登录已失效，请重新登录");
    }
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
      h("div", { class: "rm", title: S.user.display_name }, h("div", { class: "av" }, S.user.display_name.slice(0, 1))),
      h("button", { class: "linkbtn", onclick: () => logout() }, "退出"));
  }
  return h("div", { class: "top" },
    h("div", { class: "logo", onclick: () => go(S.user ? "#/chat" : "#/login") },
      h("div", { class: "mk" }, "G"), h("b", null, "Global Pathway"),
      h("small", null, "留学顾问")),
    h("div", { class: "r" }, right));
}

function shell(content, cur) {
  $app.replaceChildren(topBar(cur), h("div", { class: "gold" }), content);
}

async function logout(message) {
  try { await api("POST", "/logout", {}); } catch (_) { /* session may already be gone */ }
  S.token = null; S.csrf = null; S.user = null; S.pid = null; S.profiles = []; S.fb = {}; S.pending = null;
  S.followSeed = null; S.followTrail = []; S.suppressFields = [];
  localStorage.removeItem("gp_pid");
  if (message) toast(message, true);
  go("#/login");
}

async function route() {
  closeOverlay();
  const [page, arg] = location.hash.replace(/^#\/?/, "").split("/");
  try {
    if (!S.fields) S.fields = (await api("GET", "/fields")).fields;
    if (page === "login") return renderLogin();
    if (!S.user) {
      try {
        S.user = await api("GET", "/me");
      } catch (e) {
        return renderLogin();
      }
    }
    if (page === "onboarding") return await renderOnboarding();
    if (page === "report") return await renderReport(arg);
    if (page === "feedback") return await renderFeedback();
    return await renderChat(page === "chat" ? arg : null);
  } catch (e) {
    if (!S.user) return renderLogin();
    shell(h("div", { class: "page" }, h("div", { class: "empty-state" },
      h("p", null, "页面暂时无法打开，请稍后重试。"),
      h("button", { class: "btn", onclick: route }, "重试"))), page);
  }
}

// ---------- /login ----------

function renderLogin() {
  const username = h("input", { name: "u", placeholder: "用户名", autocomplete: "username", maxlength: "64" });
  const password = h("input", { name: "p", type: "password", placeholder: "密码（8–16 位）",
    autocomplete: "current-password", minlength: "8", maxlength: "16" });
  const togglePassword = h("button", { class: "login-eye", type: "button", "aria-label": "显示密码",
    title: "显示密码", onclick: (event) => {
      const visible = password.type === "password";
      password.type = visible ? "text" : "password";
      event.currentTarget.setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
      event.currentTarget.title = visible ? "隐藏密码" : "显示密码";
    } }, h("span", { class: "login-eye-icon", "aria-hidden": "true" }));
  const form = h("form", { class: "login-form", onsubmit: (e) => {
      e.preventDefault();
      passwordLogin(username.value.trim(), password.value);
    } },
    h("label", { class: "login-field" },
      h("span", { class: "login-field-en" }, "USERNAME"),
      h("span", { class: "login-field-zh" }, "用户名"),
      h("span", { class: "login-input-wrap" }, h("span", { class: "login-user-icon", "aria-hidden": "true" }), username)),
    h("label", { class: "login-field" },
      h("span", { class: "login-field-en" }, "PASSWORD"),
      h("span", { class: "login-field-zh" }, "密码"),
      h("span", { class: "login-input-wrap" }, h("span", { class: "login-lock-icon", "aria-hidden": "true" }), password, togglePassword)),
    h("button", { class: "btn login-submit", type: "submit" }, "登录"));
  $app.replaceChildren(h("main", { class: "login-page" },
    h("section", { class: "login-photo", "aria-label": "校园摄影作品" },
      h("p", { class: "login-caption" }, "为下一段旅程，做好准备")),
    h("section", { class: "login-panel" },
      h("div", { class: "login-content" },
        h("h1", { class: "login-title sf" }, "欢迎回来"),
        h("p", { class: "login-lead" }, "继续规划您的留学与跨境生活"), form))));
}

async function afterAuth(res) {
  if (res.csrf_token) S.csrf = res.csrf_token;
  S.token = true;
  S.user = await api("GET", "/me");
  const { profiles } = await api("GET", "/profiles");
  S.pid = null;
  localStorage.removeItem("gp_pid");
  go(profiles.length ? "#/chat" : "#/onboarding");
}

async function passwordLogin(username, password) {
  if (!username || !password) return toast("请输入用户名和密码", true);
  if (password.length < 8 || password.length > 16) return toast("密码长度须为 8–16 位", true);
  try {
    const res = await api("POST", "/login", { username, password });
    await afterAuth(res);
  } catch (e) {
    toast(userFacingError(e, "登录暂时未成功，请检查信息后重试。"), true);
  }
}

// ---------- field inputs ----------

function fieldInput(code, value) {
  const f = S.fields[code] || { kind: "text", options: [] };
  if (code === "school" || code === "course") {
    // Populated by schoolCourseInputs / form wiring — placeholder until then.
    return h("select", { name: code }, h("option", { value: "" }, "加载中…"));
  }
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
  // School / program selects store raw ids (not JSON-encoded enum values).
  if (code === "school" || code === "course") return raw;
  if (f.options.length) return JSON.parse(raw);
  if (f.kind === "age") return parseInt(raw, 10);
  if (f.kind === "amount") return Number(raw);
  return raw;
}

// ---------- /onboarding ----------

function buildProfileCreateForm({ modal = false, onCreated, onCancel, submitLabel = "创建档案" } = {}) {
  const codes = ["role", "beneficiary", "destination_country", "school", "course", "offer_status", "planned_start_date"];
  const required = ["role", "beneficiary", "destination_country"];
  const err = h("div", { class: "formerr" });
  const { schoolSel, courseSel, fillSchools, fillPrograms } = schoolCourseInputs();
  const destInput = fieldInput("destination_country");
  destInput.addEventListener("change", () => {
    let dest;
    try { dest = destInput.value ? JSON.parse(destInput.value) : undefined; } catch (_) { dest = undefined; }
    fillSchools(dest);
  });
  let submitButton;
  const form = h("form", { class: modal ? "profile-create-form" : "card profile-create-form", onsubmit: async (e) => {
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
    submitButton.disabled = true;
    try {
      const p = await api("POST", "/profiles", { fields, synthetic: true });
      await onCreated(p);
    } catch (x) {
      err.textContent = userFacingError(x, "档案暂时无法创建，请检查填写内容后重试。");
      submitButton.disabled = false;
    }
  } },
  h("div", { class: "form" },
    h("label", null, "角色 *", fieldInput("role")),
    h("label", null, "服务对象 *", fieldInput("beneficiary")),
    h("label", null, "目的地 *", destInput),
    h("label", null, "学校", schoolSel),
    h("label", null, "课程 / 学位", courseSel),
    h("label", null, "Offer 状态", fieldInput("offer_status")),
    h("label", null, "入学日期", fieldInput("planned_start_date"))),
  err);
  submitButton = h("button", { class: "btn", type: "submit" }, submitLabel);
  form.append(h("div", { class: "btns" },
    onCancel ? h("button", { class: "btn ghost", type: "button", onclick: onCancel }, "取消") : null,
    submitButton));
  form.querySelector('[name="beneficiary"]')?.focus();
  // Populate dependent school/course selectors without blocking the form.
  fillSchools(undefined);
  return form;
}

async function renderOnboarding() {
  const { profiles } = await api("GET", "/profiles");
  const form = buildProfileCreateForm({
    onCreated: (profile) => go("#/chat/" + profile.profile_id),
    onCancel: profiles.length ? () => go("#/chat") : null,
    submitLabel: "创建并进入方案",
  });
  shell(h("div", { class: "page" },
    h("h1", { class: "sf" }, "新建留学档案"),
    form), "onboarding");
}

function openNewProfileDialog() {
  if (S.busy) return toast("当前回复还在处理中，请稍后再新建档案。", true);
  const form = buildProfileCreateForm({
    modal: true,
    submitLabel: "创建档案",
    onCancel: closeOverlay,
    onCreated: (profile) => {
      closeOverlay();
      go("#/chat/" + profile.profile_id);
    },
  });
  overlay(h("section", { class: "modal profile-create-modal", role: "dialog", "aria-modal": "true",
    "aria-labelledby": "new-profile-title" },
    h("button", { class: "close", type: "button", "aria-label": "关闭", onclick: closeOverlay }, "×"),
    h("div", { class: "lab" }, "新增一份留学计划"),
    h("h3", { id: "new-profile-title", class: "sf" }, "新建档案"),
    h("p", { class: "note" }, "填写这份计划的基本信息，创建后会在当前页面打开新的档案。"),
    form));
  form.querySelector('[name="beneficiary"]')?.focus();
}

// ---------- /chat ----------

async function loadMessages() {
  S.messages = (await api("GET", `/sessions/${S.conv.session_id || S.conv.conversation_id}/messages?limit=200`)).messages;
}
async function loadAdvice() {
  const result = await api("GET", `/profiles/${S.pid}/advice`);
  S.advice = result.advice;
  S.adviceScopes = result.scoped_advice || {};
}
async function loadPlan() { S.plan = (await api("GET", `/profiles/${S.pid}/plan`)).items; }
async function loadProposals() { S.proposals = (await api("GET", `/profiles/${S.pid}/proposals`)).items; }
async function loadRelationships() {
  S.relationships = (await api("GET", `/profiles/${S.pid}/bank-relationships`)).relationships || [];
}
async function loadRelationshipProposals() {
  S.relationshipProposals = (await api("GET", `/profiles/${S.pid}/bank-relationship-proposals`)).proposals || [];
}

async function refreshProfile() {
  S.profile = await api("GET", `/profiles/${S.pid}`);
  syncProfile();
}

function viewProfile(profile) {
  if (!profile) return;
  if (profile.profile_id !== S.pid && S.busy) {
    toast("当前回复还在处理中，完成后即可查看其他档案。", true);
    return;
  }
  // Same profile: eye icon toggles the plan sheet open / closed.
  if (profile.profile_id === S.pid) {
    if (!S.collapsed) {
      closeCurrentProfile();
      return;
    }
    S.collapsed = false;
    if (S.el.sheet) S.el.sheet.classList.remove("collapsed");
    drawSide();
    return;
  }
  S.collapsed = false;
  go("#/chat/" + profile.profile_id);
}

function selectProfile(profile) {
  if (!profile) return;
  if (profile.profile_id === S.pid) {
    if (!S.collapsed) closeCurrentProfile();
    return;
  }
  if (S.busy) {
    toast("当前操作还在处理中，完成后即可切换档案。", true);
    return;
  }
  // Selecting a profile opens its conversation; the eye icon opens its plan.
  S.collapsed = true;
  go("#/chat/" + profile.profile_id);
}

function closeCurrentProfile() {
  if (!S.el.sheet) return;
  S.collapsed = true;
  S.el.sheet.classList.add("collapsed");
  // Refresh sidebar so the eye icon title/aria switches back to「查看」.
  if (S.el.side) drawSide();
}

async function loadChatSecondary(profileId) {
  const results = await Promise.allSettled([
    loadPlan(), loadProposals(), loadRelationships(), loadRelationshipProposals(),
  ]);
  // Ignore a late secondary load after the customer deliberately changed
  // profiles.  This is a display guard; service ownership checks still guard
  // the underlying data.
  if (S.pid !== profileId) return;
  const failed = results.find((item) => item.status === "rejected");
  if (failed) toast(userFacingError(failed.reason, "部分方案资料暂未加载，请稍后重试。"), true);
  drawSide();
  drawSheet();
  drawChat({ keep: true });
}

function syncProfile() {
  S.profiles = S.profiles.map((p) => (p.profile_id === S.profile.profile_id ? S.profile : p));
}

function lastAnswer() {
  for (let i = S.messages.length - 1; i >= 0; i--) if (S.messages[i].role === "assistant") return S.messages[i].body;
  return null;
}

function isProfileField(code) {
  const definition = S.fields[code];
  return definition && ["core_profile", "preference"].includes(definition.storage_class || "core_profile");
}

function deriveMissing() {
  const last = lastAnswer();
  const codes = new Set();
  if (last) {
    (last.missing_fields || []).forEach((c) => codes.add(c));
    (last.follow_up_questions || []).forEach((q) => codes.add(q.field));
    (last.answer.materials || []).forEach((m) => (m.pending_fields || []).forEach((c) => codes.add(c)));
  }
  S.missing = new Set([...codes].filter((c) => isProfileField(c) && val(S.profile, c) === undefined));
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
  // Conversation is the primary surface.  Render it as soon as its critical
  // data is ready instead of holding the whole page for plans and account
  // records that can safely fill in a moment later.
  S.plan = [];
  S.proposals = [];
  S.relationships = [];
  S.relationshipProposals = [];
  await Promise.all([loadMessages(), loadAdvice()]);
  deriveMissing();
  drawChatPage();
  void loadChatSecondary(pid);
  // Resolve school / program labels for the sheet and every sidebar row.
  try {
    await ensureCatalogForProfiles(S.profiles);
  } catch (e) {
    S.catalogReady = false;
    toast(userFacingError(e, "学校或课程目录暂不可用，档案信息仍可继续查看。"), true);
  }
  if (S.pid === pid) {
    drawSide();
    drawSheet();
  }
}

function drawChatPage() {
  const side = h("div", { class: "side" });
  const sheet = h("div", { class: "sheet" + (S.collapsed ? " collapsed" : "") });
  if (S.sheetH) sheet.style.height = S.sheetH;
  const chat = h("div", { class: "chat" });
  const rail = h("aside", { class: "next-rail" });
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
  S.el = { side, sheet, chat, rail, input, sendBtn };
  shell(h("div", { class: "wrap" }, side, h("div", { class: "main" }, sheet, bar, chat,
    h("div", { class: "in" }, input, sendBtn)), rail), "chat");
  drawSide();
  drawSheet();
  drawNextRail();
  drawChat();
}

function drawNextRail() {
  const rail = S.el.rail;
  if (!rail) return;
  const pending = (S.plan || []).filter((item) => !item.done).slice(0, 3);
  const stale = staleAdviceTopics();
  const relationship = (S.relationships || []).find((item) => ["selected", "applying", "blocked"].includes(item.status));
  const rows = [];
  if (relationship) rows.push(h("div", { class: "next-item" }, h("i", { class: "next-dot active" }),
    h("div", null, h("b", null, "银行账户进度"), h("span", null,
      `${relationship.product_name} · ${{ selected: "准备申请", applying: "申请中", blocked: "待处理" }[relationship.status] || "已记录"}`))));
  pending.forEach((item) => rows.push(h("div", { class: "next-item next-todo" }, h("i", { class: "next-dot" }),
    h("div", null, h("b", null, item.text), h("span", null, "待完成")))));
  stale.forEach((topic) => rows.push(h("div", { class: "next-item" }, h("i", { class: "next-dot warm" }),
    h("div", null, h("b", null, `${adviceTopicLabel(topic)}待更新`), h("span", null, "可在资料工作区中重新生成")))));
  const children = [
    h("div", { class: "rail-label" }, "接下来"),
    h("p", { class: "rail-intro" }, "不需要一次完成；我会在合适的时候提醒您。"),
    ...(rows.length ? rows : [h("div", { class: "rail-empty" }, "暂时没有待处理事项。")]),
  ];
  rail.replaceChildren(...children);
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
  const school = valueLabel("school", val(p, "school"));
  const course = valueLabel("course", val(p, "course"));
  return {
    school: school !== "—" ? school : valueLabel("destination_country", val(p, "destination_country")),
    course: course !== "—" ? course : "",
    start: m ? `${m[1]}年${+m[2]}月入学` : "入学时间待定",
  };
}

async function deleteProfile(p) {
  if (S.busy) return toast("请等当前回答完成后再删除档案", true);
  const name = val(p, "beneficiary") || "未命名档案";
  if (!window.confirm(`确定删除「${name}」吗？该档案的通话记录、建议和待办也会一起删除，且无法恢复。`)) return;
  try {
    await api("DELETE", `/profiles/${p.profile_id}`, { expected_version: p.version });
    S.profiles = S.profiles.filter((item) => item.profile_id !== p.profile_id);
    if (S.pid === p.profile_id) {
      S.pid = null; S.profile = null; S.conv = null;
      localStorage.removeItem("gp_pid");
      go(S.profiles.length ? `#/chat/${S.profiles[0].profile_id}` : "#/onboarding");
    } else {
      drawSide();
    }
    toast("档案及通话记录已删除");
  } catch (e) {
    toast(userFacingError(e, "档案删除暂未完成，请稍后重试。"), true);
  }
}

function drawSide() {
  S.el.side.replaceChildren(
    h("div", { class: "lab" }, "我的档案"),
    ...S.profiles.map((p) => {
      const name = val(p, "beneficiary") || "未命名档案";
      const summary = profileSub(p);
      return h("div", { class: "pf" + (p.profile_id === S.pid ? " on2" : "") + (S.busy ? " busy" : ""),
        tabindex: "0", role: "group", title: `切换到${name}的会话`,
        onclick: () => selectProfile(p),
        onkeydown: (event) => {
          if (event.target !== event.currentTarget || !["Enter", " "].includes(event.key)) return;
          event.preventDefault();
          selectProfile(p);
        } },
        h("div", { class: "av2 sf" }, name.slice(-1)),
        h("div", { class: "pf-body" },
          h("b", { class: "sf", title: name }, name),
          h("div", { class: "pf-sub" },
            summary.school && summary.school !== "—" ? h("span", { class: "pf-school", title: summary.school }, summary.school) : null,
            summary.course ? h("span", { class: "pf-course", title: summary.course }, summary.course) : null,
            h("span", { class: "pf-date" }, summary.start))),
        h("div", { class: "pf-actions" },
          h("button", { class: "pf-view", type: "button",
            "aria-expanded": String(p.profile_id === S.pid && !S.collapsed),
            title: p.profile_id === S.pid && !S.collapsed ? `收起${name}档案` : `查看${name}档案`,
            "aria-label": p.profile_id === S.pid && !S.collapsed ? `收起${name}档案` : `查看${name}档案`,
            onclick: (event) => { event.stopPropagation(); viewProfile(p); } }, profileActionIcon("view")),
          h("button", { class: "pf-delete", type: "button", title: `删除${name}`,
            "aria-label": `删除${name}`, onclick: (event) => { event.stopPropagation(); deleteProfile(p); } }, profileActionIcon("delete"))));
    }),
    h("button", { class: "new", onclick: openNewProfileDialog }, "＋ 新建档案"));
}

function drawSheet() {
  const p = S.profile;
  const name = val(p, "beneficiary") || "未命名档案";
  const filled = GRID.filter((c) => val(p, c) !== undefined).length;
  const done = S.plan.filter((i) => i.done).length;
  S.el.sheet.replaceChildren(
    h("div", { class: "sh" },
      h("h2", { class: "sf" }, `${name} · 留学准备方案`),
      h("span", { class: "meta" },
        h("span", null, "基本信息 ", h("em", { class: "sf" }, filled), ` / ${GRID.length}`),
        h("span", null, "待办已完成 ", h("em", { class: "sf" }, done), ` / ${S.plan.length}`),
        h("button", { class: "btn", onclick: () => openReportDialog(S.pid) }, "导出报告"),
        h("button", { class: "btn ghost profile-sheet-close", type: "button", onclick: closeCurrentProfile }, "收起档案"))),
    h("div", { class: "mt" }, "一 · 留学基本信息", h("button", { class: "ed", onclick: () => openEdit() }, "✎ 编辑信息")),
    h("div", { class: "grid" }, GRID.map(cell)),
    h("div", { class: "mt" }, "二 · 银行账户"),
    bankRelationshipsBox(),
    h("div", { class: "mt" }, "三 · 为您定制的方案"),
    h("div", { class: "plan" }, TOPICS.map(planCard)),
    h("div", { class: "mt" }, "四 · 我的待办"),
    todoBox());
  if (S.flash.length) {
    const flashed = S.flash;
    S.flash = [];
    setTimeout(() => flashed.forEach((k) => document.getElementById("card-" + k)?.classList.remove("flash")), 2400);
  }
  drawNextRail();
}

function bankRelationshipsBox() {
  const market = { "CN-HK": "香港", UK: "英国", US: "美国", CN: "中国内地" };
  const status = { selected: "已选择", applying: "申请中", open: "已开户",
    blocked: "待处理", closed: "已关闭" };
  if (!S.relationships.length) {
    return h("div", { class: "bank-rels empty" },
      "还没有确认保存的银行账户或申请计划。下方开户建议会保留当前咨询；选定产品后可再保存进度。");
  }
  return h("div", { class: "bank-rels" }, S.relationships.map((item) =>
    h("div", { class: "bank-rel" },
      h("span", { class: "market-tag" }, market[item.market] || "其他地区"),
      h("b", null, item.product_name),
      h("span", { class: "rel-status " + item.status }, status[item.status] || "进度已更新"))));
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

/** Shared plan-card primitives — same visual language as credit-card advice. */
function adviceFact(label, value, { strong = true } = {}) {
  if (value == null || value === false) return null;
  const isNode = typeof value === "object";
  return h("div", { class: "cc-fact" },
    h("span", null, label),
    isNode ? h("span", null, value)
      : strong ? h("strong", null, value) : h("span", null, value));
}

function adviceOption({ label, title, body, lines, facts, featured = true } = {}) {
  const textLines = (lines || []).filter((s) => s && !isInternalOutput(s));
  return h("div", { class: "cc-option" + (featured ? " featured" : "") },
    label ? h("div", { class: "cc-label" }, label) : null,
    title ? h("div", { class: "cc-name" }, title) : null,
    body ? h("p", { class: "cc-reason" },
      ...(typeof body === "string" ? linkify(body) : [body])) : null,
    ...textLines.map((line) => h("p", { class: "cc-reason" }, ...linkify(line))),
    ...(facts || []).filter(Boolean));
}

function adviceNote(title, text) {
  if (!text) return null;
  return h("div", { class: "cc-apply" },
    h("b", null, title),
    typeof text === "string" ? h("span", null, text) : text);
}

function advicePanel(t, { id, status, statusClass = "", flash = "", wide = true, kids }) {
  // Plan cards always stack full-width so empty modules do not leave a half-blank cell.
  return h("div", {
    class: "p topic-advice topic-wide" + flash,
    id,
  }, cardHead(t, h("span", { class: "st " + statusClass }, status)), ...kids.filter(Boolean));
}

function remittancePaymentSteps(pl) {
  const saved = visibleOutputLines(pl.payment_steps || []);
  if (saved.length) return saved;
  const companions = pl.domain_payload?.companion_bubbles || [];
  const payment = companions.find((c) => c && c.kind === "school_payment");
  return visibleOutputLines(payment?.steps || []);
}

function remittanceFxSteps(pl, paymentSteps) {
  const saved = visibleOutputLines(pl.fx_steps || []);
  if (saved.length) return saved.filter((s) => !isInternalOutput(s));
  const pay = new Set(paymentSteps);
  return visibleOutputLines(pl.steps || []).filter((s) => !pay.has(s) && !isTuitionCardSteps([s]));
}

function remittancePaymentTitle(steps, fallback) {
  const first = String(steps[0] || "");
  const hit = first.match(/^(.+?)\s*学费参考/);
  if (hit) return hit[1].trim();
  const school = val(S.profile, "school");
  return school ? `${valueLabel("school", school) || school}` : fallback;
}

function remittanceCard(t) {
  const a = S.advice.remittance;
  const flash = S.flash.includes("remittance") ? " flash" : "";
  if (!a) {
    return advicePanel(t, {
      id: "card-remittance", status: "未开始", flash,
      kids: [adviceFact("用途", empty()), adviceFact("材料", empty()), adviceFact("来源", empty())],
    });
  }
  const pl = a.payload || {};
  const purpose = pl.remittance_purpose || pl.fx_mode || pl.domain_payload?.remittance_purpose
    || pl.domain_payload?.fx_mode;
  const purposeLabel = { tuition: "学费", living: "生活费", overseas: "境外消费/换汇" }[purpose]
    || (purpose ? customerText(valueLabel("remittance_purpose", purpose), "其他外汇用途") : "—");
  const mats = pl.materials || pl.domain_payload?.materials || [];
  const cites = pl.citations || pl.domain_payload?.citations || [];
  const req = mats.filter((m) => m.applicability === "required" && !isInternalOutput(m.name_zh));
  const materialNames = req.map((m) => m.name_zh).filter(Boolean);
  const deadline = val(S.profile, "payment_deadline");
  const paymentSteps = remittancePaymentSteps(pl).filter((s) => !isInternalOutput(s));
  const fxSteps = remittanceFxSteps(pl, paymentSteps).filter((s) => !isInternalOutput(s));
  return advicePanel(t, {
    id: "card-remittance", status: "已确认", statusClass: "ok", flash, wide: true,
    kids: [
      paymentSteps.length ? adviceOption({
        label: "怎么交学费",
        title: remittancePaymentTitle(paymentSteps, "学校官方缴费"),
        lines: paymentSteps.slice(0, 8),
        featured: true,
      }) : null,
      fxSteps.length ? adviceOption({
        label: "购汇与汇款",
        title: purposeLabel !== "—" ? `汇款用途 · ${purposeLabel}` : "购汇要点",
        lines: fxSteps.slice(0, 6),
        featured: false,
        facts: [
          deadline ? adviceFact("缴费截止",
            valueLabel("payment_deadline", deadline) || deadline) : null,
        ],
      }) : adviceFact("用途", purposeLabel),
      adviceFact("必备材料",
        materialNames.length ? materialNames.join("、") : empty(),
        { strong: false }),
      adviceFact("来源", sourceRefs(cites), { strong: false }),
    ],
  });
}

function accountDocumentLabel(doc) {
  return ({ identity: "身份证明", address: "地址证明", student_status: "学生或录取证明",
    tax: "税务证明", income: "收入证明" })[doc.document_type]
    || customerText(doc.name_zh || doc.description || doc.document_type, "");
}

function accountRuleLabel(result) {
  const rule = result.rule || {};
  if (rule.field_code === "applicant_age" && rule.operator === "gte" && rule.value_numeric !== null) {
    return `年龄至少 ${Number(rule.value_numeric)} 岁`;
  }
  if (rule.field_code === "bank_confirmation") return "需通过银行的身份与地址核验";
  return customerText(rule.description || rule.condition_note, "");
}

function accountAdviceDisplay(advice) {
  const pl = advice?.payload || {};
  const dp = pl.domain_payload || {};
  const state = dp.conversation_state || {};
  const candidates = Array.isArray(dp.candidate_results) ? dp.candidate_results : [];
  const focusId = state.selected_product_id || state.focused_product_id;
  const assessed = candidates.find((item) => item.product?.account_id === focusId) || candidates[0] || {};
  const legacyProducts = dp.products || pl.products || [];
  const product = assessed.product || legacyProducts[0] || {};
  const documents = (pl.materials || dp.materials || assessed.documents || [])
    .filter((item) => item.applicability === "required" || item.requirement_level === "required"
      || (!item.applicability && !item.requirement_level));
  const documentNames = [...new Set(documents.map(accountDocumentLabel).filter(Boolean))];
  const ruleLabels = [...new Set((assessed.rule_results || [])
    .map(accountRuleLabel).filter(Boolean))];
  const summaryProduct = (pl.summary || "").match(/「(.+?)」/)?.[1];
  const unresolved = state.unresolved_fields || [];
  return {
    pl, dp, state, assessed,
    region: dp.account_region || pl.account_region,
    identity: dp.applicant_identity || pl.applicant_identity,
    productName: product.name_zh || product.product_name || summaryProduct,
    documentNames, ruleLabels,
    needsInput: assessed.status === "needs_input" || unresolved.length > 0,
    needsVerification: pl.result_status === "needs_verification"
      || assessed.status === "needs_verification",
  };
}

function accountCard(t) {
  const a = S.advice.account;
  const flash = S.flash.includes("account") ? " flash" : "";
  if (!a) {
    return advicePanel(t, {
      id: "card-account", status: "未开始", flash,
      kids: [
        adviceFact("地区", empty()), adviceFact("产品", empty()),
        adviceFact("材料", empty()), adviceFact("门槛", empty()),
      ],
    });
  }
  const display = accountAdviceDisplay(a);
  const { pl, dp } = display;
  const region = display.region
    || val(S.profile, "account_region") || val(S.profile, "destination_country");
  const identity = display.identity || val(S.profile, "applicant_identity")
    || val(S.profile, "role");
  const productName = customerText(display.productName, "");
  const eligibility = display.ruleLabels.slice(0, 2).join("；")
    || visibleOutputLines(pl.steps || []).find((s) => String(s).startsWith("公开资格要点："))?.replace("公开资格要点：", "")
    || "";
  const warn = display.needsVerification;
  const pending = display.needsInput && !warn;
  const scopes = Object.keys(S.adviceScopes.account || {});
  const scopeNames = scopes.map((key) => ({ "CN-HK": "香港", UK: "英国", US: "美国", CN: "中国内地" }[key] || key));
  const summary = customerText(pl.summary, "");
  const status = warn ? "待核实" : pending ? "待补充" : "已确认";
  return advicePanel(t, {
    id: "card-account",
    status,
    statusClass: warn || pending ? "warn" : "ok",
    flash,
    wide: !!productName,
    kids: [
      adviceOption({
        label: productName ? "开户方案" : "当前进度",
        title: productName || "待选定开户产品",
        body: summary && summary.length < 180 ? summary : null,
        featured: !!productName,
        facts: [
          adviceFact("地区", valueLabel("destination_country", region) || region || empty()),
          adviceFact("申请人",
            valueLabel("applicant_identity", identity) || identity || empty()),
          adviceFact("必备材料",
            display.documentNames.length ? display.documentNames.join("、") : empty(),
            { strong: false }),
          eligibility ? adviceFact("门槛", eligibility, { strong: false }) : adviceFact("门槛", empty()),
          scopes.length > 1 ? adviceFact("已咨询地区", scopeNames.join("、"), { strong: false }) : null,
        ],
      }),
      adviceFact("来源", sourceRefs(pl.citations || dp.citations || []), { strong: false }),
    ],
  });
}

function planCard(t) {
  let card;
  if (t.key === "visa") card = visaCard(t);
  else if (t.key === "credit_card") card = creditCardCard(t);
  else if (t.key === "remittance") card = remittanceCard(t);
  else if (t.key === "account") card = accountCard(t);
  else card = h("div", { class: "p", id: "card-" + t.key },
    cardHead(t, h("span", { class: "st" }, "待补充")),
    t.rows.map((r) => row(r, empty())));
  return withAdviceLifecycle(card, t.key);
}

const ADVICE_TOPIC_LABEL = { credit_card: "信用卡建议", account: "开户建议",
  remittance: "汇款建议", visa: "签证建议" };
const adviceTopicLabel = (topic) => ADVICE_TOPIC_LABEL[topic] || "其他建议";

function withAdviceLifecycle(card, topic) {
  const advice = S.advice[topic];
  if (!advice || advice.status !== "stale") return card;
  const refreshing = S.refreshingTopics.has(topic);
  card.classList.add("advice-stale");
  card.append(h("div", { class: "advice-refresh" },
    h("b", null, refreshing ? "正在为您更新这份建议…" : "您的资料已有更新，这份建议可以再为您核对一次。"),
    h("button", { class: "btn sm", disabled: refreshing, onclick: () => refreshAdvice(topic) },
      refreshing ? "更新中…" : "更新建议")));
  return card;
}

async function refreshAdvice(topic) {
  return refreshAdviceTopics([topic]);
}

function staleAdviceTopics() {
  return Object.entries(S.advice || {})
    .filter(([, advice]) => advice && advice.status === "stale")
    .map(([topic]) => topic);
}

async function refreshAdviceTopics(topics) {
  const selected = [...new Set(topics || [])].filter((topic) => S.advice[topic]);
  if (!selected.length) return;
  if (S.busy) { toast("上一项操作还在处理中，请稍候"); return; }
  S.busy = true;
  selected.forEach((topic) => S.refreshingTopics.add(topic));
  drawSheet();
  drawSide();
  toast(`正在依次重新生成${selected.map(adviceTopicLabel).join("、")}，完成后会自动显示。`);
  try {
    const results = [];
    // Keep regeneration ordered: the current service performs each job within
    // the request, and parallel prompts would make the UI less predictable.
    for (const topic of selected) {
      results.push(await api("POST", `/profiles/${S.pid}/advice/${topic}/refresh`, {}));
    }
    await Promise.all([loadAdvice(), loadMessages()]);
    S.flash = results.filter((result) => result.refreshed).map((result) => result.topic);
    drawSheet();
    // A refresh creates a new assistant reply.  Updating only the side-panel
    // left that reply invisible until a browser reload rebuilt the chat.
    drawChat({ keep: true });
    const completed = results.filter((result) => result.refreshed).map((result) => adviceTopicLabel(result.topic));
    toast(completed.length ? `${completed.join("、")}已重新生成`
      : "建议暂未更新，请稍后重试。", !completed.length);
  } catch (e) {
    toast(userFacingError(e, "建议暂未更新，请稍后重试。"), true);
  } finally {
    selected.forEach((topic) => S.refreshingTopics.delete(topic));
    S.busy = false;
    drawSheet();
    drawSide();
  }
}

function openAdviceImpactPanel(topics) {
  const affected = [...new Set(topics || [])].filter((topic) => S.advice[topic]);
  if (!affected.length) return toast("档案已保存");
  const selected = new Set(affected);
  const choices = h("div", { class: "proposal-advice-choices" },
    h("div", null, "这些建议会受到本次资料修改的影响。您可以现在更新，也可以稍后处理。"),
    affected.map((topic) => {
      const input = h("input", { type: "checkbox", checked: true });
      input.addEventListener("change", () => input.checked ? selected.add(topic) : selected.delete(topic));
      return h("label", null, input, adviceTopicLabel(topic));
    }));
  overlay(h("div", { class: "modal impact-modal" },
    h("h3", { class: "sf" }, "资料已保存"),
    h("p", { class: "note" }, "原有资料已更新；这不会打断您继续咨询。"), choices,
    h("div", { class: "btns" },
      h("button", { class: "btn ghost", type: "button", onclick: closeOverlay }, "稍后处理"),
      h("button", { class: "btn", type: "button", onclick: async () => {
        closeOverlay();
        await refreshAdviceTopics([...selected]);
      } }, "现在重新生成"))));
}

function cardsFromAnswerSteps(steps) {
  const cards = [];
  let marketRequirement = "";
  (steps || []).forEach((line, index) => {
    if (line.endsWith("发行的卡") && steps[index + 1]) marketRequirement = steps[index + 1];
    const colon = line.indexOf("：");
    const label = colon >= 0 ? line.slice(0, colon) : "";
    const value = colon >= 0 ? line.slice(colon + 1).trim() : "";
    const roles = { 主推荐: "primary", 初步选择: "primary", 场景搭配: "supplement",
      附加推荐: "supplement", 其他选择: "alternative", 待核对选择: "alternative" };
    const network = line.match(/^(.+?)（(?:万事达卡|威士卡|银联卡|银行卡|Visa|Mastercard|UnionPay)）$/);
    if (roles[label]) cards.push({ product_name: value, role: roles[label], display_label: label });
    else if (network) cards.push({ product_name: network[1], role: "candidate" });
    else if (cards.length && ["为什么适合你", "为什么适合您", "推荐理由", "年费", "主要权益", "申请前提", "申请限制"].includes(label)) {
      const field = { 为什么适合你: "reason_text", 为什么适合您: "reason_text", 推荐理由: "reason_text", 年费: "fee_text",
        主要权益: "benefits_text", 申请前提: "application_note", 申请限制: "application_note" }[label];
      cards[cards.length - 1][field] = value;
    }
  });
  cards.forEach((card) => { if (!card.application_note && marketRequirement) card.application_note = marketRequirement; });
  return cards;
}

function savedCardDisplay(advice) {
  const pl = advice.payload || {};
  const source = S.messages.find((message) =>
    message.role === "assistant" && message.message_id === advice.source_message_id);
  const steps = visibleOutputLines(source?.body?.answer?.steps || []);
  const recovered = cardsFromAnswerSteps(steps);
  const saved = Array.isArray(pl.cards) ? pl.cards : [];
  const fromAnswer = new Map(recovered.map((card) => [card.product_name, card]));
  const cards = (saved.length ? saved.map((card) => {
    const match = fromAnswer.get(card.product_name) || {};
    const merged = { ...match, ...card };
    for (const field of ["reason_text", "fee_text", "benefits_text", "application_note"])
      if (!merged[field] && match[field]) merged[field] = match[field];
    return merged;
  }) : recovered).map((card) => ({
    ...card,
    product_name: customerText(card.product_name, ""),
    reason_text: customerText(card.reason_text, ""),
    fee_text: customerText(card.fee_text, ""),
    benefits_text: customerText(card.benefits_text, ""),
    application_note: customerText(card.application_note, ""),
  })).filter((card) => card.product_name);
  return { cards,
    preview: !!pl.preview || steps.some((step) => step.startsWith("提前了解：")) };
}

function cardAdviceOption(card, label, featured) {
  return adviceOption({
    label: customerText(label, "信用卡方案"),
    title: card.product_name,
    body: card.reason_text || null,
    featured,
    facts: [
      adviceFact("年费及费用", customerText(card.fee_text, "请以官网最新条款为准")),
      customerText(card.benefits_text)
        ? adviceFact("主要权益", customerText(card.benefits_text), { strong: false })
        : null,
    ],
  });
}

function isBackupCard(card) {
  const market = String(card.market || "");
  return card.role === "backup"
    || /Hong Kong|^HK$|CN-HK/i.test(market);
}

function creditCardCard(t) {
  const a = S.advice.credit_card;
  const flash = S.flash.includes("credit_card") ? " flash" : "";
  if (!a) {
    return advicePanel(t, {
      id: "card-credit_card", status: "未开始", flash, wide: true,
      kids: (t.rows || ["产品", "费用 / 限制", "申请条件"]).map((r) => adviceFact(r, empty())),
    });
  }

  const pl = a.payload || {};
  const display = savedCardDisplay(a);
  // 侧栏只展示当地主推/备选，香港备用卡留在对话正文。
  const cards = display.cards.filter((card) => !isBackupCard(card));
  const hasPrimary = cards.some((card) => card.role === "primary");
  const state = !cards.length ? "当前建议" :
    pl.result_status === "needs_verification" ? "待核实" :
    hasPrimary ? "已推荐" : "供您比较";
  const stateClass = pl.result_status === "needs_verification" ? "warn" :
    hasPrimary ? "ok" : "";
  const first = cards.find((card) => card.role === "primary") || cards[0];
  const others = cards.filter((card) => card !== first);
  const source = S.messages.find((message) =>
    message.role === "assistant" && message.message_id === a.source_message_id);
  const marketPrereq = visibleOutputLines(source?.body?.answer?.steps || []).find((step) =>
    /均要求已有|到英国读书|香港卡可在留学地使用/.test(step));
  const condition = customerText(marketPrereq || first?.application_note,
    "申请资格及最终批核以当地汇丰的官方规定为准。");
  return advicePanel(t, {
    id: "card-credit_card", status: state, statusClass: stateClass, flash, wide: true,
    kids: cards.length ? [
      cardAdviceOption(first, hasPrimary ? "首选推荐" : "方案一 · 供比较", hasPrimary),
      others.length ? h("div", { class: "cc-others" },
        h("div", { class: "cc-section" }, hasPrimary ? "其他选择" : "一起比较"),
        others.slice(0, 2).map((card, index) => h("div", { class: "cc-secondary" },
          cardAdviceOption(card, card.display_label ||
            (card.role === "supplement" ? "场景搭配" : hasPrimary ? "备选方案" : `方案${index + 2} · 供比较`), false))),
        others.length > 2
          ? h("p", { class: "cc-extra" }, `另有 ${others.length - 2} 张卡可在下方回答中查看。`)
          : null) : null,
      adviceNote("申请前请确认", condition),
    ] : [h("p", { class: "cc-empty" },
      customerText(pl.summary, "目前还没有可展示的具体卡片。"))],
  });
}

/** Citations: numbered pills inline; numbered named links in the reference list. */
function buildCiteMap(citations) {
  const cmap = {};
  const unique = [];
  const byKey = new Map();
  (citations || []).forEach((c) => {
    const key = c.source_url || c.source_name || c.citation_id;
    if (!byKey.has(key)) {
      const item = { ...c, n: byKey.size + 1 };
      byKey.set(key, item);
      unique.push(item);
    }
    cmap[c.citation_id] = byKey.get(key);
  });
  return { cmap, unique };
}

function citeLabel(c) {
  const name = (c.source_name || "").trim();
  if (name) return name;
  try { return new URL(safeHttpUrl(c.source_url)).hostname.replace(/^www\./, ""); }
  catch { return "来源"; }
}

function citeMark(c) {
  if (!c) return null;
  const label = `[${c.n}]`;
  const href = safeHttpUrl(c.source_url);
  if (!href) return h("span", { class: "cite-mark" }, label);
  return h("a", { class: "cite-mark", href, target: "_blank", rel: "noopener noreferrer",
    title: citeLabel(c) }, label);
}

function citeRef(c) {
  if (!c) return null;
  const text = `[${c.n}] ${citeLabel(c)}`;
  const href = safeHttpUrl(c.source_url);
  if (!href) return h("span", { class: "cite-ref" }, text);
  return h("a", { class: "cite-ref", href, target: "_blank", rel: "noopener noreferrer",
    title: href }, text);
}

function sourceRefs(citations) {
  const { unique } = buildCiteMap(citations);
  if (!unique.length) return empty();
  // Keep the side-panel compact: show a one-line summary, expand on click.
  return h("details", { class: "cites-fold" },
    h("summary", null, `${unique.length} 条官方依据`),
    h("div", { class: "cites-list" }, unique.map((c) => h("div", null, citeRef(c)))));
}

function visaCard(t) {
  const a = S.advice.visa;
  const flash = S.flash.includes("visa") ? " flash" : "";
  const visa = val(S.profile, "visa_category");
  const dest = val(S.profile, "destination_country");
  if (!a) {
    return advicePanel(t, {
      id: "card-visa", status: "未开始", flash,
      kids: [
        adviceFact("类别", visa ? valueLabel("visa_category", visa) : empty()),
        adviceFact("材料", empty()),
        adviceFact("待确认", empty()),
      ],
    });
  }
  const pl = a.payload;
  const mats = (pl.materials || []).filter((m) => !isInternalOutput(m.name_zh)
    && !isInternalOutput(m.name_en) && !isInternalOutput(m.condition_text));
  const req = mats.filter((m) => m.applicability === "required");
  const cond = mats.filter((m) => m.applicability !== "required");
  const unconfirmed = pl.unconfirmed_fields || [];
  const warn = unconfirmed.length || mats.some((m) => m.applicability === "unverified");
  const reqNames = req.map((m) => m.name_zh).filter(Boolean);
  const condNames = cond.map((m) => m.name_zh).filter(Boolean);
  const category = [
    valueLabel("destination_country", dest),
    valueLabel("visa_category", visa),
  ].filter(Boolean).join(" · ") || "签证材料清单";
  const summary = customerText(pl.summary, "");
  return advicePanel(t, {
    id: "card-visa",
    status: warn ? "待核实" : "已确认",
    statusClass: warn ? "warn" : "ok",
    flash,
    wide: true,
    kids: [
      adviceOption({
        label: "签证方案",
        title: category,
        body: summary && summary.length < 180 ? summary : null,
        featured: true,
        facts: [
          adviceFact("必备材料",
            reqNames.length ? reqNames.join("、") : empty(),
            { strong: false }),
          adviceFact("视情况",
            condNames.length ? condNames.join("、") : "无",
            { strong: false }),
          adviceFact("待确认",
            unconfirmed.length
              ? h("span", { class: "warn" }, unconfirmed.map(label).join("、"))
              : "无",
            { strong: false }),
        ],
      }),
      adviceFact("来源", sourceRefs(pl.citations), { strong: false }),
    ],
  });
}

function todoBox() {
  const input = h("input", { placeholder: "添加一条待办，例如「预约签证中心」", maxlength: "200" });
  return h("div", { class: "todo" },
    S.plan.length ? h("ul", null, S.plan.map((i) => h("li", { class: i.done ? "done" : "" },
      h("input", { type: "checkbox", checked: i.done, onchange: (e) => updatePlan(i, { done: e.target.checked }) }),
      h("span", null, i.text),
      h("button", { class: "todo-edit", type: "button", "aria-label": `编辑待办：${i.text}`,
        onclick: () => editPlan(i) }, "编辑"),
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
  } catch (e) { toast(userFacingError(e, "待办暂未添加，请稍后重试。"), true); }
}

function editPlan(item) {
  const input = h("input", { type: "text", maxlength: "200", value: item.text, "aria-label": "待办内容" });
  const err = h("div", { class: "formerr", role: "alert" });
  const save = async () => {
    const text = input.value.trim();
    if (!text) { err.textContent = "请填写待办内容。"; input.focus(); return; }
    if (text === item.text) return closeOverlay();
    try {
      await api("PATCH", `/plan-items/${item.item_id}`, { text });
      await loadPlan();
      closeOverlay();
      drawSheet();
      drawChat({ keep: true });
      toast("待办已更新");
    } catch (e) {
      err.textContent = userFacingError(e, "待办暂未更新，请稍后重试。");
    }
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); save(); }
  });
  overlay(h("div", { class: "modal" },
    h("h3", { class: "sf" }, "编辑待办"),
    h("label", { class: "todo-edit-label" }, "待办内容", input), err,
    h("div", { class: "btns" },
      h("button", { class: "btn ghost", type: "button", onclick: closeOverlay }, "取消"),
      h("button", { class: "btn", type: "button", onclick: save }, "保存"))));
  input.focus();
  input.select();
}

async function updatePlan(item, change) {
  try {
    await api("PATCH", `/plan-items/${item.item_id}`, change);
    await loadPlan();
    drawSheet();
  } catch (e) { toast(userFacingError(e, "待办暂未更新，请稍后重试。"), true); }
}

async function deletePlan(item) {
  try {
    await api("DELETE", `/plan-items/${item.item_id}`);
    await loadPlan();
    drawSheet();
    drawChat({ keep: true });
  } catch (e) { toast(userFacingError(e, "待办暂未删除，请稍后重试。"), true); }
}

// ---------- chat stream ----------

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
    const bubble = answerBubble(m.body, latest);
    if (bubble) items.push(bubble);
  });
  if (S.pending) {
    items.push(h("div", { class: "m u pending" }, S.pending.text));
    if (S.pending.error) {
      const p = S.pending;
      items.push(h("div", { class: "m err" }, h("p", null, userFacingError(p.error, "这次没有完成处理，您可以重试。")),
        h("div", { class: "chips" },
          h("button", { class: "s", onclick: () => {
            S.pending = null;
            send(p.text, p.cid, p.changed, p.confirmedFields, p.expectedProfileVersion, {
              sessionFields: p.sessionFields, clearedFields: p.clearedFields,
            });
          } }, "重试"),
          h("button", { onclick: () => { S.pending = null; drawChat(); } }, "放弃"))));
    } else if (S.pending.phases && S.pending.phases.length > 1) {
      // Step chain: completed steps get a tick and fade; the active one keeps
      // the pulse dot and re-renders as the chain advances.
      const phases = S.pending.phases;
      const idx = S.pending.phaseIdx || 0;
      items.push(h("div", { class: "m thinking-chain" },
        phases.slice(0, idx).map((p) =>
          h("div", { class: "chain-step done" }, h("span", { class: "tick" }, "✓"), p)),
        h("div", { class: "chain-step thinking" }, phases[idx])));
    } else {
      items.push(h("div", { class: "m thinking" }, S.pending.phase || "已收到，正在为您核对…"));
    }
  }
  box.replaceChildren(...items);
  box.scrollTop = keep ? scroll : box.scrollHeight;
}

function isCardRecommendationSteps(steps) {
  return steps.some((s) => s.startsWith("年费："))
    && steps.some((s) => /(?:主推荐|初步选择)：|汇丰.+发行的卡/.test(s));
}

/** Tuition remittance card picks: "首选 Foo（Bank）：年净收益约 …" */
function isTuitionCardSteps(steps) {
  return (steps || []).some((s) => /^(首选|备选|补充)\s+.+\s*（.+）\s*：\s*年净收益约/.test(String(s)));
}

function renderTuitionCardSteps(steps) {
  const box = h("div", { class: "card-rec tuition-cards" });
  for (const s of steps || []) {
    const m = String(s).match(/^(首选|备选|补充)\s+(.+)$/);
    if (m) {
      box.append(h("div", { class: "card-name" },
        h("span", { class: "role-tag" }, m[1]),
        h("span", null, ...linkify(m[2]))));
    } else {
      box.append(h("div", { class: "card-note" }, ...linkify(s)));
    }
  }
  // Summary already carries the intro; no second "可参考的卡片" heading.
  return [box];
}

function cardStepForDisplay(step) {
  return String(step)
    .replace(/\s*[（(]\s*s\d+(?:\s*[,，、;；]\s*s\d+)*\s*[）)]/gi, "")
    .replace(/^顾问分析：/, "")
    .replace(/^客户需求匹配：/, "")
    .replace(/^价值拆账：/, "按您提供的信息估算，")
    .replace(/^怎样才能真正用到价值：/, "用卡时可以留意：")
    .replace(/^(?:适合与取舍|组合建议|下一步建议)：/, "")
    .replace(/^客户经理视角：$/, "")
    .trim();
}

function multiDomainSections(steps) {
  const sections = [];
  let current = null;
  for (const step of steps || []) {
    if (String(step).startsWith("## ")) {
      current = { title: String(step).replace(/^##\s*/, ""), lines: [] };
      sections.push(current);
    } else if (current) {
      current.lines.push(step);
    }
  }
  return sections;
}

function renderAnswerSteps(steps) {
  const multi = multiDomainSections(steps);
  if (multi.length && steps.some((s) => String(s).startsWith("## "))) {
    return multi.map((sec) => h("div", { class: "multi-sec" },
      h("div", { class: "sub-h" }, sec.title),
      ...sec.lines.map((line) => h("p", null, ...linkify(line)))));
  }
  if (isTuitionCardSteps(steps)) {
    return renderTuitionCardSteps(steps);
  }
  if (!isCardRecommendationSteps(steps)) {
    return [h("div", { class: "sub-h" }, "给您的建议"),
      h("ol", { class: "lim" }, steps.map((s) => h("li", null, ...linkify(s))))];
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
    else if (/^(主推荐|初步选择|场景搭配|附加推荐|其他选择|待核对选择)：/.test(s)) {
      const i = s.indexOf("：");
      box.append(h("div", { class: "card-name" },
        h("span", { class: "role-tag" }, s.slice(0, i)),
        h("span", null, ...linkify(s.slice(i + 1)))));
    }
    else if (/^(推荐理由|为什么适合你|为什么适合您|年费|主要权益|您可享|你可享|申请限制|申请前提)：/.test(s)) {
      const i = s.indexOf("：");
      box.append(h("div", { class: `card-detail ${/^(?:推荐理由|为什么适合你|为什么适合您)/.test(s) ? "reason" : ""}` },
        h("span", { class: "k" }, s.slice(0, i + 1)), ...linkify(s.slice(i + 1))));
    } else if (s.startsWith("免年费选择：")) box.append(h("div", { class: "card-alt" }, ...linkify(s)));
    else if (/（(?:Visa|Mastercard|UnionPay|银联卡|威士卡|万事达卡)/.test(s) || /（[A-Za-z]+）$/.test(s)) {
      box.append(h("div", { class: "card-name" }, ...linkify(s)));
    } else box.append(h("div", { class: "card-note" }, ...linkify(s)));
  }
  const preview = steps.some((s) => s.startsWith("提前了解："));
  const compare = steps.some((s) => s.startsWith("对比参考："));
  return [h("div", { class: "sub-h" }, preview ? "提前了解的信用卡" :
    compare ? "两张卡怎么选" : "为您挑选的信用卡"), box];
}

function visibleOutputLines(lines) {
  return (lines || []).filter((line) => !isInternalOutput(line));
}

/**
 * Backend filler lines (minimal-domain needs_input turns) that carry no advice.
 * Filtering them removes the empty "给您的建议" section above follow-up questions.
 */
const FILLER_STEPS = new Set([
  "先确认会影响建议方向的关键信息，再给出准备清单。",
]);
function adviceSteps(lines) {
  return visibleOutputLines(lines).filter((s) => !FILLER_STEPS.has(String(s).trim()));
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
    const kids = [];
    // Multi-intent: show each domain's short summary before the first follow-up.
    const safeSteps = adviceSteps(b.answer?.steps || []);
    if (b.answer?.summary && !isInternalOutput(b.answer.summary)) {
      kids.push(h("p", null, ...linkify(b.answer.summary)));
    }
    if (safeSteps.length) kids.push(...renderAnswerSteps(safeSteps));
    kids.push(followUp({ ...question,
      text: isInternalOutput(question.text) ? "请补充相关信息，我会继续为您核对。" : question.text,
      options: (question.options || []).filter((o) => !isInternalOutput(o.label) && !isInternalOutput(o.message)),
    }));
    if (canGoBackFollowUp(question.field)) {
      kids.push(h("div", { class: "fq-nav" },
        h("button", {
          class: "btn sm back",
          type: "button",
          disabled: !!S.busy,
          title: S.busy ? "正在处理上一条，请稍候" : "返回上一道追问并重新选择",
          onclick: goBackFollowUp,
        }, "上一步")));
    }
    return h("div", { class: "m s-needs_input ask-only" }, kids);
  }

  const st = STATUS[b.status] || STATUS.cannot_answer;
  const { cmap, unique: citeList } = buildCiteMap(b.citations || []);
  const mats = (b.answer.materials || []).filter((m) => !isInternalOutput(m.name_zh)
    && !isInternalOutput(m.name_en) && !isInternalOutput(m.condition_text));
  const kids = [];

  if (b.status !== "answered") {
    kids.push(h("div", { class: "status" }, h("span", { class: "st " + st.cls }, st.label), h("span", { class: "why" }, st.why)));
  }
  const safeSummary = isInternalOutput(b.answer.summary)
    ? "当前资料不足以支持确定回答，请按页面提示补充信息或核实官方要求。"
    : b.answer.summary;
  const rawSteps = adviceSteps(b.answer.steps).filter((s) => !String(s).startsWith("政策原文："));
  const stepList = isCardRecommendationSteps(rawSteps)
    ? rawSteps.map(cardStepForDisplay).filter(Boolean) : rawSteps;
  const multi = multiDomainSections(stepList);
  const cardAnswer = isCardRecommendationSteps(stepList);
  if (!cardAnswer) {
    kids.push(h("p", null, ...linkify(safeSummary)));
  }
  // Multi-intent answers already nest domain copy under ## sections; keep materials
  // out of the top so bank docs do not look like a free-floating visa list.
  if (mats.length && multi.length <= 1) {
    kids.push(h("ul", { class: "mat" }, mats.map((m) => materialItem(m, cmap))));
  }
  if (stepList.length) kids.push(...renderAnswerSteps(stepList));
  if ((b.conflicts || []).length) {
    kids.push(h("div", { class: "sub-h" }, "不同来源说法不一致（并列展示，未替您取舍）"));
    b.conflicts.filter((cf) => !isInternalOutput(cf.name_zh)).forEach((cf) => kids.push(h("div", null, cf.name_zh), h("div", { class: "conf" }, cf.variants
      .filter((v) => !isInternalOutput(v.source_name) && !isInternalOutput(v.condition)).map((v) =>
        h("div", null, h("b", null, v.source_name), v.required ? "需要" : "不需要", v.condition ? `（${v.condition}）` : "")))));
  }
  if (citeList.length) {
    kids.push(h("details", { class: "evidence" },
      h("summary", null, `查看官方依据（${citeList.length}）`),
      h("div", { class: "evidence-list" }, citeList.map((c) => h("div", null, citeRef(c))))));
  }
  if ((b.answer.limitations || []).length) {
    const limitations = visibleOutputLines(b.answer.limitations);
    if (limitations.length) kids.push(h("div", { class: "sub-h" }, "温馨提示"), h("ul", { class: "lim" }, limitations.map((s) => h("li", null, ...linkify(s)))));
  }
  if (b.status !== "answered" && (b.next_actions || []).length) {
    const nextActions = visibleOutputLines(b.next_actions);
    if (nextActions.length) kids.push(h("div", { class: "sub-h" }, "下一步"), h("ul", { class: "lim" }, nextActions.map((s) => h("li", null, ...linkify(s)))));
  }
  // Hide internal/debug notices (routing audit JSON, etc.) from the customer UI.
  const HIDDEN_NOTICE = new Set([
    "missing_evidence", "llm_fallback", "domain_workflow_todo", "intent_routing",
    "llm_routing_low_confidence", "unsupported_intent", "partial_out_of_scope",
  ]);
  (b.notices || []).filter((n) => n && !HIDDEN_NOTICE.has(n.code) && !isInternalOutput(n.message) && !(n.message || "").trim().startsWith("{"))
    .forEach((n) => kids.push(h("div", { class: "notice" }, ...linkify(n.message))));
  const open = new Set(S.proposals.map((p) => p.proposal_id));
  // Only propose fields the UI knows how to label (registered profile fields).
  const profileProposals = (b.profile_change_proposals || [])
    .filter((p) => open.has(p.proposal_id) && S.fields && S.fields[p.field_code]);
  if (profileProposals.length) kids.push(proposalBundleCard(profileProposals));
  const openRelationships = new Set(S.relationshipProposals.map((p) => p.proposal_id));
  (b.bank_relationship_proposals || [])
    .filter((p) => openRelationships.has(p.proposal_id))
    .forEach((p) => kids.push(relationshipProposalCard(p)));
  kids.push(actsRow(b));
  return h("div", { class: "m s-" + b.status }, kids);
}

function materialItem(m, cmap) {
  const todo = `准备材料：${m.name_zh}`;
  const exists = S.plan.some((i) => i.text === todo);
  return h("li", null,
    h("span", null, m.name_zh),
    m.name_en && m.name_en !== m.name_zh ? h("span", { class: "en" }, m.name_en) : null,
    h("span", { class: "tagx " + m.applicability }, TAG[m.applicability] || m.applicability),
    h("button", { class: "addtodo", disabled: exists, onclick: () => addPlan(todo) }, exists ? "✓ 已在待办" : "+ 待办"),
    m.condition_text ? h("span", { class: "cond" }, `条件：${m.condition_text}`) : null,
    (m.pending_fields || []).length ? h("span", { class: "pend" }, `待确认：${m.pending_fields.map(label).join("、")}`) : null);
}

function currentFollowUpQuestion() {
  const latest = lastAnswer();
  if (latest?.status !== "needs_input") return null;
  const qs = latest.follow_up_questions || [];
  return qs.find((q) => val(S.profile, q.field) === undefined) || qs[0] || null;
}

/** 本会话里真正作为追问问过的字段（自动解析入档的不算「上一步」）。 */
function askedFollowUpFields() {
  const asked = new Set();
  for (const m of S.messages) {
    const body = (m && m.body) || {};
    for (const q of body.follow_up_questions || []) {
      if (q && q.field) asked.add(q.field);
    }
  }
  return asked;
}

/** 当前问题之前、档案里已确认且可撤销的追问字段（trail 丢了也能继续上一步）。 */
function undoableFollowFields(currentField) {
  const suppressed = new Set(S.suppressFields || []);
  const trailFields = new Set((S.followTrail || []).map((item) => item.field));
  const asked = askedFollowUpFields();
  const idx = FOLLOW_UNDO_ORDER.indexOf(currentField);
  const candidates = idx >= 0 ? FOLLOW_UNDO_ORDER.slice(0, idx) : FOLLOW_UNDO_ORDER.slice();
  return candidates.filter((code) => {
    if (suppressed.has(code) || code === currentField) return false;
    // Only fields this conversation actually asked may be undone — profile
    // values auto-parsed from message text were never a user step.
    if (!asked.has(code)) return false;
    if (trailFields.has(code)) return true;
    return val(S.profile, code) !== undefined;
  });
}

function canGoBackFollowUp(currentField) {
  return (S.followTrail && S.followTrail.length > 0)
    || undoableFollowFields(currentField).length > 0;
}

function followUp(q) {
  const f = S.fields[q.field] || { kind: "text", options: [], label: q.field };
  const options = (q.options && q.options.length) ? q.options : f.options;
  const chips = h("div", { class: "chips" });
  if (q.input_type === "multi_select") {
    const selected = new Map();
    const confirm = h("button", { class: "s", disabled: true }, "确认选择");
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
      onclick: () => answerField(q.field, o.value, o.label, q.persist_mode, o.message)
    }, o.label)));
  } else {
    const input = h("input", { type: f.kind === "date" ? "date" : f.kind === "age" ? "number" : "text", placeholder: f.label });
    input.dataset.field = q.field;
    const ok = () => {
      const v = readInput(input, q.field);
      if (v !== undefined) answerField(q.field, v, valueLabel(q.field, v), q.persist_mode);
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

async function answerField(code, value, text, persistMode, routeMessage) {
  if (S.busy) { toast("上一条还在处理中，请稍候再提交"); return; }
  if (currentFollowUpQuestion()?.field !== code) {
    try {
      await Promise.all([loadMessages(), refreshProfile()]);
      deriveMissing();
      drawChat({ keep: true });
      toast("问题已更新，请回答现在显示的追问");
    } catch (e) { toast(userFacingError(e, "这道追问暂时无法更新，请稍后重试。"), true); }
    return;
  }
  // Disambiguation choices steer only this turn. They are deliberately not
  // profile fields, so the user can ask about the other product next time.
  if (persistMode === "session_only") {
    if (code !== "banking_help_type" && S.fields[code]) {
      await send(`${label(code)}：${text}`, null, [], {}, S.profile.version, {
        sessionFields: { [code]: value },
      });
      return;
    }
    const routeText = routeMessage || {
      bank_account: "我想了解银行账户或借记卡开户",
      credit_card: "我想了解信用卡申请和推荐",
    }[value] || text;
    await send(routeText, null, [], {}, S.profile.version);
    return;
  }
  if (persistMode === "proposal") {
    await send(`${label(code)}：${text}`, null, [], {}, S.profile.version, {
      sessionFields: { [code]: value },
    });
    return;
  }
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
  let last = S.followTrail.length ? S.followTrail.pop() : null;
  if (!last) {
    const current = currentFollowUpQuestion();
    const priors = undoableFollowFields(current && current.field);
    if (!priors.length) { toast("没有可返回的步骤"); return; }
    const field = priors[priors.length - 1];
    const value = val(S.profile, field);
    last = { field, value, label: valueLabel(field, value), persistMode: "save_on_select" };
  }
  if (!S.suppressFields.includes(last.field)) S.suppressFields.push(last.field);
  // 同步丢掉 trail 里同一字段及之后的步骤，避免和档案回退错位。
  const cut = S.followTrail.findIndex((item) => item.field === last.field);
  if (cut >= 0) S.followTrail = S.followTrail.slice(0, cut);
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

function proposalValue(p, value) {
  // Show real values in the change card: asset/income bands are already ranges
  // (their option labels reveal nothing beyond the band), and hiding them made
  // old→new renders read as "已提供 → 已提供", an unconfirmable change.
  return value === null || value === undefined ? "（空）" : valueLabel(p.field_code, value);
}

function proposalBundleCard(proposals) {
  const affected = [...new Set(proposals.flatMap((p) => p.affected_advice_topics || []))]
    .filter((topic) => S.advice[topic]);
  const selected = new Set(affected);
  const choices = affected.length ? h("div", { class: "proposal-advice-choices" },
    h("div", null, "以下建议可能因这次资料更新而变化。您想先更新哪些？"),
    affected.map((topic) => {
      const input = h("input", { type: "checkbox", checked: true });
      input.addEventListener("change", () => input.checked ? selected.add(topic) : selected.delete(topic));
      return h("label", null, input, adviceTopicLabel(topic));
    }),
    h("div", { class: "note" }, "暂不更新的建议会保留当前版本；之后您随时可以再更新。")) : null;
  return h("div", { class: "prop" },
    h("div", null, "要把这些信息更新到学生资料吗？"),
    h("ul", { class: "proposal-list" }, proposals.map((p) => h("li", null,
      h("b", null, label(p.field_code)), "　", proposalValue(p, p.old_value), " → ",
      h("b", null, proposalValue(p, p.new_value))))),
    choices,
    h("div", { class: "btns" },
      h("button", { class: "btn sm", onclick: () => decideProposalBundle(
        proposals, true, [...selected]), disabled: !!S.busy }, affected.length ? "确认并更新建议" : "确认更新"),
      h("button", { class: "btn sm ghost", onclick: () => decideProposalBundle(proposals, false), disabled: !!S.busy }, "这次先不保存")));
}

function relationshipProposalCard(p) {
  const market = { "CN-HK": "香港", UK: "英国", US: "美国", CN: "中国内地" }[p.market] || "其他地区";
  const status = { selected: "准备申请", applying: "申请中", open: "已经开户",
    blocked: "申请受阻", closed: "已经关闭" }[p.proposed_status] || "进度已更新";
  const actions = p.proposed_status === "selected"
    ? [h("button", { class: "btn sm", disabled: !!S.busy, onclick: () => decideRelationship(p, true, "selected") }, "记录为准备申请"),
       h("button", { class: "btn sm", disabled: !!S.busy, onclick: () => decideRelationship(p, true, "open") }, "已经开好了")]
    : [h("button", { class: "btn sm", disabled: !!S.busy, onclick: () => decideRelationship(p, true, p.proposed_status) }, "记录进度")];
  return h("div", { class: "prop relationship-prop" },
    h("div", null, "要记录这项银行账户进度吗？ ", h("b", null, `${market} · ${p.product_name}`),
      h("span", { class: "rel-proposed" }, status)),
    h("div", { class: "btns" },
      ...actions,
      h("button", { class: "btn sm ghost", disabled: !!S.busy, onclick: () => decideRelationship(p, false) }, "这次先不记录")));
}

async function decideRelationship(p, confirm, status) {
  if (S.busy) { toast("上一项操作还在处理中，请稍候"); return; }
  S.busy = true;
  toast(confirm ? "正在记录账户进度…" : "这次进度不会写入档案");
  drawChat({ keep: true });
  try {
    if (confirm) {
      await api("POST", `/bank-relationship-proposals/${p.proposal_id}/confirm`, {
        expected_version: S.profile.version, status,
      });
    } else {
      await api("POST", `/bank-relationship-proposals/${p.proposal_id}/dismiss`, {});
    }
    await Promise.all([refreshProfile(), loadRelationships(), loadRelationshipProposals()]);
    drawSheet();
    drawChat({ keep: true });
    toast(confirm ? "已记录银行账户进度" : "这次不会写入资料");
  } catch (e) {
    if (e.code === "VERSION_CONFLICT") await refreshProfile();
    toast(userFacingError(e, "银行账户进度暂未保存，请稍后重试。"), true);
  } finally {
    S.busy = false;
    drawSide();
    drawChat({ keep: true });
  }
}

async function decideProposalBundle(proposals, confirm, refreshTopics = []) {
  if (S.busy) { toast("上一项操作还在处理中，请稍候"); return; }
  S.busy = true;
  toast(confirm ? "正在更新资料并处理相关建议…" : "正在保留本次对话内容…");
  drawChat({ keep: true });
  try {
    if (confirm) {
      S.profile = await api("POST", "/profile-proposals/confirm-batch", {
        proposal_ids: proposals.map((p) => p.proposal_id), expected_version: S.profile.version,
        refresh_topics: refreshTopics,
      });
      syncProfile();
      S.highlight = new Set(proposals.map((p) => p.field_code));
    } else {
      await Promise.all(proposals.map((p) => api("POST", `/profile-proposals/${p.proposal_id}/dismiss`, {})));
    }
    await Promise.all([loadProposals(), confirm ? loadAdvice() : Promise.resolve(),
      confirm ? loadMessages() : Promise.resolve()]);
    if (confirm) S.flash = refreshTopics.slice();
    deriveMissing();
    drawSheet();
    drawSide();
    drawChat({ keep: true });
    toast(confirm ? "已写入档案" : "已忽略该提议");
  } catch (e) {
    if (e.code === "VERSION_CONFLICT") await refreshProfile();
    toast(userFacingError(e, "资料暂未更新，请稍后重试。"), true);
  } finally {
    S.busy = false;
    drawSide();
    drawChat({ keep: true });
  }
}

function actsRow(b) {
  const st = STATUS[b.status] || STATUS.cannot_answer;
  return h("div", { class: "acts" },
    h("span", { class: "fb" },
      h("span", { class: "st " + st.cls }, st.label), h("span", { class: "sep" }, "｜"),
      h("button", { onclick: () => { S.el.input.placeholder = "继续补充您的情况或追问…"; S.el.input.focus(); } }, "继续补充")));
}

async function feedback(b, kind, note) {
  try {
    const res = await api("POST", `/messages/${b.message_id}/feedback`, { kind, note: note || "" });
    const state = S.fb[b.message_id];
    state.kind = kind;
    state.open = false;
    if (res.feedback_id) state.feedbackId = res.feedback_id;
    drawChat({ keep: true });
  } catch (e) { toast(userFacingError(e, "反馈暂未提交，请稍后重试。"), true); }
}

/**
 * Waiting-bubble step chains that mirror what the agent actually does for each
 * intent, instead of two generic lines. The active step shows the pulse dot;
 * earlier steps fade to ticks while the request is in flight.
 */
function thinkingPhases(text, isStructured) {
  const t = String(text || "");
  if (isStructured) {
    return [
      "正在把您的选择记入学生资料…",
      "正在按新信息重新核对受影响的建议…",
      "正在整理更新后的建议…",
    ];
  }
  if (/学费|缴费|付款|截止|汇款|购汇|电汇|汇出|换汇|额度|汇到|汇过去|怎么汇/.test(t)) {
    return [
      "正在确认您的学校与课程信息…",
      "正在查询学校官方学费目录…",
      "正在核对学费档位与缴费要求…",
      "正在测算购汇额度与所需材料…",
      "正在起草回答并核对数字…",
    ];
  }
  if (/信用卡|办卡|刷卡|返现|年费|羊毛/.test(t)) {
    return [
      "正在确认您的所在地与用卡需求…",
      "正在比对各行信用卡的年费与权益…",
      "正在核对每张卡的申请条件…",
      "正在计算长期持有的综合收益…",
      "正在起草推荐并核对条款…",
    ];
  }
  if (/开户|账户|借记卡|银行卡/.test(t)) {
    return [
      "正在确认您的身份与所在地…",
      "正在匹配可办理的银行与账户类型…",
      "正在核对开户门槛与所需证件…",
      "正在整理开户流程与材料清单…",
    ];
  }
  if (/签证|visa|护照|资金证明|存款证明/.test(t)) {
    return [
      "正在确认您的签证类型与目的地…",
      "正在核对签证材料清单…",
      "正在核对资金证明等要求…",
      "正在整理办理流程与时间线…",
    ];
  }
  return ["正在核对与您当前资料相关的条件…", "正在整理适合您的建议，请稍候…"];
}

async function send(text, retryCid, changed, confirmedFields, expectedProfileVersion, opts = {}) {
  if (S.busy) {
    toast("上一条还在处理中，请稍候再提交");
    return null;
  }
  S.busy = true;
  if (S.el.sendBtn) S.el.sendBtn.disabled = true;
  const confirmed = confirmedFields || {};
  const sessionFields = opts.sessionFields || {};
  const clearedFields = opts.clearedFields || [];
  const isStructured = Object.keys(confirmed).length > 0 || Object.keys(sessionFields).length > 0;
  // Fresh free-text starts a new chain. Structured answers and "上一步" keep it.
  if (!isStructured && !opts.isBack && !retryCid) {
    S.followTrail = [];
    S.followSeed = text;
    S.suppressFields = [];
  }
  S.highlight = new Set(changed || []);
  const context = {
    profileId: S.pid,
    conversationId: S.conv.session_id || S.conv.conversation_id,
  };
  const pending = { text, cid: retryCid || uuid(), error: null, changed,
    confirmedFields: confirmed, sessionFields, expectedProfileVersion, clearedFields };
  S.pending = pending;
  // Intent-specific chain: first step shows immediately, then advances while
  // the request is in flight. Stale pending (switched profile / new message)
  // stops the chain through the guard below.
  const phases = thinkingPhases(text, isStructured);
  pending.phases = phases;
  pending.phaseIdx = 0;
  pending.phase = phases[0];
  let phaseTimer = 0;
  const advancePhase = (delay) => {
    phaseTimer = setTimeout(() => {
      if (S.pending !== pending || S.pid !== context.profileId) return;
      if (pending.phaseIdx < phases.length - 1) {
        pending.phaseIdx += 1;
        pending.phase = phases[pending.phaseIdx];
        drawChat({ keep: true });
      }
      advancePhase(1600);
    }, delay);
  };
  advancePhase(950);
  drawChat();
  try {
    const res = await api("POST", `/sessions/${context.conversationId}/messages`,
      { text, client_message_id: pending.cid, confirmed_fields: pending.confirmedFields,
        session_fields: pending.sessionFields,
        expected_profile_version: pending.expectedProfileVersion,
        cleared_fields: pending.clearedFields }, { timeout: 90000 });
    if (S.pid !== context.profileId || (S.conv.session_id || S.conv.conversation_id) !== context.conversationId) {
      return res;
    }
    S.pending = null;
    // Render the answer as soon as it is available.  Plans and account records
    // then catch up without blocking the conversation from becoming usable.
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
    void loadChatSecondary(context.profileId);
    return res;
  } catch (e) {
    if (S.pid !== context.profileId) return null;
    if (e.code === "STALE_FOLLOW_UP") {
      S.pending = null;
      S.followTrail = [];
      S.suppressFields = [];
      try {
        await Promise.all([loadMessages(), refreshProfile()]);
        deriveMissing();
        drawSheet();
        drawSide();
        toast("问题已更新，请回答现在显示的追问");
      } catch (refreshError) {
        toast(userFacingError(refreshError, "问题暂未刷新，请稍后重试。"), true);
      }
    } else if (S.pending) S.pending.error = e;
    return null;
  } finally {
    clearTimeout(phaseTimer);
    // busy 必须在重绘前清掉，否则「上一步」会带着 disabled 画进 DOM 后一直点不了。
    S.busy = false;
    if (S.pid === context.profileId) {
      if (S.el.sendBtn) S.el.sendBtn.disabled = false;
      drawSide();
      drawChat({ keep: true });
    }
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
      h("span", null, "核验时间"), h("span", null, fmtFull(c.verified_at))),
    h("div", { class: "sub-h" }, "原文引用"), h("blockquote", null, customerText(c.quote, "暂无可展示的原文摘录。")),
    c.condition_quote && !isInternalOutput(c.condition_quote) ? [h("div", { class: "sub-h" }, "适用条件原文"), h("blockquote", null, c.condition_quote)] : null,
    snap));
  try {
    const s = await api("GET", `/knowledge/snapshots/${encodeURIComponent(c.revision_id)}`);
    snap.textContent = `官方资料状态：${AVAIL[s.availability] || "已核验"} · 最近核验于 ${fmtFull(s.verified_at)}`;
  } catch (e) {
    snap.textContent = "暂时无法查看官方资料状态，您仍可通过上方链接查看原文。";
  }
}

async function openEdit(focus) {
  const p = S.profile;
  const durableGrid = GRID.filter(isProfileField);
  const extra = Object.keys(p.fields).filter((c) => !durableGrid.includes(c) && isProfileField(c) && val(p, c) !== undefined);
  const missing = [...S.missing].filter((c) => !durableGrid.includes(c) && !extra.includes(c) && isProfileField(c));
  const codes = [...durableGrid, ...extra, ...missing];
  const { schoolSel, courseSel, fillSchools, fillPrograms } = schoolCourseInputs({
    destination_country: val(p, "destination_country"),
  });
  const inputs = {};
  for (const c of codes) {
    if (c === "school") inputs[c] = schoolSel;
    else if (c === "course") inputs[c] = courseSel;
    else inputs[c] = fieldInput(c, val(p, c));
  }
  if (inputs.destination_country) {
    inputs.destination_country.addEventListener("change", async () => {
      let dest;
      try { dest = inputs.destination_country.value ? JSON.parse(inputs.destination_country.value) : undefined; }
      catch (_) { dest = undefined; }
      await fillSchools(dest);
    });
  }
  const form = h("form", { class: "form", onsubmit: (e) => e.preventDefault() },
    codes.map((c) => h("label", { class: c === focus ? "focus" : "" }, GRID_LABEL[c] || label(c), inputs[c])));
  const err = h("div", { class: "formerr" });
  await fillSchools(val(p, "destination_country"), val(p, "school"));
  await fillPrograms(val(p, "school"), val(p, "course"));
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
    // Direct editing is already an explicit user confirmation.  We only offer
    // a regeneration choice when it overwrites/removes an existing value;
    // filling an empty field should stay frictionless.
    const replacedExisting = Object.keys(set).some((code) => val(p, code) !== undefined) || clear.length > 0;
    try {
      S.profile = await api("PATCH", `/profiles/${S.pid}`, { expected_version: p.version, fields: set, clear_fields: clear });
      await loadAdvice();
      syncProfile();
      S.highlight = new Set([...Object.keys(set), ...clear]);
      deriveMissing();
      closeOverlay();
      drawSheet();
      drawSide();
      drawChat({ keep: true });
      if (replacedExisting) openAdviceImpactPanel(staleAdviceTopics());
      else toast("资料已保存");
    } catch (e) {
      err.textContent = userFacingError(e, "资料暂未保存，请检查填写内容后重试。");
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

// ---------- report ----------

function reportDocument(profile, adv, plan) {
  const now = new Date().toISOString();
  const name = val(profile, "beneficiary") || "未命名档案";
  const advice = adv.advice || {};
  const cites = Object.values(advice).flatMap((item) => item?.payload?.citations || []);
  const cn = {};
  const uniqueCites = cites.filter((c) => c && c.citation_id && !cn[c.citation_id]
    && (cn[c.citation_id] = Object.keys(cn).length + 1));
  const pairs = [];
  for (let i = 0; i < GRID.length; i += 2) pairs.push(GRID.slice(i, i + 2));
  const topicBlock = (t) => {
    const item = advice[t.key];
    if (!item) return h("article", { class: "report-topic" },
      h("div", { class: "report-topic-head" }, h("span", null, t.no), h("h3", { class: "sf" }, t.title),
        h("em", null, "尚未开始")),
      h("p", { class: "note" }, "您可以在对话中提出相关问题，系统会逐步补全这部分方案。"));
    const pl = item.payload || {};
    const summary = customerText(pl.summary, "这部分建议仍在整理中，请以已核验资料为准。");
    const steps = (pl.steps || []).filter((step) => !isInternalOutput(step)).slice(0, 6);
    const materials = (pl.materials || []).filter((m) => !isInternalOutput(m.name_zh)
        && !isInternalOutput(m.name_en) && !isInternalOutput(m.condition_text)).map((m) => h("li", null, m.name_en && !isInternalOutput(m.name_en) ? `${m.name_zh}（${m.name_en}）` : m.name_zh,
        h("span", { class: "tagx " + m.applicability }, TAG[m.applicability]),
        cn[m.citation_id] ? ` [${cn[m.citation_id]}]` : "",
        m.condition_text && !isInternalOutput(m.condition_text) ? h("div", { class: "note" }, `条件：${m.condition_text}`) : null,
        (m.pending_fields || []).length ? h("div", { class: "warn" }, `待确认：${m.pending_fields.map(label).join("、")}`) : null));
    return h("article", { class: "report-topic" },
      h("div", { class: "report-topic-head" }, h("span", null, t.no), h("h3", { class: "sf" }, t.title),
        h("em", null, item.status === "stale" ? "建议待更新" : `更新于 ${fmtFull(item.updated_at)}`)),
      h("p", { class: "report-summary" }, summary),
      steps.length ? h("ol", { class: "report-steps" }, steps.map((step) => h("li", null, step))) : null,
      materials.length ? h("ol", { class: "report-materials" }, materials) : null,
      (pl.unconfirmed_fields || []).length ? h("p", { class: "warn" }, `待确认信息：${pl.unconfirmed_fields.map(label).join("、")}`) : null);
  };
  return h("div", { class: "report" },
    h("header", { class: "report-cover" },
      h("p", { class: "report-kicker" }, "GLOBAL PATHWAY · PRIVATE BANKING"),
      h("h1", { class: "sf" }, `${name} · 留学准备方案报告`),
      h("div", { class: "rmeta" }, h("span", null, "留学准备方案快照"), h("span", null, `生成于 ${fmtFull(now)}`))),
    h("h2", null, "一 · 留学基本信息"),
    h("table", null, pairs.map((pair) => h("tr", null, pair.map((c) => [h("th", null, GRID_LABEL[c] || label(c)),
      h("td", null, val(profile, c) === undefined ? "待确认" : valueLabel(c, val(profile, c)))])))),
    h("h2", null, "二 · 定制方案"),
    TOPICS.map(topicBlock),
    h("h2", null, "三 · 我的待办"),
    plan.items.length ? h("ol", null, plan.items.map((i) => h("li", null, `${i.done ? "☑" : "☐"} ${i.text}`)))
      : h("p", { class: "note" }, "暂无待办。"),
    h("h2", null, "四 · 资料来源"),
    uniqueCites.length ? h("ol", null, uniqueCites.map((c) => h("li", null, h("b", null, c.source_name), " ", safeLink(c.source_url),
      h("div", { class: "note" }, `官方资料核验时间：${fmtFull(c.verified_at)}`),
      h("div", { class: "note" }, customerText(c.quote, "").slice(0, 160) + (String(c.quote || "").length > 160 ? "…" : "")))))
      : h("p", { class: "note" }, "暂无引用来源。"),
    h("div", { class: "disc" },
      `本报告是 ${fmtFull(now)} 的方案快照，内容仅基于当时已发布的官方资料；政策可能变化，请以官方最新公布为准。`,
      "标注「待核实 / 待确认」的项目需补充信息或人工确认后再使用。"));
}

async function downloadReport(pid, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "正在生成…";
  try {
    const response = await fetch(`/v1/profiles/${encodeURIComponent(pid)}/report.pdf`, {
      credentials: "same-origin", headers: { Accept: "application/pdf" },
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new ApiError(payload.code || "DOWNLOAD_FAILED", payload.message || "报告下载失败");
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = h("a", { href: url, download: "global-pathway-report.pdf" });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("报告已开始下载");
  } catch (error) {
    toast(userFacingError(error, "报告暂时无法下载，请稍后重试。"), true);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function openReportDialog(pid) {
  if (!pid) return;
  const panel = h("section", { class: "modal report-modal", role: "dialog", "aria-modal": "true",
    "aria-labelledby": "report-title" },
    h("div", { class: "report-modal-bar" },
      h("div", null, h("span", { class: "lab" }, "方案快照"), h("h3", { id: "report-title", class: "sf" }, "导出报告")),
      h("div", { class: "report-modal-actions" },
        h("button", { class: "btn ghost", type: "button", onclick: closeOverlay }, "关闭"),
        h("button", { class: "btn", type: "button", onclick: (event) => downloadReport(pid, event.currentTarget) }, "下载 PDF"))),
    h("div", { class: "report-modal-loading" }, "正在整理这份方案报告…"));
  overlay(panel);
  try {
    const [profile, adv, plan] = await Promise.all([
      api("GET", `/profiles/${pid}`), api("GET", `/profiles/${pid}/advice`), api("GET", `/profiles/${pid}/plan`),
    ]);
    if (!document.body.contains(panel)) return;
    panel.querySelector(".report-modal-loading").replaceWith(reportDocument(profile, adv, plan));
  } catch (e) {
    if (!document.body.contains(panel)) return;
    panel.querySelector(".report-modal-loading").replaceWith(h("div", { class: "report-modal-empty" },
      h("p", null, userFacingError(e, "报告暂时无法生成，请稍后重试。")),
      h("button", { class: "btn", type: "button", onclick: () => openReportDialog(pid) }, "重试")));
  }
}

async function renderReport(pid) {
  // Preserve direct links from older reports while keeping the application in chat.
  go("#/chat/" + pid);
  await openReportDialog(pid);
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
