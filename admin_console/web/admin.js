"use strict";

const $app = document.getElementById("app");
const DOMAIN_LABEL = {
  VISA_POLICY: "签证政策", FX_POLICY: "购汇政策", TUITION: "学费", SCHOOL: "学校", PROGRAM: "项目",
  ACCOUNT_THRESHOLD: "开户门槛", CREDIT_CARD: "信用卡",
};
const ACTION_LABEL = { CREATE: "新增", UPDATE: "更新" };
const FIELD_LABEL = {
  annual_quota_amount: "年度购汇额度", annual_quota_currency: "额度币种", quota_reset: "额度重置方式",
  over_quota_allowed: "超过年度额度是否仍可购汇", tuition_direct_to_school: "学费是否直接汇给学校",
  remitter_relation: "代办人关系", can_use_family_quota: "是否可以使用家人的购汇额度",
  effective_from: "生效日期", effective_to: "截止日期", purpose_code: "用途", country: "国家",
  policy_type: "政策类型", title: "标题", remark: "备注", amount: "金额", currency: "币种",
  academic_year: "学年", fee_status: "学生类别", fee_basis: "计费方式", study_mode: "修读方式",
  rate_label: "费率说明", cas_required: "是否需要 CAS", i20_required: "是否需要 I-20",
  tb_test_required: "是否需要肺结核检测", english_level: "英语要求", visa_fee: "签证费",
  applies_to_city: "适用城市",
};
const VALUE_LABEL = {
  immediate_family: "直系亲属（父母、子女、配偶）", close_relatives: "近亲属",
  anyone_authorized: "经授权的他人", self_only: "仅限本人", calendar_year: "自然年",
  tuition: "学费", living: "生活费", study_abroad: "留学购汇", student: "学生签证",
  home: "本地学生", international: "国际学生", local: "本地", non_local: "非本地",
  full_time: "全日制", part_time: "非全日制",
};
const BOOL_FIELDS = new Set([
  "over_quota_allowed", "tuition_direct_to_school", "can_use_family_quota",
  "cas_required", "i20_required", "tb_test_required", "funds_tuition_required",
]);
const HIDDEN_FIELDS = new Set([
  "policy_id", "tuition_id", "program_id", "school_id", "card_id", "threshold_id",
]);

const A = {
  token: sessionStorage.getItem("gp_admin_token"), admin: sessionStorage.getItem("gp_admin_name"),
  domain: "", data: null, detail: null, siblings: [], picks: {}, conflicts: [], note: "", busy: false,
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

function fmtField(key, value) {
  if (BOOL_FIELDS.has(key) && (value === 0 || value === 1 || value === true || value === false)) {
    return value === 1 || value === true ? "是" : "否";
  }
  if (typeof value === "string" && VALUE_LABEL[value]) return VALUE_LABEL[value];
  return fmtVal(value);
}

function humanValue(raw) {
  const text = String(raw).trim().replace(/^['"]|['"]$/g, "");
  return VALUE_LABEL[text] || text;
}

function explainReason(text) {
  if (!text) return "—";
  return text.split(/；|;/).map((part) => {
    const conflict = part.match(/^(?:冲突|跨页面冲突)\s+([a-z_]+)\s*[：:]\s*(.+)$/);
    if (conflict) {
      let values = conflict[2].trim();
      if (values.startsWith("[")) {
        try { values = JSON.parse(values.replace(/'/g, '"')); } catch { values = [values]; }
      } else values = [values];
      const shown = (Array.isArray(values) ? values : [values]).map(humanValue).join("、");
      return `不同来源对「${FIELD_LABEL[conflict[1]] || conflict[1]}」的说法不一致：${shown}。请对照原文网页确认采用哪一种。`;
    }
    const changed = part.match(/^字段变更\s+\d+\s*项[：:]\s*(.+)$/);
    if (changed) {
      const bits = changed[1].split(";").map((bit) => {
        const one = bit.trim().match(/^([a-z_]+):\s*(.*?)\s*(?:→|->)\s*(.*)$/);
        if (!one) return bit.trim();
        return `「${FIELD_LABEL[one[1]] || one[1]}」由 ${humanValue(one[2])} 改为 ${humanValue(one[3])}`;
      }).filter(Boolean);
      return "与已入库内容不同：" + bits.join("；");
    }
    return part.trim();
  }).filter(Boolean).join("；");
}

function toast(msg, bad) {
  const t = h("div", { class: "toast" + (bad ? " bad" : ""), role: bad ? "alert" : "status",
    "aria-live": bad ? "assertive" : "polite" }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), 3600);
}

function userFacingError(error, fallback) {
  const messages = {
    NETWORK: "暂时无法连接服务，请稍后重试。",
    ADMIN_UNAUTHORIZED: "登录已失效，请重新登录。",
    INVALID_CREDENTIALS: "用户名或密码不正确，请检查后重试。",
  };
  const code = error && error.code;
  return Object.prototype.hasOwnProperty.call(messages, code) ? messages[code]
    : (fallback || "操作暂未完成，请稍后重试。");
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
  const right = [];
  if (A.token) {
    right.push(
      h("div", { class: "rm" }, h("div", { class: "av sf" }, (A.admin || "管").slice(0, 1)), A.admin),
      h("button", { class: "linkbtn", onclick: logout }, "退出"));
  }
  return h("div", { class: "top" },
    h("div", { class: "logo" },
      h("div", { class: "mk sf" }, "审"), h("b", { class: "sf" }, "数据审核后台"),
      h("small", null, "留学数据审核")),
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
      errEl.textContent = userFacingError(ex, "登录暂未成功，请检查信息后重试。");
    }
  };
  $app.replaceChildren(topBar(), h("div", { class: "gold" }),
    h("div", { class: "page" }, h("form", { class: "card login", onsubmit: submit },
      h("h1", { class: "sf" }, "管理员登录"),
      h("p", { class: "lead" }, "审核抓取后未能自动入库的数据，决定是否写入正式数据库。"),
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
    if (A.token) toast(userFacingError(e, "审核数据暂时无法加载，请稍后重试。"), true);
    return;
  }
  renderList();
}

function renderList() {
  const d = A.data;
  const domains = new Set([...d.domains, A.domain].filter(Boolean));
  const tabs = h("div", { class: "tabs" },
    h("button", { class: "on" }, "待审核", h("em", null, reviewGroups(d.reviews).length)),
    h("span", { class: "sp" }),
    h("select", { onchange: (e) => { A.domain = e.target.value; load(); } },
      h("option", { value: "", selected: !A.domain }, "全部域表"),
      [...domains].map((key) => h("option", { value: key, selected: A.domain === key }, DOMAIN_LABEL[key] || key))));

  const groups = reviewGroups(d.reviews);
  const body = groups.length ? h("table", { class: "tickets" },
    h("thead", null, h("tr", null,
      h("th", null, "提交时间"), h("th", null, "域表"), h("th", null, "动作"), h("th", null, "内容"),
      h("th", null, "进审核原因"))),
    h("tbody", null, groups.map((members) => {
      const r = members[0];
      return h("tr", { class: "pick", onclick: () => openDetail(members.map((item) => item.review_id)) },
        h("td", { class: "mono" }, fmtTime(r.created_at)),
        h("td", null, DOMAIN_LABEL[r.domain_table] || r.domain_table),
        h("td", null, h("span", { class: "badge " + r.action }, ACTION_LABEL[r.action] || r.action)),
        h("td", { class: "sum" }, h("div", { class: "clip" }, groupSummary(members))),
        h("td", { class: "why" }, h("div", { class: "clip" }, explainReason(r.reason))));
    })))
    : h("div", { class: "empty-state" }, "暂无待审核数据");

  $app.replaceChildren(topBar(), h("div", { class: "gold" }), h("div", { class: "page adm" },
    h("h1", { class: "sf" }, "待审核数据"),
    tabs, body));
  if (A.detail) renderDrawer();
}

// ---------- detail drawer ----------

function reviewGroups(reviews) {
  const groups = [];
  const seen = new Map();
  for (const review of reviews) {
    if (!review.group_key) {
      groups.push([review]);
      continue;
    }
    if (!seen.has(review.group_key)) {
      const bucket = [];
      seen.set(review.group_key, bucket);
      groups.push(bucket);
    }
    seen.get(review.group_key).push(review);
  }
  return groups;
}

function groupSummary(members) {
  if (members.length === 1) return members[0].summary || "未命名内容";
  const labels = members.map((review) => {
    const summary = review.summary || "";
    return summary.includes("：") ? summary.split("：").pop() : (VALUE_LABEL[review.purpose] || summary);
  });
  const head = (members[0].summary || "留学购汇").split("：")[0];
  return `${head}：${[...new Set(labels)].join("、")}`;
}

function conflictChoices(evidence) {
  const byField = new Map();
  for (const fact of evidence || []) {
    if (!fact.field || fact.value === undefined || fact.value === null) continue;
    const key = JSON.stringify(fact.value);
    if (!byField.has(fact.field)) byField.set(fact.field, new Map());
    const values = byField.get(fact.field);
    if (!values.has(key)) values.set(key, { value: fact.value, quotes: [], sources: [] });
    const choice = values.get(key);
    const quote = fact.evidence_quote || fact.quote;
    if (quote && !choice.quotes.includes(quote)) choice.quotes.push(quote);
    const marker = (fact.source_url || "") + "\n" + (fact.source_name || "");
    if (marker.trim() && !choice.sources.some((source) => source.marker === marker)) {
      choice.sources.push({ marker, url: fact.source_url, name: fact.source_name });
    }
  }
  return [...byField].filter(([, values]) => values.size > 1)
    .map(([field, values]) => ({ field, choices: [...values.values()] }));
}

async function openDetail(ids) {
  const list = Array.isArray(ids) ? ids : [ids];
  try {
    const reviews = [];
    for (const id of list) reviews.push(await api("GET", "/reviews/" + encodeURIComponent(id)));
    A.detail = reviews[0];
    A.siblings = reviews.slice(1);
    A.picks = {};
    A.note = "";
    A.conflicts = conflictChoices(reviews[0].evidence);
  } catch (e) {
    toast(userFacingError(e, "审核详情暂时无法打开，请稍后重试。"), true);
    return;
  }
  renderDrawer();
}

function closeDrawer() {
  A.detail = null;
  document.querySelectorAll(".scrim,.drawer").forEach((el) => el.remove());
}

function fieldTable(obj, current, skip) {
  const hidden = new Set([...(skip || []), ...HIDDEN_FIELDS]);
  const keys = Object.keys(obj).filter((k) => !hidden.has(k));
  if (!keys.length) return h("div", { class: "note" }, "无字段变更");
  return h("table", { class: "fields" }, h("tbody", null, keys.map((k) => {
    const changed = current && k in current && fmtField(k, current[k]) !== fmtField(k, obj[k]);
    return h("tr", null, h("td", null, FIELD_LABEL[k] || k), h("td", { class: changed ? "chg" : null },
      fmtField(k, obj[k]), changed ? h("div", { class: "note" }, "库内现值：" + fmtField(k, current[k])) : null));
  })));
}

function docList(docs, gone) {
  return h("ul", { class: "docs" + (gone ? " gone" : "") }, docs.map((doc) => h("li", null,
    h("b", null, doc.name_zh || doc.code), doc.required === false ? "（视情况）" : "",
    doc.still_quoted ? h("span", { class: "q" }, "注意：原引用仍在页面上，可能是漏抽") : null,
    doc.evidence_quote ? h("span", { class: "q" }, "“" + doc.evidence_quote + "”") : null,
    doc.condition_quote ? h("span", { class: "q" }, "条件：" + doc.condition_quote) : null)));
}

function sourceLinksView(reviews) {
  const sources = new Map();
  const add = (url, name) => {
    if (typeof url !== "string" || !/^https?:\/\//i.test(url) || sources.has(url)) return;
    let label = name;
    if (!label) {
      try { label = new URL(url).hostname; } catch (_) { label = "查看原文"; }
    }
    sources.set(url, label);
  };
  for (const review of reviews) {
    for (const evidence of review.evidence || []) add(evidence.source_url, evidence.source_name);
    const proposed = review.proposed_value || {};
    add(proposed.source_url, proposed.source_name);
    add((proposed.tuition || {}).source_url, proposed.school_key || proposed.program_key);
    add((proposed.policy || {}).source_url, (proposed.policy || {}).title);
  }
  if (!sources.size) return [];
  return [h("div", { class: "sec" }, "政策原文"),
    h("div", { class: "source-links" }, [...sources].map(([url, name]) =>
      h("a", { href: url, target: "_blank", rel: "noopener noreferrer", title: url },
        h("span", null, name), h("span", { class: "external", "aria-hidden": "true" }, "↗"))))];
}

function proposedView(r) {
  const p = r.proposed_value || {};
  const parts = [];
  if (p.tuition) {
    parts.push(h("div", { class: "sec" }, "拟写入学费"),
      h("div", { class: "note" }, `学校 ${p.school_key || "—"} · 项目 ${p.program_key || "—"}`),
      fieldTable(p.tuition, r.current));
  } else if (p.policy) {
    const conflictFields = (A.conflicts || []).map((conflict) => conflict.field);
    const unchanged = fieldTable(p.policy, r.current, ["policy_id", "country", "policy_type", "title", "purpose_code", ...conflictFields]);
    if (Object.keys(p.policy).some((key) => !["policy_id", "country", "policy_type", "title", "purpose_code", ...conflictFields].includes(key))) {
      parts.push(h("div", { class: "sec" }, "一并写入的其他内容"), unchanged);
    }
    if (p.documents && p.documents.length) parts.push(h("div", { class: "sec" }, "新增 / 变更材料"), docList(p.documents));
    if (p.removed_documents && p.removed_documents.length) {
      parts.push(h("div", { class: "sec" }, "批准后移除的材料"), docList(p.removed_documents, true));
    }
  } else {
    parts.push(h("div", { class: "sec" }, "拟写入内容"), h("pre", { class: "raw" }, JSON.stringify(p, null, 2)));
  }
  return parts;
}

const CHOICE_TERMS = {
  immediate_family: ["直系亲属", "父母", "配偶", "子女"],
  close_relatives: ["近亲属", "兄弟姐妹", "祖父母"],
  anyone_authorized: ["他人", "授权委托书", "授权"],
  self_only: ["本人"],
};

function highlightTerms(text, terms) {
  const sorted = [...terms].filter((term) => text.includes(term)).sort((a, b) => b.length - a.length);
  if (!sorted.length) return text;
  const nodes = [];
  let i = 0;
  while (i < text.length) {
    const hit = sorted.find((term) => text.startsWith(term, i));
    if (hit) {
      nodes.push(h("mark", null, hit));
      i += hit.length;
    } else {
      let j = i + 1;
      while (j < text.length && !sorted.some((term) => text.startsWith(term, j))) j++;
      nodes.push(text.slice(i, j));
      i = j;
    }
  }
  return nodes;
}

function choiceTerms(field, choice, others) {
  const terms = CHOICE_TERMS[choice.value] || [];
  const elsewhere = others.flatMap((item) => [fmtField(field, item.value), ...item.quotes]).join("\n");
  const inQuote = terms.filter((term) => choice.quotes.some((text) => text.includes(term)) && !elsewhere.includes(term));
  const pool = inQuote.length ? inQuote : terms.filter((term) => fmtField(field, choice.value).includes(term));
  pool.sort((a, b) => b.length - a.length);
  return pool.length ? [pool[0]] : [];
}

function conflictView(conflicts) {
  if (!conflicts.length) return [];
  return [h("div", { class: "sec" }, "请选定一种说法"),
    conflicts.map((conflict) => h("div", { class: "conflict" },
      h("h4", null, FIELD_LABEL[conflict.field] || conflict.field),
      h("div", { class: "cols" + (conflict.choices.length > 2 ? " many" : ""), style: conflict.choices.length > 2 ? `grid-template-columns:repeat(${conflict.choices.length},minmax(0,1fr))` : "" },
        conflict.choices.map((choice) => {
          const selected = JSON.stringify(A.picks[conflict.field]) === JSON.stringify(choice.value);
          const terms = choiceTerms(conflict.field, choice, conflict.choices.filter((item) => item !== choice));
          const label = fmtField(conflict.field, choice.value);
          const quote = choice.quotes.find((text) => terms.some((term) => text.includes(term))) || choice.quotes[0];
          return h("div", { class: "choice" + (selected ? " on" : "") },
            h("div", { class: "choice-h" },
              h("b", null, highlightTerms(label, terms)),
              h("button", { class: "btn" + (selected ? "" : " ghost"), type: "button", onclick: () => {
                A.picks[conflict.field] = choice.value;
                renderDrawer();
              } }, selected ? "已采用" : "采用这个说法")),
            quote ? h("blockquote", null, highlightTerms(quote, terms)) : null,
            choice.sources.map((source) => source.url
              ? h("a", { href: source.url, target: "_blank", rel: "noopener noreferrer" }, source.name || source.url)
              : (source.name || null)));
        }))))];
}

function decideView(r) {
  const note = h("textarea", {
    placeholder: "审核备注（驳回必填，批准可选）", maxlength: "500",
    oninput: (e) => { A.note = e.target.value; },
  });
  note.value = A.note || "";
  const ids = [r.review_id, ...A.siblings.map((item) => item.review_id)];
  const decide = async (verb) => {
    const text = note.value.trim();
    if (verb === "reject" && !text) { toast("驳回必须填写原因", true); note.focus(); return; }
    const missing = (A.conflicts || []).filter((conflict) => !(conflict.field in A.picks));
    if (verb === "approve" && missing.length) {
      toast("请先选定" + missing.map((conflict) => FIELD_LABEL[conflict.field] || conflict.field).join("、"), true);
      return;
    }
    const scope = ids.length > 1 ? "学费和生活费会一起写入。" : "";
    if (verb === "approve" && !confirm("确认把选定的说法写入正式数据库？" + scope)) return;
    if (A.busy) return;
    A.busy = true;
    try {
      for (const id of ids) {
        await api("POST", `/reviews/${encodeURIComponent(id)}/${verb}`,
          { note: text || null, ...(verb === "approve" ? { selections: A.picks } : {}) });
      }
      closeDrawer();
      toast(verb === "approve" ? "已按选定说法写入正式库" : "已驳回，数据未入库");
      await load();
    } catch (e) {
      toast(userFacingError(e, "操作暂未完成，请稍后重试。"), true);
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
  const members = [r, ...A.siblings];
  const kv = [["类型", DOMAIN_LABEL[r.domain_table] || r.domain_table], ["动作", ACTION_LABEL[r.action] || r.action],
    ["提交时间", fmtTime(r.created_at)]];
  document.body.append(
    h("div", { class: "scrim", onclick: closeDrawer }),
    h("div", { class: "drawer wide" + ((A.conflicts || []).length ? " compare" : "") },
      h("button", { class: "close", onclick: closeDrawer }, "×"),
      h("h3", { class: "sf" }, groupSummary(members.map((item) => ({
        summary: item.summary,
        purpose: ((item.proposed_value || {}).policy || {}).purpose_code,
      })))),
      h("div", { class: "kv" }, kv.map(([k, v]) => [h("span", null, k), h("span", null, v)])),
      h("div", { class: "sec" }, "为什么需要人工确认"), h("blockquote", null, explainReason(r.reason)),
      sourceLinksView(members),
      conflictView(A.conflicts || []),
      proposedView(r),
      r.current ? [h("div", { class: "sec" }, "目前库里的内容"), fieldTable(r.current)]
        : h("div", { class: "note" }, "库内尚无该记录，批准后新增。"),
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
