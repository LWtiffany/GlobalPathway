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
      { fields: { role: "parent", beneficiary: "王小明", destination_country: "UK",
        school: "f545e6b8-2118-5951-8f44-6f8e1f15110c", // University College London
        offer_status: "conditional", planned_start_date: "2026-09-21", payer: "parent" },
        todos: ["与学校确认 CAS 发放时间", "预约签证中心递交时间"] },
      { fields: { role: "parent", beneficiary: "王小悦", destination_country: "CN-HK",
        school: "6ee509ee-2598-5cdc-8b73-01de1d34015f", // The University of Hong Kong (HKU)
        offer_status: "none" } },
    ] },
  { user: "李同学", tag: "学生 · 美国",
    desc: "本人申请：李华，美国 NYU 数据科学硕士，无条件录取，学生签证。",
    profiles: [
      { fields: { role: "student", beneficiary: "李华", destination_country: "US",
        school: "fdda5cc2-8be6-5f9b-a148-e93c1972cc8c", // New York University
        offer_status: "unconditional", planned_start_date: "2026-09-02",
        visa_category: "student_visa", payer: "self" } },
    ] },
  { user: "陈同学", tag: "学生 · 香港",
    desc: "本人申请：陈思，香港中文大学 MPhil，内地居民，学生签证。",
    profiles: [
      { fields: { role: "student", beneficiary: "陈思", destination_country: "CN-HK",
        school: "f8b7d239-425b-5234-8277-a81abf4fc163", // The Chinese University of Hong Kong
        offer_status: "unconditional", planned_start_date: "2026-09-01",
        visa_category: "student_visa", applicant_residence: "CN" } },
    ] },
];

const S = {
  token: localStorage.getItem("gp_token"), user: null, fields: null, profiles: [], pid: localStorage.getItem("gp_pid"),
  profile: null, conv: null, messages: [], advice: {}, adviceScopes: {}, plan: [], proposals: [],
  relationships: [], relationshipProposals: [], missing: new Set(),
  highlight: new Set(), flash: [], busy: false, refreshingTopics: new Set(), pending: null, sheetH: null, collapsed: false, fb: {}, el: {},
  // One-question-at-a-time follow-ups; trail lets the user undo a wrong tap.
  followSeed: null, followTrail: [], suppressFields: [],
  schools: [], programs: [], schoolById: {}, programById: {}, catalogReady: false,
};

// 追问撤销顺序（靠前的可先被「上一步」清掉）。trail 丢失时仍可按档案回退。
const FOLLOW_UNDO_ORDER = [
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
      toast(e.message || "无法加载学校列表", true);
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
      toast(e.message || "无法加载课程列表", true);
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

async function renderOnboarding() {
  const codes = ["role", "beneficiary", "destination_country", "school", "course", "offer_status", "planned_start_date"];
  const required = ["role", "beneficiary", "destination_country"];
  const { profiles } = await api("GET", "/profiles");
  const err = h("div", { class: "formerr" });
  const { schoolSel, courseSel, fillSchools, fillPrograms } = schoolCourseInputs();
  const destInput = fieldInput("destination_country");
  destInput.addEventListener("change", () => {
    let dest;
    try { dest = destInput.value ? JSON.parse(destInput.value) : undefined; } catch (_) { dest = undefined; }
    fillSchools(dest);
  });
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
  h("div", { class: "form" },
    h("label", null, "角色 *", fieldInput("role")),
    h("label", null, "服务对象 *", fieldInput("beneficiary")),
    h("label", null, "目的地 *", destInput),
    h("label", null, "学校", schoolSel),
    h("label", null, "课程 / 学位", courseSel),
    h("label", null, "Offer 状态", fieldInput("offer_status")),
    h("label", null, "入学日期", fieldInput("planned_start_date"))),
  err,
  h("div", { class: "btns" },
    profiles.length ? h("button", { class: "btn ghost", type: "button", onclick: () => go("#/chat") }, "返回") : null,
    h("button", { class: "btn" }, "创建并进入方案")));
  shell(h("div", { class: "page" },
    h("h1", { class: "sf" }, "新建留学档案"),
    form), "onboarding");
  // Prefill school list if destination already selected somehow.
  try {
    const dest = destInput.value ? JSON.parse(destInput.value) : undefined;
    if (dest) await fillSchools(dest);
  } catch (_) {}
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
  await Promise.all([loadMessages(), loadAdvice(), loadPlan(), loadProposals(),
    loadRelationships(), loadRelationshipProposals()]);
  // Resolve school / program labels for the sheet and every sidebar row.
  try {
    await ensureCatalogForProfiles(S.profiles);
  } catch (e) {
    S.catalogReady = false;
    toast((e && e.message) || "学校/课程目录暂不可用，将显示占位文案", true);
  }
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
  const school = valueLabel("school", val(p, "school"));
  const course = valueLabel("course", val(p, "course"));
  const parts = [
    (school !== "—" ? school : null) || valueLabel("destination_country", val(p, "destination_country")),
    course !== "—" ? course : null,
    m ? `${m[1]}年${+m[2]}月` : "入学待定",
  ];
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
  S.el.sheet.replaceChildren(
    h("div", { class: "sh" },
      h("h2", { class: "sf" }, `${name} · 留学准备方案`),
      h("span", { class: "meta" },
        h("span", null, "基本信息 ", h("em", { class: "sf" }, filled), ` / ${GRID.length}`),
        h("span", null, "待办已完成 ", h("em", { class: "sf" }, done), ` / ${S.plan.length}`),
        h("button", { class: "btn", onclick: () => go("#/report/" + S.pid) }, "导出报告"))),
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
      h("span", { class: "market-tag" }, market[item.market] || item.market),
      h("b", null, item.product_name),
      h("span", { class: "rel-status " + item.status }, status[item.status] || item.status))));
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

function remittanceCard(t) {
  const a = S.advice.remittance;
  const flash = S.flash.includes("remittance") ? " flash" : "";
  if (!a) {
    return h("div", { class: "p" + flash, id: "card-remittance" },
      cardHead(t, h("span", { class: "st" }, "未开始")),
      row("用途", empty()), row("材料", empty()), row("来源", empty()));
  }
  const pl = a.payload || {};
  const purpose = pl.remittance_purpose || pl.fx_mode || pl.domain_payload?.remittance_purpose
    || pl.domain_payload?.fx_mode;
  const purposeLabel = { tuition: "学费", living: "生活费", overseas: "境外消费/换汇" }[purpose]
    || valueLabel("remittance_purpose", purpose) || purpose || "—";
  const mats = pl.materials || pl.domain_payload?.materials || [];
  const cites = pl.citations || pl.domain_payload?.citations || [];
  const req = mats.filter((m) => m.applicability === "required");
  const names = req.slice(0, 2).map((m) => m.name_zh).join("、");
  return h("div", { class: "p" + flash, id: "card-remittance" },
    cardHead(t, h("span", { class: "st ok" }, "已确认")),
    row("用途", purposeLabel),
    row("必备材料", req.length ? `${names}${req.length > 2 ? " 等" : ""} ${req.length} 项` : empty()),
    row("来源", sourceRefs(cites)));
}

function accountDocumentLabel(doc) {
  return ({ identity: "身份证明", address: "地址证明", student_status: "学生或录取证明",
    tax: "税务证明", income: "收入证明" })[doc.document_type]
    || doc.name_zh || doc.description || doc.document_type;
}

function accountRuleLabel(result) {
  const rule = result.rule || {};
  if (rule.field_code === "applicant_age" && rule.operator === "gte" && rule.value_numeric !== null) {
    return `年龄至少 ${Number(rule.value_numeric)} 岁`;
  }
  if (rule.field_code === "bank_confirmation") return "需通过银行的身份与地址核验";
  return rule.description || rule.condition_note || "";
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
    return h("div", { class: "p" + flash, id: "card-account" },
      cardHead(t, h("span", { class: "st" }, "未开始")),
      row("地区", empty()), row("产品", empty()), row("材料", empty()), row("门槛", empty()));
  }
  const display = accountAdviceDisplay(a);
  const { pl, dp } = display;
  const region = display.region
    || val(S.profile, "account_region") || val(S.profile, "destination_country");
  const identity = display.identity || val(S.profile, "applicant_identity")
    || val(S.profile, "role");
  const productName = display.productName || empty();
  const names = display.documentNames.slice(0, 2).join("、");
  const eligibility = display.ruleLabels.slice(0, 2).join("；")
    || (pl.steps || []).find((s) => String(s).startsWith("公开资格要点："))?.replace("公开资格要点：", "")
    || empty();
  const warn = display.needsVerification;
  const pending = display.needsInput && !warn;
  const scopes = Object.keys(S.adviceScopes.account || {});
  const scopeNames = scopes.map((key) => ({ "CN-HK": "香港", UK: "英国", US: "美国", CN: "中国内地" }[key] || key));
  return h("div", { class: "p" + flash, id: "card-account" },
    cardHead(t, h("span", { class: "st " + (warn || pending ? "warn" : "ok") },
      warn ? "待核实" : pending ? "待补充" : "已确认")),
    row("地区", valueLabel("destination_country", region) || region || empty()),
    row("产品", typeof productName === "string" ? productName : productName),
    row("申请人", valueLabel("applicant_identity", identity) || identity || empty()),
    row("必备材料", display.documentNames.length
      ? `${names}${display.documentNames.length > 2 ? " 等" : ""} ${display.documentNames.length} 项` : empty()),
    row("门槛", eligibility),
    scopes.length > 1 ? row("已咨询地区", scopeNames.join("、")) : null,
    row("来源", sourceRefs(pl.citations || dp.citations || [])));
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

function withAdviceLifecycle(card, topic) {
  const advice = S.advice[topic];
  if (!advice || advice.status !== "stale") return card;
  const refreshing = S.refreshingTopics.has(topic);
  card.classList.add("advice-stale");
  card.append(h("div", { class: "advice-refresh" },
    h("b", null, refreshing ? "已开始重新生成建议，请稍候。" : "资料已变化，这份建议可能不再适用。"),
    h("button", { class: "btn sm", disabled: refreshing, onclick: () => refreshAdvice(topic) },
      refreshing ? "生成中…" : "重新生成建议")));
  return card;
}

async function refreshAdvice(topic) {
  if (S.busy) { toast("上一项操作还在处理中，请稍候"); return; }
  S.busy = true;
  S.refreshingTopics.add(topic);
  drawSheet();
  toast(`已开始重新生成${ADVICE_TOPIC_LABEL[topic] || "建议"}，完成后会自动显示。`);
  try {
    const result = await api("POST", `/profiles/${S.pid}/advice/${topic}/refresh`, {});
    await Promise.all([loadAdvice(), loadMessages()]);
    if (result.refreshed) S.flash = [topic];
    drawSheet();
    // A refresh creates a new assistant reply.  Updating only the side-panel
    // left that reply invisible until a browser reload rebuilt the chat.
    drawChat({ keep: true });
    toast(result.refreshed ? `${ADVICE_TOPIC_LABEL[topic] || "建议"}已重新生成`
      : "还缺少必要信息，建议暂时保持待更新");
  } catch (e) {
    toast(e.message, true);
  } finally {
    S.refreshingTopics.delete(topic);
    S.busy = false;
    drawSheet();
  }
}

function cardsFromAnswerSteps(steps) {
  const cards = [];
  let marketRequirement = "";
  (steps || []).forEach((line, index) => {
    if (line.endsWith("发行的卡") && steps[index + 1]) marketRequirement = steps[index + 1];
    const colon = line.indexOf("：");
    const label = colon >= 0 ? line.slice(0, colon) : "";
    const value = colon >= 0 ? line.slice(colon + 1).trim() : "";
    const roles = { 主推荐: "primary", 场景搭配: "supplement", 其他选择: "alternative" };
    const network = line.match(/^(.+?)（(?:万事达卡|威士卡|银联卡|银行卡|Visa|Mastercard|UnionPay)）$/);
    if (roles[label]) cards.push({ product_name: value, role: roles[label] });
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
  const steps = source?.body?.answer?.steps || [];
  const recovered = cardsFromAnswerSteps(steps);
  const saved = Array.isArray(pl.cards) ? pl.cards : [];
  const fromAnswer = new Map(recovered.map((card) => [card.product_name, card]));
  const cards = saved.length ? saved.map((card) => {
    const match = fromAnswer.get(card.product_name) || {};
    const merged = { ...match, ...card };
    for (const field of ["reason_text", "fee_text", "benefits_text", "application_note"])
      if (!merged[field] && match[field]) merged[field] = match[field];
    return merged;
  }) : recovered;
  return { cards,
    preview: !!pl.preview || steps.some((step) => step.startsWith("提前了解：")) };
}

function cardAdviceOption(card, label, featured) {
  return h("div", { class: "cc-option" + (featured ? " featured" : "") },
    h("div", { class: "cc-label" }, label),
    h("div", { class: "cc-name" }, card.product_name),
    card.reason_text ? h("p", { class: "cc-reason" }, card.reason_text) : null,
    h("div", { class: "cc-fact" }, h("span", null, "年费及费用"),
      h("strong", null, card.fee_text || "请以官网最新条款为准")),
    card.benefits_text ? h("div", { class: "cc-fact" }, h("span", null, "主要权益"),
      h("span", null, card.benefits_text)) : null);
}

function isBackupCard(card) {
  const market = String(card.market || "");
  return card.role === "backup"
    || /Hong Kong|^HK$|CN-HK/i.test(market);
}

function creditCardCard(t) {
  const a = S.advice.credit_card;
  const flash = S.flash.includes("credit_card") ? " flash" : "";
  const base = "p credit-advice" + flash;
  if (!a) return h("div", { class: base, id: "card-credit_card" },
    cardHead(t, h("span", { class: "st" }, "未开始")),
    ...(t.rows || ["产品", "费用 / 限制", "申请条件"]).map((r) => row(r, empty())));

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
  const marketPrereq = (source?.body?.answer?.steps || []).find((step) =>
    /均要求已有|到英国读书|香港卡可在留学地使用/.test(step));
  const condition = marketPrereq || first?.application_note
    || "申请资格及最终批核以当地汇丰的官方规定为准。";
  return h("div", { class: base, id: "card-credit_card" },
    cardHead(t, h("span", { class: "st " + stateClass }, state)),
    cards.length ? [
      cardAdviceOption(first, hasPrimary ? "首选推荐" : "方案一 · 供比较", hasPrimary),
      others.length ? h("div", { class: "cc-others" },
        h("div", { class: "cc-section" }, hasPrimary ? "其他选择" : "一起比较"),
        others.slice(0, 2).map((card, index) => h("div", { class: "cc-secondary" },
          cardAdviceOption(card, card.role === "supplement" ? "场景搭配" :
            hasPrimary ? "备选方案" : `方案${index + 2} · 供比较`, false))),
        others.length > 2 ? h("p", { class: "cc-extra" }, `另有 ${others.length - 2} 张卡可在下方回答中查看。`) : null) : null,
      h("div", { class: "cc-apply" }, h("b", null, "申请前请确认"), h("span", null, condition)),
    ] : h("p", { class: "cc-empty" }, pl.summary || "目前还没有可展示的具体卡片。"));
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
  try { return new URL(c.source_url).hostname.replace(/^www\./, ""); }
  catch { return "来源"; }
}

function citeMark(c) {
  if (!c) return null;
  const label = `[${c.n}]`;
  if (!c.source_url) return h("span", { class: "cite-mark" }, label);
  return h("a", { class: "cite-mark", href: c.source_url, target: "_blank", rel: "noopener noreferrer",
    title: citeLabel(c) }, label);
}

function citeRef(c) {
  if (!c) return null;
  const text = `[${c.n}] ${citeLabel(c)}`;
  if (!c.source_url) return h("span", { class: "cite-ref" }, text);
  return h("a", { class: "cite-ref", href: c.source_url, target: "_blank", rel: "noopener noreferrer",
    title: c.source_url }, text);
}

function sourceRefs(citations) {
  const { unique } = buildCiteMap(citations);
  if (!unique.length) return empty();
  return h("div", { class: "cites-list" }, unique.map((c) => h("div", null, citeRef(c))));
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
    row("来源", sourceRefs(pl.citations)));
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
      items.push(h("div", { class: "m err" }, h("p", null, p.error.message),
        h("div", { class: "chips" },
          h("button", { class: "s", onclick: () => {
            S.pending = null;
            send(p.text, p.cid, p.changed, p.confirmedFields, p.expectedProfileVersion, {
              sessionFields: p.sessionFields, clearedFields: p.clearedFields,
            });
          } }, "重试"),
          h("button", { onclick: () => { S.pending = null; drawChat(); } }, "放弃"))));
    } else {
      items.push(h("div", { class: "m thinking" }, "正在回答…"));
    }
  }
  box.replaceChildren(...items);
  box.scrollTop = keep ? scroll : box.scrollHeight;
}

function isCardRecommendationSteps(steps) {
  return steps.some((s) => s.startsWith("年费："))
    && steps.some((s) => /主推荐：|汇丰.+发行的卡/.test(s));
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
      ...sec.lines.map((line) => h("p", null, line))));
  }
  if (!isCardRecommendationSteps(steps)) {
    return [h("div", { class: "sub-h" }, "给您的建议"),
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
    else if (/^(推荐理由|为什么适合你|为什么适合您|年费|主要权益|您可享|你可享|申请限制|申请前提)：/.test(s)) {
      const i = s.indexOf("：");
      box.append(h("div", { class: `card-detail ${/^(?:推荐理由|为什么适合你|为什么适合您)/.test(s) ? "reason" : ""}` },
        h("span", { class: "k" }, s.slice(0, i + 1)), s.slice(i + 1)));
    } else if (s.startsWith("免年费选择：")) box.append(h("div", { class: "card-alt" }, s));
    else if (/（(?:Visa|Mastercard|UnionPay|银联卡|威士卡|万事达卡)/.test(s) || /（[A-Za-z]+）$/.test(s)) {
      box.append(h("div", { class: "card-name" }, s));
    } else box.append(h("div", { class: "card-note" }, s));
  }
  const preview = steps.some((s) => s.startsWith("提前了解："));
  const compare = steps.some((s) => s.startsWith("对比参考："));
  return [h("div", { class: "sub-h" }, preview ? "提前了解的信用卡" :
    compare ? "两张卡怎么选" : "为您挑选的信用卡"), box];
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
    const multi = multiDomainSections(b.answer?.steps || []);
    if (multi.length > 1) {
      if (b.answer?.summary) kids.push(h("p", null, b.answer.summary));
      kids.push(...multi.map((sec) => h("div", { class: "multi-sec" },
        h("div", { class: "sub-h" }, sec.title),
        sec.lines[0] ? h("p", null, sec.lines[0]) : null)));
    }
    kids.push(followUp(question));
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
  const mats = b.answer.materials || [];
  const kids = [];

  if (b.status !== "answered") {
    kids.push(h("div", { class: "status" }, h("span", { class: "st " + st.cls }, st.label), h("span", { class: "why" }, st.why)));
  }
  const stepList = (b.answer.steps || []).filter((s) => !String(s).startsWith("政策原文："));
  const multi = multiDomainSections(stepList);
  const cardAnswer = isCardRecommendationSteps(stepList);
  if (!cardAnswer) {
    kids.push(h("p", null, b.answer.summary));
  }
  // Multi-intent answers already nest domain copy under ## sections; keep materials
  // out of the top so bank docs do not look like a free-floating visa list.
  if (mats.length && multi.length <= 1) {
    kids.push(h("ul", { class: "mat" }, mats.map((m) => materialItem(m, cmap))));
  }
  if (stepList.length) kids.push(...renderAnswerSteps(stepList));
  if ((b.conflicts || []).length) {
    kids.push(h("div", { class: "sub-h" }, "不同来源说法不一致（并列展示，未替您取舍）"));
    b.conflicts.forEach((cf) => kids.push(h("div", null, cf.name_zh), h("div", { class: "conf" }, cf.variants.map((v) =>
      h("div", null, h("b", null, v.source_name), v.required ? "需要" : "不需要", v.condition ? `（${v.condition}）` : "")))));
  }
  if (citeList.length) {
    kids.push(h("details", { class: "evidence" },
      h("summary", null, `查看官方依据（${citeList.length}）`),
      h("div", { class: "evidence-list" }, citeList.map((c) => h("div", null, citeRef(c))))));
  }
  if ((b.answer.limitations || []).length) {
    kids.push(h("div", { class: "sub-h" }, "温馨提示"), h("ul", { class: "lim" }, b.answer.limitations.map((s) => h("li", null, s))));
  }
  if (b.status !== "answered" && (b.next_actions || []).length) {
    kids.push(h("div", { class: "sub-h" }, "下一步"), h("ul", { class: "lim" }, b.next_actions.map((s) => h("li", null, s))));
  }
  // Hide internal/debug notices (routing audit JSON, etc.) from the customer UI.
  const HIDDEN_NOTICE = new Set([
    "intent_routing", "llm_routing_low_confidence", "unsupported_intent", "partial_out_of_scope",
  ]);
  (b.notices || []).filter((n) => n && !HIDDEN_NOTICE.has(n.code) && !(n.message || "").trim().startsWith("{"))
    .forEach((n) => kids.push(h("div", { class: "notice" }, n.message)));
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
  for (let i = S.messages.length - 1; i >= 0; i--) {
    const m = S.messages[i];
    if (m.role !== "assistant" || m.body?.status !== "needs_input") continue;
    const qs = m.body.follow_up_questions || [];
    return qs.find((q) => val(S.profile, q.field) === undefined) || qs[0] || null;
  }
  return null;
}

/** 当前问题之前、档案里已确认且可撤销的追问字段（trail 丢了也能继续上一步）。 */
function undoableFollowFields(currentField) {
  const suppressed = new Set(S.suppressFields || []);
  const trailFields = new Set((S.followTrail || []).map((item) => item.field));
  const idx = FOLLOW_UNDO_ORDER.indexOf(currentField);
  const candidates = idx >= 0 ? FOLLOW_UNDO_ORDER.slice(0, idx) : FOLLOW_UNDO_ORDER.slice();
  return candidates.filter((code) => {
    if (suppressed.has(code) || code === currentField) return false;
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
  if (S.fields[p.field_code]?.sensitivity === "financial") return "已提供（金额不在此处重复显示）";
  return value === null || value === undefined ? "（空）" : valueLabel(p.field_code, value);
}

function proposalBundleCard(proposals) {
  const affected = [...new Set(proposals.flatMap((p) => p.affected_advice_topics || []))]
    .filter((topic) => S.advice[topic]);
  const selected = new Set(affected);
  const choices = affected.length ? h("div", { class: "proposal-advice-choices" },
    h("div", null, "这些现有建议依赖本次修改。请选择现在要重新生成的内容："),
    affected.map((topic) => {
      const input = h("input", { type: "checkbox", checked: true });
      input.addEventListener("change", () => input.checked ? selected.add(topic) : selected.delete(topic));
      return h("label", null, input, ADVICE_TOPIC_LABEL[topic] || topic);
    }),
    h("div", { class: "note" }, "未选择的建议会保留，但会标记为“待更新”，以后可在建议卡上重新生成。")) : null;
  return h("div", { class: "prop" },
    h("div", null, "要把这些信息更新到学生资料吗？"),
    h("ul", { class: "proposal-list" }, proposals.map((p) => h("li", null,
      h("b", null, label(p.field_code)), "　", proposalValue(p, p.old_value), " → ",
      h("b", null, proposalValue(p, p.new_value))))),
    choices,
    h("div", { class: "btns" },
      h("button", { class: "btn sm", onclick: () => decideProposalBundle(
        proposals, true, [...selected]) }, affected.length ? "更新资料并处理建议" : "更新资料"),
      h("button", { class: "btn sm ghost", onclick: () => decideProposalBundle(proposals, false) }, "仅本次使用")));
}

function relationshipProposalCard(p) {
  const market = { "CN-HK": "香港", UK: "英国", US: "美国", CN: "中国内地" }[p.market] || p.market;
  const status = { selected: "准备申请", applying: "申请中", open: "已经开户",
    blocked: "申请受阻", closed: "已经关闭" }[p.proposed_status] || p.proposed_status;
  const actions = p.proposed_status === "selected"
    ? [h("button", { class: "btn sm", onclick: () => decideRelationship(p, true, "selected") }, "记录为准备申请"),
       h("button", { class: "btn sm", onclick: () => decideRelationship(p, true, "open") }, "已经开好了")]
    : [h("button", { class: "btn sm", onclick: () => decideRelationship(p, true, p.proposed_status) }, "记录进度")];
  return h("div", { class: "prop relationship-prop" },
    h("div", null, "要记录这项银行账户进度吗？ ", h("b", null, `${market} · ${p.product_name}`),
      h("span", { class: "rel-proposed" }, status)),
    h("div", { class: "btns" },
      ...actions,
      h("button", { class: "btn sm ghost", onclick: () => decideRelationship(p, false) }, "仅本次讨论")));
}

async function decideRelationship(p, confirm, status) {
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
    toast(e.message, true);
  }
}

async function decideProposalBundle(proposals, confirm, refreshTopics = []) {
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
    toast(e.message, true);
  }
}

function actsRow(b) {
  const st = STATUS[b.status] || STATUS.cannot_answer;
  const state = S.fb[b.message_id] || (S.fb[b.message_id] = {});
  const parts = [h("div", { class: "acts" },
    h("span", { class: "fb" },
      h("span", { class: "st " + st.cls }, st.label), h("span", { class: "sep" }, "｜"),
      h("button", { class: state.kind === "wrong" ? "on" : "", onclick: () => {
        state.open = !state.open;
        drawChat({ keep: true });
      } }, "信息有误"), "·",
      h("button", { onclick: () => { S.el.input.placeholder = "继续补充您的情况或追问…"; S.el.input.focus(); } }, "继续补充")))];
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
  S.pending = { text, cid: retryCid || uuid(), error: null, changed,
    confirmedFields: confirmed, sessionFields, expectedProfileVersion, clearedFields };
  drawChat();
  try {
    const res = await api("POST", `/sessions/${S.conv.session_id || S.conv.conversation_id}/messages`,
      { text, client_message_id: S.pending.cid, confirmed_fields: S.pending.confirmedFields,
        session_fields: S.pending.sessionFields,
        expected_profile_version: S.pending.expectedProfileVersion,
        cleared_fields: S.pending.clearedFields }, { timeout: 90000 });
    S.pending = null;
    await Promise.all([loadMessages(), refreshProfile(), loadAdvice(), loadProposals(),
      loadRelationships(), loadRelationshipProposals()]);
    deriveMissing();
    S.flash = res.advice_updates || [];
    if (res.status && res.status !== "needs_input") {
      S.followTrail = [];
      S.followSeed = null;
      S.suppressFields = [];
    }
    drawSheet();
    drawSide();
    return res;
  } catch (e) {
    if (S.pending) S.pending.error = e;
    return null;
  } finally {
    // busy 必须在重绘前清掉，否则「上一步」会带着 disabled 画进 DOM 后一直点不了。
    S.busy = false;
    if (S.el.sendBtn) S.el.sendBtn.disabled = false;
    drawChat({ keep: true });
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
      toast(`档案已保存（v${S.profile.version}）；受影响的建议已标记待更新。`);
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
