/* 数据建模中心 MVP — 前端工作台（原生 JS 单页应用）
 *
 * 视图：工作台 / 业务需求 / 模型中心 / 运行事件
 * 模型编辑器：基本信息 / 字段设计 / 模型关系 / 版本与差异 / DDL 预览
 * 所有写操作走后端强制校验；前端只改善体验。
 */

"use strict";

// ---------------------------------------------------------------- 基础工具

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function fmtTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

const LAYERS = ["ODS", "DWD", "DWS", "ADS", "DIM"];
const MODEL_TYPES = ["TABLE", "FACT", "DIMENSION", "VIEW"];
const ENGINES = ["postgresql", "mysql", "hive"];
const LOGICAL_TYPES = ["STRING", "VARCHAR", "INTEGER", "BIGINT", "DECIMAL", "DATE", "TIMESTAMP", "BOOLEAN"];
const SENSITIVITIES = ["public", "internal", "sensitive", "restricted"];
const REL_TYPES = ["depends_on", "joins_to", "derived_from", "references"];
const CARDINALITIES = ["unspecified", "one_to_one", "one_to_many", "many_to_one", "many_to_many"];
const PRIORITIES = ["P0", "P1", "P2", "P3"];
const REQ_STATUSES = ["draft", "in_analysis", "modeled", "accepted", "rejected"];

const STATUS_LABEL = {
  draft: "草稿", in_review: "待评审", approved: "已通过", published: "已发布",
};
const REQ_STATUS_LABEL = {
  draft: "草稿", in_analysis: "调研中", modeled: "已建模", accepted: "已验收", rejected: "已拒绝",
};
const REL_TYPE_LABEL = {
  depends_on: "依赖", joins_to: "关联", derived_from: "派生", references: "引用",
};

// V0.8 表级约束与索引
const CONSTRAINT_TYPES = ["primary_key", "unique", "check", "foreign_key"];
const CONSTRAINT_TYPE_LABEL = {
  primary_key: "主键", unique: "唯一", check: "校验", foreign_key: "外键",
};
const ON_DELETE_ACTIONS = ["NO ACTION", "RESTRICT", "CASCADE", "SET NULL"];
const INDEX_TYPES = ["btree", "hash", "gin"];

// ---------------------------------------------------------------- API

// V1.2：api_key 模式登录态（localStorage 存令牌；demo 模式不使用，
// 后端中间件直接放行，X-Actor 仅作演示身份标记）。
const TOKEN_KEY = "dmc_token";

function getToken() { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; } }
function setToken(t) { try { localStorage.setItem(TOKEN_KEY, String(t).trim()); } catch (e) { /* 隐私模式 */ } }
function clearToken() { try { localStorage.removeItem(TOKEN_KEY); } catch (e) { /* 忽略 */ } }

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", "X-Actor": "demo.user" };
  const token = getToken();
  if (token) headers["Authorization"] = "Bearer " + token;
  if (options.headers) Object.assign(headers, options.headers);
  let resp;
  try {
    resp = await fetch(path, { ...options, headers });
  } catch (e) {
    throw { code: "NETWORK_ERROR", message: "网络请求失败：" + e.message, request_id: "" };
  }
  let data = null;
  try { data = await resp.json(); } catch (e) { /* 非 JSON */ }
  if (!resp.ok) {
    const err = (data && data.error) || {};
    throw {
      code: err.code || "HTTP_" + resp.status,
      message: err.message || ("请求失败（" + resp.status + "）"),
      details: err.details || {},
      request_id: (data && data.request_id) || resp.headers.get("X-Request-ID") || "",
    };
  }
  return data;
}

function toast(message, type, code) {
  const box = $("#toast-container");
  const el = document.createElement("div");
  el.className = "toast " + (type || "");
  el.innerHTML = escapeHtml(message) +
    (code ? `<span class="t-code">${escapeHtml(code)}</span>` : "");
  box.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 4200);
  setTimeout(() => el.remove(), 4700);
}

function showError(e) {
  console.error(e);
  toast((e.message || "未知错误") + (e.request_id ? `（request_id: ${e.request_id}）` : ""), "error", e.code);
}

// ---------------------------------------------------------------- 弹窗

let modalSubmitHandler = null;

function openModal(title, bodyHtml, footerButtons) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = bodyHtml;
  const footer = $("#modal-footer");
  footer.innerHTML = "";
  (footerButtons || []).forEach((b) => {
    const btn = document.createElement("button");
    btn.className = "btn " + (b.cls || "");
    if (b.id) btn.id = b.id;
    btn.textContent = b.label;
    btn.onclick = () => b.onClick();
    footer.appendChild(btn);
  });
  $("#modal-mask").hidden = false;
}

function closeModal() {
  $("#modal-mask").hidden = true;
  modalSubmitHandler = null;
}

$("#modal-close").addEventListener("click", closeModal);
$("#modal-mask").addEventListener("click", (e) => { if (e.target.id === "modal-mask") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

/** 通用表单弹窗：fields = [{key,label,type,value,options,required,placeholder}] */
function formModal(title, fields, submitLabel, onSubmit) {
  const rows = fields.map((f) => {
    let input;
    if (f.type === "select") {
      input = `<select class="select" data-key="${f.key}">` +
        (f.options || []).map((o) =>
          `<option value="${escapeHtml(o.value != null ? o.value : o)}" ${String(f.value) === String(o.value != null ? o.value : o) ? "selected" : ""}>${escapeHtml(o.label != null ? o.label : o)}</option>`
        ).join("") + `</select>`;
    } else if (f.type === "textarea") {
      input = `<textarea class="textarea" data-key="${f.key}" placeholder="${escapeHtml(f.placeholder || "")}">${escapeHtml(f.value || "")}</textarea>`;
    } else {
      input = `<input class="input" type="${f.type || "text"}" data-key="${f.key}" value="${escapeHtml(f.value == null ? "" : f.value)}" placeholder="${escapeHtml(f.placeholder || "")}" ${f.required ? "required" : ""} />`;
    }
    return `<div class="form-row"><label>${escapeHtml(f.label)}${f.required ? ' <span style="color:var(--danger)">*</span>' : ""}</label>${input}</div>`;
  }).join("");
  openModal(title, `<form id="modal-form" onsubmit="return false">${rows}</form>`, [
    { label: "取消", cls: "btn-ghost", onClick: closeModal },
    {
      label: submitLabel, cls: "btn-primary", onClick: async () => {
        const values = {};
        let ok = true;
        $$("#modal-form [data-key]").forEach((el) => {
          const v = el.value.trim();
          if (el.hasAttribute("required") && !v) { ok = false; el.style.borderColor = "var(--danger)"; }
          else el.style.borderColor = "";
          values[el.dataset.key] = v;
        });
        if (!ok) { toast("请填写必填项", "error"); return; }
        await onSubmit(values);
      },
    },
  ]);
}

// ---------------------------------------------------------------- 全局状态

const state = {
  view: "dashboard",
  projects: [],
  projectId: null,
  subjectAreas: [],
  requirements: [],
  models: [],
  recentModels: [],  // 最近访问的模型 id（U7，按项目 localStorage 持久化，最多 8 条）
  editor: null, // { model, tab, fields(本地编辑副本), dirty }
  // 模型关系图（V0.9 视图切换 → V1.2 Neo4j 画布 / neovis.js）
  graphView: "current",
  graphSnapshots: [],
  graphSnapshotNo: null,
  graphNodes: [],
  graphEdges: [],
  graphExternalEdges: [],
  graphPayload: null,
  graphViz: null,            // 当前 neovis 实例（Neo4j 画布）
  graphLayoutMode: "force",  // 画布布局：force（力导向）| layered（分层 LR）
  graphSelectedNode: null,   // 点击选中的模型 vis id（详情卡）
  graphSelectedEdge: null,   // 点击选中的边 vis id
  graphHoverNode: null,      // hover 中的模型 vis id（瞬时高亮）
  graphHoverEdge: null,      // hover 中的边 vis id（瞬时高亮）
  graphPathFocus: null,      // {nodes:Set, edges:Set(edge_key)} 最短路径查询聚焦
  pathRows: [],              // 路径查询结果行（节点步进，供分页渲染）
  pathPage: 1,               // 路径结果当前页（1 起）
  pathPageSize: 10,          // 路径结果每页条数（0 = 全部）
  graphShowAllLabels: false, // 边标签常显开关
  graphApplyHighlight: null, // 当前图谱实例的高亮刷新函数（路径查询回调）
  graphCleanup: null,        // 离开图谱视图时的清理
  graphEdgeKeyToVisId: {},   // edge_key → vis 边 id（路径查询高亮映射）
  graphEdgeById: {},         // vis 边 id → {edge, external}（边详情回查）
  // 用户与权限（V1.2 RBAC）
  me: null,                  // /api/auth/me：身份 + 有效能力（capabilities）
  users: [],                 // 平台用户列表（admin）
  usersFilter: { q: "", status: "" },
  members: [],               // 当前项目成员（owner/admin 管理）
  keys: [],                  // API Key 列表（本人或 admin 代查）
  busMatrix: null,           // 业务总线矩阵：{mode:"overview"|"area", data}
};

// 调试 / E2E 钩子：暴露全局状态与图谱实例（只读用途）
window.state = state;

const VIEW_TITLES = {
  dashboard: "工作台", requirements: "业务需求", models: "模型中心", graph: "模型关系图", events: "运行事件", users: "用户与权限",
};

// ---------------------------------------------------------------- 数据加载

async function loadProjects() {
  state.projects = await api("/api/projects");
  const sel = $("#project-select");
  sel.innerHTML = state.projects.map((p) =>
    `<option value="${p.id}">${escapeHtml(p.project_key)} · ${escapeHtml(p.name)}</option>`
  ).join("");
  if (!state.projects.length) {
    state.projectId = null;
    return;
  }
  if (!state.projectId || !state.projects.some((p) => p.id === state.projectId)) {
    state.projectId = state.projects[0].id;
  }
  sel.value = String(state.projectId);
}

async function loadProjectContext() {
  if (!state.projectId) { state.subjectAreas = []; state.requirements = []; state.models = []; return; }
  const [areas, reqs, models] = await Promise.all([
    api(`/api/projects/${state.projectId}/subject-areas`),
    api(`/api/projects/${state.projectId}/requirements`),
    api(`/api/projects/${state.projectId}/models`),
  ]);
  state.subjectAreas = areas;
  state.requirements = reqs;
  state.models = models;
  loadRecentModels();
}

// ---- 最近访问（U7）：按项目记忆，localStorage 持久化，最多 8 条 ----

function loadRecentModels() {
  try {
    const raw = localStorage.getItem("mdl_recent_" + state.projectId);
    state.recentModels = raw ? JSON.parse(raw) : [];
  } catch (e) { state.recentModels = []; }
}

function pushRecentModel(id) {
  if (!state.projectId) return;
  state.recentModels = [id, ...state.recentModels.filter((x) => x !== id)].slice(0, 8);
  try {
    localStorage.setItem("mdl_recent_" + state.projectId, JSON.stringify(state.recentModels));
  } catch (e) { /* 隐私模式等场景下静默失败 */ }
}

// ---------------------------------------------------------------- 视图框架

function setView(view) {
  state.view = view;
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $("#view-title").textContent = VIEW_TITLES[view] || view;
  const proj = state.projects.find((p) => p.id === state.projectId);
  $("#view-meta").textContent = proj ? `${proj.project_key} · ${proj.name}` : "";
  renderView();
}

async function renderView() {
  // 离开图谱视图时销毁画布实例（元素已随 innerHTML 销毁，监听随 network.destroy 释放）
  if (state.graphCleanup) { state.graphCleanup(); state.graphCleanup = null; }
  destroyGraphViz();
  const content = $("#content");
  content.innerHTML = `<div class="empty"><div class="empty-icon">◌</div><div class="empty-title">加载中…</div></div>`;
  // 用户与权限是平台级视图：无项目时仍可管理用户/密钥（成员卡除外）
  if (!state.projectId && state.view !== "users") {
    content.innerHTML = `
      <div class="empty">
        <div class="empty-icon">▤</div>
        <div class="empty-title">还没有项目</div>
        <div class="empty-desc">创建第一个项目，开始数据建模工作。</div>
        <div class="mt14"><button class="btn btn-primary" id="empty-new-project">+ 新建项目</button></div>
      </div>`;
    $("#empty-new-project").onclick = newProjectModal;
    return;
  }
  try {
    if (state.view === "dashboard") await renderDashboard(content);
    else if (state.view === "requirements") await renderRequirements(content);
    else if (state.view === "models") await renderModels(content);
    else if (state.view === "graph") await renderGraphView(content);
    else if (state.view === "events") await renderEvents(content);
    else if (state.view === "users") await renderUsers(content);
  } catch (e) { showError(e); }
}

// ---------------------------------------------------------------- 工作台

async function renderDashboard(content) {
  await loadProjectContext();
  const d = await api(`/api/dashboard/${state.projectId}`);
  const byStatus = d.models.by_status || {};
  const reqByStatus = d.requirements.by_status || {};
  const areaRows = aggModelsByArea();
  const layerRows = aggModelsByLayer();
  const statusRows = aggModelsByStatus();
  content.innerHTML = `
    <div class="grid grid-4">
      <div class="stat-card"><div class="stat-label">模型总数</div><div class="stat-value accent">${d.models.total}</div><div class="stat-foot">草稿 ${byStatus.draft || 0} · 待评审 ${byStatus.in_review || 0}</div></div>
      <div class="stat-card"><div class="stat-label">已发布模型</div><div class="stat-value success">${byStatus.published || 0}</div><div class="stat-foot">正式版本 ${d.published_versions} 个</div></div>
      <div class="stat-card"><div class="stat-label">业务需求</div><div class="stat-value">${d.requirements.total}</div><div class="stat-foot">调研中 ${reqByStatus.in_analysis || 0} · 已建模 ${reqByStatus.modeled || 0}</div></div>
      <div class="stat-card"><div class="stat-label">模型关系</div><div class="stat-value warning">${d.relationships}</div><div class="stat-foot">依赖 / 关联 / 派生 / 引用</div></div>
    </div>
    <div class="grid grid-3 mt20">
      <div class="card chart-card">
        <div class="card-title">各业务域模型分布 <span class="sub">按数量排序</span></div>
        ${areaRows.length ? svgBarChartH(areaRows, "chart-area-bar") : chartEmpty("暂无模型数据")}
      </div>
      <div class="card chart-card">
        <div class="card-title">数仓分层占比 <span class="sub">ODS → ADS</span></div>
        ${layerRows.length ? svgDonutChart(layerRows, "chart-layer-donut") : chartEmpty("暂无模型数据")}
      </div>
      <div class="card chart-card">
        <div class="card-title">模型状态构成 <span class="sub">生命周期</span></div>
        ${statusRows.length ? svgStackedBar(statusRows, "chart-status-stack") : chartEmpty("暂无模型数据")}
      </div>
    </div>
    <div class="grid grid-2 mt20">
      <div class="card">
        <div class="card-title">最近模型 <span class="sub">点击进入编辑器</span></div>
        ${state.models.length ? `<div class="table-wrap"><table class="data"><thead><tr><th>模型</th><th>分层</th><th>状态</th><th>版本</th></tr></thead><tbody>
          ${state.models.slice(-8).reverse().map((m) => `
            <tr>
              <td><span class="link" data-open-model="${m.id}">${escapeHtml(m.name)}</span><div class="small muted mono">${escapeHtml(m.model_key)}</div></td>
              <td>${escapeHtml(m.layer)}</td>
              <td><span class="badge badge-${m.status}">${STATUS_LABEL[m.status] || m.status}</span></td>
              <td class="mono">${m.published_version_no ? "V" + m.published_version_no : "R" + m.edit_revision}</td>
            </tr>`).join("")}
        </tbody></table></div>` : `<div class="empty"><div class="empty-desc">暂无模型，去模型中心创建。</div></div>`}
      </div>
      <div class="card">
        <div class="card-title">最近动态</div>
        ${d.recent_events.length ? `<div class="timeline">
          ${d.recent_events.map((e) => `
            <div class="timeline-item">
              <span class="t-type">${escapeHtml(e.event_type)}</span>
              <span class="t-time">${fmtTime(e.created_at)}</span>
              <div class="t-detail">${escapeHtml(JSON.stringify(e.detail))}</div>
            </div>`).join("")}
        </div>` : `<div class="empty"><div class="empty-desc">暂无动态。</div></div>`}
      </div>
    </div>`;
  $$("[data-open-model]", content).forEach((el) =>
    el.addEventListener("click", () => openModelEditor(Number(el.dataset.openModel))));
}

// ---------------------------------------------------------------- 工作台图表
// 原生 SVG 手绘，不引外部依赖；分层/状态配色复用图谱引擎的 LAYER_COLORS /
// STATUS_COLORS（hex，canvas 与 SVG 通用），保证两处颜色语义一致。
// 数据源：state.models（loadProjectContext 已加载）+ state.subjectAreas。

const CHART_PALETTE = [
  "#4f8cff", "#35c98d", "#f0a63c", "#58b6e8",
  "#c084fc", "#f06292", "#4db6ac", "#ffb74d", "#7986cb", "#ba68c8",
];

function aggModelsByArea() {
  const counts = new Map();
  for (const a of state.subjectAreas) counts.set(a.name, 0);
  for (const m of state.models) {
    const area = state.subjectAreas.find((a) => a.id === m.subject_area_id);
    const key = area ? area.name : "未归属";
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return [...counts.entries()]
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name, "zh"));
}

function aggModelsByLayer() {
  const counts = new Map();
  for (const m of state.models) counts.set(m.layer, (counts.get(m.layer) || 0) + 1);
  const order = [...LAYERS, ...[...counts.keys()].filter((l) => !LAYERS.includes(l))];
  return order.filter((l) => counts.has(l))
    .map((layer) => ({ name: layer, count: counts.get(layer) }));
}

function aggModelsByStatus() {
  const order = ["draft", "in_review", "approved", "published"];
  const counts = new Map();
  for (const m of state.models) counts.set(m.status, (counts.get(m.status) || 0) + 1);
  return order.filter((s) => counts.has(s))
    .map((status) => ({ name: status, label: STATUS_LABEL[status] || status, count: counts.get(status) }));
}

function chartEmpty(text) {
  return `<div class="empty chart-empty"><div class="empty-icon">▤</div><div class="empty-desc">${escapeHtml(text)}</div></div>`;
}

/** 横向柱状图：label | 轨道+柱 | 数值 */
function svgBarChartH(items, svgId) {
  const rowH = 30, labelW = 92, trackW = 268, valW = 30, padTop = 6;
  const W = labelW + trackW + valW + 12;
  const H = items.length * rowH + padTop * 2;
  const max = Math.max(...items.map((i) => i.count), 1);
  const rows = items.map((it, i) => {
    const y = padTop + i * rowH;
    const w = Math.max(4, (trackW * it.count) / max);
    const color = CHART_PALETTE[i % CHART_PALETTE.length];
    const short = it.name.length > 6 ? it.name.slice(0, 6) + "…" : it.name;
    return `
      <g>
        <title>${escapeHtml(it.name)}：${it.count} 个模型</title>
        <text x="0" y="${y + rowH / 2 + 4}" class="chart-label">${escapeHtml(short)}</text>
        <rect x="${labelW}" y="${y + (rowH - 14) / 2}" width="${trackW}" height="14" rx="7" class="chart-track" />
        <rect x="${labelW}" y="${y + (rowH - 14) / 2}" width="${w.toFixed(1)}" height="14" rx="7"
              fill="${color}" class="chart-bar-anim" style="animation-delay:${(i * 60).toFixed(0)}ms" />
        <text x="${labelW + trackW + 8}" y="${y + rowH / 2 + 4}" class="chart-value">${it.count}</text>
      </g>`;
  }).join("");
  return `<svg id="${svgId}" class="chart-svg" viewBox="0 0 ${W} ${H}" role="img"
      preserveAspectRatio="xMinYMin meet" aria-label="各业务域模型分布柱状图">${rows}</svg>`;
}

/** 环形图：分段圆环 + 中心总数；图例由 HTML 渲染 */
function svgDonutChart(items, svgId) {
  const total = items.reduce((s, i) => s + i.count, 0);
  const cx = 92, cy = 92, r = 62, sw = 22;
  const C = 2 * Math.PI * r;
  let acc = 0;
  const segs = total > 0 ? items.map((it, i) => {
    const len = (C * it.count) / total;
    const color = LAYER_COLORS[it.name] || CHART_PALETTE[i % CHART_PALETTE.length];
    const dash = Math.max(0, len - (items.length > 1 ? 2 : 0));
    const s = `<circle class="chart-donut-seg" cx="${cx}" cy="${cy}" r="${r}" fill="none"
      stroke="${color}" stroke-width="${sw}"
      stroke-dasharray="${dash.toFixed(2)} ${(C - dash).toFixed(2)}"
      stroke-dashoffset="${(-acc).toFixed(2)}"
      transform="rotate(-90 ${cx} ${cy})">
      <title>${escapeHtml(it.name)}：${it.count} 个（${((it.count / total) * 100).toFixed(1)}%）</title>
    </circle>`;
    acc += len;
    return s;
  }).join("") : `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" class="chart-donut-track" stroke-width="${sw}" />`;
  const center = `
    <text x="${cx}" y="${cy - 2}" class="chart-donut-num">${total}</text>
    <text x="${cx}" y="${cy + 18}" class="chart-donut-sub">模型总数</text>`;
  const legend = `<div class="chart-legend">${items.map((it, i) => {
    const color = LAYER_COLORS[it.name] || CHART_PALETTE[i % CHART_PALETTE.length];
    const pct = total ? ((it.count / total) * 100).toFixed(1) : "0.0";
    return `<div class="chart-legend-row">
      <span class="chart-legend-dot" style="background:${color}"></span>
      <span>${escapeHtml(it.name)}</span>
      <span class="chart-legend-count">${it.count} · ${pct}%</span>
    </div>`;
  }).join("")}</div>`;
  return `<div class="chart-donut-wrap">
    <svg id="${svgId}" class="chart-svg chart-donut-svg" viewBox="0 0 ${cx * 2} ${cy * 2}" role="img"
         preserveAspectRatio="xMidYMid meet" aria-label="数仓分层模型占比环形图">${segs}${center}</svg>
    ${legend}</div>`;
}

/** 单条堆叠条形图 + 图例（模型状态构成） */
function svgStackedBar(items, svgId) {
  const total = items.reduce((s, i) => s + i.count, 0) || 1;
  const W = 460, H = 26;
  let x = 0;
  const segs = items.map((it) => {
    const w = (W * it.count) / total;
    const color = STATUS_COLORS[it.name] || "var(--text-faint)";
    const s = `<rect x="${x.toFixed(2)}" y="0" width="${Math.max(0, w - 2).toFixed(2)}" height="${H}" rx="6"
      fill="${color}" class="chart-bar-anim">
      <title>${escapeHtml(it.label || it.name)}：${it.count} 个（${((it.count / total) * 100).toFixed(1)}%）</title>
    </rect>`;
    x += w;
    return s;
  }).join("");
  const legend = `<div class="chart-legend">${items.map((it) => {
    const color = STATUS_COLORS[it.name] || "var(--text-faint)";
    const pct = ((it.count / total) * 100).toFixed(1);
    return `<div class="chart-legend-row">
      <span class="chart-legend-dot" style="background:${color}"></span>
      <span>${escapeHtml(it.label || it.name)}</span>
      <span class="chart-legend-count">${it.count} · ${pct}%</span>
    </div>`;
  }).join("")}</div>`;
  return `<div class="chart-stack-wrap">
    <svg id="${svgId}" class="chart-svg" viewBox="0 0 ${W} ${H}" role="img"
         preserveAspectRatio="xMinYMin meet" aria-label="模型状态构成堆叠图">${segs}</svg>
    ${legend}</div>`;
}

// ---------------------------------------------------------------- 业务总线矩阵（V1.3）
// Kimball 总线矩阵：行 = 业务过程（DWD/DWS 模型），列 = 维度（DIM 模型），
// 单元格 = 过程与维度的直连关系。默认展示企业级总览（主题域 × 维度热力图 +
// 一致性维度榜），点击主题域下钻域内矩阵（过程 × 维度 + 字段映射明细 +
// 本域/外域维度复用对比）。

const BM_REL_COLORS = {
  joins_to: "#4f8cff", derived_from: "#35c98d", depends_on: "#f0a63c", references: "#8d99b5",
};
const BM_HEAT_STEPS = 4;

function bmShortDimName(name) {
  const short = String(name || "").replace(/维表$/, "");
  return short.length > 4 ? short.slice(0, 4) : short;
}

async function renderBusMatrixCard() {
  const box = $("#bus-matrix");
  if (!box) return;
  box.innerHTML = `<div class="empty chart-empty"><div class="empty-icon">◌</div><div class="empty-desc">总线矩阵推导中…</div></div>`;
  try {
    const d = await api(`/api/projects/${state.projectId}/bus-matrix`);
    state.busMatrix = { mode: "overview", data: d };
    renderBusMatrix();
  } catch (e) {
    box.innerHTML = `<div class="empty chart-empty"><div class="empty-icon">⚠</div><div class="empty-desc">总线矩阵加载失败：${escapeHtml(e.message || "未知错误")}</div></div>`;
  }
}

async function busMatrixDrill(areaId) {
  const box = $("#bus-matrix");
  if (!box) return;
  box.innerHTML = `<div class="empty chart-empty"><div class="empty-icon">◌</div><div class="empty-desc">推导域内矩阵…</div></div>`;
  try {
    const d = await api(`/api/projects/${state.projectId}/bus-matrix?subject_area_id=${areaId}`);
    state.busMatrix = { mode: "area", data: d };
    renderBusMatrix();
  } catch (e) {
    showError(e);
    await renderBusMatrixCard();
  }
}

function renderBusMatrix() {
  const box = $("#bus-matrix");
  const bm = state.busMatrix;
  if (!box || !bm) return;
  if (bm.mode === "area") {
    box.innerHTML = busMatrixAreaHtml(bm.data);
    wireBusMatrixArea();
  } else {
    box.innerHTML = busMatrixOverviewHtml(bm.data);
    wireBusMatrixOverview();
  }
}

// ---- 企业级总览 ----

function busMatrixOverviewHtml(d) {
  const s = d.summary;
  const alertCount = s.orphan_dimensions.length + s.unmapped_processes.length;
  return `
    <div class="grid grid-4">
      <div class="stat-card"><div class="stat-label">业务过程（DWD/DWS）</div><div class="stat-value accent">${s.process_total}</div><div class="stat-foot">分布于 ${s.subject_area_total} 个主题域</div></div>
      <div class="stat-card"><div class="stat-label">一致性维度</div><div class="stat-value success">${s.shared_dimensions.length}</div><div class="stat-foot">跨域复用 · 维度共 ${s.dimension_total} 个</div></div>
      <div class="stat-card"><div class="stat-label">过程 → 维度直连</div><div class="stat-value warning">${s.relation_total}</div><div class="stat-foot">${s.mapped_cells} 个映射单元格</div></div>
      <div class="stat-card"><div class="stat-label">治理提示</div><div class="stat-value" style="color:${alertCount ? "var(--danger)" : "var(--success)"}">${alertCount}</div><div class="stat-foot">孤岛维度 ${s.orphan_dimensions.length} · 未挂载过程 ${s.unmapped_processes.length}</div></div>
    </div>
    <div class="mt20">${busMatrixHeatmapHtml(d)}</div>
    <div class="grid grid-2 mt20">
      <div class="card chart-card">
        <div class="card-title">维度复用度 <span class="sub">引用过程数 · 跨域复用即一致性维度</span></div>
        ${d.dimensions.length ? busMatrixReuseOverviewSvg(d) : chartEmpty("项目内暂无 DIM 维度模型")}
      </div>
      <div class="card">
        <div class="card-title">一致性维度榜 <span class="sub">总线契约的核心资产</span></div>
        ${s.shared_dimensions.length ? `<div class="table-wrap"><table class="data">
          <thead><tr><th>维度</th><th class="num">覆盖主题域</th><th class="num">引用过程</th></tr></thead>
          <tbody>${s.shared_dimensions.map((x) => `<tr>
            <td><span class="mono">${escapeHtml(x.model_key)}</span><div class="small muted">${escapeHtml(x.name)}</div></td>
            <td class="num">${x.area_count}</td><td class="num">${x.process_count}</td></tr>`).join("")}
          </tbody></table></div>` : `<div class="empty"><div class="empty-icon">◇</div><div class="empty-title">暂无一致性维度</div><div class="empty-desc">各主题域尚未形成跨域复用的维度契约。</div></div>`}
      </div>
    </div>
    <div class="bm-notes mt20">${busMatrixOverviewNotes(d)}</div>`;
}

function busMatrixHeatmapHtml(d) {
  if (!d.dimensions.length)
    return `<div class="empty"><div class="empty-icon">▦</div><div class="empty-title">暂无维度模型</div><div class="empty-desc">总线矩阵需要项目内存在 DIM 层维度模型作为列。</div></div>`;
  if (!d.matrix.length)
    return `<div class="empty"><div class="empty-desc">暂无主题域与业务过程。</div></div>`;
  const max = Math.max(1, ...d.matrix.flatMap((r) => r.counts));
  const head = `<tr><th class="bm-corner">主题域 / 维度</th>${d.dimensions.map((dim) =>
    `<th class="bm-dim-col" title="${escapeHtml(dim.model_key)} · ${escapeHtml(dim.name)}">${escapeHtml(bmShortDimName(dim.name))}</th>`).join("")}</tr>`;
  const rows = d.matrix.map((row) => {
    const label = row.unassigned
      ? `<span class="muted">未归属过程</span><div class="small muted mono">${row.process_count} 个过程</div>`
      : `<div style="font-weight:600">${escapeHtml(row.subject_area.name)}</div>
         <div class="small muted mono">${escapeHtml(row.subject_area.area_key)} · ${row.process_count} 个过程</div>`;
    const cells = row.counts.map((c, ci) => {
      if (!c) return `<td class="bm-cell bm-cell-0"></td>`;
      const level = Math.max(1, Math.min(BM_HEAT_STEPS, Math.ceil((c / max) * BM_HEAT_STEPS)));
      const areaName = row.unassigned ? "未归属" : row.subject_area.name;
      return `<td class="bm-cell bm-cell-${level}" title="${escapeHtml(areaName)} × ${escapeHtml(d.dimensions[ci].name)}：${c} 个过程引用${row.unassigned ? "" : "，点击下钻域内矩阵"}">${c}</td>`;
    }).join("");
    return row.unassigned
      ? `<tr><td class="bm-area-cell">${label}</td>${cells}</tr>`
      : `<tr class="bm-area-row" data-bm-area="${row.subject_area.id}"><td class="bm-area-cell">${label}</td>${cells}</tr>`;
  }).join("");
  return `<div class="table-wrap"><table class="data bm-heat"><thead>${head}</thead><tbody>${rows}</tbody></table></div>
    <div class="bm-heat-legend small muted">色深 = 该主题域引用该维度的过程数 · 点击行下钻域内矩阵</div>`;
}

function busMatrixReuseOverviewSvg(d) {
  const rows = d.dimensions.map((dim, ci) => {
    let count = 0, areas = 0;
    for (const r of d.matrix) { count += r.counts[ci]; if (r.counts[ci] > 0) areas += 1; }
    return { name: dim.name, model_key: dim.model_key, count, areas };
  }).filter((r) => r.count > 0)
    .sort((a, b) => b.count - a.count || b.areas - a.areas || a.model_key.localeCompare(b.model_key));
  if (!rows.length) return chartEmpty("暂无过程 → 维度直连关系");
  const rowH = 30, labelW = 92, trackW = 240, valW = 64, padTop = 6;
  const W = labelW + trackW + valW + 12;
  const H = rows.length * rowH + padTop * 2;
  const max = Math.max(...rows.map((r) => r.count), 1);
  const bars = rows.map((it, i) => {
    const y = padTop + i * rowH;
    const w = Math.max(4, (trackW * it.count) / max);
    const color = it.areas >= 2 ? "#4f8cff" : "#58b6e8";
    const short = it.name.length > 6 ? it.name.slice(0, 6) + "…" : it.name;
    return `<g>
      <title>${escapeHtml(it.name)}（${escapeHtml(it.model_key)}）：${it.count} 个过程引用 · 覆盖 ${it.areas} 个主题域</title>
      <text x="0" y="${y + rowH / 2 + 4}" class="chart-label">${escapeHtml(short)}</text>
      <rect x="${labelW}" y="${y + (rowH - 14) / 2}" width="${trackW}" height="14" rx="7" class="chart-track" />
      <rect x="${labelW}" y="${y + (rowH - 14) / 2}" width="${w.toFixed(1)}" height="14" rx="7" fill="${color}" class="chart-bar-anim" style="animation-delay:${(i * 60).toFixed(0)}ms" />
      <text x="${labelW + trackW + 8}" y="${y + rowH / 2 + 4}" class="chart-value">${it.count} · ${it.areas} 域</text>
    </g>`;
  }).join("");
  return `<svg class="chart-svg" viewBox="0 0 ${W} ${H}" role="img" preserveAspectRatio="xMinYMin meet" aria-label="维度复用度条形图">${bars}</svg>
    <div class="chart-legend">
      <div class="chart-legend-row"><span class="chart-legend-dot" style="background:#4f8cff"></span><span>一致性维度（≥2 域复用）</span></div>
      <div class="chart-legend-row"><span class="chart-legend-dot" style="background:#58b6e8"></span><span>单域维度</span></div>
    </div>`;
}

function busMatrixOverviewNotes(d) {
  const s = d.summary;
  const notes = [];
  if (s.orphan_dimensions.length)
    notes.push(`<div class="bm-note bm-note-danger">孤岛维度（无任何业务过程引用）：${s.orphan_dimensions.map((x) => `<span class="mono">${escapeHtml(x.model_key)}</span>`).join("、")}</div>`);
  if (s.unmapped_processes.length)
    notes.push(`<div class="bm-note bm-note-warning">未挂载任何维度的过程（${s.unmapped_processes.length} 个）：${s.unmapped_processes.map((x) => `<span class="mono">${escapeHtml(x.model_key)}</span>`).join("、")}</div>`);
  if (!notes.length)
    notes.push(`<div class="bm-note bm-note-ok">全部业务过程均已显式挂载维度，总线契约完整。</div>`);
  return notes.join("");
}

// ---- 域内明细 ----

function busMatrixAreaHtml(d) {
  const s = d.summary;
  return `
    <div class="flex flex-wrap" style="align-items:center;gap:10px">
      <button class="btn btn-sm" id="bm-back">← 返回企业总览</button>
      <span style="font-weight:650;font-size:15px">${escapeHtml(d.subject_area.name)}</span>
      <span class="mono small muted">${escapeHtml(d.subject_area.area_key)}</span>
      ${d.subject_area.owner ? `<span class="badge badge-neutral">${escapeHtml(d.subject_area.owner)}</span>` : ""}
      <span class="small muted">· 域内总线矩阵</span>
    </div>
    <div class="grid grid-4 mt14">
      <div class="stat-card"><div class="stat-label">域内业务过程</div><div class="stat-value accent">${s.process_total}</div><div class="stat-foot">DWD / DWS 模型</div></div>
      <div class="stat-card"><div class="stat-label">映射单元格</div><div class="stat-value success">${s.mapped_cells}</div><div class="stat-foot">${s.relation_total} 条直连关系</div></div>
      <div class="stat-card"><div class="stat-label">跨域引用</div><div class="stat-value warning">${s.cross_area_relations}</div><div class="stat-foot">引用外域维度的关系数</div></div>
      <div class="stat-card"><div class="stat-label">未挂载过程</div><div class="stat-value" style="color:${s.unmapped_processes.length ? "var(--danger)" : "var(--success)"}">${s.unmapped_processes.length}</div><div class="stat-foot">${s.unmapped_processes.length ? "见下方提示" : "域内过程全部挂载维度"}</div></div>
    </div>
    <div class="mt20">${busMatrixAreaTableHtml(d)}</div>
    <div class="grid grid-2 mt20">
      <div class="card chart-card">
        <div class="card-title">维度复用对比 <span class="sub">本域过程 vs 外域过程</span></div>
        ${d.dimension_reuse.some((r) => r.total_process_count > 0) ? busMatrixReuseAreaSvg(d) : chartEmpty("该域暂无维度直连关系")}
      </div>
      <div class="card">
        <div class="card-title">字段映射明细 <span class="sub">点击矩阵单元格查看</span></div>
        <div id="bm-detail"><div class="empty"><div class="empty-desc">选择左侧矩阵中的一个映射单元格。</div></div></div>
      </div>
    </div>
    ${s.unmapped_processes.length ? `<div class="bm-notes mt20"><div class="bm-note bm-note-warning">未挂载维度的过程：${s.unmapped_processes.map((x) => `<span class="mono">${escapeHtml(x.model_key)}</span>（${escapeHtml(x.name)}）`).join("、")}</div></div>` : ""}`;
}

function busMatrixAreaTableHtml(d) {
  if (!d.processes.length)
    return `<div class="empty"><div class="empty-icon">▦</div><div class="empty-title">域内暂无业务过程</div><div class="empty-desc">该主题域下没有 DWD / DWS 模型。</div></div>`;
  if (!d.dimensions.length)
    return `<div class="empty"><div class="empty-icon">▦</div><div class="empty-title">项目内暂无维度</div><div class="empty-desc">总线矩阵需要 DIM 层维度模型作为列。</div></div>`;
  const cellByKey = {};
  for (const c of d.cells) cellByKey[`${c.process_id}:${c.dimension_id}`] = c;
  const head = `<tr><th class="bm-corner">业务过程 / 维度</th>${d.dimensions.map((dim) =>
    `<th class="bm-dim-col" title="${escapeHtml(dim.model_key)} · ${escapeHtml(dim.name)}">${escapeHtml(bmShortDimName(dim.name))}</th>`).join("")}</tr>`;
  const rows = d.processes.map((p) => {
    const cells = d.dimensions.map((dim) => {
      const c = cellByKey[`${p.id}:${dim.id}`];
      if (!c) return `<td class="bm-cell bm-cell-0"></td>`;
      const relType = c.relationship_types.find((t) => BM_REL_COLORS[t]) || c.relationship_types[0];
      const color = BM_REL_COLORS[relType] || "#8d99b5";
      const relLabel = c.relationship_types.map((t) => REL_TYPE_LABEL[t] || t).join(" / ");
      return `<td class="bm-cell bm-cell-filled" data-bm-cell="${p.id}:${dim.id}" style="--bm-cell-color:${color}"
        title="${escapeHtml(p.name)} → ${escapeHtml(dim.name)}：${escapeHtml(relLabel)}，点击查看字段映射">${c.edge_count}</td>`;
    }).join("");
    return `<tr>
      <td class="bm-area-cell">
        <div style="font-weight:600">${escapeHtml(p.name)}</div>
        <div class="small muted mono">${escapeHtml(p.model_key)}</div>
        <div class="small muted"><span class="badge badge-neutral">${escapeHtml(p.layer)}</span> 覆盖 ${p.dimension_count} 维</div>
      </td>${cells}</tr>`;
  }).join("");
  return `<div class="table-wrap"><table class="data bm-heat bm-area-matrix"><thead>${head}</thead><tbody>${rows}</tbody></table></div>
    <div class="bm-heat-legend small muted">数字 = 直连关系数 · 颜色 = 关系类型（${REL_TYPES.map((t) => REL_TYPE_LABEL[t]).join(" / ")}）· 点击单元格查看字段映射</div>`;
}

function busMatrixReuseAreaSvg(d) {
  const rows = d.dimension_reuse
    .filter((r) => r.total_process_count > 0)
    .map((r) => ({ name: r.name, model_key: r.model_key, inArea: r.in_area_process_count, outArea: r.out_of_area_process_count, total: r.total_process_count, shared: r.is_shared }))
    .sort((a, b) => b.total - a.total || a.model_key.localeCompare(b.model_key));
  const rowH = 30, labelW = 92, trackW = 210, valW = 52, padTop = 6;
  const W = labelW + trackW + valW + 12;
  const H = rows.length * rowH + padTop * 2;
  const max = Math.max(...rows.map((r) => r.total), 1);
  const bars = rows.map((it, i) => {
    const y = padTop + i * rowH;
    const wIn = (trackW * it.inArea) / max;
    const wOut = (trackW * it.outArea) / max;
    const short = it.name.length > 6 ? it.name.slice(0, 6) + "…" : it.name;
    return `<g>
      <title>${escapeHtml(it.name)}（${escapeHtml(it.model_key)}）：本域 ${it.inArea} 个过程 · 外域 ${it.outArea} 个过程${it.shared ? " · 一致性维度" : ""}</title>
      <text x="0" y="${y + rowH / 2 + 4}" class="chart-label">${escapeHtml(short)}</text>
      <rect x="${labelW}" y="${y + (rowH - 14) / 2}" width="${trackW}" height="14" rx="7" class="chart-track" />
      ${it.inArea ? `<rect x="${labelW}" y="${y + (rowH - 14) / 2}" width="${Math.max(3, wIn).toFixed(1)}" height="14" rx="7" fill="#4f8cff" class="chart-bar-anim" style="animation-delay:${(i * 60).toFixed(0)}ms" />` : ""}
      ${it.outArea ? `<rect x="${(labelW + wIn).toFixed(1)}" y="${y + (rowH - 14) / 2}" width="${Math.max(3, wOut - 2).toFixed(1)}" height="14" rx="7" fill="#58b6e8" class="chart-bar-anim" style="animation-delay:${(i * 60 + 30).toFixed(0)}ms" />` : ""}
      <text x="${labelW + trackW + 8}" y="${y + rowH / 2 + 4}" class="chart-value">${it.total}${it.shared ? " ★" : ""}</text>
    </g>`;
  }).join("");
  return `<svg class="chart-svg" viewBox="0 0 ${W} ${H}" role="img" preserveAspectRatio="xMinYMin meet" aria-label="维度复用对比条形图">${bars}</svg>
    <div class="chart-legend">
      <div class="chart-legend-row"><span class="chart-legend-dot" style="background:#4f8cff"></span><span>本域过程引用</span></div>
      <div class="chart-legend-row"><span class="chart-legend-dot" style="background:#58b6e8"></span><span>外域过程引用（★ = 一致性维度）</span></div>
    </div>`;
}

function busMatrixDetailHtml(d, cell) {
  const p = d.processes.find((x) => x.id === cell.process_id);
  const dim = d.dimensions.find((x) => x.id === cell.dimension_id);
  if (!p || !dim) return "";
  return `
    <div style="font-weight:650">${escapeHtml(p.name)} <span class="muted">→</span> ${escapeHtml(dim.name)}</div>
    <div class="small muted mono">${escapeHtml(p.model_key)} × ${escapeHtml(dim.model_key)}</div>
    <div class="mt14">${cell.field_pairs.map((e) => `
      <div class="bm-edge">
        <div class="flex" style="gap:8px;align-items:center;flex-wrap:wrap">
          <span class="badge" style="background:${BM_REL_COLORS[e.relationship_type] || "var(--panel-3)"};color:#fff">${escapeHtml(REL_TYPE_LABEL[e.relationship_type] || e.relationship_type)}</span>
          <span class="badge badge-neutral">${escapeHtml(CARDINALITY_LABEL[e.cardinality] || e.cardinality)}</span>
          <span class="small muted">${e.direction === "process_to_dimension" ? "过程 → 维度" : "维度 → 过程"}</span>
        </div>
        <div class="bm-edge-fields mono">${escapeHtml(e.source_fields.join(", ") || "—")} <span class="muted">⇒</span> ${escapeHtml(e.target_fields.join(", ") || "—")}</div>
        ${e.description ? `<div class="small muted mt8">${escapeHtml(e.description)}</div>` : ""}
      </div>`).join("")}</div>`;
}

// ---- 事件绑定 ----

function wireBusMatrixOverview() {
  $$("[data-bm-area]").forEach((el) =>
    el.addEventListener("click", () => busMatrixDrill(Number(el.dataset.bmArea))));
}

function wireBusMatrixArea() {
  const back = $("#bm-back");
  if (back) back.onclick = renderBusMatrixCard;
  const first = $("[data-bm-cell]");
  $$("[data-bm-cell]").forEach((el) =>
    el.addEventListener("click", () => {
      $$("[data-bm-cell]").forEach((x) => x.classList.remove("bm-cell-selected"));
      el.classList.add("bm-cell-selected");
      const [pid, did] = el.dataset.bmCell.split(":").map(Number);
      const cell = state.busMatrix.data.cells.find((c) => c.process_id === pid && c.dimension_id === did);
      const box = $("#bm-detail");
      if (box && cell) box.innerHTML = busMatrixDetailHtml(state.busMatrix.data, cell);
    }));
  if (first) first.click();
}

// ---------------------------------------------------------------- 业务需求

async function renderRequirements(content) {
  await loadProjectContext();
  content.innerHTML = `
    <div class="card">
      <div class="card-title">主题域 <span class="sub">项目内业务分类，与数仓分层相互独立</span>
        <button class="btn btn-sm" id="btn-new-area">+ 新建主题域</button>
      </div>
      ${state.subjectAreas.length ? `<div class="flex flex-wrap">
        ${state.subjectAreas.map((a) => `
          <div class="stat-card" style="min-width:200px">
            <div class="flex"><span class="mono" style="color:var(--accent)">${escapeHtml(a.area_key)}</span><span class="grow"></span><span class="small muted">${escapeHtml(a.owner || "")}</span></div>
            <div style="font-weight:600;margin-top:2px">${escapeHtml(a.name)}</div>
            <div class="small muted mt8">${escapeHtml(a.description || "—")}</div>
            <div class="mt8"><button class="btn btn-sm" data-bm-area="${a.id}">总线矩阵</button></div>
          </div>`).join("")}
      </div>` : `<div class="empty"><div class="empty-desc">暂无主题域。</div></div>`}
    </div>
    <div class="card">
      <div class="card-title">业务总线矩阵 <span class="sub">Kimball 总线矩阵 · 业务过程 × 维度一致性视图</span>
        <button class="btn btn-sm" id="btn-bm-refresh">刷新</button>
      </div>
      <div id="bus-matrix"><div class="empty chart-empty"><div class="empty-icon">◌</div><div class="empty-desc">总线矩阵推导中…</div></div></div>
    </div>
    <div class="card">
      <div class="card-title">业务需求 <span class="sub">建模工作的上游输入</span>
        <button class="btn btn-sm btn-primary" id="btn-new-req">+ 登记需求</button>
      </div>
      <div class="toolbar" id="req-toolbar">
        <input class="input" id="req-search" placeholder="搜索标题 / 需求编码" style="width:220px" />
        <select class="select" id="req-filter-domain" style="width:130px"><option value="">全部业务域</option></select>
        <select class="select" id="req-filter-priority" style="width:110px"><option value="">全部优先级</option>${PRIORITIES.map((p) => `<option value="${p}">${p}</option>`).join("")}</select>
        <select class="select" id="req-filter-status" style="width:120px"><option value="">全部状态</option>${REQ_STATUSES.map((s) => `<option value="${s}">${REQ_STATUS_LABEL[s]}</option>`).join("")}</select>
        <span class="small muted" id="req-filter-count"></span>
        <div class="spacer"></div>
        <button class="btn btn-ghost btn-sm" id="req-filter-clear">清空筛选</button>
      </div>
      <div id="req-table"></div>
    </div>`;

  // 业务域候选项取自现有需求（自由文本字段，无独立字典）
  const domains = [...new Set(state.requirements.map((r) => (r.business_domain || "").trim()).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b, "zh"));
  const domSel = $("#req-filter-domain");
  domains.forEach((dm) => {
    const opt = document.createElement("option");
    opt.value = dm; opt.textContent = dm;
    domSel.appendChild(opt);
  });

  function applyReqFilter() {
    const q = $("#req-search").value.trim().toLowerCase();
    const domain = $("#req-filter-domain").value;
    const priority = $("#req-filter-priority").value;
    const status = $("#req-filter-status").value;
    const rows = state.requirements.filter((r) =>
      (!q || r.title.toLowerCase().includes(q) || r.requirement_key.toLowerCase().includes(q)) &&
      (!domain || (r.business_domain || "") === domain) &&
      (!priority || r.priority === priority) &&
      (!status || r.status === status));
    const filtered = rows.length !== state.requirements.length;
    $("#req-filter-count").textContent = filtered
      ? `筛选出 ${rows.length} / ${state.requirements.length} 条`
      : `共 ${state.requirements.length} 条`;
    $("#req-filter-clear").style.visibility = filtered ? "visible" : "hidden";
    $("#req-table").innerHTML = !state.requirements.length
      ? `<div class="empty"><div class="empty-icon">◈</div><div class="empty-title">暂无需求</div>
          <div class="empty-desc">点击右上角「登记需求」，把业务诉求纳入建模闭环。</div></div>`
      : rows.length ? `<div class="table-wrap"><table class="data">
      <thead><tr><th>需求编码</th><th>标题</th><th>业务域</th><th>优先级</th><th>状态</th><th>负责人</th><th class="num">关联模型</th><th>操作</th></tr></thead>
      <tbody>${rows.map((r) => `
        <tr>
          <td class="mono">${escapeHtml(r.requirement_key)}</td>
          <td>${escapeHtml(r.title)}</td>
          <td>${escapeHtml(r.business_domain || "—")}</td>
          <td><span class="badge badge-neutral">${escapeHtml(r.priority)}</span></td>
          <td><span class="badge badge-${r.status === "accepted" ? "published" : r.status === "rejected" ? "error" : r.status === "in_analysis" ? "in_review" : "draft"}">${REQ_STATUS_LABEL[r.status] || r.status}</span></td>
          <td>${escapeHtml(r.owner || "—")}</td>
          <td class="num">${r.linked_model_count}</td>
          <td class="nowrap">
            <button class="btn btn-sm" data-req-edit="${r.id}">编辑</button>
            <button class="btn btn-sm" data-req-models="${r.id}">关联模型</button>
          </td>
        </tr>`).join("")}
      </tbody></table></div>`
      : `<div class="empty"><div class="empty-icon">▦</div><div class="empty-title">没有匹配的需求</div>
          <div class="empty-desc">调整筛选条件，或登记新需求。</div>
          <div class="mt14"><button class="btn btn-sm" id="req-filter-reset">清空筛选</button></div></div>`;
    $$("[data-req-edit]", $("#req-table")).forEach((el) =>
      el.addEventListener("click", () => {
        const r = state.requirements.find((x) => x.id === Number(el.dataset.reqEdit));
        requirementModal(r);
      }));
    $$("[data-req-models]", $("#req-table")).forEach((el) =>
      el.addEventListener("click", () => requirementModelsModal(Number(el.dataset.reqModels))));
    const reset = $("#req-filter-reset");
    if (reset) reset.onclick = clearReqFilter;
  }

  function clearReqFilter() {
    $("#req-search").value = "";
    $("#req-filter-domain").value = "";
    $("#req-filter-priority").value = "";
    $("#req-filter-status").value = "";
    applyReqFilter();
  }

  $("#req-search").addEventListener("input", applyReqFilter);
  $("#req-search").addEventListener("keydown", (e) => { if (e.key === "Enter") applyReqFilter(); });
  $("#req-filter-domain").addEventListener("change", applyReqFilter);
  $("#req-filter-priority").addEventListener("change", applyReqFilter);
  $("#req-filter-status").addEventListener("change", applyReqFilter);
  $("#req-filter-clear").addEventListener("click", clearReqFilter);
  applyReqFilter();

  $("#btn-new-area").onclick = () => formModal("新建主题域", [
    { key: "area_key", label: "主题域编码", required: true, placeholder: "如 SALES" },
    { key: "name", label: "名称", required: true, placeholder: "如 销售主题域" },
    { key: "owner", label: "负责人" },
    { key: "description", label: "描述", type: "textarea" },
  ], "创建", async (v) => {
    try {
      await api(`/api/projects/${state.projectId}/subject-areas`, {
        method: "POST", body: JSON.stringify(v),
      });
      closeModal(); toast("主题域已创建", "success"); await renderView();
    } catch (e) { showError(e); }
  });

  $("#btn-new-req").onclick = () => requirementModal();

  // 业务总线矩阵：默认企业级总览，主题域卡片 / 热力行均可下钻
  $("#btn-bm-refresh").onclick = renderBusMatrixCard;
  renderBusMatrixCard();
}

function requirementModal(req) {
  formModal(req ? "编辑需求" : "登记业务需求", [
    { key: "requirement_key", label: "需求编码", required: true, value: req && req.requirement_key, placeholder: "如 REQ_SALES_001" },
    { key: "title", label: "标题", required: true, value: req && req.title },
    { key: "business_domain", label: "业务域", value: req && req.business_domain, placeholder: "如 销售经营" },
    { key: "priority", label: "优先级", type: "select", value: (req && req.priority) || "P2", options: PRIORITIES },
    { key: "status", label: "状态", type: "select", value: (req && req.status) || "draft", options: REQ_STATUSES.map((s) => ({ value: s, label: REQ_STATUS_LABEL[s] })) },
    { key: "owner", label: "负责人", value: req && req.owner },
    { key: "description", label: "业务描述", type: "textarea", value: req && req.description },
    { key: "acceptance_criteria", label: "验收口径", type: "textarea", value: req && req.acceptance_criteria },
  ], req ? "保存" : "创建", async (v) => {
    try {
      if (req) {
        await api(`/api/requirements/${req.id}`, { method: "PUT", body: JSON.stringify(v) });
        toast("需求已更新", "success");
      } else {
        await api(`/api/projects/${state.projectId}/requirements`, { method: "POST", body: JSON.stringify(v) });
        toast("需求已登记", "success");
      }
      closeModal(); await renderView();
    } catch (e) { showError(e); }
  });
}

async function requirementModelsModal(reqId) {
  const req = state.requirements.find((r) => r.id === reqId);
  const linked = await api(`/api/requirements/${reqId}/models`);
  const linkedIds = new Set(linked.map((m) => m.id));
  const options = state.models.map((m) => ({ value: m.id, label: `${m.model_key} · ${m.name}` }));
  openModal(`关联模型 · ${req ? req.title : reqId}`, `
    <div class="small muted">勾选需要关联到该需求的模型（同项目）：</div>
    <div class="mt14" style="max-height:300px;overflow-y:auto">
      ${state.models.length ? state.models.map((m) => `
        <label class="flex" style="padding:6px 4px">
          <input type="checkbox" data-link-model="${m.id}" ${linkedIds.has(m.id) ? "checked" : ""} style="width:15px;height:15px;accent-color:var(--accent)" />
          <span class="mono small">${escapeHtml(m.model_key)}</span>
          <span>${escapeHtml(m.name)}</span>
          <span class="badge badge-${m.status}">${STATUS_LABEL[m.status] || m.status}</span>
        </label>`).join("") : `<div class="empty"><div class="empty-desc">项目内暂无模型。</div></div>`}
    </div>`, [
    { label: "关闭", cls: "btn-ghost", onClick: closeModal },
    {
      label: "保存关联", cls: "btn-primary", onClick: async () => {
        try {
          const wanted = new Set($$("[data-link-model]:checked").map((el) => Number(el.dataset.linkModel)));
          for (const id of linkedIds) {
            if (!wanted.has(id)) await api(`/api/requirements/${reqId}/models/${id}`, { method: "DELETE" });
          }
          for (const id of wanted) {
            if (!linkedIds.has(id)) await api(`/api/requirements/${reqId}/models/${id}`, { method: "POST" });
          }
          closeModal(); toast("关联已更新", "success"); await renderView();
        } catch (e) { showError(e); }
      },
    },
  ]);
}

// ---------------------------------------------------------------- 模型中心

async function renderModels(content) {
  await loadProjectContext();
  const layers = [...new Set(state.models.map((m) => m.layer))];
  content.innerHTML = `
    <div class="toolbar">
      <input class="input" id="model-search" placeholder="搜索模型编码 / 名称" style="width:240px" />
      <select class="select" id="filter-layer" style="width:120px"><option value="">全部分层</option>${layers.map((l) => `<option>${l}</option>`).join("")}</select>
      <select class="select" id="filter-status" style="width:120px">
        <option value="">全部状态</option>
        <option value="draft">草稿</option><option value="in_review">待评审</option>
        <option value="approved">已通过</option><option value="published">已发布</option>
      </select>
      <div class="spacer"></div>
      <button class="btn btn-primary" id="btn-new-model">+ 新建模型</button>
    </div>
    <div id="model-list"></div>`;
  $("#btn-new-model").onclick = () => modelModal();
  const recents = state.recentModels
    .map((id) => state.models.find((m) => m.id === id))
    .filter(Boolean);
  const applyFilter = () => {
    const q = $("#model-search").value.trim().toLowerCase();
    const layer = $("#filter-layer").value;
    const status = $("#filter-status").value;
    const rows = state.models.filter((m) =>
      (!q || m.model_key.toLowerCase().includes(q) || m.name.toLowerCase().includes(q)) &&
      (!layer || m.layer === layer) && (!status || m.status === status));
    const recentBar = recents.length
      ? `<div class="recent-bar"><span class="small muted">最近访问</span>${recents.map((m) =>
          `<button class="recent-chip" data-open-model="${m.id}" title="${escapeHtml(m.model_key)}">${escapeHtml(m.name)}</button>`).join("")}</div>`
      : "";
    $("#model-list").innerHTML = recentBar + (rows.length ? `<div class="table-wrap"><table class="data">
      <thead><tr><th>模型</th><th>主题域</th><th>分层</th><th>类型</th><th>引擎</th><th>状态</th><th>版本</th><th class="num">字段</th><th>负责人</th></tr></thead>
      <tbody>${rows.map((m) => {
        const area = state.subjectAreas.find((a) => a.id === m.subject_area_id);
        return `<tr>
          <td><span class="link" data-open-model="${m.id}">${escapeHtml(m.name)}</span><div class="small muted mono">${escapeHtml(m.model_key)}</div></td>
          <td>${area ? escapeHtml(area.name) : "—"}</td>
          <td>${escapeHtml(m.layer)}</td>
          <td>${escapeHtml(m.model_type)}</td>
          <td class="mono small">${escapeHtml(m.target_engine)}</td>
          <td><span class="badge badge-${m.status}">${STATUS_LABEL[m.status] || m.status}</span></td>
          <td class="mono">${m.published_version_no ? "V" + m.published_version_no : "R" + m.edit_revision}</td>
          <td class="num">${m.fields.length}</td>
          <td>${escapeHtml(m.created_by || "—")}</td>
        </tr>`;
      }).join("")}</tbody></table></div>`
      : `<div class="empty"><div class="empty-icon">▦</div><div class="empty-title">没有匹配的模型</div><div class="empty-desc">调整筛选条件，或新建模型。</div></div>`);
    $$("[data-open-model]").forEach((el) =>
      el.addEventListener("click", () => openModelEditor(Number(el.dataset.openModel))));
  };
  $("#model-search").addEventListener("input", applyFilter);
  $("#filter-layer").addEventListener("change", applyFilter);
  $("#filter-status").addEventListener("change", applyFilter);
  applyFilter();
}

function modelModal() {
  formModal("新建模型草稿", [
    { key: "model_key", label: "模型编码", required: true, placeholder: "snake_case，如 dwd_sales_order" },
    { key: "name", label: "模型名称", required: true, placeholder: "如 销售订单明细" },
    { key: "layer", label: "数仓分层", type: "select", value: "DWD", options: LAYERS },
    { key: "model_type", label: "模型类型", type: "select", value: "TABLE", options: MODEL_TYPES },
    { key: "target_engine", label: "目标引擎", type: "select", value: "postgresql", options: ENGINES },
    { key: "subject_area_id", label: "主题域", type: "select", value: "", options: [{ value: "", label: "（不归属）" }, ...state.subjectAreas.map((a) => ({ value: a.id, label: a.name }))] },
    { key: "description", label: "模型描述", type: "textarea", placeholder: "每行代表什么？业务口径？" },
  ], "创建草稿", async (v) => {
    try {
      const body = {
        model_key: v.model_key, name: v.name, description: v.description,
        layer: v.layer, model_type: v.model_type, target_engine: v.target_engine,
        subject_area_id: v.subject_area_id ? Number(v.subject_area_id) : null,
        fields: [],
      };
      const m = await api(`/api/projects/${state.projectId}/models`, { method: "POST", body: JSON.stringify(body) });
      closeModal(); toast("模型草稿已创建", "success");
      await openModelEditor(m.id);
    } catch (e) { showError(e); }
  });
}

// ---------------------------------------------------------------- 运行事件

async function renderEvents(content) {
  const events = await api(`/api/projects/${state.projectId}/events?limit=100`);
  content.innerHTML = `
    <div class="card">
      <div class="card-title">业务活动时间线 <span class="sub">与 request_id 关联，可排障</span></div>
      ${events.length ? `<div class="timeline">
        ${events.map((e) => `
          <div class="timeline-item">
            <span class="t-type">${escapeHtml(e.event_type)}</span>
            <span class="t-time">${fmtTime(e.created_at)}</span>
            <span class="small muted">by ${escapeHtml(e.actor)}</span>
            <div class="t-detail">${escapeHtml(JSON.stringify(e.detail))}</div>
            ${e.request_id ? `<div class="small" style="color:var(--text-faint);font-family:var(--mono)">request_id: ${escapeHtml(e.request_id)}</div>` : ""}
          </div>`).join("")}
      </div>` : `<div class="empty"><div class="empty-desc">暂无事件。</div></div>`}
    </div>`;
}

// ---------------------------------------------------------------- 模型关系图与外部血缘（V0.9 → V1.2 Neo4j 画布）

const GRAPH_LAYERS = ["ODS", "DWD", "DIM", "DWS", "ADS"];

const GRAPH_VIEWS = [
  { key: "current", label: "当前设计", hint: "草稿实时图，含未发布模型与当前关系" },
  { key: "published_latest", label: "最近发布", hint: "最近一次项目图谱快照（发布事务写入，不受草稿影响）" },
  { key: "snapshot", label: "历史快照", hint: "按快照号读取不可变历史图" },
  { key: "lineage", label: "外部血缘", hint: "设计关系 + 外部来源系统观测边（分域展示）" },
];
const LINEAGE_TYPE_LABEL = {
  etl_data_flow: "ETL 数据流", scheduler_dependency: "调度依赖",
  bi_consumption: "BI 消费", dbt_dependency: "dbt 依赖", other: "其他",
};
const SCAN_MODE_LABEL = { incremental: "增量", full_snapshot: "完整快照" };
const PATH_DIRECTIONS = [
  { value: "downstream", label: "下游（正向）" },
  { value: "upstream", label: "上游（反向）" },
  { value: "any", label: "双向" },
];
const CARDINALITY_LABEL = {
  unspecified: "未指定", one_to_one: "一对一", one_to_many: "一对多",
  many_to_one: "多对一", many_to_many: "多对多",
};

/* ---------------- Neo4j 画布引擎（neovis.js，V1.2） ----------------
 * 采用 Neo4j 官方可视化组件 neovis.js（内置 vis-network canvas 渲染）：
 * - 数据源为 dataFunction 适配器：把 REST 图谱响应转换为 Neo4j 记录流，
 *   该路径不创建 Neo4j driver；未来接入真实 Neo4j 只需把 dataFunction
 *   换成 neo4j.driver 会话查询（记录协议不变）
 * - 交互：节点拖动、滚轮缩放、空白拖拽平移、单击节点/边出详情面板、
 *   双击聚焦邻居子图、hover 高亮并淡化无关元素、路径查询结果聚焦高亮
 * - 布局：力导向（barnesHut）↔ 分层（ODS→DWD→DIM→DWS→ADS，LR 方向） */

const LAYER_COLORS = {
  ODS: "#58b6e8", DWD: "#4f8cff", DIM: "#f0a63c", DWS: "#35c98d", ADS: "#c084fc",
};
const LAYER_FALLBACK_COLOR = "#8d99b5";
const STATUS_COLORS = {
  published: "#35c98d", in_review: "#f0a63c", approved: "#58b6e8", draft: "#5d6a87",
};
const NODE_BG = "#1c2540";
const NODE_BG_DIM = "rgba(20,27,47,0.35)";
const NODE_BG_HOVER = "#232e4e";
const EDGE_COLOR = "#4a5878";
const EDGE_FOCUS_COLOR = "#4f8cff";
const EXTERNAL_EDGE_COLOR = "#a855f7";
const EXT_EDGE_ID_BASE = 10000000; // 外部血缘边 vis id 偏移（与内部边 id 空间隔离）
const EDGE_HASH_BASE = 2000000;   // 快照视图边（无 id）哈希 vis id 偏移段
const NODE_FONT = { color: "#e8edf7", size: 13, face: "system-ui", strokeWidth: 3, strokeColor: "#0d1220", align: "center" };
const NODE_FONT_DIM = { color: "rgba(232,237,247,0.30)", size: 13, face: "system-ui", strokeWidth: 3, strokeColor: "#0d1220", align: "center" };

/** 边的 vis id：实时视图用库 id；快照视图（published_latest/snapshot）无 id 字段，
 *  按 edge_key 哈希派生稳定数值 id（FNV-1a，落在 EDGE_HASH_BASE 段，避开库 id 与外部边号段）。
 *  注意：neovis 的 identity 必须是数值，缺失/NaN 会让所有边塌成同一个 id 被 DataSet 去重。 */
function edgeVisId(e, idx) {
  if (e.id != null && Number.isFinite(Number(e.id))) return Number(e.id);
  const key = e.edge_key != null ? String(e.edge_key) : ("edge#" + idx);
  let h = 2166136261;
  for (let i = 0; i < key.length; i++) {
    h ^= key.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return EDGE_HASH_BASE + ((h >>> 1) % 1000000);
}

function graphViewMeta(key) {
  return GRAPH_VIEWS.find((v) => v.key === key) || GRAPH_VIEWS[0];
}

function nodeVersionText(n) {
  return n.published_version_no
    ? "V" + n.published_version_no
    : (n.version_no ? "V" + n.version_no : (n.status === "published" ? "已发布" : "未发布"));
}

function hexA(hex, a) {
  const h = String(hex || "").replace("#", "");
  const r = parseInt(h.slice(0, 2), 16) || 0;
  const g = parseInt(h.slice(2, 4), 16) || 0;
  const b = parseInt(h.slice(4, 6), 16) || 0;
  return `rgba(${r},${g},${b},${a})`;
}

function lineageImportTemplate(nodes) {
  const keys = nodes.map((n) => n.model_key);
  const src = keys[0] || "ods_src_model";
  const tgt = keys[1] || "dwd_target_model";
  return JSON.stringify({
    source_system: "airflow",
    run_id: "demo-run-001",
    scan_mode: "incremental",
    scope_key: "default",
    complete_scan: false,
    edges: [{
      source_ref: "ref-001",
      job_key: "dag_demo.task_a",
      source_model_key: src,
      target_model_key: tgt,
      lineage_type: "etl_data_flow",
      transformation_type: "sql",
      confidence: 90,
      metadata: { dag_id: "dag_demo", task_id: "task_a" },
    }],
  }, null, 2);
}

/* ---- REST → Neo4j 记录适配器（neovis dataFunction 消费） ----
 * neovis 记录协议（鸭子类型，经水合器识别）：
 * - Node：{identity:{low,high}, labels:[...], properties:{...}}
 * - Relationship：{identity:{low,high}, start, end, type, properties}
 * - Record：可枚举键（作 keys）+ 非枚举 _fields/_fieldLookup
 * identity 必须是 {low,high} 形状（水合器解构后交给 Integer 构造）。 */

function neoInt(n) {
  const v = Number(n);
  return { low: Number.isFinite(v) ? v : 0, high: 0 };
}

function neoNode(identity, labels, properties) {
  return { identity: neoInt(identity), labels, properties };
}

function neoRel(identity, start, end, type, properties) {
  return { identity: neoInt(identity), start: neoInt(start), end: neoInt(end), type, properties };
}

function neoRecord(fields) {
  // neovis v2 #createSingleRecord：new Neo4j.types.Record(record.keys, fields.map(dumbToNeo4j), record._fieldLookup)
  // —— keys（字段名数组）为必填，否则 Record 构造器读 keys.length 即抛错
  const keys = fields.map((_, i) => "k" + i);
  const rec = {};
  fields.forEach((f, i) => { rec[keys[i]] = f; });
  rec.keys = keys;
  Object.defineProperty(rec, "_fields", { value: fields, enumerable: false });
  Object.defineProperty(rec, "_fieldLookup", { value: {}, enumerable: false });
  return rec;
}

/** 图谱数据 → neovis 记录流（异步生成器）。两端不在图中的边直接跳过。 */
function makeGraphDataFunction(nodes, edges, externalEdges) {
  const nodeIds = new Set(nodes.map((n) => n.id));
  return async function* graphRecords() {
    for (const n of nodes) {
      yield neoRecord([neoNode(n.id, ["Model"], {
        model_key: n.model_key,
        name: n.name,
        layer: n.layer || "其他",
        target_engine: n.target_engine,
        status: n.status,
        field_count: n.field_count,
        version: nodeVersionText(n),
        caption: `${n.name}\n${n.model_key} · ${n.layer || "其他"}\n${nodeVersionText(n)} · ${n.field_count} 字段`,
      })]);
    }
    for (const [i, e] of edges.entries()) {
      if (!nodeIds.has(e.source_model_id) || !nodeIds.has(e.target_model_id)) continue;
      const sf = (e.source_fields || []).join(",");
      const tf = (e.target_fields || []).join(",");
      const relLabel = REL_TYPE_LABEL[e.relationship_type] || e.relationship_type;
      const fields = sf || tf ? `\n${sf || "?"}→${tf || "?"}` : "";
      yield neoRecord([neoRel(e._visId, e.source_model_id, e.target_model_id, e.relationship_type, {
        rel_key: e.edge_key,
        caption: relLabel + fields,
        external: false,
      })]);
    }
    for (const [i, e] of externalEdges.entries()) {
      if (!nodeIds.has(e.source_model_id) || !nodeIds.has(e.target_model_id)) continue;
      const vid = EXT_EDGE_ID_BASE + (e.id != null ? Number(e.id) : i);
      const lt = LINEAGE_TYPE_LABEL[e.lineage_type] || e.lineage_type || "外部";
      yield neoRecord([neoRel(vid, e.source_model_id, e.target_model_id, "EXTERNAL_LINEAGE", {
        rel_key: e.edge_key || ("external:" + vid),
        caption: "⬡ " + lt,
        external: true,
      })]);
    }
  };
}

/** neovis 配置：labels/relationships 把 Node 属性映射到 vis 节点/边 */
function neoVisConfig(dataFunction) {
  return {
    containerId: "neo-canvas",
    dataFunction,
    consoleDebug: false,
    groupAsLabel: false,
    labels: { Model: { label: "caption" } },
    defaultLabelConfig: { label: "caption" },
    relationships: {
      depends_on: { label: "caption" },
      joins_to: { label: "caption" },
      derived_from: { label: "caption" },
      references: { label: "caption" },
      EXTERNAL_LINEAGE: { label: "caption" },
    },
    defaultRelationshipsConfig: { label: "caption" },
    visConfig: {
      nodes: {
        shape: "box",
        margin: { top: 9, bottom: 9, left: 11, right: 11 },
        borderWidth: 1.6,
        borderWidthSelected: 2.6,
        shapeProperties: { borderRadius: 8 },
        color: {
          border: LAYER_FALLBACK_COLOR, background: NODE_BG,
          hover: { border: EDGE_FOCUS_COLOR, background: NODE_BG_HOVER },
          highlight: { border: EDGE_FOCUS_COLOR, background: NODE_BG_HOVER },
        },
        font: { ...NODE_FONT },
        shadow: { enabled: true, color: "rgba(2,6,16,0.45)", size: 14, x: 0, y: 4 },
      },
      edges: {
        arrows: { to: { enabled: true, scaleFactor: 0.62, type: "arrow" } },
        smooth: { enabled: true, type: "cubicBezier", forceDirection: "horizontal", roundness: 0.5 },
        color: { color: EDGE_COLOR, highlight: EDGE_FOCUS_COLOR, hover: EDGE_FOCUS_COLOR, inherit: false },
        width: 1.4,
        hoverWidth: 2.2,
        selectionWidth: 2.2,
        font: { color: "#aeb9d4", size: 10.5, face: "system-ui", strokeWidth: 3.5, strokeColor: "#0d1220", align: "middle" },
      },
      layout: { improvedLayout: false, randomSeed: 7 },
      physics: {
        enabled: true,
        solver: "barnesHut",
        barnesHut: {
          gravitationalConstant: -9500, centralGravity: 0.4, springLength: 195,
          springConstant: 0.035, damping: 0.62, avoidOverlap: 0.22,
        },
        stabilization: { enabled: true, iterations: 260, updateInterval: 20, fit: true },
        timestep: 0.42,
        adaptiveTimestep: true,
        minVelocity: 0.6,
        maxVelocity: 45,
      },
      interaction: {
        hover: true,
        hideEdgesOnDrag: false,
        hideNodesOnDrag: false,
        navigationButtons: false,
        keyboard: false,
        multiselect: false,
        selectable: true,
        dragNodes: true,
        dragView: true,
        zoomView: true,
        zoomSpeed: 0.65,
      },
    },
  };
}

/** 销毁当前图谱实例（切视图/重渲染前调用，释放 canvas 与监听） */
function destroyGraphViz() {
  if (state.graphViz) {
    try {
      const net = state.graphViz.network;
      if (net) {
        // neovis.js 在每次 render 数据加载完成后会挂一个 10s 定时器调用
        // network.stopSimulation()；而 vis 的 destroy() 会 delete this.physics，
        // 定时器晚到再执行即抛
        // "Cannot read properties of undefined (reading 'stopSimulation')"。
        // 先中和方法再销毁，让第三方定时器落在无副作用的空实现上。
        net.stopSimulation = function () {};
        net.stabilize = function () {};
        net.destroy();
      }
    } catch (e) { /* 忽略 */ }
    state.graphViz = null;
  }
  state.graphApplyHighlight = null;
}

async function renderGraphView(content) {
  if (!state.graphView) state.graphView = "current";
  // 高亮/选择状态归属单次渲染的画布实例：视图/快照切换即失效
  destroyGraphViz();
  state.graphPathFocus = null;
  state.graphSelectedNode = null;
  state.graphSelectedEdge = null;
  state.graphHoverNode = null;
  state.graphHoverEdge = null;
  content.innerHTML = `<div class="small muted">加载关系图…</div>`;

  // 快照历史：用于快照浏览器 + 历史快照视图默认选择
  let snapshots = [];
  try { snapshots = await api(`/api/projects/${state.projectId}/model-graph/snapshots`); }
  catch (e) { snapshots = []; }
  state.graphSnapshots = snapshots;
  if (state.graphView === "snapshot" && !state.graphSnapshotNo && snapshots.length) {
    state.graphSnapshotNo = snapshots[0].snapshot_no; // 最新（列表倒序）
  }

  // 图谱数据
  let g;
  try {
    const params = new URLSearchParams({ view: state.graphView });
    if (state.graphView === "snapshot" && state.graphSnapshotNo) {
      params.set("snapshot_no", String(state.graphSnapshotNo));
    }
    g = await api(`/api/projects/${state.projectId}/model-graph?${params.toString()}`);
  } catch (e) {
    if (e.code === "GRAPH_SNAPSHOT_NOT_FOUND") {
      content.innerHTML = `
        <div class="card">
          <div class="card-title">模型关系图</div>
          <div class="empty"><div class="empty-icon">▦</div>
            <div class="empty-title">${escapeHtml(graphViewMeta(state.graphView).label)}：${escapeHtml(e.message)}</div>
            <div class="empty-desc">发布模型后会自动生成项目图谱快照；迁移基线会在升级时创建一次。</div>
          </div>
        </div>`;
      renderGraphPanels(content, [], [], snapshots);
      return;
    }
    showError(e);
    return;
  }
  state.graphNodes = g.nodes || [];
  state.graphEdges = g.edges || [];
  state.graphExternalEdges = g.external_edges || [];
  state.graphPayload = g;

  const nodes = state.graphNodes;
  const edges = state.graphEdges;
  const externalEdges = state.graphExternalEdges;

  // vis id ↔ 业务键映射（路径查询高亮 / 边详情回查用）
  // _visId 在此统一派生：实时视图=库 id；快照视图=edge_key 哈希 id；外部边=EXT_EDGE_ID_BASE+id
  state.graphEdgeKeyToVisId = {};
  state.graphEdgeById = {};
  const usedVids = new Set();
  edges.forEach((e, i) => {
    let vid = edgeVisId(e, i);
    while (usedVids.has(vid)) vid += 1; // 哈希碰撞时顺延，保证 DataSet 内唯一
    usedVids.add(vid);
    e._visId = vid;
    if (e.edge_key) state.graphEdgeKeyToVisId[e.edge_key] = vid;
    state.graphEdgeById[vid] = { edge: e, external: false };
  });
  externalEdges.forEach((e, i) => {
    const vid = EXT_EDGE_ID_BASE + (e.id != null ? Number(e.id) : i);
    e._visId = vid;
    if (e.edge_key) state.graphEdgeKeyToVisId[e.edge_key] = vid;
    state.graphEdgeById[vid] = { edge: e, external: true };
  });

  const meta = graphViewMeta(state.graphView);
  const viewBadge = state.graphView === "published_latest" || state.graphView === "snapshot"
    ? `快照 #${g.snapshot_no} · ${g.snapshot_type === "migration_baseline" ? "升级基线" : "发布"} · hash ${String(g.graph_hash || "").slice(0, 12)}`
    : "";

  const legend = `
    <div class="graph-legend">
      ${GRAPH_LAYERS.map((l) => `<span><i class="legend-swatch" style="background:${LAYER_COLORS[l]}"></i>${l}</span>`).join("")}
      <span><i class="legend-swatch legend-swatch-ext"></i>⬡ 外部血缘</span>
      <span class="muted">拖动节点整理 · 滚轮缩放 · 空白拖拽平移 · 单击看详情 · 双击聚焦邻居</span>
    </div>`;

  content.innerHTML = `
    <div class="card">
      <div class="card-title">模型关系图 <span class="sub">${escapeHtml(meta.hint)}</span></div>
      <div class="graph-toolbar">
        <div class="seg" role="tablist">
          ${GRAPH_VIEWS.map((v) => `
            <button class="tab${state.graphView === v.key ? " active" : ""}" data-graph-view="${v.key}"
              aria-selected="${state.graphView === v.key}">${escapeHtml(v.label)}</button>`).join("")}
        </div>
        ${state.graphView === "snapshot" ? `
          <label class="small muted">快照号
            <select class="select" id="graph-snapshot-select" style="width:auto;display:inline-block">
              ${snapshots.map((s) => `
                <option value="${s.snapshot_no}" ${state.graphSnapshotNo === s.snapshot_no ? "selected" : ""}>
                  #${s.snapshot_no} · ${s.snapshot_type === "migration_baseline" ? "升级基线" : "发布"} · ${s.node_count}节点/${s.edge_count}边 · ${fmtTime(s.created_at)}
                </option>`).join("")}
            </select>
          </label>` : ""}
        <span class="graph-view-hint">${viewBadge ? escapeHtml(viewBadge) : ""}</span>
        <div class="graph-zoom-controls">
          <button class="btn btn-sm btn-ghost" id="graph-zoom-out" title="缩小">−</button>
          <span class="graph-zoom-label mono" id="graph-zoom-label">100%</span>
          <button class="btn btn-sm btn-ghost" id="graph-zoom-in" title="放大">＋</button>
          <button class="btn btn-sm btn-ghost" id="graph-fit" title="缩放平移以适应全部节点">⤢ 适应视图</button>
          <button class="btn btn-sm btn-ghost" id="graph-layout" title="在力导向与分层布局之间切换">分层布局</button>
          <button class="btn btn-sm btn-ghost${state.graphShowAllLabels ? " active" : ""}" id="graph-labels" title="显示 / 隐藏全部边标签">边标签</button>
          <button class="btn btn-sm btn-ghost" id="graph-restabilize" title="重新运行力导向布局计算">重新布局</button>
          <button class="btn btn-sm btn-ghost" id="graph-reload" title="重新拉取图谱数据">重载</button>
        </div>
      </div>
      <div class="graph-wrap">${legend}
        <div class="graph-stage" id="graph-stage">
          <div id="neo-canvas"></div>
          ${nodes.length ? "" : `<div class="empty" style="padding:60px 0"><div class="empty-desc">当前视图下暂无模型节点。</div></div>`}
          <div class="neo-loading" id="neo-loading"${nodes.length ? "" : " hidden"}><span class="neo-spinner"></span>图谱布局计算中…</div>
          <div class="neo-tip" id="neo-tip" hidden></div>
          <div class="graph-detail" id="graph-detail" hidden></div>
        </div>
      </div>
      <div class="small muted mt14">共 ${nodes.length} 个模型 / ${edges.length} 条关系${externalEdges.length ? ` / ${externalEdges.length} 条外部观测边` : ""}。图为候选视图：关系未配置、方向配置错误或存在未建模的外部依赖时，不代表完整真实血缘。</div>
    </div>`;

  $$("[data-graph-view]", content).forEach((el) => {
    el.addEventListener("click", () => {
      state.graphView = el.dataset.graphView;
      state.graphPathFocus = null; // 路径高亮归属旧视图
      if (state.graphView === "snapshot" && !state.graphSnapshotNo && state.graphSnapshots && state.graphSnapshots.length) {
        state.graphSnapshotNo = state.graphSnapshots[0].snapshot_no;
      }
      renderGraphView(content);
    });
  });
  const snapSel = $("#graph-snapshot-select", content);
  if (snapSel) {
    snapSel.addEventListener("change", () => {
      state.graphSnapshotNo = Number(snapSel.value);
      state.graphPathFocus = null;
      renderGraphView(content);
    });
  }

  if (nodes.length) initNeoGraph(content, nodes, edges, externalEdges);
  renderGraphPanels(content, nodes, edges, snapshots);
}

/** 解析 neovis.js 暴露的构造器：
 *  v2.x UMD 打包为命名空间对象（window.NeoVis.NeoVis / .default 才是构造器）；
 *  兼容旧版 UMD（全局即构造器）。取不到返回 null。 */
function getNeoVisCtor() {
  const g = window.NeoVis;
  if (!g) return null;
  if (typeof g === "function") return g;
  if (typeof g.NeoVis === "function") return g.NeoVis;
  if (typeof g.default === "function") return g.default;
  return null;
}

/** 创建 neovis 实例并接线事件 / 工具栏 */
function initNeoGraph(content, nodes, edges, externalEdges) {
  const NeoVisCtor = getNeoVisCtor();
  if (!NeoVisCtor) {
    const loading = $("#neo-loading", content);
    if (loading) loading.innerHTML = `<span style="color:var(--danger)">图谱组件（neovis.js）加载失败，请检查 /vendor/neovis.js</span>`;
    return;
  }
  let viz;
  try {
    viz = new NeoVisCtor(neoVisConfig(makeGraphDataFunction(nodes, edges, externalEdges)));
  } catch (e) {
    const loading = $("#neo-loading", content);
    if (loading) loading.innerHTML = `<span style="color:var(--danger)">图谱初始化失败：${escapeHtml(e.message)}</span>`;
    return;
  }
  state.graphViz = viz;
  // 每次重建画布后都要重新挂回高亮刷新回调（destroyGraphViz 会把它置空，
  // 否则路径查询等状态回调全部失效）
  state.graphApplyHighlight = applyNeoStyles;
  let wired = false;

  viz.registerOnEvent("completed", () => {
    const net = viz.network;
    hideNeoLoading(content);
    if (!net) return;
    applyNeoStyles();
    if (!wired) {
      wired = true;
      wireNeoNetworkEvents(net, viz, content);
      try { net.fit({ animation: false }); } catch (e) { /* 忽略 */ }
      updateNeoZoomLabel(content);
    }
  });
  viz.registerOnEvent("error", (e) => {
    hideNeoLoading(content);
    const err = (e && e.error) || e;
    const loading = $("#neo-loading", content);
    if (loading) {
      loading.hidden = false;
      loading.innerHTML = `<span style="color:var(--danger)">图谱渲染失败：${escapeHtml(err && err.message ? err.message : String(err))}</span>`;
    }
  });

  wireNeoToolbar(content, viz);

  try { viz.render(); } catch (e) { showNeoError(content, e); }
}

function hideNeoLoading(content) {
  const loading = $("#neo-loading", content);
  if (loading) loading.hidden = true;
}

function showNeoError(content, e) {
  const loading = $("#neo-loading", content);
  if (loading) {
    loading.hidden = false;
    loading.innerHTML = `<span style="color:var(--danger)">图谱渲染失败：${escapeHtml(e && e.message ? e.message : String(e))}</span>`;
  }
}

function updateNeoZoomLabel(content) {
  const viz = state.graphViz;
  const label = $("#graph-zoom-label", content || $("#content"));
  if (!viz || !viz.network || !label) return;
  label.textContent = Math.round(viz.network.getScale() * 100) + "%";
}

/** vis network 事件：hover 高亮 + 自绘 tooltip + 双击聚焦 + 空白取消选择 */
function wireNeoNetworkEvents(net, viz, content) {
  const tip = $("#neo-tip", content);
  const stage = $("#graph-stage", content);

  net.on("hoverNode", (e) => {
    state.graphHoverNode = e.node;
    applyNeoStyles();
    showNeoTip(tip, stage, e.pointer && e.pointer.DOM, neoTipHtmlNode(e.node));
  });
  net.on("blurNode", () => {
    state.graphHoverNode = null;
    applyNeoStyles();
    if (tip) tip.hidden = true;
  });
  net.on("hoverEdge", (e) => {
    state.graphHoverEdge = e.edge;
    applyNeoStyles();
    showNeoTip(tip, stage, e.pointer && e.pointer.DOM, neoTipHtmlEdge(e.edge));
  });
  net.on("blurEdge", () => {
    state.graphHoverEdge = null;
    applyNeoStyles();
    if (tip) tip.hidden = true;
  });
  net.on("doubleClick", (e) => {
    let id = e.nodes && e.nodes.length ? e.nodes[0] : null;
    if (id == null && e.pointer && e.pointer.DOM) id = net.getNodeAt(e.pointer.DOM);
    if (id != null) focusNeoNode(viz, id);
  });
  // 自绘 click：neovis 的 clickNode/clickEdge 依赖 selection，这里用
  // getNodeAt/getEdgeAt 兜底，保证点击节点出详情、点击边出关系信息
  net.on("click", (e) => {
    if (e.nodes && e.nodes.length) { selectNeoNode(viz, e.nodes[0]); return; }
    if (e.edges && e.edges.length) { selectNeoEdge(viz, e.edges[0]); return; }
    const dom = e.pointer && e.pointer.DOM;
    if (dom) {
      const nodeId = net.getNodeAt(dom);
      if (nodeId != null) { selectNeoNode(viz, nodeId); return; }
      const edgeId = net.getEdgeAt(dom);
      if (edgeId != null) { selectNeoEdge(viz, edgeId); return; }
    }
    clearNeoSelection(); // 空白单击：取消选择
  });
  net.on("zoom", () => updateNeoZoomLabel(content));
  // fit / 聚焦 / 布局切换后的稳定化都走动画或 stabilization fit，不会触发 zoom
  // 事件，需在动画结束 / 稳定化完成时补刷标签，避免显示陈旧百分比
  net.on("animationFinished", () => updateNeoZoomLabel(content));
  net.on("stabilizationIterationsDone", () => updateNeoZoomLabel(content));
  net.on("dragStart", () => { if (tip) tip.hidden = true; });
}

/** 选中边：详情卡 + 高亮两端 */
function selectNeoEdge(viz, visEdgeId) {
  state.graphSelectedEdge = visEdgeId;
  showNeoEdgeDetail(viz, visEdgeId);
  applyNeoStyles();
}

/** 工具栏：缩放 / 适应 / 布局切换 / 边标签 / 重新布局 / 重载 */
function wireNeoToolbar(content, viz) {
  const zoomBy = (factor) => {
    const net = viz.network;
    if (!net) return;
    // 下限 0.02：114 节点 fit 后 scale≈0.03，若钳在 0.12，缩小按钮会反向放大
    const s = Math.min(4, Math.max(0.02, net.getScale() * factor));
    net.moveTo({ scale: s, animation: true });
    updateNeoZoomLabel(content);
  };
  const zoomIn = $("#graph-zoom-in", content);
  if (zoomIn) zoomIn.addEventListener("click", () => zoomBy(1.3));
  const zoomOut = $("#graph-zoom-out", content);
  if (zoomOut) zoomOut.addEventListener("click", () => zoomBy(1 / 1.3));
  const fitBtn = $("#graph-fit", content);
  if (fitBtn) fitBtn.addEventListener("click", () => {
    const net = viz.network;
    if (net) net.fit({ animation: true });
  });
  const labelsBtn = $("#graph-labels", content);
  if (labelsBtn) labelsBtn.addEventListener("click", (e) => {
    state.graphShowAllLabels = !state.graphShowAllLabels;
    e.currentTarget.classList.toggle("active", state.graphShowAllLabels);
    applyNeoStyles();
  });
  const layoutBtn = $("#graph-layout", content);
  if (layoutBtn) layoutBtn.addEventListener("click", (e) => {
    state.graphLayoutMode = state.graphLayoutMode === "layered" ? "force" : "layered";
    const layered = state.graphLayoutMode === "layered";
    e.currentTarget.textContent = layered ? "力导向布局" : "分层布局";
    e.currentTarget.classList.toggle("active", layered);
    applyNeoLayout(viz);
  });
  const restabBtn = $("#graph-restabilize", content);
  if (restabBtn) restabBtn.addEventListener("click", () => {
    const net = viz.network;
    if (!net) return;
    try {
      net.stopSimulation();
      net.stabilize(); // stabilization.fit:true，结束后自动适应视图
    } catch (e) { /* 忽略 */ }
  });
  const reloadBtn = $("#graph-reload", content);
  if (reloadBtn) reloadBtn.addEventListener("click", () => renderGraphView(content));
}

/** 布局切换：力导向（barnesHut）↔ 分层（hierarchical LR，vis 自动切换 solver） */
function applyNeoLayout(viz) {
  const net = viz && viz.network;
  if (!net) return;
  if (state.graphLayoutMode === "layered") {
    net.setOptions({
      layout: {
        hierarchical: {
          enabled: true, direction: "LR", sortMethod: "directed",
          levelSeparation: 200, nodeSpacing: 140, treeSpacing: 220,
          blockShifting: true, edgeMinimization: true, parentCentralization: true,
          shakeTowards: "leaves",
        },
      },
      physics: {
        enabled: true,
        solver: "hierarchicalRepulsion",
        hierarchicalRepulsion: { nodeDistance: 165, springLength: 165, centralGravity: 0, springConstant: 0.01, damping: 0.4 },
        stabilization: { enabled: true, iterations: 150, fit: true },
        timestep: 0.5,
      },
      edges: { smooth: { enabled: true, type: "cubicBezier", forceDirection: "horizontal", roundness: 0.55 } },
    });
  } else {
    net.setOptions({
      layout: { hierarchical: { enabled: false } },
      physics: {
        enabled: true,
        solver: "barnesHut",
        barnesHut: {
          gravitationalConstant: -9500, centralGravity: 0.4, springLength: 195,
          springConstant: 0.035, damping: 0.62, avoidOverlap: 0.22,
        },
        stabilization: { enabled: true, iterations: 220, fit: true },
        timestep: 0.42,
        adaptiveTimestep: true,
      },
      edges: { smooth: { enabled: true, type: "cubicBezier", forceDirection: "horizontal", roundness: 0.5 } },
    });
  }
  applyNeoStyles();
  try { net.stabilize(); } catch (e) { /* 忽略 */ }
}

/* ---- 画布样式与高亮 ---- */

/** 依据聚焦状态重算全部节点/边样式（路径查询 > 选中/hover；其余淡化） */
function applyNeoStyles() {
  const viz = state.graphViz;
  if (!viz || !viz.network) return;
  const ds = viz.network.body.data;
  if (!ds || !ds.nodes) return;

  const focusNodes = new Set();
  const focusEdges = new Set();
  if (state.graphPathFocus) {
    (state.graphPathFocus.nodes || []).forEach((id) => focusNodes.add(id));
    (state.graphPathFocus.edges || []).forEach((k) => {
      const v = state.graphEdgeKeyToVisId ? state.graphEdgeKeyToVisId[k] : null;
      if (v != null) focusEdges.add(v);
    });
  }
  const selN = state.graphSelectedNode != null ? state.graphSelectedNode : state.graphHoverNode;
  if (selN != null) {
    focusNodes.add(selN);
    ds.edges.forEach((item) => {
      if (item.from === selN || item.to === selN) {
        focusEdges.add(item.id);
        focusNodes.add(item.from === selN ? item.to : item.from);
      }
    });
  }
  if (state.graphHoverEdge != null) {
    const item = ds.edges.get(state.graphHoverEdge);
    if (item) {
      focusEdges.add(item.id);
      focusNodes.add(item.from);
      focusNodes.add(item.to);
    }
  }
  const active = focusNodes.size > 0 || focusEdges.size > 0;

  const nodeUpdates = [];
  ds.nodes.forEach((item) => {
    const p = (item.raw && item.raw.properties) || {};
    const dim = active && !focusNodes.has(item.id);
    const focused = focusNodes.has(item.id);
    const layer = LAYER_COLORS[p.layer] || LAYER_FALLBACK_COLOR;
    nodeUpdates.push({
      id: item.id,
      color: {
        background: dim ? NODE_BG_DIM : NODE_BG,
        border: dim ? hexA(layer, 0.25) : (focused ? EDGE_FOCUS_COLOR : layer),
        hover: { border: EDGE_FOCUS_COLOR, background: NODE_BG_HOVER },
        highlight: { border: EDGE_FOCUS_COLOR, background: NODE_BG_HOVER },
      },
      font: dim ? NODE_FONT_DIM : NODE_FONT,
      borderWidth: focused ? 2.6 : 1.6,
    });
  });
  if (nodeUpdates.length) ds.nodes.update(nodeUpdates);

  const edgeUpdates = [];
  ds.edges.forEach((item) => {
    const p = (item.raw && item.raw.properties) || {};
    const external = p.external === true;
    const base = external ? EXTERNAL_EDGE_COLOR : EDGE_COLOR;
    const dim = active && !focusEdges.has(item.id);
    const focused = focusEdges.has(item.id);
    edgeUpdates.push({
      id: item.id,
      color: {
        color: dim ? hexA(base, 0.12) : (focused ? EDGE_FOCUS_COLOR : base),
        highlight: EDGE_FOCUS_COLOR,
        hover: EDGE_FOCUS_COLOR,
        inherit: false,
      },
      width: focused ? 2.6 : 1.4,
      dashes: external,
      label: (state.graphShowAllLabels || focused) ? (p.caption || undefined) : undefined,
    });
  });
  if (edgeUpdates.length) ds.edges.update(edgeUpdates);
}
state.graphApplyHighlight = applyNeoStyles;

/* ---- 自绘 tooltip ---- */

function showNeoTip(tip, stage, dom, html) {
  if (!tip || !stage || !html) { if (tip) tip.hidden = true; return; }
  tip.innerHTML = html;
  tip.hidden = false;
  const sw = stage.clientWidth, sh = stage.clientHeight;
  const px = dom ? dom.x : 24, py = dom ? dom.y : 24;
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let x = px + 16, y = py + 16;
  if (x + tw > sw - 8) x = Math.max(8, px - tw - 14);
  if (y + th > sh - 8) y = Math.max(8, py - th - 14);
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}

function neoTipHtmlNode(id) {
  const n = state.graphNodes.find((x) => x.id === id);
  if (!n) return "";
  return `<div class="neo-tip-title"><span class="graph-node-dot graph-node-dot-${n.status}"></span>${escapeHtml(n.name)}</div>
    <div class="mono neo-tip-key">${escapeHtml(n.model_key)}</div>
    <div class="neo-tip-rows">
      <div><span>分层</span><b>${escapeHtml(n.layer || "-")}</b></div>
      <div><span>引擎</span><b>${escapeHtml(n.target_engine || "-")}</b></div>
      <div><span>状态</span><b>${escapeHtml(STATUS_LABEL[n.status] || n.status)} · ${escapeHtml(nodeVersionText(n))}</b></div>
      <div><span>字段</span><b>${n.field_count}</b></div>
    </div>`;
}

function neoTipHtmlEdge(vid) {
  const rec = state.graphEdgeById[vid];
  if (!rec) return "";
  const e = rec.edge;
  if (rec.external) {
    return `<div class="neo-tip-title"><i class="legend-swatch legend-swatch-ext"></i>外部血缘</div>
      <div class="neo-tip-rows">
        <div><span>类型</span><b>${escapeHtml(LINEAGE_TYPE_LABEL[e.lineage_type] || e.lineage_type || "-")}</b></div>
        <div><span>来源</span><b>${escapeHtml(e.source_system || "-")}/${escapeHtml(e.scope_key || "-")}</b></div>
        <div><span>作业</span><b class="mono">${escapeHtml(e.job_key || "-")}</b></div>
        <div><span>置信度</span><b>${e.confidence != null ? e.confidence : "-"}</b></div>
      </div>`;
  }
  const sf = (e.source_fields || []).join(",");
  const tf = (e.target_fields || []).join(",");
  return `<div class="neo-tip-title">${escapeHtml(REL_TYPE_LABEL[e.relationship_type] || e.relationship_type)}</div>
    <div class="neo-tip-rows">
      <div><span>基数</span><b>${escapeHtml(CARDINALITY_LABEL[e.cardinality] || e.cardinality || "-")}</b></div>
      <div><span>字段</span><b class="mono">${escapeHtml(sf || "-")} → ${escapeHtml(tf || "-")}</b></div>
      <div><span>关系键</span><b class="mono">${escapeHtml(e.edge_key || "-")}</b></div>
    </div>`;
}

/* ---- 详情面板（Neo4j 式属性卡） ---- */

function showNeoNodeDetail(viz, modelId) {
  const content = $("#content");
  const detail = $("#graph-detail", content);
  if (!detail) return;
  const n = state.graphNodes.find((x) => x.id === modelId);
  if (!n) return;
  const nodeById = new Map(state.graphNodes.map((x) => [x.id, x]));

  const ups = [];
  const downs = [];
  state.graphEdges.forEach((e) => {
    if (e.target_model_id === modelId) ups.push({ e, external: false });
    if (e.source_model_id === modelId) downs.push({ e, external: false });
  });
  state.graphExternalEdges.forEach((e, i) => {
    const vid = EXT_EDGE_ID_BASE + (e.id != null ? Number(e.id) : i);
    if (e.target_model_id === modelId) ups.push({ e, external: true });
    if (e.source_model_id === modelId) downs.push({ e, external: true });
  });

  const relLine = (rec, dir) => {
    const e = rec.e;
    const otherId = dir === "up" ? e.source_model_id : e.target_model_id;
    const other = nodeById.get(otherId);
    const type = rec.external
      ? "外部 " + (LINEAGE_TYPE_LABEL[e.lineage_type] || e.lineage_type)
      : (REL_TYPE_LABEL[e.relationship_type] || e.relationship_type);
    return `<li data-neo-focus-node="${otherId}" title="点击聚焦该模型">
      <span class="mono">${escapeHtml(other ? other.model_key : "#" + otherId)}</span>
      <span class="muted">${escapeHtml(type)}</span></li>`;
  };
  const relBlock = (title, list) => list.length
    ? `<div class="small mt4"><b>${title}</b> <span class="muted">${list.length}</span></div>
       <ul class="graph-detail-list">${list.slice(0, 6).map((r, i) => relLine(r, title === "上游" ? "up" : "down")).join("")}
       ${list.length > 6 ? `<li class="muted">等 ${list.length} 条…</li>` : ""}</ul>`
    : "";

  detail.innerHTML = `
    <div class="graph-detail-head">
      <span class="graph-node-dot graph-node-dot-${n.status}"></span>
      <span class="graph-detail-name">${escapeHtml(n.name)}</span>
      <button class="graph-detail-close" id="graph-detail-close" title="关闭">×</button>
    </div>
    <div class="graph-detail-rows">
      <div><span>模型键</span><b class="mono">${escapeHtml(n.model_key)}</b></div>
      <div><span>分层 / 引擎</span><b>${escapeHtml(n.layer)} · ${escapeHtml(n.target_engine)}</b></div>
      <div><span>状态 / 版本</span><b>${escapeHtml(STATUS_LABEL[n.status] || n.status)} · ${escapeHtml(nodeVersionText(n))}</b></div>
      <div><span>字段数</span><b>${n.field_count}</b></div>
    </div>
    <div class="graph-detail-rels">${ups.length || downs.length
      ? `${relBlock("上游", ups)}${relBlock("下游", downs)}`
      : `<div class="small muted">暂无关联关系（可在模型编辑器「模型关系」页配置）</div>`}</div>
    <div class="graph-detail-actions">
      <button class="btn btn-sm btn-primary" id="graph-detail-open">打开编辑器</button>
      <button class="btn btn-sm" id="graph-detail-focus">聚焦邻居</button>
    </div>`;
  detail.hidden = false;

  $("#graph-detail-close", detail).addEventListener("click", clearNeoSelection);
  $("#graph-detail-open", detail).addEventListener("click", () => {
    if (state.editor && state.editor.dirty) {
      confirmModal("放弃未保存的更改？",
        "当前模型编辑器存在未保存修改，进入其他模型将丢弃这些更改。",
        "放弃更改", () => openModelEditor(modelId));
    } else {
      openModelEditor(modelId);
    }
  });
  $("#graph-detail-focus", detail).addEventListener("click", () => {
    const ids = new Set([modelId]);
    state.graphEdges.forEach((e) => {
      if (e.source_model_id === modelId) ids.add(e.target_model_id);
      if (e.target_model_id === modelId) ids.add(e.source_model_id);
    });
    fitNeoNodes(viz, ids);
  });
  $$("[data-neo-focus-node]", detail).forEach((el) => {
    el.addEventListener("click", () => focusNeoNode(viz, Number(el.dataset.neoFocusNode)));
  });
}

function showNeoEdgeDetail(viz, visEdgeId) {
  const content = $("#content");
  const detail = $("#graph-detail", content);
  if (!detail) return;
  const rec = state.graphEdgeById[visEdgeId];
  if (!rec) return;
  const e = rec.edge;
  const nodeById = new Map(state.graphNodes.map((x) => [x.id, x]));
  const src = nodeById.get(e.source_model_id);
  const tgt = nodeById.get(e.target_model_id);

  const rows = rec.external ? `
      <div><span>血缘类型</span><b>${escapeHtml(LINEAGE_TYPE_LABEL[e.lineage_type] || e.lineage_type || "-")}</b></div>
      <div><span>来源系统</span><b>${escapeHtml(e.source_system || "-")}/${escapeHtml(e.scope_key || "-")}</b></div>
      <div><span>作业</span><b class="mono">${escapeHtml(e.job_key || "-")}</b></div>
      <div><span>置信度</span><b>${e.confidence != null ? e.confidence : "-"}</b></div>`
    : `
      <div><span>关系类型</span><b>${escapeHtml(REL_TYPE_LABEL[e.relationship_type] || e.relationship_type)}</b></div>
      <div><span>基数</span><b>${escapeHtml(CARDINALITY_LABEL[e.cardinality] || e.cardinality || "-")}</b></div>
      <div><span>关联字段</span><b class="mono">${escapeHtml((e.source_fields || []).join(",") || "-")} → ${escapeHtml((e.target_fields || []).join(",") || "-")}</b></div>
      <div><span>关系键</span><b class="mono">${escapeHtml(e.edge_key || "-")}</b></div>`;

  detail.innerHTML = `
    <div class="graph-detail-head">
      <i class="legend-swatch${rec.external ? " legend-swatch-ext" : ""}"></i>
      <span class="graph-detail-name">${rec.external ? "外部血缘边" : escapeHtml(REL_TYPE_LABEL[e.relationship_type] || e.relationship_type)}</span>
      <button class="graph-detail-close" id="graph-detail-close" title="关闭">×</button>
    </div>
    <div class="graph-detail-rows">
      <div><span>起点</span><b class="mono">${escapeHtml(src ? src.model_key : "#" + e.source_model_id)}</b></div>
      <div><span>终点</span><b class="mono">${escapeHtml(tgt ? tgt.model_key : "#" + e.target_model_id)}</b></div>
      ${rows}
      ${!rec.external && e.description ? `<div><span>说明</span><b style="font-weight:400">${escapeHtml(e.description)}</b></div>` : ""}
    </div>
    <div class="graph-detail-actions">
      <button class="btn btn-sm" id="graph-edge-focus">聚焦两端</button>
    </div>`;
  detail.hidden = false;
  $("#graph-detail-close", detail).addEventListener("click", clearNeoSelection);
  $("#graph-edge-focus", detail).addEventListener("click", () => {
    fitNeoNodes(viz, new Set([e.source_model_id, e.target_model_id]));
  });
}

/** 选中节点：详情 + 邻居高亮（不移动视图；双击/跳转才聚焦） */
function selectNeoNode(viz, id) {
  state.graphSelectedNode = id;
  showNeoNodeDetail(viz, id);
  applyNeoStyles();
}

/** 聚焦节点：选中 + 高亮邻居 + 适配视图（双击 / 关系列表跳转用） */
function focusNeoNode(viz, id) {
  selectNeoNode(viz, id);
  const ids = new Set([id]);
  state.graphEdges.forEach((e) => {
    if (e.source_model_id === id) ids.add(e.target_model_id);
    if (e.target_model_id === id) ids.add(e.source_model_id);
  });
  fitNeoNodes(viz, ids);
}

function clearNeoSelection() {
  state.graphSelectedNode = null;
  state.graphSelectedEdge = null;
  const detail = $("#graph-detail", $("#content"));
  if (detail) { detail.hidden = true; detail.innerHTML = ""; }
  applyNeoStyles();
}

function fitNeoNodes(viz, ids) {
  const net = viz && viz.network;
  if (!net) return;
  const list = [...ids].filter((x) => x != null);
  if (!list.length) { net.fit({ animation: true }); return; }
  net.fit({ nodes: list, animation: { duration: 350, easingFunction: "easeInOutQuad" } });
}

/** 图谱下方面板：最短路径查询（独立全宽横排，结果分页）
 *  + 快照历史与对比 / 外部血缘导入（两列并列） */
function renderGraphPanels(content, nodes, edges, snapshots) {
  const nodeOptions = nodes.map((n) =>
    `<option value="${n.id}">${escapeHtml(n.model_key)} · ${escapeHtml(n.name)}</option>`).join("");
  state.pathRows = [];
  state.pathPage = 1;

  // ① 最短路径查询：单独占一横排（全宽卡片）
  const pathCard = document.createElement("div");
  pathCard.className = "card";
  pathCard.id = "graph-path-card";
  pathCard.innerHTML = `
    <div class="card-title">最短路径查询 <span class="sub">BFS 边数最少；depth 上限 1–50；结果按当前视图计算并分页展示</span></div>
    ${nodes.length >= 2 ? `
      <div class="path-form">
        <select class="select" id="path-source">${nodeOptions}</select>
        <span class="muted">→</span>
        <select class="select" id="path-target">${nodeOptions}</select>
        <select class="select" id="path-direction">
          ${PATH_DIRECTIONS.map((d) => `<option value="${d.value}">${escapeHtml(d.label)}</option>`).join("")}
        </select>
        <input class="input" id="path-max-depth" type="number" min="1" max="50" value="20" style="width:96px" title="max_depth" />
        <button class="btn btn-primary btn-sm" id="btn-query-path">查询路径</button>
      </div>
      <div id="path-result"></div>`
    : `<div class="small muted">需要至少两个模型节点才能查询路径。</div>`}
  `;
  content.appendChild(pathCard);

  // ② 快照对比 + 外部血缘导入：下方两列并列
  const panels = document.createElement("div");
  panels.className = "panel-grid";
  panels.innerHTML = `
    <div class="card" id="graph-snapshot-card">
      <div class="card-title">快照历史与对比 <span class="sub">不可变；diff 按模型 id 差集与 edge_key 对齐</span></div>
      ${snapshots.length ? `
        <div class="mini-form">
          <select class="select" id="diff-from" style="flex:1">
            ${snapshots.slice().sort((a, b) => a.snapshot_no - b.snapshot_no).map((s) => `
              <option value="${s.snapshot_no}">#${s.snapshot_no} ${s.snapshot_type === "migration_baseline" ? "升级基线" : "发布"}</option>`).join("")}
          </select>
          <span class="muted">→</span>
          <select class="select" id="diff-to" style="flex:1">
            ${snapshots.slice().sort((a, b) => a.snapshot_no - b.snapshot_no).map((s) => `
              <option value="${s.snapshot_no}" ${s.snapshot_no === snapshots[0].snapshot_no ? "selected" : ""}>#${s.snapshot_no} ${s.snapshot_type === "migration_baseline" ? "升级基线" : "发布"}</option>`).join("")}
          </select>
          <button class="btn btn-sm" id="btn-run-diff">对比</button>
        </div>
        <div class="table-wrap"><table class="data">
          <thead><tr><th>快照</th><th>类型</th><th class="num">节点</th><th class="num">关系</th><th class="num">未纳入</th><th>hash</th><th>时间</th></tr></thead>
          <tbody>
            ${snapshots.map((s) => `
              <tr>
                <td class="mono">#${s.snapshot_no}</td>
                <td>${s.snapshot_type === "migration_baseline" ? '<span class="badge badge-warning">升级基线</span>' : '<span class="badge badge-published">发布</span>'}</td>
                <td class="num">${s.node_count}</td>
                <td class="num">${s.edge_count}</td>
                <td class="num">${s.omitted_edge_count}</td>
                <td class="mono small" title="${escapeHtml(s.graph_hash)}">${escapeHtml(String(s.graph_hash || "").slice(0, 10))}</td>
                <td class="small">${fmtTime(s.created_at)}</td>
              </tr>`).join("")}
          </tbody>
        </table></div>
        <div id="diff-result" class="small muted mt8">选择两个快照号后点击对比。</div>`
      : `<div class="small muted">暂无快照。发布模型后自动生成；升级基线在首次启动时创建一次。</div>`}
    </div>

    <div class="card" id="graph-lineage-card">
      <div class="card-title">外部血缘导入 <span class="sub">来源系统观测边；与设计关系分域</span></div>
      <details class="lineage-panel"${state.lineageDetailsOpen ? " open" : ""}>
        <summary>导入批次（JSON）· 预检 / 提交</summary>
        <div class="small muted mt8">原子校验、幂等 upsert；同 scope+run_id 同 payload 重放返回 replayed=true，不同 payload 返回 409。full_snapshot 必须非 default scope。</div>
        <textarea class="textarea import-json" id="lineage-json" spellcheck="false">${escapeHtml(state.lineageJsonText || lineageImportTemplate(nodes))}</textarea>
        <div class="mini-form mt8">
          <button class="btn btn-sm" id="btn-lineage-preview">预检（只读）</button>
          <button class="btn btn-sm btn-primary" id="btn-lineage-import">提交导入</button>
          <span class="small muted">导入后可在下方批次表与外部边列表中查看。</span>
        </div>
        <div id="lineage-result" class="small muted">${state.lineageResultHtml || ""}</div>
      </details>
      <div class="mt12"><div class="card-title" style="font-size:13px">最近导入批次</div>
        <div id="lineage-runs" class="small muted">加载中…</div>
      </div>
      <div class="mt12"><div class="card-title" style="font-size:13px">来源健康度 <span class="sub">stale_after_minutes 阈值</span></div>
        <div id="lineage-health" class="small muted">加载中…</div>
      </div>
    </div>`;
  content.appendChild(panels);

  // --- 路径查询（结果分页：默认每页 10 条，每页数量可在结果筛选栏调整） ---
  const btnPath = $("#btn-query-path", pathCard);
  if (btnPath) {
    btnPath.addEventListener("click", async () => {
      const src = $("#path-source", pathCard).value;
      const tgt = $("#path-target", pathCard).value;
      const dir = $("#path-direction", pathCard).value;
      const depth = $("#path-max-depth", pathCard).value;
      const out = $("#path-result", pathCard);
      out.innerHTML = `<span class="muted">查询中…</span>`;
      try {
        const params = new URLSearchParams({
          source_model_id: src, target_model_id: tgt,
          direction: dir, max_depth: depth, view: state.graphView,
        });
        if (state.graphView === "snapshot" && state.graphSnapshotNo) {
          params.set("snapshot_no", String(state.graphSnapshotNo));
        }
        const r = await api(`/api/projects/${state.projectId}/model-graph/path?${params.toString()}`);
        if (r.found) {
          // 路径聚焦：在图谱中高亮该路径的节点与边，并适配视图到路径
          state.graphPathFocus = {
            nodes: new Set(r.nodes.map((n) => n.id)),
            edges: new Set(r.edges.map((e) => e.edge_key)),
          };
          if (state.graphApplyHighlight) state.graphApplyHighlight();
          if (state.graphViz) fitNeoNodes(state.graphViz, state.graphPathFocus.nodes);
          showPathResult(out, r);
        } else {
          state.graphPathFocus = null;
          if (state.graphApplyHighlight) state.graphApplyHighlight();
          const reasonText = r.reason === "MAX_DEPTH_REACHED"
            ? `深度上限 ${depth} 内未到达：仍存在未探索邻居（truncated）`
            : "在允许范围内完整探索后确认无路径";
          out.innerHTML = `<div class="small"><b>未找到路径</b> · ${escapeHtml(reasonText)}</div>`;
        }
      } catch (e) { showError(e); out.innerHTML = `<span class="small" style="color:var(--danger)">${escapeHtml(e.message || "查询失败")}</span>`; }
    });
  }

  // --- 快照 diff ---
  const btnDiff = $("#btn-run-diff", panels);
  if (btnDiff) {
    btnDiff.addEventListener("click", async () => {
      const from = $("#diff-from", panels).value;
      const to = $("#diff-to", panels).value;
      const out = $("#diff-result", panels);
      out.innerHTML = `<span class="muted">计算中…</span>`;
      try {
        const d = await api(`/api/projects/${state.projectId}/model-graph/diff?from_snapshot_no=${from}&to_snapshot_no=${to}`);
        const nodeLines = [
          d.nodes.added.length ? `<li>新增节点 ${d.nodes.added.length}：${d.nodes.added.map((n) => escapeHtml(n.model_key)).join(", ")}</li>` : "",
          d.nodes.removed.length ? `<li>移除节点 ${d.nodes.removed.length}：${d.nodes.removed.map((n) => escapeHtml(n.model_key)).join(", ")}</li>` : "",
          d.nodes.modified.length ? `<li>节点属性变更 ${d.nodes.modified.length}：${d.nodes.modified.map((m) => `${escapeHtml(m.model_key)}.${escapeHtml(m.attribute)}`).join(", ")}</li>` : "",
        ].filter(Boolean);
        const edgeLines = [
          d.edges.added.length ? `<li>新增关系 ${d.edges.added.length}：${d.edges.added.map((e) => escapeHtml(e.edge_key)).join(", ")}</li>` : "",
          d.edges.removed.length ? `<li>移除关系 ${d.edges.removed.length}：${d.edges.removed.map((e) => escapeHtml(e.edge_key)).join(", ")}</li>` : "",
          d.edges.modified.length ? `<li>关系属性变更 ${d.edges.modified.length}</li>` : "",
        ].filter(Boolean);
        out.innerHTML = `
          <div class="small"><b>#${from} → #${to}</b>${nodeLines.length || edgeLines.length ? "" : " 无差异"}</div>
          ${nodeLines.length ? `<ul class="small mt8" style="padding-left:18px">${nodeLines.join("")}</ul>` : ""}
          ${edgeLines.length ? `<ul class="small mt8" style="padding-left:18px">${edgeLines.join("")}</ul>` : ""}
          <div class="small muted mt8">hash ${escapeHtml(String(d.from_hash).slice(0, 10))} → ${escapeHtml(String(d.to_hash).slice(0, 10))}</div>`;
      } catch (e) { showError(e); out.innerHTML = `<span class="small" style="color:var(--danger)">${escapeHtml(e.message || "对比失败")}</span>`; }
    });
  }

  // --- 血缘导入 ---
  loadLineagePanels(panels);

  const lineageDetails = $("details.lineage-panel", panels);
  if (lineageDetails) {
    $("details.lineage-panel summary", panels).addEventListener("click", () => {
      state.lineageDetailsOpen = lineageDetails.open;
    });
  }

  const btnPreview = $("#btn-lineage-preview", panels);
  if (btnPreview) {
    btnPreview.addEventListener("click", () => submitLineage(panels, "preview"));
  }
  const btnImport = $("#btn-lineage-import", panels);
  if (btnImport) {
    btnImport.addEventListener("click", () => submitLineage(panels, "import"));
  }
}

// ---------------------------------------------------------------- 路径查询结果分页
// 结果按路径节点步进分页展示：默认每页 10 条，每页数量在结果筛选栏调整
// （10 / 20 / 50 / 全部）；未查询时结果区为空、不占空间。

const PATH_PAGE_SIZES = [10, 20, 50, 0]; // 0 = 全部

function pathPageCount() {
  const n = (state.pathRows || []).length;
  if (!state.pathPageSize) return 1;
  return Math.max(1, Math.ceil(n / state.pathPageSize));
}

/** 边的关系类型：优先从当前视图边数据回查，退化时从 edge_key 末段解析 */
function pathEdgeMeta(edgeKey) {
  const e = state.graphEdges.find((x) => x.edge_key === edgeKey)
    || state.graphExternalEdges.find((x) => x.edge_key === edgeKey);
  if (e) return e.relationship_type || e.lineage_type || null;
  const parts = String(edgeKey || "").split(":");
  return parts.length >= 4 ? parts[parts.length - 1] : null;
}

/** 渲染「找到路径」结果：摘要 + 分页筛选栏 + 分页表格 + 高亮控制 */
function showPathResult(out, r) {
  state.pathRows = r.nodes.map((n, i) => ({ node: n, edge: r.edges[i - 1] || null }));
  state.pathPage = 1;
  state.pathPageSize = 10;
  const pathText = r.nodes.map((n) => escapeHtml(n.model_key)).join(" → ");
  out.innerHTML = `
    <div class="small"><b>找到路径</b> · 距离 ${r.distance} · 视图 ${escapeHtml(r.view)}${r.snapshot_no ? ` #${r.snapshot_no}` : ""} · 已在上方图谱高亮</div>
    <div class="mono small mt8">${pathText}</div>
    <div class="path-toolbar">
      <span class="small muted" id="path-page-info"></span>
      <span class="grow"></span>
      <label class="small muted nowrap">每页
        <select class="select" id="path-page-size">
          ${PATH_PAGE_SIZES.map((s) => `<option value="${s}"${s === 10 ? " selected" : ""}>${s === 0 ? "全部" : s}</option>`).join("")}
        </select> 条
      </label>
    </div>
    <div class="table-wrap mt8"><table class="data" id="path-table">
      <thead><tr><th style="width:40px">#</th><th>模型</th><th>分层</th><th>到达关系</th><th>遍历方向</th></tr></thead>
      <tbody></tbody>
    </table></div>
    <div class="path-pager">
      <button class="btn btn-sm btn-ghost" id="path-prev">‹ 上一页</button>
      <span class="small muted" id="path-page-label"></span>
      <button class="btn btn-sm btn-ghost" id="path-next">下一页 ›</button>
      <span class="grow"></span>
      <button class="btn btn-sm" id="btn-clear-path-focus">清除图谱高亮</button>
    </div>`;
  renderPathPage(out);
  $("#path-page-size", out).addEventListener("change", (e) => {
    state.pathPageSize = Number(e.target.value);
    state.pathPage = 1;
    renderPathPage(out);
  });
  $("#path-prev", out).addEventListener("click", () => {
    if (state.pathPage > 1) { state.pathPage -= 1; renderPathPage(out); }
  });
  $("#path-next", out).addEventListener("click", () => {
    if (state.pathPage < pathPageCount()) { state.pathPage += 1; renderPathPage(out); }
  });
  $("#btn-clear-path-focus", out).addEventListener("click", () => {
    state.graphPathFocus = null;
    if (state.graphApplyHighlight) state.graphApplyHighlight();
    state.pathRows = [];
    out.innerHTML = `<span class="small muted">已清除路径高亮，可调整起终点重新查询。</span>`;
  });
}

/** 按当前页渲染路径结果表格与分页状态 */
function renderPathPage(out) {
  const rows = state.pathRows || [];
  const size = state.pathPageSize;
  const pages = pathPageCount();
  const start = size ? (state.pathPage - 1) * size : 0;
  const pageRows = rows.slice(start, size ? start + size : rows.length);
  const tbody = $("#path-table tbody", out);
  if (tbody) {
    tbody.innerHTML = pageRows.map((row, i) => {
      const n = row.node;
      const e = row.edge;
      const rel = e ? pathEdgeMeta(e.edge_key) : null;
      const relLabel = rel ? (REL_TYPE_LABEL[rel] || rel) : "—";
      const key = String(e && e.edge_key || "");
      return `<tr>
        <td class="mono muted">${start + i + 1}</td>
        <td><span class="link" data-path-node="${n.id}" title="在图谱中聚焦该模型">${escapeHtml(n.model_key)}</span><div class="small muted">${escapeHtml(n.name)}</div></td>
        <td>${escapeHtml(n.layer)}</td>
        <td>${e
          ? `<div class="small">${escapeHtml(relLabel)}</div><div class="mono small muted" title="${escapeHtml(key)}">${escapeHtml(key.length > 30 ? key.slice(0, 30) + "…" : key)}</div>`
          : `<span class="muted">起点</span>`}</td>
        <td class="small">${e ? (e.traversal_direction === "reverse" ? "反向" : "正向") : "—"}</td>
      </tr>`;
    }).join("");
    $$("[data-path-node]", tbody).forEach((el) =>
      el.addEventListener("click", () => {
        if (state.graphViz) focusNeoNode(state.graphViz, Number(el.dataset.pathNode));
      }));
  }
  const info = $("#path-page-info", out);
  if (info) info.textContent = `共 ${rows.length} 个路径节点`;
  const label = $("#path-page-label", out);
  if (label) label.textContent = `第 ${state.pathPage} / ${pages} 页`;
  const prev = $("#path-prev", out);
  if (prev) prev.disabled = state.pathPage <= 1;
  const next = $("#path-next", out);
  if (next) next.disabled = state.pathPage >= pages;
}

async function submitLineage(root, mode) {
  const out = $("#lineage-result", root);
  const text = $("#lineage-json", root).value;
  let payload;
  try { payload = JSON.parse(text); }
  catch (e) {
    out.innerHTML = `<span style="color:var(--danger)">JSON 解析失败：${escapeHtml(e.message)}</span>`;
    return;
  }
  out.innerHTML = `<span class="muted">${mode === "preview" ? "预检中…" : "导入中…"}</span>`;
  const details = root.querySelector("details.lineage-panel");
  if (details) state.lineageDetailsOpen = details.open;
  state.lineageJsonText = text; // 跨重渲染保留用户编辑内容
  try {
    const r = await api(`/api/projects/${state.projectId}/lineage/${mode}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (mode === "preview") {
      out.innerHTML = `<span style="color:var(--success)">预检通过（未写入）：预计 新增 ${r.inserted} · 更新 ${r.updated} · 停用 ${r.deactivated} · 收到 ${r.received}</span>`;
    } else if (r.replayed) {
      out.innerHTML = `<span style="color:var(--info)">幂等重放：该批次已成功导入过（replayed=true），未重复写入。</span>`;
    } else {
      out.innerHTML = `<span style="color:var(--success)">导入成功：新增 ${r.inserted} · 更新 ${r.updated} · 停用 ${r.deactivated} · 收到 ${r.received}</span>`;
    }
    // 结果与展开状态随重渲染保留
    state.lineageResultHtml = out.innerHTML;
    loadLineagePanels(root);
    // 导入成功后刷新图谱（新增外部边即时可见）；预检不重渲染
    if (mode === "import" && state.graphView === "lineage") {
      renderGraphView($("#content"));
    }
  } catch (e) {
    let detail = escapeHtml(e.message || "导入失败");
    if (e.details && e.details.errors && e.details.errors.length) {
      detail += `<ul class="small mt8" style="padding-left:18px">` +
        e.details.errors.map((err) =>
          `<li>${err.row_index != null ? `第 ${err.row_index} 条边 · ` : ""}${escapeHtml(err.field || "")} · ${escapeHtml(err.code || "")}：${escapeHtml(err.message || "")}</li>`).join("") +
        `</ul>`;
    }
    out.innerHTML = `<span style="color:var(--danger)">${detail}</span>`;
    state.lineageResultHtml = out.innerHTML;
    loadLineagePanels(root);
  }
}

async function loadLineagePanels(root) {
  const runsEl = $("#lineage-runs", root);
  const healthEl = $("#lineage-health", root);
  if (!runsEl || !healthEl) return;
  try {
    const [runs, health, edges] = await Promise.all([
      api(`/api/projects/${state.projectId}/lineage/import-runs?limit=20`),
      api(`/api/projects/${state.projectId}/lineage/health`),
      api(`/api/projects/${state.projectId}/lineage/edges?limit=200`),
    ]);
    state.graphExternalEdges = edges;
    runsEl.innerHTML = runs.length ? `
      <div class="table-wrap"><table class="data">
        <thead><tr><th>来源/scope</th><th>run_id</th><th>模式</th><th>状态</th><th class="num">收/增/更/停</th><th class="num">尝试/失败</th><th>时间</th><th>操作</th></tr></thead>
        <tbody>
          ${runs.map((r) => `
            <tr>
              <td class="mono">${escapeHtml(r.source_system)}/${escapeHtml(r.scope_key)}</td>
              <td class="mono small">${escapeHtml(r.run_id)}</td>
              <td>${SCAN_MODE_LABEL[r.scan_mode] || r.scan_mode}${r.complete_scan ? " · 完整" : ""}</td>
              <td>${r.status === "completed" ? '<span class="badge badge-published">成功</span>' : '<span class="badge badge-error">失败</span>'}</td>
              <td class="num mono">${r.received_count}/${r.inserted_count}/${r.updated_count}/${r.deactivated_count}</td>
              <td class="num mono">${r.attempt_count}/${r.failure_count}</td>
              <td class="small">${fmtTime(r.created_at)}</td>
              <td class="nowrap">${r.status === "failed" && r.error_summary && r.error_summary.length
                ? `<button class="btn btn-sm" data-run-errors="${r.id}">错误摘要</button>` : ""}</td>
            </tr>`).join("")}
        </tbody>
      </table></div>`
      : `<div class="small muted">暂无导入批次。</div>`;

    $$("[data-run-errors]", runsEl).forEach((el) => {
      el.addEventListener("click", () => {
        const run = runs.find((r) => String(r.id) === el.dataset.runErrors);
        if (!run) return;
        openModal(`批次错误摘要 · ${run.source_system}/${run.run_id}`,
          `<div class="small muted mb8">scope ${escapeHtml(run.scope_key)} · 尝试 ${run.attempt_count} 次 · 失败 ${run.failure_count} 次 · request_id ${escapeHtml(run.request_id || "-")}</div>
           <pre class="mono small" style="white-space:pre-wrap">${escapeHtml(JSON.stringify(run.error_summary, null, 2))}</pre>`,
          [{ label: "关闭", cls: "btn-ghost", onClick: closeModal }]);
      });
    });

    healthEl.innerHTML = `
      <div class="small muted mb8">stale 阈值 ${health.stale_after_minutes} 分钟 · active 边 ${health.total_active_edges} · 过期候选 ${health.total_stale_candidates}</div>
      ${health.groups.length ? `<div class="table-wrap"><table class="data">
        <thead><tr><th>来源/scope</th><th class="num">active</th><th class="num">inactive</th><th>最近成功扫描</th><th>最近失败</th><th class="num">连续失败</th><th class="num">过期候选</th></tr></thead>
        <tbody>
          ${health.groups.map((g) => `
            <tr>
              <td class="mono">${escapeHtml(g.source_system)}/${escapeHtml(g.scope_key)}</td>
              <td class="num">${g.active_edges}</td>
              <td class="num">${g.inactive_edges}</td>
              <td class="small">${fmtTime(g.last_successful_run_at)}</td>
              <td class="small">${fmtTime(g.last_failed_run_at)}</td>
              <td class="num">${g.consecutive_failures}</td>
              <td class="num">${g.stale_candidates}</td>
            </tr>`).join("")}
        </tbody>
      </table></div>`
      : `<div class="small muted">暂无外部血缘数据。</div>`}

      <div class="mt12"><div class="card-title" style="font-size:13px">外部观测边 <span class="sub">active ${edges.length} 条（当前过滤）</span></div>
        ${edges.length ? `<div class="table-wrap"><table class="data">
          <thead><tr><th>来源/scope</th><th>source_ref</th><th>类型</th><th class="num">置信</th><th>最近观测</th><th>操作</th></tr></thead>
          <tbody>
            ${edges.slice(0, 50).map((e) => `
              <tr>
                <td class="mono small">${escapeHtml(e.source_system)}/${escapeHtml(e.scope_key)}</td>
                <td class="mono small" title="${escapeHtml(e.source_ref)}">${escapeHtml(String(e.source_ref).slice(0, 28))}</td>
                <td>${LINEAGE_TYPE_LABEL[e.lineage_type] || e.lineage_type}</td>
                <td class="num">${e.confidence}</td>
                <td class="small">${fmtTime(e.last_seen_at || e.observed_at)}</td>
                <td class="nowrap"><button class="btn btn-sm" data-edge-deactivate="${e.id}">停用</button></td>
              </tr>`).join("")}
          </tbody>
        </table></div>`
        : `<div class="small muted">暂无 active 外部边。</div>`}
      </div>`;

    $$("[data-edge-deactivate]", healthEl).forEach((el) => {
      el.addEventListener("click", async () => {
        try {
          await api(`/api/projects/${state.projectId}/lineage/edges/${el.dataset.edgeDeactivate}/deactivate`, { method: "POST" });
          toast("已停用外部边");
          loadLineagePanels(root);
        } catch (e) { showError(e); }
      });
    });
  } catch (e) {
    runsEl.innerHTML = `<span class="small" style="color:var(--danger)">${escapeHtml(e.message || "加载失败")}</span>`;
    healthEl.innerHTML = "";
  }
}

// ---------------------------------------------------------------- 模型编辑器

async function openModelEditor(modelId) {
  try {
    const model = await api(`/api/models/${modelId}`);
    pushRecentModel(modelId);
    state.editor = {
      model,
      tab: "basic",
      fields: model.fields.map((f) => ({ ...f })),
      dirty: false,
      detailIndex: null,   // 字段详细配置面板当前选中的字段下标
      fieldFilter: "",     // 字段快速列表筛选词（仅影响显示）
      basic: null,         // 基本信息页草稿（跨 tab 保留未保存编辑）
      impactCache: null,   // 影响分析上次结果 {result, codes}（跨 tab 留存，保存后失效）
    };
    // 不能走 setView()：它会触发异步 renderView()→renderModels()，
    // 其网络请求完成后会把 content 覆盖成模型列表，导致编辑器闪现即消失。
    // 这里只同步更新视图状态与导航高亮，然后直接渲染编辑器。
    state.view = "models";
    $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === "models"));
    renderEditor();
  } catch (e) { showError(e); }
}

function renderEditor() {
  const ed = state.editor;
  if (!ed) return;
  const m = ed.model;
  const content = $("#content");
  $("#view-title").textContent = "模型编辑器";
  $("#view-meta").innerHTML = `<span class="mono">${escapeHtml(m.model_key)}</span>`;

  const actions = [];
  if (m.status === "draft") {
    actions.push(`<button class="btn btn-primary" id="ed-save">保存草稿</button>`);
    actions.push(`<button class="btn" id="ed-validate">运行校验</button>`);
    actions.push(`<button class="btn btn-success" id="ed-submit">提交评审</button>`);
  } else if (m.status === "in_review") {
    actions.push(`<button class="btn btn-success" id="ed-approve">评审通过</button>`);
    actions.push(`<button class="btn btn-warning" id="ed-reject">退回修改</button>`);
  } else if (m.status === "approved") {
    actions.push(`<button class="btn btn-success" id="ed-publish">正式发布</button>`);
  } else if (m.status === "published") {
    actions.push(`<button class="btn btn-primary" id="ed-start-change">基于已发布版本发起变更</button>`);
  }
  actions.push(`<button class="btn btn-ghost" id="ed-back">返回列表</button>`);

  content.innerHTML = `
    <div class="editor-head">
      <div>
        <div class="title">${escapeHtml(m.name)} <span class="badge badge-${m.status}">${STATUS_LABEL[m.status] || m.status}</span></div>
        <div class="key">${escapeHtml(m.model_key)} · ${escapeHtml(m.layer)} / ${escapeHtml(m.model_type)} · ${escapeHtml(m.target_engine)} · 修订 R${m.edit_revision}${m.published_version_no ? ` · 已发布 V${m.published_version_no}` : ""}</div>
      </div>
      <div class="spacer"></div>
      <div class="actions">${actions.join("")}</div>
    </div>
    <div class="tabs">
      <button class="tab ${ed.tab === "basic" ? "active" : ""}" data-tab="basic">基本信息</button>
      <button class="tab ${ed.tab === "fields" ? "active" : ""}" data-tab="fields">字段设计 <span class="small muted">(${ed.fields.length})</span></button>
      <button class="tab ${ed.tab === "relations" ? "active" : ""}" data-tab="relations">模型关系 <span class="small muted">(${(m.relationships || []).length})</span></button>
      <button class="tab ${ed.tab === "constraints" ? "active" : ""}" data-tab="constraints">表级约束 <span class="small muted">(${(m.constraints || []).length})</span></button>
      <button class="tab ${ed.tab === "indexes" ? "active" : ""}" data-tab="indexes">索引 <span class="small muted">(${(m.indexes || []).length})</span></button>
      <button class="tab ${ed.tab === "impact" ? "active" : ""}" data-tab="impact">影响分析</button>
      <button class="tab ${ed.tab === "versions" ? "active" : ""}" data-tab="versions">版本与差异</button>
      <button class="tab ${ed.tab === "ddl" ? "active" : ""}" data-tab="ddl">DDL 预览</button>
    </div>
    <div id="editor-body"></div>`;

  $$(".tab", content).forEach((t) => t.addEventListener("click", () => {
    ed.tab = t.dataset.tab; renderEditor();
  }));
  // 存在未保存更改时关闭编辑器需二次确认（文档 3.3）
  $("#ed-back").onclick = () => {
    if (ed.dirty) {
      confirmModal("放弃未保存的更改？",
        "当前模型存在未保存的修改（字段或基本信息），返回列表将丢弃这些更改。",
        "放弃更改", () => { state.editor = null; setView("models"); });
    } else {
      state.editor = null; setView("models");
    }
  };

  if (m.status === "draft") {
    $("#ed-save").onclick = saveDraft;
    $("#ed-validate").onclick = runValidate;
    $("#ed-submit").onclick = () => submitReviewModal(m);
  } else if (m.status === "in_review") {
    $("#ed-approve").onclick = () => decisionModal("评审通过", "填写评审结论（可选）。", "通过", async (comment) => {
      try {
        await api(`/api/models/${m.id}/approve`, { method: "POST", body: JSON.stringify({ comment }) });
        closeModal(); toast("评审已通过", "success"); await refreshEditor();
      } catch (e) { showError(e); }
    });
    $("#ed-reject").onclick = () => decisionModal("退回修改", "退回必须填写意见或整改说明。", "退回", async (comment) => {
      try {
        await api(`/api/models/${m.id}/reject`, { method: "POST", body: JSON.stringify({ comment }) });
        closeModal(); toast("已退回，设计人可修改后重新提交", "success"); await refreshEditor();
      } catch (e) { showError(e); }
    }, true);
  } else if (m.status === "approved") {
    $("#ed-publish").onclick = () => decisionModal("正式发布", "发布将生成不可变版本快照（事务内写入活动事件与审计）。", "发布", async (comment) => {
      try {
        const r = await api(`/api/models/${m.id}/publish`, { method: "POST", body: JSON.stringify({ comment }) });
        closeModal(); toast(`已发布 V${r.version_no}`, "success"); await refreshEditor();
      } catch (e) { showError(e); }
    });
  } else if (m.status === "published") {
    $("#ed-start-change").onclick = () => decisionModal("发起变更", "将从已发布版本派生新的变更草稿，历史快照保留不变。", "发起变更", async () => {
      try {
        await api(`/api/models/${m.id}/start-change`, { method: "POST", body: JSON.stringify({}) });
        closeModal(); toast("变更草稿已创建", "success"); await refreshEditor();
      } catch (e) { showError(e); }
    });
  }

  const body = $("#editor-body");
  if (ed.tab === "basic") renderBasicTab(body);
  else if (ed.tab === "fields") renderFieldsTab(body);
  else if (ed.tab === "relations") renderRelationsTab(body);
  else if (ed.tab === "constraints") renderConstraintsTab(body);
  else if (ed.tab === "indexes") renderIndexesTab(body);
  else if (ed.tab === "impact") renderImpactTab(body);
  else if (ed.tab === "versions") renderVersionsTab(body);
  else if (ed.tab === "ddl") renderDdlTab(body);
}

async function refreshEditor() {
  const ed = state.editor;
  if (!ed) return;
  const model = await api(`/api/models/${ed.model.id}`);
  ed.model = model;
  ed.fields = model.fields.map((f) => ({ ...f }));
  ed.dirty = false;
  ed.detailIndex = null;
  ed.basic = null;
  ed.impactCache = null;   // 数据已刷新，上次影响分析结果失效
  renderEditor();
}

function decisionModal(title, desc, submitLabel, onSubmit, requireComment) {
  openModal(title, `
    <div class="small muted">${escapeHtml(desc)}</div>
    <div class="form-row mt14"><label>意见 / 变更说明${requireComment ? ' <span style="color:var(--danger)">*</span>' : ""}</label>
      <textarea class="textarea" id="decision-comment" placeholder="填写结论、原因或整改说明"></textarea></div>`, [
    { label: "取消", cls: "btn-ghost", onClick: closeModal },
    {
      label: submitLabel, cls: "btn-primary", onClick: async () => {
        const comment = $("#decision-comment").value.trim();
        if (requireComment && !comment) { toast("必须填写意见", "error"); return; }
        await onSubmit(comment);
      },
    },
  ]);
}

/** M3：提交评审弹窗——内嵌评审上下文摘要（校验结论 / 变更汇总 / 下游影响 /
 *  血缘覆盖 / 图谱差异），评审人开箱即见本次变更全貌；存在阻断级校验错误时
 *  提交按钮禁用（服务端同样会拦截，双保险）。 */
function submitReviewModal(m) {
  openModal("提交评审", `
    <div class="small muted">提交后将冻结当前草稿并进入评审。以下为本次变更的评审上下文（只读，供评审决策）。</div>
    <div id="rc-loading" class="small muted mt14">正在生成评审上下文…</div>
    <div id="rc-body" hidden></div>
    <div class="form-row mt14"><label>意见 / 变更说明</label>
      <textarea class="textarea" id="decision-comment" placeholder="填写结论、原因或整改说明"></textarea></div>`, [
    { label: "取消", cls: "btn-ghost", onClick: closeModal },
    {
      label: "提交评审", cls: "btn-primary", id: "rc-submit", onClick: async () => {
        const comment = $("#decision-comment").value.trim();
        try {
          await api(`/api/models/${m.id}/submit-review`, { method: "POST", body: JSON.stringify({}) });
          closeModal(); toast("已提交评审", "success"); await refreshEditor();
        } catch (e) { showError(e); }
      },
    },
  ]);
  const submitBtn = $("#rc-submit");
  submitBtn.disabled = true;
  (async () => {
    try {
      const rc = await api(`/api/models/${m.id}/review-context`);
      renderReviewContext(rc, {
        fields: (m.fields || []).length,
        constraints: (m.constraints || []).length,
        indexes: (m.indexes || []).length,
        relationships: (m.relationships || []).length,
      });
      const blocked = !rc.validation || rc.validation.status !== "PASSED";
      submitBtn.disabled = blocked;
      if (blocked) submitBtn.title = "存在阻断级校验错误，请先在「运行校验」中修复后再提交";
    } catch (e) {
      $("#rc-loading").innerHTML =
        `<div class="issue error"><span class="msg">评审上下文加载失败：${escapeHtml(e.message || "未知错误")}（提交时仍会执行服务端校验）</span></div>`;
      submitBtn.disabled = false;   // 上下文加载失败不阻断提交，由服务端校验兜底
    }
  })();
}

function renderReviewContext(rc, counts) {
  counts = counts || {};
  $("#rc-loading").hidden = true;
  const body = $("#rc-body");
  const v = rc.validation || {};
  const s = v.summary || { errors: 0, warnings: 0, infos: 0 };
  const ok = v.status === "PASSED";
  const cs = rc.change_summary || {};
  const f = cs.fields || {}, con = cs.constraints || {}, ix = cs.indexes || {}, rel = cs.relationships || {};
  const cnt = (d) => `+${(d.added || []).length} / ~${(d.modified || []).length} / -${(d.removed || []).length}`;
  const changed = (f.added || []).length + (f.removed || []).length + (f.modified || []).length
    + (cs.model_attributes || []).length
    + (con.added || []).length + (con.removed || []).length + (con.modified || []).length
    + (ix.added || []).length + (ix.removed || []).length + (ix.modified || []).length
    + (rel.added || []).length + (rel.removed || []).length + (rel.modified || []).length;
  const imp = rc.impact || {};
  const lc = rc.lineage_coverage || {};
  const gd = rc.graph_diff || {};
  const noBaseline = rc.analysis_mode === "no_published_baseline";
  // 无发布基线时后端返回空结构差异；此处展示首次发布内容计数，避免"共 0 项变化"误导
  const summaryCell = noBaseline
    ? `<div class="rc-cell-body">首次发布，无对比基线
        <div class="small muted">字段 ${counts.fields || 0} · 约束 ${counts.constraints || 0} · 索引 ${counts.indexes || 0} · 关系 ${counts.relationships || 0}</div>
        ${(cs.model_level_reasons || []).length ? `<div class="mt8">${cs.model_level_reasons.map((r) => `<span class="badge badge-warning">${escapeHtml(r)}</span>`).join(" ")}</div>` : ""}
      </div>`
    : `<div class="rc-cell-body">共 <b class="rc-count">${changed}</b> 项变化
        <div class="small muted">字段 ${cnt(f)} · 属性 ${(cs.model_attributes || []).length} · 约束 ${cnt(con)} · 索引 ${cnt(ix)} · 关系 ${cnt(rel)}</div>
        ${(cs.model_level_reasons || []).length ? `<div class="mt8">${cs.model_level_reasons.map((r) => `<span class="badge badge-warning">${escapeHtml(r)}</span>`).join(" ")}</div>` : ""}
      </div>`;
  body.innerHTML = `
    <div class="rc-grid">
      <div class="rc-cell">
        <div class="rc-cell-title">校验结论
          <span class="badge ${ok ? "badge-published" : "badge-error"}">${ok ? "通过" : "未通过"}</span></div>
        <div class="rc-cell-body">错误 <b class="rc-count">${s.errors || 0}</b> · 警告 <b class="rc-count">${s.warnings || 0}</b> · 提示 <b class="rc-count">${s.infos || 0}</b></div>
      </div>
      <div class="rc-cell">
        <div class="rc-cell-title">变更汇总 <span class="sub">${noBaseline ? "首次发布" : "草稿 vs 最近发布"}</span></div>
        ${summaryCell}
      </div>
      <div class="rc-cell">
        <div class="rc-cell-title">候选下游影响 <span class="badge badge-neutral">${imp.impacted_count || 0} 个模型</span></div>
        <div class="rc-cell-body">
          ${(imp.top_impacted || []).length
            ? `<ul class="rc-list">${imp.top_impacted.slice(0, 5).map((t) => `<li><span class="mono">${escapeHtml(t.model_key || "")}</span> <span class="small muted">${escapeHtml(t.layer || "")} · ${(t.paths || []).length} 条路径</span></li>`).join("")}</ul>`
              + ((imp.top_impacted || []).length > 5 ? `<div class="small muted">等共 ${imp.top_impacted.length} 个，详见「影响分析」页签</div>` : "")
            : '<span class="small muted">未发现候选下游模型</span>'}
          ${imp.truncated ? `<div class="mt8"><span class="badge badge-error">结果已截断</span></div>` : ""}
        </div>
      </div>
      <div class="rc-cell">
        <div class="rc-cell-title">血缘覆盖</div>
        <div class="rc-cell-body">
          出边 <b class="rc-count">${lc.out_design_edges || 0}</b> · 外部佐证 <b class="rc-count">${lc.covered_by_external || 0}</b> · 外部入边 <b class="rc-count">${lc.external_inbound_edges || 0}</b>
          ${(lc.uncovered || []).length ? `<div class="issue warning mt8"><span class="msg">${(lc.uncovered || []).length} 条设计关系暂无外部血缘佐证，评审时需人工核对</span></div>` : ""}
        </div>
      </div>
    </div>
    ${gd.available ? `<div class="small muted mt14">图谱差异（V${gd.from_snapshot_no} → V${gd.to_snapshot_no}）：节点 +${gd.summary.nodes_added} -${gd.summary.nodes_removed} ~${gd.summary.nodes_modified} · 边 +${gd.summary.edges_added} -${gd.summary.edges_removed} ~${gd.summary.edges_modified}</div>` : ""}
    <div class="rc-fine">${escapeHtml(rc.direction_semantics || "")}</div>`;
  body.hidden = false;
}

// ---- 基本信息 ----

function renderBasicTab(body) {
  const ed = state.editor;
  const m = ed.model;
  const editable = m.status === "draft";
  // 基本信息草稿：跨 tab 保留未保存编辑，保存时与字段一起原子提交（文档 7.1）
  if (!ed.basic) {
    ed.basic = {
      name: m.name, description: m.description, layer: m.layer,
      model_type: m.model_type, target_engine: m.target_engine,
      subject_area_id: m.subject_area_id || "",
      requirement_ids: [...(m.requirement_ids || [])],
    };
  }
  const b = ed.basic;
  const areaOptions = [{ value: "", label: "（不归属）" }, ...state.subjectAreas.map((a) => ({ value: a.id, label: a.name }))];
  body.innerHTML = `
    <div class="card">
      <div class="card-title">基本信息 ${editable ? "" : '<span class="sub">非草稿状态只读</span>'}</div>
      <div class="form-grid">
        <div class="form-row"><label>模型名称</label><input class="input" id="bf-name" value="${escapeHtml(b.name)}" ${editable ? "" : "disabled"} /></div>
        <div class="form-row"><label>模型编码</label><input class="input mono" id="bf-key" value="${escapeHtml(m.model_key)}" disabled /></div>
        <div class="form-row"><label>数仓分层</label><select class="select" id="bf-layer" ${editable ? "" : "disabled"}>${LAYERS.map((l) => `<option ${b.layer === l ? "selected" : ""}>${l}</option>`).join("")}</select></div>
        <div class="form-row"><label>模型类型</label><select class="select" id="bf-type" ${editable ? "" : "disabled"}>${MODEL_TYPES.map((t) => `<option ${b.model_type === t ? "selected" : ""}>${t}</option>`).join("")}</select></div>
        <div class="form-row"><label>目标引擎</label><select class="select" id="bf-engine" ${editable ? "" : "disabled"}>${ENGINES.map((e) => `<option ${b.target_engine === e ? "selected" : ""}>${e}</option>`).join("")}</select></div>
        <div class="form-row"><label>主题域</label><select class="select" id="bf-area" ${editable ? "" : "disabled"}>${areaOptions.map((o) => `<option value="${o.value}" ${String(b.subject_area_id || "") === String(o.value) ? "selected" : ""}>${escapeHtml(o.label)}</option>`).join("")}</select></div>
      </div>
      <div class="form-row"><label>模型描述</label><textarea class="textarea" id="bf-desc" ${editable ? "" : "disabled"}>${escapeHtml(b.description)}</textarea></div>
      <div class="form-row"><label>关联需求</label>
        <div class="flex flex-wrap">
          ${state.requirements.length ? state.requirements.map((r) => `
            <label class="flex small" style="gap:5px">
              <input type="checkbox" data-bf-req="${r.id}" ${b.requirement_ids.includes(r.id) ? "checked" : ""} ${editable ? "" : "disabled"} style="width:14px;height:14px;accent-color:var(--accent)" />
              ${escapeHtml(r.requirement_key)}
            </label>`).join("") : '<span class="small muted">项目内暂无需求</span>'}
        </div>
      </div>
      ${editable ? `<div class="mt14"><button class="btn btn-primary" id="bf-save">保存草稿</button>
        <span class="small muted" style="margin-left:10px">保存采用乐观并发（expected_revision=R${m.edit_revision}），冲突时返回 409</span></div>` : ""}
    </div>`;
  if (editable) {
    const bindText = (id, key) => {
      const el = $(id);
      if (el) el.addEventListener("input", () => { b[key] = el.value; ed.dirty = true; });
    };
    const bindSel = (id, key) => {
      const el = $(id);
      if (el) el.addEventListener("change", () => { b[key] = el.value; ed.dirty = true; });
    };
    bindText("#bf-name", "name");
    bindText("#bf-desc", "description");
    bindSel("#bf-layer", "layer");
    bindSel("#bf-type", "model_type");
    bindSel("#bf-engine", "target_engine");
    bindSel("#bf-area", "subject_area_id");
    $$("[data-bf-req]").forEach((el) => el.addEventListener("change", () => {
      b.requirement_ids = $$("[data-bf-req]:checked").map((x) => Number(x.dataset.bfReq));
      ed.dirty = true;
    }));
    $("#bf-save").onclick = saveDraft;
  }
}

async function saveDraft() {
  const ed = state.editor;
  const m = ed.model;
  const b = ed.basic || {};
  const body = {
    model_key: m.model_key,
    name: String(b.name != null ? b.name : m.name).trim(),
    description: b.description != null ? b.description : m.description,
    layer: b.layer || m.layer,
    model_type: b.model_type || m.model_type,
    target_engine: b.target_engine || m.target_engine,
    subject_area_id: b.subject_area_id ? Number(b.subject_area_id) : null,
    requirement_ids: b.requirement_ids || m.requirement_ids || [],
    fields: ed.fields.map((f) => ({
      field_code: f.field_code, field_name: f.field_name, logical_type: f.logical_type,
      length: f.length || null, precision: f.precision || null, scale: f.scale || null,
      nullable: !!f.nullable, is_primary_key: !!f.is_primary_key, is_unique: !!f.is_unique,
      default_value: f.default_value || null, description: f.description || "",
      source_table: f.source_table || "", source_field: f.source_field || "",
      sensitivity: f.sensitivity || "internal",
    })),
    expected_revision: m.edit_revision,
  };
  if (!body.name) { toast("模型名称不能为空", "error"); return; }
  try {
    await api(`/api/models/${m.id}`, { method: "PUT", body: JSON.stringify(body) });
    toast(`草稿已保存`, "success");
    await refreshEditor();
  } catch (e) {
    if (e.code === "REVISION_CONFLICT") {
      toast("模型已被其他操作更新（409 并发冲突），已刷新为最新版本，请重新编辑", "error", e.code);
      await refreshEditor();
    } else showError(e);
  }
}

// ---- 字段设计（V0.7：快速列表 + 详细配置面板 + TSV 批量导入）----

// 常用技术字段：插入后仍需人工核对编码与约束
const COMMON_TECH_FIELDS = [
  { field_code: "id", field_name: "技术主键", logical_type: "BIGINT", is_primary_key: true, nullable: false, description: "技术主键（代理键）" },
  { field_code: "created_at", field_name: "记录创建时间", logical_type: "TIMESTAMP", nullable: false, default_value: "CURRENT_TIMESTAMP", description: "记录创建时间" },
  { field_code: "updated_at", field_name: "记录更新时间", logical_type: "TIMESTAMP", nullable: true, description: "记录最后更新时间" },
  { field_code: "is_deleted", field_name: "逻辑删除标记", logical_type: "BOOLEAN", nullable: false, default_value: "FALSE", description: "逻辑删除标记" },
  { field_code: "dw_create_time", field_name: "数仓入库时间", logical_type: "TIMESTAMP", nullable: false, default_value: "CURRENT_TIMESTAMP", description: "数仓入库时间" },
  { field_code: "dw_update_time", field_name: "数仓更新时间", logical_type: "TIMESTAMP", nullable: true, description: "数仓更新时间" },
];

// 14 列导入模板（文档附录 A）：空类型参数留空而非填 NULL
const TSV_TEMPLATE_TEXT = [
  "field_code\tfield_name\tlogical_type\tlength\tprecision\tscale\tis_primary_key\tis_unique\tnullable\tdefault_value\tdescription\tsource_table\tsource_field\tsensitivity",
  "customer_code\t客户业务编码\tVARCHAR\t64\t\t\tfalse\ttrue\tfalse\t\t客户业务唯一编码\tods_customer\tcustomer_code\tsensitive",
  "customer_id\t客户编号\tBIGINT\t\t\t\ttrue\tfalse\tfalse\t\t客户技术主键\tods_customer\tcustomer_id\tinternal",
  "total_amount\t合同总金额\tDECIMAL\t\t18\t2\tfalse\tfalse\tfalse\t0\t含税合同总金额\tods_contract\tamount\tinternal",
  "created_at\t记录创建时间\tTIMESTAMP\t\t\t\tfalse\tfalse\tfalse\tCURRENT_TIMESTAMP\t记录入库时间\tods_contract\tetl_time\tinternal",
].join("\n");

/** 布尔多格式解析：是/否、true/false、1/0、yes/no；空值取默认 */
function parseBoolCell(v, dflt) {
  const s = String(v == null ? "" : v).trim().toLowerCase();
  if (!s) return dflt;
  if (["是", "yes", "true", "1", "y"].includes(s)) return true;
  if (["否", "no", "false", "0", "n"].includes(s)) return false;
  return dflt;
}

function numCell(v) {
  const s = String(v == null ? "" : v).trim();
  if (!s) return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

/** 物理类型预览（文档 Table 12 的客户端镜像，仅展示估算结果） */
function physicalTypePreview(f, engine) {
  switch (f.logical_type) {
    case "STRING": return engine === "postgresql" ? "TEXT" : engine === "mysql" ? "VARCHAR(255)" : "STRING";
    case "VARCHAR": return f.length ? "VARCHAR(" + f.length + ")" : "VARCHAR(?)";
    case "INTEGER": return engine === "mysql" ? "INT" : "INTEGER";
    case "BIGINT": return "BIGINT";
    case "DECIMAL": {
      const base = engine === "postgresql" ? "NUMERIC" : "DECIMAL";
      return (f.precision != null && f.scale != null)
        ? base + "(" + f.precision + "," + f.scale + ")" : base + "(?,?)";
    }
    case "DATE": return "DATE";
    case "TIMESTAMP": return "TIMESTAMP";
    case "BOOLEAN": return "BOOLEAN";
    default: return f.logical_type;
  }
}

/** TSV 解析：≥10 列按 14 列模板，否则按 V0.6 六列旧格式；未知类型回退 STRING */
function parseTsvRows(text) {
  const lines = String(text || "").split(/\r?\n/).filter((l) => l.trim());
  const rows = [];
  let skipped = 0;
  let format = "14col";
  let formatDecided = false;
  lines.forEach((line, idx) => {
    const cols = line.split("\t").map((c) => c.trim());
    if (idx === 0 && cols.length && cols[0].toLowerCase() === "field_code") return; // 表头
    if (!cols[0]) { skipped++; return; }
    const is14 = cols.length >= 10;
    if (!formatDecided) { format = is14 ? "14col" : "v06"; formatDecided = true; }
    const type = LOGICAL_TYPES.includes((cols[2] || "").toUpperCase())
      ? cols[2].toUpperCase() : "STRING";
    if (is14) {
      rows.push({
        field_code: cols[0], field_name: cols[1] || "", logical_type: type,
        length: numCell(cols[3]), precision: numCell(cols[4]), scale: numCell(cols[5]),
        is_primary_key: parseBoolCell(cols[6], false),
        is_unique: parseBoolCell(cols[7], false),
        nullable: parseBoolCell(cols[8], true),
        default_value: cols[9] || null,
        description: cols[10] || "",
        source_table: cols[11] || "", source_field: cols[12] || "",
        sensitivity: SENSITIVITIES.includes(cols[13]) ? cols[13] : "internal",
      });
    } else {
      rows.push({
        field_code: cols[0], field_name: cols[1] || "", logical_type: type,
        length: null, precision: null, scale: null,
        is_primary_key: parseBoolCell(cols[3], false),
        is_unique: false,
        nullable: parseBoolCell(cols[4], true),
        default_value: null,
        description: cols[5] || "",
        source_table: "", source_field: "", sensitivity: "internal",
      });
    }
  });
  return { rows, skipped, format };
}

function downloadTextFile(filename, text) {
  const blob = new Blob([text], { type: "text/tab-separated-values;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function confirmModal(title, desc, confirmLabel, onConfirm) {
  openModal(title, `<div class="small muted">${escapeHtml(desc)}</div>`, [
    { label: "取消", cls: "btn-ghost", onClick: closeModal },
    { label: confirmLabel, cls: "btn-danger", onClick: onConfirm },
  ]);
}

function descSummary(text) {
  const s = String(text || "");
  return s.length > 26 ? s.slice(0, 26) + "…" : s;
}

function fieldRowHtml(f, i, editable, lastIdx) {
  const dis = editable ? "" : "disabled";
  const desc = f.description || "";
  return `<tr data-field-row="${i}">
    <td class="num muted">${i + 1}</td>
    <td><input class="mono" data-fk="field_code" value="${escapeHtml(f.field_code)}" ${dis} placeholder="snake_case" /></td>
    <td><input data-fk="field_name" value="${escapeHtml(f.field_name)}" ${dis} /></td>
    <td><select data-fk="logical_type" ${dis}>${LOGICAL_TYPES.map((t) => `<option ${f.logical_type === t ? "selected" : ""}>${t}</option>`).join("")}</select></td>
    <td style="text-align:center"><input type="checkbox" data-fk="is_primary_key" ${f.is_primary_key ? "checked" : ""} ${dis} /></td>
    <td style="text-align:center"><input type="checkbox" data-fk="is_unique" ${f.is_unique ? "checked" : ""} ${dis} /></td>
    <td style="text-align:center"><input type="checkbox" data-fk="nullable" ${f.nullable ? "checked" : ""} ${dis} /></td>
    <td class="small muted" title="${escapeHtml(desc)}">${escapeHtml(descSummary(desc)) || "—"}</td>
    <td class="ops">
      <button class="btn btn-sm" data-fld-config="${i}">配置</button>
      ${editable ? `
        <button class="btn btn-sm" data-fld-up="${i}" ${i === 0 ? "disabled" : ""} title="上移">↑</button>
        <button class="btn btn-sm" data-fld-down="${i}" ${i === lastIdx ? "disabled" : ""} title="下移">↓</button>
        <button class="btn btn-sm" data-fld-copy="${i}" title="复制（主键/唯一默认清除）">复制</button>
        <button class="btn btn-sm btn-danger" data-fld-del="${i}">删除</button>` : ""}
    </td>
  </tr>`;
}

function renderFieldsTab(body) {
  const ed = state.editor;
  const m = ed.model;
  const editable = m.status === "draft";
  body.innerHTML = `
    <div class="card">
      <div class="card-title">字段设计 <span class="sub">${editable ? "快速列表维护高频属性；「配置」编辑类型参数/默认值/来源/敏感级" : "非草稿状态只读，发起变更后可编辑"}</span></div>
      <div class="toolbar">
        ${editable ? `
          <button class="btn btn-sm btn-primary" id="fld-add">+ 新增字段</button>
          <button class="btn btn-sm" id="fld-import">批量导入</button>
          <button class="btn btn-sm" id="fld-common">常用技术字段</button>` : ""}
        <input class="input" id="fld-filter" style="max-width:300px" placeholder="筛选：编码/中文名/说明/类型/来源" value="${escapeHtml(ed.fieldFilter || "")}" />
        <div class="spacer"></div>
        <span class="small muted">共 ${ed.fields.length} 个字段</span>
      </div>
      ${ed.fields.length ? `<div class="table-wrap"><table class="data fields-grid">
        <thead><tr>
          <th class="num">#</th><th>字段编码</th><th>中文名</th><th>逻辑类型</th>
          <th>主键</th><th>唯一</th><th>可空</th><th>业务说明</th><th>操作</th>
        </tr></thead>
        <tbody id="fields-tbody"></tbody>
      </table></div>`
      : `<div class="empty"><div class="empty-icon">▦</div><div class="empty-title">还没有字段</div><div class="empty-desc">${editable ? "点击「新增字段」「批量导入」或「常用技术字段」开始设计。" : "该模型暂无字段。"}</div></div>`}
      <div id="field-detail-wrap" class="mt14"></div>
      ${editable ? `<div class="mt14 flex flex-wrap">
        <button class="btn btn-primary" id="fld-save">保存草稿</button>
        <button class="btn" id="fld-validate">运行校验</button>
        <span class="small muted">保存采用乐观并发（expected_revision=R${m.edit_revision}），冲突时返回 409</span>
      </div>` : ""}
      <div id="validation-panel" class="mt20"></div>
    </div>`;

  if (editable) {
    $("#fld-add").onclick = () => {
      ed.fields.push({
        field_code: "", field_name: "", logical_type: "STRING", length: null,
        precision: null, scale: null, nullable: true, is_primary_key: false,
        is_unique: false, default_value: null, description: "", source_table: "",
        source_field: "", sensitivity: "internal",
      });
      ed.dirty = true;
      ed.detailIndex = ed.fields.length - 1;
      renderEditor();
    };
    $("#fld-import").onclick = importModal;
    $("#fld-common").onclick = commonFieldsModal;
    $("#fld-save").onclick = saveDraft;
    $("#fld-validate").onclick = runValidate;
  }
  // 筛选只影响显示，不改变字段集合与顺序
  $("#fld-filter").addEventListener("input", (e) => {
    ed.fieldFilter = e.target.value;
    renderFieldRows();
  });
  renderFieldRows();
  if (ed.detailIndex != null) renderFieldDetail($("#field-detail-wrap"));
}

/** 只重绘 tbody：筛选输入时保留焦点 */
function renderFieldRows() {
  const ed = state.editor;
  const tbody = $("#fields-tbody");
  if (!tbody) return;
  const editable = ed.model.status === "draft";
  const kw = (ed.fieldFilter || "").trim().toLowerCase();
  const visible = [];
  ed.fields.forEach((f, i) => {
    if (!kw) { visible.push({ f, i }); return; }
    const hay = [f.field_code, f.field_name, f.description, f.logical_type,
      f.source_table, f.source_field].join(" ").toLowerCase();
    if (hay.includes(kw)) visible.push({ f, i });
  });
  if (!visible.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="muted small" style="text-align:center;padding:18px">${ed.fields.length ? "没有匹配的字段" : "暂无字段"}</td></tr>`;
    return;
  }
  const last = ed.fields.length - 1;
  tbody.innerHTML = visible.map(({ f, i }) =>
    fieldRowHtml(f, i, editable, last)
  ).join("");
  bindFieldRowEvents();
}

function bindFieldRowEvents() {
  const ed = state.editor;
  const editable = ed.model.status === "draft";
  $$("#fields-tbody [data-field-row]").forEach((tr) => {
    const i = Number(tr.dataset.fieldRow);
    $$("[data-fk]", tr).forEach((el) => {
      el.addEventListener("change", () => {
        const k = el.dataset.fk;
        if (el.type === "checkbox") {
          ed.fields[i][k] = el.checked;
          // 主键/可空联动：勾选主键自动关闭可空；打开可空自动取消主键
          if (k === "is_primary_key" && el.checked) ed.fields[i].nullable = false;
          if (k === "nullable" && el.checked) ed.fields[i].is_primary_key = false;
        } else {
          ed.fields[i][k] = el.value;
          // 逻辑类型切换联动（与详情面板一致）：VARCHAR 补 255、DECIMAL 补 18,2，
          // 并清理不适用参数，避免保存后触发 DECIMAL_*_REQUIRED 阻断错误
          if (k === "logical_type") {
            const f = ed.fields[i];
            const t = el.value;
            if (t === "VARCHAR" && f.length == null) f.length = 255;
            if (t === "DECIMAL") {
              if (f.precision == null) f.precision = 18;
              if (f.scale == null) f.scale = 2;
            }
            if (t !== "VARCHAR") f.length = null;
            if (t !== "DECIMAL") { f.precision = null; f.scale = null; }
          }
        }
        ed.dirty = true;
        syncQuickRow(i);
        if (ed.detailIndex === i) renderFieldDetail($("#field-detail-wrap"));
      });
    });
  });
  $$("#fields-tbody [data-fld-config]").forEach((el) =>
    el.addEventListener("click", () => {
      ed.detailIndex = Number(el.dataset.fldConfig);
      renderFieldsTab($("#editor-body"));
    }));
  if (!editable) return;
  $$("#fields-tbody [data-fld-up]").forEach((el) =>
    el.addEventListener("click", () => {
      const i = Number(el.dataset.fldUp);
      if (i <= 0) return;
      const t = ed.fields[i - 1]; ed.fields[i - 1] = ed.fields[i]; ed.fields[i] = t;
      ed.dirty = true;
      if (ed.detailIndex === i) ed.detailIndex = i - 1;
      else if (ed.detailIndex === i - 1) ed.detailIndex = i;
      renderEditor();
    }));
  $$("#fields-tbody [data-fld-down]").forEach((el) =>
    el.addEventListener("click", () => {
      const i = Number(el.dataset.fldDown);
      if (i >= ed.fields.length - 1) return;
      const t = ed.fields[i + 1]; ed.fields[i + 1] = ed.fields[i]; ed.fields[i] = t;
      ed.dirty = true;
      if (ed.detailIndex === i) ed.detailIndex = i + 1;
      else if (ed.detailIndex === i + 1) ed.detailIndex = i;
      renderEditor();
    }));
  $$("#fields-tbody [data-fld-copy]").forEach((el) =>
    el.addEventListener("click", () => {
      const i = Number(el.dataset.fldCopy);
      const f = ed.fields[i];
      ed.fields.splice(i + 1, 0, {
        ...f,
        field_code: (f.field_code || "field") + "_copy",
        is_primary_key: false,
        is_unique: false,
      });
      ed.dirty = true;
      ed.detailIndex = i + 1;
      toast("已复制字段（主键/唯一已清除），请修改字段编码后保存", "success");
      renderEditor();
    }));
  $$("#fields-tbody [data-fld-del]").forEach((el) =>
    el.addEventListener("click", () => {
      const i = Number(el.dataset.fldDel);
      const f = ed.fields[i];
      confirmModal("删除字段",
        `将删除字段「${f.field_code || "(未命名)"}」。只有保存草稿后删除才持久化，未保存时关闭编辑器可恢复。`,
        "删除", () => {
          ed.fields.splice(i, 1);
          ed.dirty = true;
          if (ed.detailIndex === i) ed.detailIndex = null;
          else if (ed.detailIndex != null && ed.detailIndex > i) ed.detailIndex -= 1;
          renderEditor();
        });
    }));
}

/** 详情面板文本编辑后同步快速列表对应单元格（不重渲染，保留焦点） */
function syncQuickRow(idx) {
  const ed = state.editor;
  const f = ed.fields[idx];
  const tr = document.querySelector('#fields-tbody tr[data-field-row="' + idx + '"]');
  if (!tr) return;
  const set = (k, v) => {
    const el = tr.querySelector('[data-fk="' + k + '"]');
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!v;
    else el.value = v == null ? "" : v;
  };
  set("field_code", f.field_code);
  set("field_name", f.field_name);
  set("logical_type", f.logical_type);
  set("is_primary_key", f.is_primary_key);
  set("is_unique", f.is_unique);
  set("nullable", f.nullable);
}

function updateDetailPreview() {
  const ed = state.editor;
  if (ed.detailIndex == null) return;
  const el = $(".fd-preview");
  if (el) el.textContent = physicalTypePreview(ed.fields[ed.detailIndex], ed.model.target_engine);
}

/** 字段详细配置面板（文档 3.3 / Table 9） */
function renderFieldDetail(wrap) {
  const ed = state.editor;
  const idx = ed.detailIndex;
  if (idx == null || idx >= ed.fields.length) { wrap.innerHTML = ""; return; }
  const f = ed.fields[idx];
  const m = ed.model;
  const editable = m.status === "draft";
  const dis = editable ? "" : "disabled";
  const isVarchar = f.logical_type === "VARCHAR";
  const isDecimal = f.logical_type === "DECIMAL";
  wrap.innerHTML = `
    <div class="card field-detail">
      <div class="card-title">字段详细配置
        <span class="sub">#${idx + 1} · <span class="mono">${escapeHtml(f.field_code || "(未命名)")}</span> · 物理类型预览 <span class="fd-preview">${escapeHtml(physicalTypePreview(f, m.target_engine))}</span></span>
        <button class="btn btn-sm btn-ghost" id="fd-close">关闭</button>
      </div>
      <div class="form-grid">
        <div class="form-row"><label>字段编码</label><input class="input mono" id="fd-code" value="${escapeHtml(f.field_code)}" ${dis} placeholder="snake_case，首位不可为数字" /></div>
        <div class="form-row"><label>中文名称</label><input class="input" id="fd-name" value="${escapeHtml(f.field_name)}" ${dis} /></div>
        <div class="form-row"><label>逻辑类型</label><select class="select" id="fd-type" ${dis}>${LOGICAL_TYPES.map((t) => `<option ${f.logical_type === t ? "selected" : ""}>${t}</option>`).join("")}</select></div>
        ${isVarchar ? `<div class="form-row"><label>长度</label><input class="input" id="fd-length" type="number" min="1" max="65535" value="${f.length == null ? "" : f.length}" ${dis} /></div>` : ""}
        ${isDecimal ? `<div class="form-row"><label>总精度</label><input class="input" id="fd-precision" type="number" min="1" max="38" value="${f.precision == null ? "" : f.precision}" ${dis} /></div>` : ""}
        ${isDecimal ? `<div class="form-row"><label>小数位</label><input class="input" id="fd-scale" type="number" min="0" max="18" value="${f.scale == null ? "" : f.scale}" ${dis} /></div>` : ""}
        <div class="form-row"><label>约束</label>
          <div class="flex flex-wrap">
            <label class="flex small nowrap" style="gap:5px"><input type="checkbox" id="fd-pk" ${f.is_primary_key ? "checked" : ""} ${dis} style="width:14px;height:14px;accent-color:var(--accent)" /> 主键</label>
            <label class="flex small nowrap" style="gap:5px"><input type="checkbox" id="fd-unique" ${f.is_unique ? "checked" : ""} ${dis} style="width:14px;height:14px;accent-color:var(--accent)" /> 唯一</label>
            <label class="flex small nowrap" style="gap:5px"><input type="checkbox" id="fd-nullable" ${f.nullable ? "checked" : ""} ${dis} style="width:14px;height:14px;accent-color:var(--accent)" /> 可空</label>
          </div>
        </div>
        <div class="form-row"><label>默认值</label><input class="input mono" id="fd-default" value="${escapeHtml(f.default_value || "")}" ${dis} placeholder="0 / 'unknown' / CURRENT_TIMESTAMP" /></div>
        <div class="form-row"><label>敏感级别</label><select class="select" id="fd-sensitivity" ${dis}>${SENSITIVITIES.map((s) => `<option ${f.sensitivity === s ? "selected" : ""}>${s}</option>`).join("")}</select></div>
      </div>
      <div class="form-row"><label>业务定义</label><textarea class="textarea" id="fd-desc" ${dis} placeholder="字段口径、枚举、单位或业务语义">${escapeHtml(f.description || "")}</textarea></div>
      <div class="form-grid">
        <div class="form-row"><label>来源表</label><input class="input mono" id="fd-src-table" value="${escapeHtml(f.source_table || "")}" ${dis} /></div>
        <div class="form-row"><label>来源字段</label><input class="input mono" id="fd-src-field" value="${escapeHtml(f.source_field || "")}" ${dis} /></div>
      </div>
      <div class="small muted">默认值白名单：数字、TRUE/FALSE/NULL、CURRENT_DATE/CURRENT_TIMESTAMP、简单单引号常量；保存时服务端复检，不合法将阻断校验与评审。</div>
    </div>`;
  $("#fd-close").onclick = () => {
    ed.detailIndex = null;
    renderFieldsTab($("#editor-body"));
  };
  if (!editable) return;
  // 文本类输入：即时更新状态 + 同步快速列表（不重渲染，保留焦点）
  const bindText = (id, key, num) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("input", () => {
      ed.fields[idx][key] = num ? (el.value === "" ? null : Number(el.value)) : el.value;
      ed.dirty = true;
      syncQuickRow(idx);
      updateDetailPreview();
    });
  };
  bindText("#fd-code", "field_code");
  bindText("#fd-name", "field_name");
  bindText("#fd-length", "length", true);
  bindText("#fd-precision", "precision", true);
  bindText("#fd-scale", "scale", true);
  bindText("#fd-default", "default_value");
  bindText("#fd-desc", "description");
  bindText("#fd-src-table", "source_table");
  bindText("#fd-src-field", "source_field");
  // 逻辑类型切换：参数联动（VARCHAR 建议 255、DECIMAL 建议 18,2；切换清理不适用参数）
  $("#fd-type").addEventListener("change", (e) => {
    const f2 = ed.fields[idx];
    const t = e.target.value;
    f2.logical_type = t;
    if (t === "VARCHAR" && f2.length == null) f2.length = 255;
    if (t === "DECIMAL") {
      if (f2.precision == null) f2.precision = 18;
      if (f2.scale == null) f2.scale = 2;
    }
    if (t !== "VARCHAR") f2.length = null;
    if (t !== "DECIMAL") { f2.precision = null; f2.scale = null; }
    ed.dirty = true;
    renderFieldDetail($("#field-detail-wrap"));
    syncQuickRow(idx);
  });
  $("#fd-sensitivity").addEventListener("change", (e) => {
    ed.fields[idx].sensitivity = e.target.value;
    ed.dirty = true;
  });
  $("#fd-pk").addEventListener("change", (e) => {
    const f2 = ed.fields[idx];
    f2.is_primary_key = e.target.checked;
    if (e.target.checked) { f2.nullable = false; $("#fd-nullable").checked = false; }
    ed.dirty = true;
    syncQuickRow(idx);
  });
  $("#fd-unique").addEventListener("change", (e) => {
    ed.fields[idx].is_unique = e.target.checked;
    ed.dirty = true;
    syncQuickRow(idx);
  });
  $("#fd-nullable").addEventListener("change", (e) => {
    const f2 = ed.fields[idx];
    f2.nullable = e.target.checked;
    if (e.target.checked) { f2.is_primary_key = false; $("#fd-pk").checked = false; }
    ed.dirty = true;
    syncQuickRow(idx);
  });
}

/** TSV 批量导入（文档 3.4）：追加/替换 + 二次确认 + 模板下载 */
function importModal() {
  const ed = state.editor;
  openModal("批量导入字段（TSV）", `
    <div class="small muted">从 Excel 复制区域后粘贴（制表符分隔）。支持 <b>14 列模板</b>与 <b>V0.6 六列旧格式</b>（编码/中文名/类型/主键/可空/说明），旧格式缺失属性按安全默认值补齐；未知逻辑类型回退 STRING，需导入后人工核对。字段名中的换行会被过滤为空行。</div>
    <div class="toolbar mt14">
      <button class="btn btn-sm" id="tsv-template">下载 14 列模板</button>
      <label class="flex small nowrap" style="gap:5px"><input type="radio" name="tsv-mode" value="append" checked style="width:14px;height:14px;accent-color:var(--accent)" /> 追加到现有字段</label>
      <label class="flex small nowrap" style="gap:5px"><input type="radio" name="tsv-mode" value="replace" style="width:14px;height:14px;accent-color:var(--accent)" /> 替换当前草稿全部字段</label>
    </div>
    <textarea class="textarea" id="tsv-area" style="min-height:170px" placeholder="customer_code\t客户业务编码\tVARCHAR\t64\t\t\tfalse\ttrue\tfalse\t\t客户业务唯一编码\tods_customer\tcustomer_code\tsensitive"></textarea>
    <div class="small muted mt8" id="tsv-preview">粘贴后显示解析预览</div>`, [
    { label: "取消", cls: "btn-ghost", onClick: closeModal },
    {
      label: "导入", cls: "btn-primary", onClick: () => {
        const mode = document.querySelector('input[name="tsv-mode"]:checked').value;
        const parsed = parseTsvRows($("#tsv-area").value);
        if (!parsed.rows.length) { toast("没有可导入的数据行", "error"); return; }
        const doImport = () => {
          if (mode === "replace") ed.fields = parsed.rows;
          else ed.fields = ed.fields.concat(parsed.rows);
          ed.dirty = true;
          ed.detailIndex = null;
          closeModal();
          toast(`已导入 ${parsed.rows.length} 行${parsed.skipped ? `，跳过 ${parsed.skipped} 行` : ""}，记得保存草稿`, "success");
          renderEditor();
        };
        // 替换模式必须二次确认（交互保护，不取代数据库备份）
        if (mode === "replace" && ed.fields.length) {
          confirmModal("确认替换全部字段",
            `替换模式将清除当前全部 ${ed.fields.length} 个字段，导入 ${parsed.rows.length} 行。保存前放弃更改可恢复原字段。`,
            "确认替换", doImport);
        } else {
          doImport();
        }
      },
    },
  ]);
  $("#tsv-template").onclick = () => downloadTextFile("field_import_template.tsv", TSV_TEMPLATE_TEXT);
  $("#tsv-area").addEventListener("input", (e) => {
    const parsed = parseTsvRows(e.target.value);
    const p = $("#tsv-preview");
    if (!parsed.rows.length && !parsed.skipped) { p.textContent = "粘贴后显示解析预览"; return; }
    const sample = parsed.rows.slice(0, 3)
      .map((r) => `${r.field_code}（${r.field_name || "-"} · ${r.logical_type}）`).join("；");
    p.innerHTML = `解析到 <b>${parsed.rows.length}</b> 行（${parsed.format === "14col" ? "14 列模板" : "V0.6 六列"}）${parsed.skipped ? `，跳过 ${parsed.skipped} 行` : ""}${sample ? `<br>示例：${escapeHtml(sample)}` : ""}`;
  });
}

/** 常用技术字段插入 */
function commonFieldsModal() {
  const ed = state.editor;
  openModal("插入常用技术字段", `
    <div class="small muted">勾选后作为新草稿字段插入（不立即写库，保存后生效）。若编码与现有字段重复，请在快速列表中修改。</div>
    <div class="mt14">${COMMON_TECH_FIELDS.map((c, i) => `
      <label class="flex small mt8" style="gap:8px;align-items:flex-start">
        <input type="checkbox" data-common="${i}" checked style="width:14px;height:14px;accent-color:var(--accent);margin-top:3px" />
        <span><span class="mono">${escapeHtml(c.field_code)}</span> · ${escapeHtml(c.field_name)} <span class="muted">(${c.logical_type}${c.is_primary_key ? " · 主键" : ""})</span></span>
      </label>`).join("")}</div>`, [
    { label: "取消", cls: "btn-ghost", onClick: closeModal },
    {
      label: "插入所选", cls: "btn-primary", onClick: () => {
        const picked = $$("[data-common]:checked").map((el) => COMMON_TECH_FIELDS[Number(el.dataset.common)]);
        if (!picked.length) { toast("未选择任何字段", "error"); return; }
        picked.forEach((c) => ed.fields.push({
          field_code: c.field_code, field_name: c.field_name, logical_type: c.logical_type,
          length: null, precision: null, scale: null,
          nullable: c.nullable, is_primary_key: !!c.is_primary_key, is_unique: false,
          default_value: c.default_value || null, description: c.description || "",
          source_table: "", source_field: "", sensitivity: "internal",
        }));
        ed.dirty = true;
        closeModal();
        toast(`已插入 ${picked.length} 个常用字段，记得保存草稿`, "success");
        renderEditor();
      },
    },
  ]);
}

async function runValidate() {
  const ed = state.editor;
  const panel = $("#validation-panel");
  if (!panel) return;
  panel.innerHTML = `<div class="small muted">校验中…</div>`;
  try {
    const r = await api(`/api/models/${ed.model.id}/validate`, { method: "POST", body: JSON.stringify({}) });
    const groups = [
      { key: "error", label: `阻断错误 ${r.summary.errors}`, cls: "error" },
      { key: "warning", label: `警告 ${r.summary.warnings}`, cls: "warning" },
      { key: "info", label: `提示 ${r.summary.infos}`, cls: "info" },
    ];
    panel.innerHTML = `
      <div class="card-title">校验结果 <span class="badge badge-${r.status === "PASSED" ? "published" : "error"}">${r.status === "PASSED" ? "通过" : "未通过"}</span>
        <span class="sub">run_id: ${escapeHtml(r.run_id)}</span></div>
      ${r.issues.length ? groups.map((g) => {
        const items = r.issues.filter((i) => i.severity === g.key);
        if (!items.length) return "";
        return `<div class="issue-group"><div class="issue-group-title"><span class="badge badge-${g.cls}">${g.label}</span></div>
          ${items.map((i) => `
            <div class="issue ${i.severity}">
              <span class="rule">${escapeHtml(i.rule_id)}</span>
              <span class="msg">${escapeHtml(i.message)}<div class="sug">建议：${escapeHtml(i.suggestion)}</div></span>
              <span class="loc">${escapeHtml(i.location)}</span>
            </div>`).join("")}</div>`;
      }).join("") : `<div class="empty"><div class="empty-desc">没有发现问题。</div></div>`}`;
  } catch (e) { showError(e); }
}

// ---- 模型关系 ----

function renderRelationsTab(body) {
  const ed = state.editor;
  const m = ed.model;
  const rels = m.relationships || [];
  const others = state.models.filter((x) => x.id !== m.id);
  body.innerHTML = `
    <div class="card">
      <div class="card-title">模型关系 <span class="sub">平台元数据，不自动创建数据库外键</span></div>
      ${rels.length ? `<div class="table-wrap"><table class="data">
        <thead><tr><th>关系</th><th>源模型</th><th>目标模型</th><th>基数</th><th>字段映射</th><th>说明</th><th></th></tr></thead>
        <tbody>${rels.map((r) => {
          const src = state.models.find((x) => x.id === r.source_model_id);
          const tgt = state.models.find((x) => x.id === r.target_model_id);
          return `<tr>
            <td><span class="badge badge-neutral">${REL_TYPE_LABEL[r.relationship_type] || r.relationship_type}</span></td>
            <td class="mono small">${escapeHtml(src ? src.model_key : r.source_model_id)}</td>
            <td class="mono small">${escapeHtml(tgt ? tgt.model_key : r.target_model_id)}</td>
            <td class="small">${escapeHtml(r.cardinality)}</td>
            <td class="mono small">${escapeHtml((r.source_fields || []).join(","))} → ${escapeHtml((r.target_fields || []).join(","))}</td>
            <td class="small muted">${escapeHtml(r.description || "—")}</td>
            <td><button class="btn btn-sm btn-danger" data-rel-del="${r.id}">删除</button></td>
          </tr>`;
        }).join("")}</tbody></table></div>`
      : `<div class="empty"><div class="empty-desc">暂无关系。</div></div>`}
    </div>
    <div class="card">
      <div class="card-title">新建关系</div>
      <div class="form-grid">
        <div class="form-row"><label>目标模型</label><select class="select" id="rel-target">${others.map((o) => `<option value="${o.id}">${escapeHtml(o.model_key)} · ${escapeHtml(o.name)}</option>`).join("") || '<option value="">（项目内无其他模型）</option>'}</select></div>
        <div class="form-row"><label>关系类型</label><select class="select" id="rel-type">${REL_TYPES.map((t) => `<option value="${t}">${REL_TYPE_LABEL[t]}（${t}）</option>`).join("")}</select></div>
        <div class="form-row"><label>基数</label><select class="select" id="rel-card">${CARDINALITIES.map((c) => `<option value="${c}">${c}</option>`).join("")}</select></div>
        <div class="form-row"><label>源字段</label><input class="input mono" id="rel-src-fields" placeholder="逗号分隔，如 customer_id" /></div>
        <div class="form-row"><label>目标字段</label><input class="input mono" id="rel-tgt-fields" placeholder="逗号分隔，如 customer_id" /></div>
        <div class="form-row full"><label>说明</label><input class="input" id="rel-desc" placeholder="关联条件、业务前提或复用约束" /></div>
      </div>
      <div class="mt14"><button class="btn btn-primary" id="rel-create">创建关系</button>
        <span class="small muted" style="margin-left:10px">源/目标字段需成对且数量一致；两端必须同项目</span></div>
    </div>`;

  $$("[data-rel-del]").forEach((el) => el.addEventListener("click", async () => {
    try {
      await api(`/api/models/${m.id}/relationships/${el.dataset.relDel}`, { method: "DELETE" });
      toast("关系已删除", "success"); await refreshEditor();
    } catch (e) { showError(e); }
  }));
  $("#rel-create").onclick = async () => {
    const targetId = Number($("#rel-target").value);
    if (!targetId) { toast("请选择目标模型", "error"); return; }
    const body = {
      target_model_id: targetId,
      relationship_type: $("#rel-type").value,
      cardinality: $("#rel-card").value,
      source_fields: $("#rel-src-fields").value.split(",").map((s) => s.trim()).filter(Boolean),
      target_fields: $("#rel-tgt-fields").value.split(",").map((s) => s.trim()).filter(Boolean),
      description: $("#rel-desc").value.trim(),
    };
    try {
      await api(`/api/models/${m.id}/relationships`, { method: "POST", body: JSON.stringify(body) });
      toast("关系已创建", "success"); await refreshEditor();
    } catch (e) { showError(e); }
  };
}

// ---- 表级约束（V0.8） ----

function renderConstraintsTab(body) {
  const ed = state.editor;
  const m = ed.model;
  const cs = m.constraints || [];
  const fieldCodes = ed.fields.map((f) => f.field_code);
  const editable = m.status === "draft";
  body.innerHTML = `
    <div class="card">
      <div class="card-title">表级约束 <span class="sub">复合主键 / 复合唯一 / CHECK / 外键 · 命名唯一，与字段级主键互斥</span>
        ${editable ? `<button class="btn btn-sm btn-primary" id="ct-new">+ 新建约束</button>` : ""}
      </div>
      ${cs.length ? `<div class="table-wrap"><table class="data">
        <thead><tr><th>名称</th><th>类型</th><th>定义</th><th>说明</th><th></th></tr></thead>
        <tbody>${cs.map((c) => {
          let def = "";
          if (c.constraint_type === "check") def = `<span class="mono">CHECK (${escapeHtml(c.expression)})</span>`;
          else if (c.constraint_type === "foreign_key") {
            const tgt = state.models.find((x) => x.id === c.referenced_model_id);
            def = `<span class="mono">(${escapeHtml((c.field_codes || []).join(","))}) → ${escapeHtml(tgt ? tgt.model_key : c.referenced_model_id)} (${escapeHtml((c.referenced_field_codes || []).join(","))}) ON DELETE ${escapeHtml(c.on_delete)}</span>`;
          } else def = `<span class="mono">(${escapeHtml((c.field_codes || []).join(","))})</span>`;
          return `<tr>
            <td class="mono">${escapeHtml(c.constraint_name)}</td>
            <td><span class="badge badge-neutral">${CONSTRAINT_TYPE_LABEL[c.constraint_type] || c.constraint_type}</span></td>
            <td class="small">${def}</td>
            <td class="small muted">${escapeHtml(c.description || "—")}</td>
            <td>${editable ? `<button class="btn btn-sm btn-danger" data-ct-del="${c.id}">删除</button>` : ""}</td>
          </tr>`;
        }).join("")}</tbody></table></div>`
      : `<div class="empty"><div class="empty-desc">暂无表级约束。</div></div>`}
      ${!editable ? `<div class="small muted mt14">模型当前为「${STATUS_LABEL[m.status] || m.status}」，约束只读；请通过“发起变更”创建草稿后修改。</div>` : ""}
    </div>`;

  if (editable) {
    $("#ct-new").onclick = () => constraintModal(null);
    $$("[data-ct-del]", body).forEach((el) => el.addEventListener("click", async () => {
      try {
        await api(`/api/models/${m.id}/constraints/${el.dataset.ctDel}`, { method: "DELETE" });
        toast("约束已删除", "success"); await refreshEditor();
      } catch (e) { showError(e); }
    }));
  }
}

/** 约束表单弹窗：按类型切换字段（文档 7.2——非适用字段清空，不让旧值被意外提交）。 */
function constraintModal(existing) {
  const ed = state.editor;
  const m = ed.model;
  const fieldCodes = ed.fields.map((f) => f.field_code);
  const others = state.models.filter((x) => x.id !== m.id);
  const cur = existing || {
    constraint_name: "", constraint_type: "primary_key", field_codes: [],
    expression: "", referenced_model_id: null, referenced_field_codes: [],
    on_delete: "NO ACTION", description: "",
  };
  const formHtml = `
      <form id="modal-form" onsubmit="return false">
        <div class="form-grid">
          <div class="form-row"><label>约束名称 <span style="color:var(--danger)">*</span></label>
            <input class="input mono" data-key="constraint_name" value="${escapeHtml(cur.constraint_name)}" placeholder="如 pk_tenant_order" required /></div>
          <div class="form-row"><label>约束类型 <span style="color:var(--danger)">*</span></label>
            <select class="select" data-key="constraint_type" id="ct-type">
              ${CONSTRAINT_TYPES.map((t) => `<option value="${t}" ${cur.constraint_type === t ? "selected" : ""}>${CONSTRAINT_TYPE_LABEL[t]}（${t}）</option>`).join("")}
            </select></div>
          <div class="form-row full ct-fields" data-when="primary_key,unique,foreign_key"><label>字段列表（逗号分隔，顺序属于结构定义）</label>
            <input class="input mono" data-key="field_codes" value="${escapeHtml((cur.field_codes || []).join(","))}" placeholder="如 tenant_id,order_id" /></div>
          <div class="form-row full ct-check" data-when="check" ${cur.constraint_type === "check" ? "" : "hidden"}><label>CHECK 表达式（受限语法：字段比较/算术/AND OR NOT/IN BETWEEN LIKE/常量；不允许函数调用与注释）</label>
            <textarea class="textarea mono" data-key="expression" rows="3" placeholder="如 order_amount >= 0">${escapeHtml(cur.expression || "")}</textarea></div>
          <div class="form-row ct-fk" data-when="foreign_key" ${cur.constraint_type === "foreign_key" ? "" : "hidden"}><label>目标模型</label>
            <select class="select" data-key="referenced_model_id">
              <option value="">（请选择）</option>
              ${others.map((o) => `<option value="${o.id}" ${cur.referenced_model_id === o.id ? "selected" : ""}>${escapeHtml(o.model_key)} · ${escapeHtml(o.name)}</option>`).join("")}
            </select></div>
          <div class="form-row ct-fk" data-when="foreign_key" ${cur.constraint_type === "foreign_key" ? "" : "hidden"}><label>ON DELETE</label>
            <select class="select" data-key="on_delete">
              ${ON_DELETE_ACTIONS.map((a) => `<option value="${a}" ${cur.on_delete === a ? "selected" : ""}>${a}</option>`).join("")}
            </select></div>
          <div class="form-row full ct-fk" data-when="foreign_key" ${cur.constraint_type === "foreign_key" ? "" : "hidden"}><label>目标字段（与源字段一一对应）</label>
            <input class="input mono" data-key="referenced_field_codes" value="${escapeHtml((cur.referenced_field_codes || []).join(","))}" placeholder="如 tenant_id" /></div>
          <div class="form-row full"><label>说明</label>
            <input class="input" data-key="description" value="${escapeHtml(cur.description || "")}" placeholder="业务意图、组合语义或删除动作理由" /></div>
        </div>
        <div class="small muted mt8">类型切换时非适用字段会在提交时清空；服务端仍会二次校验（同项目、字段存在、目标唯一性等）。</div>
      </form>`;
  openModal(existing ? "编辑约束" : "新建表级约束", formHtml, [
    { label: "取消", cls: "btn-ghost", onClick: closeModal },
    {
      label: existing ? "保存" : "创建", cls: "btn-primary", onClick: async () => {
        const vals = {};
        $$("#modal-form [data-key]").forEach((el) => {
          vals[el.dataset.key] = el.value.trim();
        });
        if (!vals.constraint_name) { toast("请填写约束名称", "error"); return; }
        const type = $("#ct-type").value;
        const payload = {
          constraint_name: vals.constraint_name,
          constraint_type: type,
          description: vals.description || "",
        };
        // 按类型切换归一化 payload（与后端 _apply_constraint_payload 同语义）
        if (type === "check") {
          payload.expression = vals.expression || "";
          payload.field_codes = [];
        } else if (type === "foreign_key") {
          payload.field_codes = splitCodes(vals.field_codes);
          payload.referenced_model_id = vals.referenced_model_id ? Number(vals.referenced_model_id) : null;
          payload.referenced_field_codes = splitCodes(vals.referenced_field_codes);
          payload.on_delete = vals.on_delete || "NO ACTION";
        } else {
          payload.field_codes = splitCodes(vals.field_codes);
        }
        try {
          if (existing) {
            await api(`/api/models/${m.id}/constraints/${existing.id}`, { method: "PUT", body: JSON.stringify(payload) });
          } else {
            await api(`/api/models/${m.id}/constraints`, { method: "POST", body: JSON.stringify(payload) });
          }
          closeModal(); toast(existing ? "约束已更新" : "约束已创建", "success");
          await refreshEditor();
        } catch (e) { showError(e); }
      },
    },
  ]);
  // 类型切换：显示/隐藏适用字段
  $("#ct-type").addEventListener("change", () => {
    const t = $("#ct-type").value;
    $$("#modal-form [data-when]").forEach((el) => {
      el.hidden = !(el.dataset.when || "").split(",").includes(t);
    });
  });
}

function splitCodes(s) {
  return (s || "").split(",").map((x) => x.trim()).filter(Boolean);
}

// ---- 索引（V0.8） ----

function renderIndexesTab(body) {
  const ed = state.editor;
  const m = ed.model;
  const idxs = m.indexes || [];
  const editable = m.status === "draft";
  const engine = m.target_engine;
  const engineNote = engine === "mysql"
    ? "MySQL 仅支持 B-tree 索引"
    : engine === "hive"
      ? "Hive 不生成物理索引，索引仅作为逻辑元数据"
      : "PostgreSQL 支持 btree/hash/gin（唯一 GIN 不支持）";
  body.innerHTML = `
    <div class="card">
      <div class="card-title">索引 <span class="sub">查询访问路径意图，不替代唯一性定义 · ${escapeHtml(engineNote)}</span>
        ${editable ? `<button class="btn btn-sm btn-primary" id="ix-new">+ 新建索引</button>` : ""}
      </div>
      ${idxs.length ? `<div class="table-wrap"><table class="data">
        <thead><tr><th>名称</th><th>类型</th><th>唯一</th><th>字段（顺序影响左前缀）</th><th>说明</th><th></th></tr></thead>
        <tbody>${idxs.map((i) => `
          <tr>
            <td class="mono">${escapeHtml(i.index_name)}</td>
            <td><span class="badge badge-neutral">${escapeHtml(i.index_type)}</span></td>
            <td>${i.is_unique ? '<span class="badge badge-info">唯一</span>' : "—"}</td>
            <td class="mono small">${escapeHtml((i.field_codes || []).join(","))}</td>
            <td class="small muted">${escapeHtml(i.description || "—")}</td>
            <td>${editable ? `<button class="btn btn-sm btn-danger" data-ix-del="${i.id}">删除</button>` : ""}</td>
          </tr>`).join("")}</tbody></table></div>`
      : `<div class="empty"><div class="empty-desc">暂无索引。</div></div>`}
      ${!editable ? `<div class="small muted mt14">模型当前为「${STATUS_LABEL[m.status] || m.status}」，索引只读；请通过“发起变更”创建草稿后修改。</div>` : ""}
    </div>`;

  if (editable) {
    $("#ix-new").onclick = () => indexModal();
    $$("[data-ix-del]", body).forEach((el) => el.addEventListener("click", async () => {
      try {
        await api(`/api/models/${m.id}/indexes/${el.dataset.ixDel}`, { method: "DELETE" });
        toast("索引已删除", "success"); await refreshEditor();
      } catch (e) { showError(e); }
    }));
  }
}

function indexModal() {
  const ed = state.editor;
  const m = ed.model;
  openModal("新建索引", `
    <form id="modal-form" onsubmit="return false">
      <div class="form-grid">
        <div class="form-row"><label>索引名称 <span style="color:var(--danger)">*</span></label>
          <input class="input mono" data-key="index_name" placeholder="如 idx_tenant_order_date" required /></div>
        <div class="form-row"><label>索引类型</label>
          <select class="select" data-key="index_type" id="ix-type">
            ${INDEX_TYPES.map((t) => `<option value="${t}">${t}</option>`).join("")}
          </select></div>
        <div class="form-row full"><label>字段列表（逗号分隔；复合索引顺序影响左前缀匹配）</label>
          <input class="input mono" data-key="field_codes" placeholder="如 tenant_id,order_date" required /></div>
        <div class="form-row"><label>唯一索引</label>
          <select class="select" data-key="is_unique"><option value="false">否</option><option value="true">是</option></select></div>
        <div class="form-row full"><label>说明</label>
          <input class="input" data-key="description" placeholder="查询场景、谓词与验证方法（上线前 EXPLAIN）" /></div>
      </div>
      <div class="small muted mt8">索引变化属于性能相关变更，不直接推断业务语义变化。引擎不支持的组合会在保存或生成 DDL 时拒绝。</div>
    </form>`, [
    { label: "取消", cls: "btn-ghost", onClick: closeModal },
    {
      label: "创建", cls: "btn-primary", onClick: async () => {
        const vals = {};
        $$("#modal-form [data-key]").forEach((el) => { vals[el.dataset.key] = el.value.trim(); });
        if (!vals.index_name) { toast("请填写索引名称", "error"); return; }
        if (!vals.field_codes) { toast("请填写字段列表", "error"); return; }
        try {
          await api(`/api/models/${m.id}/indexes`, {
            method: "POST",
            body: JSON.stringify({
              index_name: vals.index_name,
              index_type: vals.index_type,
              field_codes: splitCodes(vals.field_codes),
              is_unique: vals.is_unique === "true",
              description: vals.description || "",
            }),
          });
          closeModal(); toast("索引已创建", "success"); await refreshEditor();
        } catch (e) { showError(e); }
      },
    },
  ]);
}

// ---- 影响分析（V0.8） ----

async function renderImpactTab(body) {
  const ed = state.editor;
  const m = ed.model;
  const fieldCodes = ed.fields.map((f) => f.field_code);
  body.innerHTML = `
    <div class="card">
      <div class="card-title">变更影响分析 <span class="sub">候选结果：当前草稿 vs 最近发布快照，沿 source→target 有向边传播</span></div>
      <div class="toolbar">
        <span class="small muted">收窄字段（可选，逗号分隔）：</span>
        <input class="input mono" id="impact-fields" placeholder="如 customer_id" style="width:240px" />
        <button class="btn btn-primary" id="impact-run">${ed.impactCache ? "重新分析" : "运行分析"}</button>
      </div>
      <div class="small muted mt8">字段范围可用当前字段快速勾选：</div>
      <div class="flex flex-wrap mt8" id="impact-chips">
        ${fieldCodes.map((c) => `<label class="impact-chip"><input type="checkbox" value="${escapeHtml(c)}" /> <span class="mono">${escapeHtml(c)}</span></label>`).join("") || '<span class="small muted">当前模型无字段</span>'}
      </div>
      <div id="impact-result" class="small muted mt14">分析中…</div>
    </div>`;

  const box = $("#impact-result");
  const renderResult = (r, codes, cached) => {
    ed.impactCache = { result: r, codes };   // 跨 tab 留存（保存/状态变更时失效）
    const cs = r.change_summary || {};
    const modeLabel = r.analysis_mode === "no_published_baseline" ? "无发布基线（全模型范围）" : "草稿 vs 最近发布快照";
    const changedLabel = r.changed_field_codes === null
      ? "模型级范围"
      : (r.changed_field_codes.length ? r.changed_field_codes.join(", ") : "无相关变化");
    box.innerHTML = `
      ${cached ? `<div class="issue info"><span class="msg">以下为上次分析结果（跨页签保留，字段范围：${codes ? escapeHtml(codes) : "全部字段"}）。重新保存草稿或模型状态变化后将自动重算。</span></div>` : ""}
      <div class="flex flex-wrap small" style="gap:14px;margin-bottom:10px">
        <span>模式：<b>${escapeHtml(modeLabel)}</b></span>
        <span>变化字段：<b>${escapeHtml(changedLabel)}</b></span>
        ${cs.model_level_reasons && cs.model_level_reasons.length ? `<span class="badge badge-warning">${cs.model_level_reasons.map(escapeHtml).join(" / ")}</span>` : ""}
        ${r.truncated ? `<span class="badge badge-error">结果已截断（max_nodes）</span>` : ""}
      </div>
      <div class="issue warning"><span class="msg">${escapeHtml(r.direction_semantics || "")}</span></div>
      ${r.impacted_models && r.impacted_models.length ? `
        <div class="table-wrap mt14"><table class="data">
          <thead><tr><th>下游模型</th><th>分层</th><th>受影响字段</th><th>传播路径</th><th>历史基线边</th><th>人工确认</th></tr></thead>
          <tbody>${r.impacted_models.map((im) => `
            <tr>
              <td><span class="link" data-impact-open="${im.model_id}">${escapeHtml(im.name || im.model_key || "")}</span><div class="small muted mono">${escapeHtml(im.model_key || "")}</div></td>
              <td>${escapeHtml(im.layer || "—")}</td>
              <td class="mono small">${im.affected_fields === null ? '<span class="badge badge-warning">模型级</span>' : escapeHtml((im.affected_fields || []).join(",")) || "—"}</td>
              <td class="mono small">${(im.paths || []).map((p) => escapeHtml(p.join(" → "))).join("<br>")}${im.alternate_paths_count ? `<div class="small muted">另有 ${im.alternate_paths_count} 条备用路径</div>` : ""}</td>
              <td>${im.includes_baseline_edge ? '<span class="badge badge-info">含基线</span>' : "—"}</td>
              <td>${im.requires_manual_confirmation ? '<span class="badge badge-warning">需要</span>' : "—"}</td>
            </tr>`).join("")}
          </tbody></table></div>`
        : `<div class="empty"><div class="empty-desc">未发现受影响的候选下游模型。</div></div>`}`;
    $$("[data-impact-open]", box).forEach((el) => el.addEventListener("click", () => openModelEditor(Number(el.dataset.impactOpen))));
  };
  const runAnalysis = async () => {
    const manual = $("#impact-fields").value.trim();
    const checked = $$("#impact-chips input:checked").map((el) => el.value);
    const codes = manual ? manual : checked.join(",");
    box.innerHTML = `<div class="small muted">分析中…</div>`;
    try {
      const q = codes ? `?field_codes=${encodeURIComponent(codes)}` : "";
      const r = await api(`/api/models/${m.id}/impact${q}`);
      renderResult(r, codes, false);
    } catch (e) { showError(e); }
  };
  $("#impact-run").onclick = runAnalysis;
  // U8：有缓存且无未保存编辑时直接展示上次结果；否则自动运行一次
  if (ed.impactCache && !ed.dirty) {
    const { result, codes } = ed.impactCache;
    if (codes) $("#impact-fields").value = codes;
    renderResult(result, codes, true);
  } else {
    await runAnalysis();
  }
}

// ---- 版本与差异 ----

async function renderVersionsTab(body) {
  const ed = state.editor;
  const m = ed.model;
  body.innerHTML = `<div class="small muted">加载版本…</div>`;
  let versions = [];
  try { versions = await api(`/api/models/${m.id}/versions`); }
  catch (e) { showError(e); return; }
  body.innerHTML = `
    <div class="card">
      <div class="card-title">正式版本 <span class="sub">发布快照不可变</span></div>
      ${versions.length ? `<div class="table-wrap"><table class="data">
        <thead><tr><th>版本</th><th>状态</th><th>变更说明</th><th>发布人</th><th>时间</th></tr></thead>
        <tbody>${versions.map((v) => `
          <tr><td class="mono">V${v.version_no}</td>
            <td><span class="badge badge-published">已发布</span></td>
            <td>${escapeHtml(v.change_note || "—")}</td>
            <td>${escapeHtml(v.created_by || "—")}</td>
            <td class="small muted">${fmtTime(v.created_at)}</td></tr>`).join("")}
        </tbody></table></div>`
      : `<div class="empty"><div class="empty-desc">暂无正式版本，评审通过后发布。</div></div>`}
    </div>
    <div class="card">
      <div class="card-title">版本差异</div>
      ${versions.length >= 2 ? `
        <div class="toolbar">
          <select class="select" id="diff-from" style="width:130px">${versions.map((v) => `<option value="${v.version_no}">V${v.version_no}</option>`).join("")}</select>
          <span class="muted">→</span>
          <select class="select" id="diff-to" style="width:130px">${versions.map((v) => `<option value="${v.version_no}">V${v.version_no}</option>`).join("")}</select>
          <button class="btn btn-primary" id="diff-run">比较</button>
        </div>
        <div id="diff-result" class="small muted">选择两个版本后点击比较。</div>`
      : `<div class="empty"><div class="empty-desc">至少需要两个正式版本才能比较差异。</div></div>`}
    </div>`;
  if (versions.length >= 2) {
    $("#diff-from").value = String(versions[versions.length - 1].version_no);
    $("#diff-to").value = String(versions[0].version_no);
    $("#diff-run").onclick = async () => {
      const from = Number($("#diff-from").value);
      const to = Number($("#diff-to").value);
      const box = $("#diff-result");
      box.innerHTML = `<div class="small muted">比较中…</div>`;
      try {
        const d = await api(`/api/models/${m.id}/versions/diff?from_version=${from}&to_version=${to}`);
        const f = d.fields;
        const objDiff = (diff, nameField, describe) => {
          if (!diff) return "";
          const total = (diff.added || []).length + (diff.removed || []).length + (diff.modified || []).length;
          if (!total) return "";
          return `<div class="diff-block"><div class="diff-block-title">${describe}（${total}）</div>
            ${(diff.added || []).map((x) => `<div class="diff-item diff-add">+ ${escapeHtml(x[nameField] || JSON.stringify(x))}</div>`).join("")}
            ${(diff.removed || []).map((x) => `<div class="diff-item diff-del">- ${escapeHtml(x[nameField] || JSON.stringify(x))}</div>`).join("")}
            ${(diff.modified || []).map((x) => `<div class="diff-item diff-mod">~ ${escapeHtml(x[nameField] || x.relationship_key || "")}：${(x.changes || []).map((c) => `<span class="attr">${escapeHtml(c.attribute)}</span> ${escapeHtml(JSON.stringify(c.from))}→${escapeHtml(JSON.stringify(c.to))}`).join("，")}</div>`).join("")}
          </div>`;
        };
        box.innerHTML = `
          ${d.model_changes.length ? `<div class="diff-block"><div class="diff-block-title">模型属性变化</div>
            ${d.model_changes.map((c) => `<div class="diff-item diff-mod"><span class="attr">${escapeHtml(c.attribute)}:</span> ${escapeHtml(JSON.stringify(c.from))} → ${escapeHtml(JSON.stringify(c.to))}</div>`).join("")}</div>` : ""}
          <div class="diff-block"><div class="diff-block-title">字段新增（${f.added.length}）</div>
            ${f.added.length ? f.added.map((x) => `<div class="diff-item diff-add">+ ${escapeHtml(x.field_code)} ${escapeHtml(x.field_name || "")} ${escapeHtml(x.logical_type)}</div>`).join("") : '<div class="small muted">无</div>'}</div>
          <div class="diff-block"><div class="diff-block-title">字段删除（${f.removed.length}）</div>
            ${f.removed.length ? f.removed.map((x) => `<div class="diff-item diff-del">- ${escapeHtml(x.field_code)} ${escapeHtml(x.field_name || "")}</div>`).join("") : '<div class="small muted">无</div>'}</div>
          <div class="diff-block"><div class="diff-block-title">字段修改（${f.modified.length}）</div>
            ${f.modified.length ? f.modified.map((x) => `<div class="diff-item diff-mod">~ ${escapeHtml(x.field_code)}：${x.changes.map((c) => `<span class="attr">${escapeHtml(c.attribute)}</span> ${escapeHtml(JSON.stringify(c.from))}→${escapeHtml(JSON.stringify(c.to))}`).join("，")}</div>`).join("") : '<div class="small muted">无</div>'}</div>
          ${objDiff(d.constraints, "constraint_name", "表级约束变化")}
          ${objDiff(d.indexes, "index_name", "索引变化（性能相关）")}
          ${objDiff(d.relationships, "relationship_key", "相邻关系变化")}
          <div class="diff-block"><div class="diff-block-title">需求关联变化</div>
            ${d.requirements.added.length || d.requirements.removed.length
              ? [...d.requirements.added.map((r) => `<div class="diff-item diff-add">+ 关联需求 #${r}</div>`), ...d.requirements.removed.map((r) => `<div class="diff-item diff-del">- 解除需求 #${r}</div>`)].join("")
              : '<div class="small muted">无</div>'}</div>`;
      } catch (e) { showError(e); }
    };
  }
}

// ---- DDL 预览 ----

function renderDdlTab(body) {
  const ed = state.editor;
  const m = ed.model;
  body.innerHTML = `
    <div class="card">
      <div class="card-title">DDL 预览 <span class="sub">只生成，不执行（execute_on_target=false）</span></div>
      <div class="toolbar">
        <select class="select" id="ddl-engine" style="width:150px">${ENGINES.map((e) => `<option value="${e}" ${m.target_engine === e ? "selected" : ""}>${e}</option>`).join("")}</select>
        <input class="input" id="ddl-schema" placeholder="Schema（可选）" style="width:180px" />
        <button class="btn btn-primary" id="ddl-run">生成 DDL</button>
      </div>
      <div id="ddl-result" class="small muted">选择引擎后点击生成。存在阻断级校验错误时无法生成。</div>
    </div>`;
  $("#ddl-run").onclick = async () => {
    const box = $("#ddl-result");
    box.innerHTML = `<div class="small muted">生成中…</div>`;
    try {
      const r = await api(`/api/models/${m.id}/ddl`, {
        method: "POST",
        body: JSON.stringify({
          target_engine: $("#ddl-engine").value,
          schema_name: $("#ddl-schema").value.trim() || null,
        }),
      });
      box.innerHTML = `
        <div class="flex small muted" style="margin-bottom:8px">
          <span>引擎：${escapeHtml(r.target_engine)}</span>
          <span>生成器：${escapeHtml(r.generator_version)}</span>
          ${m.published_version_no ? `<span>版本：V${m.published_version_no}</span>` : ""}
          <span class="badge badge-info">execute_on_target=false</span>
        </div>
        ${r.warnings && r.warnings.length ? r.warnings.map((w) => `<div class="issue warning"><span class="msg">${escapeHtml(w)}</span></div>`).join("") : ""}
        ${r.unsupported_features && r.unsupported_features.length ? r.unsupported_features.map((w) => `<div class="issue info"><span class="msg">${escapeHtml(w)}</span></div>`).join("") : ""}
        <pre class="ddl">${escapeHtml(r.ddl)}</pre>`;
    } catch (e) {
      box.innerHTML = "";
      showError(e);
      if (e.details && e.details.issues) {
        box.innerHTML = `<div class="issue-group-title"><span class="badge badge-error">阻断错误</span></div>` +
          e.details.issues.map((i) => `<div class="issue error"><span class="rule">${escapeHtml(i.rule_id)}</span><span class="msg">${escapeHtml(i.message)}</span><span class="loc">${escapeHtml(i.location)}</span></div>`).join("");
      }
    }
  };
}

// ---------------------------------------------------------------- 项目

function newProjectModal() {
  formModal("新建项目", [
    { key: "project_key", label: "项目编码", required: true, placeholder: "如 PRJ_RETAIL" },
    { key: "name", label: "项目名称", required: true, placeholder: "如 零售数据平台试点" },
    { key: "description", label: "项目描述", type: "textarea" },
  ], "创建", async (v) => {
    try {
      const p = await api("/api/projects", { method: "POST", body: JSON.stringify(v) });
      closeModal(); toast("项目已创建", "success");
      state.projectId = p.id;
      await loadProjects();
      await renderView();
    } catch (e) { showError(e); }
  });
}

// ---------------------------------------------------------------- 用户与权限（V1.2 RBAC）

const ROLE_LABEL = { owner: "所有者", editor: "编辑者", viewer: "只读" };
const KIND_LABEL = { human: "自然人", service_account: "服务账号" };
const USER_STATUS_LABEL = { active: "正常", disabled: "已禁用" };
const PLATFORM_CAP_LABEL = {
  "user.manage": "用户管理",
  "api_key.provision": "代建密钥",
  "metrics.view": "观测指标",
};
const PROJECT_CAP_LABEL = {
  "model.read": "项目读取",
  "model.write": "项目写入",
  "member.manage": "成员管理",
};
const MEMBER_ROLES = ["owner", "editor", "viewer"];

function debounce(fn, ms) {
  let timer = null;
  return function (...args) {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), ms);
  };
}

// ---- 有效能力（与后端 auth.py 能力矩阵同源；服务端是权威，前端只做显隐） ----

function _capsOf(me) {
  const c = (me && me.capabilities) || {};
  return { platform: c.platform || [], projects: c.projects || [] };
}

function hasPlatformCap(cap) {
  return _capsOf(state.me).platform.indexOf(cap) >= 0;
}

function currentProjectRole() {
  const me = state.me;
  if (!me) return null;
  if (me.is_admin) return "admin";
  const row = _capsOf(me).projects.find((c) => c.project_id === state.projectId);
  return row ? row.role : null;
}

function hasProjectCap(cap) {
  const me = state.me;
  if (!me) return false;
  if (me.is_admin) return true;
  const row = _capsOf(me).projects.find((c) => c.project_id === state.projectId);
  return !!row && row.capabilities.indexOf(cap) >= 0;
}

function projectName(projectId) {
  const p = state.projects.find((x) => x.id === projectId);
  return p ? `${p.project_key} · ${p.name}` : `#${projectId}`;
}

// ---- 身份加载、顶栏芯片与登录 ----

async function loadMe() {
  const hadToken = !!getToken();
  try {
    state.me = await api("/api/auth/me");
  } catch (e) {
    state.me = null;
    // 携带令牌仍失败：失效/吊销/过期/禁用 → 清除登录态并提示重登
    if (hadToken && ["AUTH_REQUIRED", "API_KEY_INVALID", "API_KEY_REVOKED",
      "API_KEY_EXPIRED", "USER_DISABLED"].indexOf(e.code) >= 0) {
      clearToken();
      toast("登录已失效，请重新登录", "error", e.code);
    }
  }
  renderIdentityChip();
  return state.me;
}

function renderIdentityChip() {
  const chip = $("#identity-chip");
  if (!chip) return;
  const me = state.me;
  if (!me) {
    chip.innerHTML = `<button class="btn btn-ghost btn-sm" id="btn-login">登录</button>`;
    $("#btn-login").onclick = loginModal;
    return;
  }
  const role = me.is_admin ? "平台管理员"
    : (currentProjectRole() ? ROLE_LABEL[currentProjectRole()] : "无项目角色");
  const loggedIn = !!getToken();
  chip.innerHTML = `
    <span class="identity-name" title="${escapeHtml(me.user_key)}">${escapeHtml(me.display_name)}</span>
    <span class="badge ${me.is_admin ? "badge-admin" : "badge-neutral"}">${role}</span>
    <span class="identity-mode">${me.auth_mode === "demo" ? (loggedIn ? "演示模式 · 已登录" : "演示模式") : "API Key"}</span>
    ${loggedIn
      ? `<button class="btn btn-ghost btn-sm" id="btn-logout">退出</button>`
      : `<button class="btn btn-ghost btn-sm" id="btn-login">登录</button>`}`;
  const loginBtn = $("#btn-login");
  if (loginBtn) loginBtn.onclick = loginModal;
  const out = $("#btn-logout");
  if (out) {
    out.onclick = async () => {
      // V1.3：先吊销服务端会话密钥（长期 API Key 不受影响），再清本地令牌；
      // 吊销请求失败（如网络中断）也继续清理本地，避免卡在失效登录态。
      try { await api("/api/auth/logout", { method: "POST" }); } catch (e) { /* 忽略 */ }
      clearToken();
      toast("已退出登录", "success");
      await loadMe();
      if (state.view === "users") renderView();
    };
  }
  chip.onclick = (e) => {
    if (e.target.id === "btn-logout" || e.target.id === "btn-login") return; // 自带处理
    setView("users");
  };
}

function loginModal() {
  const demo = state.me && state.me.auth_mode === "demo";
  openModal("登录", `
    <div class="tabs" id="login-tabs">
      <button class="tab active" data-login-tab="password">账号密码</button>
      <button class="tab" data-login-tab="token">API Key</button>
    </div>
    <div id="login-pane-password">
      <div class="form-row"><label>账号 <span style="color:var(--danger)">*</span></label>
        <input class="input" id="login-user" placeholder="user_key 或裸登录名（如 admin）" autocomplete="username" /></div>
      <div class="form-row"><label>密码 <span style="color:var(--danger)">*</span></label>
        <input class="input" id="login-pass" type="password" placeholder="••••••••" autocomplete="current-password" /></div>
      ${demo ? `<div class="muted small mt8">演示账号（demo 模式启动时自动播种，详见 README）：
        <b>admin</b> / Admin@123456（平台管理员）；
        alice / Alice@123456（项目 owner）；bob / Bob@123456（editor）；
        carol / Carol@123456（viewer）。登录后按真实角色执行权限。</div>` : ""}
    </div>
    <div id="login-pane-token" hidden>
      <div class="form-row"><label>访问令牌 <span style="color:var(--danger)">*</span></label>
        <input class="input" id="login-token" placeholder="dmc_ak_xxx_xxx（创建时仅展示一次，请妥善保存）" /></div>
      <div class="muted small mt8">API Key 在「用户与权限 → API Key」卡片创建；平台管理员可为他人代建。</div>
    </div>`, [
    { label: "取消", cls: "btn-ghost", onClick: closeModal },
    { label: "登录", cls: "btn-primary", id: "login-submit", onClick: submitLogin },
  ]);
  let tab = "password";
  $$("#login-tabs .tab").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.loginTab;
      $$("#login-tabs .tab").forEach((b) => b.classList.toggle("active", b === btn));
      $("#login-pane-password").hidden = tab !== "password";
      $("#login-pane-token").hidden = tab !== "token";
    };
  });
  const onKey = (e) => { if (e.key === "Enter") { e.preventDefault(); submitLogin(); } };
  $("#login-user").addEventListener("keydown", onKey);
  $("#login-pass").addEventListener("keydown", onKey);
  $("#login-token").addEventListener("keydown", onKey);
  setTimeout(() => $("#login-user").focus(), 50);
}

async function submitLogin() {
  const panePassword = !$("#login-pane-password").hidden;
  try {
    if (panePassword) {
      const user = $("#login-user").value.trim();
      const pass = $("#login-pass").value;
      if (!user || !pass) { toast("请输入账号和密码", "error"); return; }
      const r = await api("/api/auth/login", {
        method: "POST", body: JSON.stringify({ login: user, password: pass }),
      });
      setToken(r.token);
      toast(`已登录：${r.display_name}`, "success");
    } else {
      const token = $("#login-token").value.trim();
      if (!token) { toast("请输入访问令牌", "error"); return; }
      setToken(token);
    }
    closeModal();
    const me = await loadMe();
    if (me) {
      if (state.view === "users") await renderView();
    } else {
      clearToken();
      toast("令牌无效或已失效", "error");
    }
  } catch (e) {
    if (panePassword && e.code === "INVALID_CREDENTIALS") {
      toast("用户名或密码错误", "error", e.code);
    } else {
      showError(e);
    }
  }
}

function passwordModal() {
  formModal("修改密码", [
    { key: "current_password", label: "当前密码", type: "password", required: true },
    { key: "new_password", label: "新密码（至少 8 位）", type: "password", required: true },
  ], "确认修改", async (v) => {
    try {
      await api("/api/auth/password", {
        method: "POST", body: JSON.stringify(v),
      });
      closeModal();
      toast("密码已修改", "success");
    } catch (e) { showError(e); }
  });
}

// ---- 视图外壳 ----

function noPermCardHtml(title, desc) {
  return `<div class="card">
    <div class="card-head"><div class="card-title">${escapeHtml(title)}</div></div>
    <div class="card-body"><p class="muted">${escapeHtml(desc)}</p></div>
  </div>`;
}

function identityBodyHtml(me) {
  const caps = _capsOf(me);
  const plat = caps.platform.map((c) =>
    `<span class="cap-badge">${escapeHtml(PLATFORM_CAP_LABEL[c] || c)}</span>`).join("")
    || `<span class="muted">无平台级能力</span>`;
  const projRows = caps.projects.map((c) => `
    <tr>
      <td class="mono">#${c.project_id}</td>
      <td>${escapeHtml(projectName(c.project_id))}</td>
      <td><span class="badge badge-role-${c.role}">${ROLE_LABEL[c.role] || c.role}</span></td>
      <td>${c.capabilities.map((k) =>
        `<span class="cap-badge sm">${escapeHtml(PROJECT_CAP_LABEL[k] || k)}</span>`).join(" ")}</td>
    </tr>`).join("");
  return `
    <div class="identity-row">
      <div class="identity-avatar">${escapeHtml((me.display_name || "?")[0])}</div>
      <div>
        <div class="identity-name-lg">${escapeHtml(me.display_name)}</div>
        <div class="muted mono">${escapeHtml(me.user_key)} · ${KIND_LABEL[me.kind] || me.kind} · ${USER_STATUS_LABEL[me.status] || me.status}</div>
      </div>
      ${me.is_admin ? `<span class="badge badge-admin">平台管理员</span>` : ""}
    </div>
    <div class="identity-caps"><span class="identity-caps-label">平台能力</span>${plat}</div>
    <div class="identity-caps"><span class="identity-caps-label">项目角色</span>${
      projRows
        ? `<div class="table-wrap"><table class="table"><thead><tr><th>项目</th><th>名称</th><th>角色</th><th>有效能力</th></tr></thead><tbody>${projRows}</tbody></table></div>`
        : `<span class="muted">暂无项目成员关系</span>`}
    </div>
    <div class="mt14">
      ${me.has_password ? `<button class="btn btn-sm" id="identity-change-password">修改密码</button>` : ""}
      ${me.auth_mode === "demo" && !getToken() ? `<button class="btn btn-sm btn-primary" id="identity-login">登录（账号密码 / API Key）</button>` : ""}
    </div>`;
}

function usersCardHtml() {
  return `
    <div class="card" id="users-manage-card">
      <div class="card-head">
        <div class="card-title">用户管理</div>
        <div class="card-actions"><button class="btn btn-primary btn-sm" id="users-new">+ 新建用户</button></div>
      </div>
      <div class="card-body">
        <div class="users-toolbar">
          <input class="input" id="users-filter-q" placeholder="搜索 user_key / 名称" />
          <select class="select" id="users-filter-status">
            <option value="">全部状态</option>
            <option value="active">正常</option>
            <option value="disabled">已禁用</option>
          </select>
          <span class="muted" id="users-count"></span>
        </div>
        <div class="table-wrap"><table class="table" id="users-table">
          <thead><tr><th>user_key</th><th>名称</th><th>类型</th><th>平台角色</th><th>状态</th><th>项目</th><th>密钥</th><th>创建时间</th><th>操作</th></tr></thead>
          <tbody><tr><td colspan="9" class="muted">加载中…</td></tr></tbody>
        </table></div>
      </div>
    </div>`;
}

function membersCardHtml(proj) {
  return `
    <div class="card" id="users-members-card">
      <div class="card-head">
        <div class="card-title">项目成员 <span class="sub">· ${escapeHtml(proj.project_key)}</span></div>
        <div class="card-actions"><button class="btn btn-primary btn-sm" id="members-add">+ 添加成员</button></div>
      </div>
      <div class="card-body">
        <div class="table-wrap"><table class="table" id="members-table">
          <thead><tr><th>user_key</th><th>名称</th><th>类型</th><th>角色</th><th>加入时间</th><th>操作</th></tr></thead>
          <tbody><tr><td colspan="6" class="muted">加载中…</td></tr></tbody>
        </table></div>
        <p class="muted mt14">主体需先由平台管理员创建（用户管理卡）；不存在时返回 404 USER_NOT_FOUND。最后一个所有者不能降级或移除。</p>
      </div>
    </div>`;
}

function keysCardHtml(canProvision) {
  return `
    <div class="card" id="users-keys-card">
      <div class="card-head">
        <div class="card-title">API Key</div>
        <div class="card-actions"><button class="btn btn-primary btn-sm" id="keys-create">+ 创建密钥</button></div>
      </div>
      <div class="card-body">
        ${canProvision ? `<div class="users-toolbar"><input class="input" id="keys-filter-user" placeholder="按 user_key 查询他人密钥（管理员）" /></div>` : ""}
        <div class="table-wrap"><table class="table" id="keys-table">
          <thead><tr><th>名称</th><th>key_id</th><th>状态</th><th>创建时间</th><th>最近使用</th><th>过期时间</th><th>操作</th></tr></thead>
          <tbody><tr><td colspan="7" class="muted">加载中…</td></tr></tbody>
        </table></div>
        <p class="muted mt14">secret 仅在创建响应中展示一次，服务端只存 SHA-256 哈希；吊销后旧令牌即时失效。</p>
      </div>
    </div>`;
}

async function renderUsers(content) {
  await loadMe();
  const me = state.me;
  if (!me) {
    content.innerHTML = `
      <div class="card">
        <div class="card-head"><div class="card-title">用户与权限</div></div>
        <div class="card-body">
          <p class="muted">当前无法获取身份信息。</p>
          <p class="muted">演示模式（AUTH_MODE=demo）下匿名请求以演示管理员身份放行，也可用账号密码登录后按真实角色执行权限；API Key 模式（AUTH_MODE=api_key）需要先用平台颁发的访问令牌或账号密码登录，按有效权限使用各项功能。</p>
          <div class="mt14"><button class="btn btn-primary" id="users-login">登录（账号密码 / API Key）</button></div>
        </div>
      </div>`;
    $("#users-login").onclick = loginModal;
    return;
  }
  const canManageUsers = hasPlatformCap("user.manage");
  const canProvision = hasPlatformCap("api_key.provision");
  const canManageMembers = hasProjectCap("member.manage");
  const proj = state.projects.find((p) => p.id === state.projectId);
  content.innerHTML = `
    ${me.auth_mode === "demo" ? `<div class="demo-banner">演示模式（AUTH_MODE=demo）：匿名请求以演示管理员身份放行；${getToken() ? "当前已登录，按真实角色执行权限校验。" : `<button class="btn btn-sm" id="demo-banner-login">登录（账号密码 / API Key）</button> 后将按真实角色执行权限校验。`}生产模式（api_key）下所有请求必须携带有效凭据。</div>` : ""}
    <div class="users-grid">
      <div class="card" id="users-identity-card">
        <div class="card-head"><div class="card-title">当前身份</div></div>
        <div class="card-body">${identityBodyHtml(me)}</div>
      </div>
      ${canManageUsers ? usersCardHtml() : noPermCardHtml("用户管理", "仅平台管理员可以管理用户与角色。")}
      ${proj
        ? (canManageMembers ? membersCardHtml(proj) : noPermCardHtml(`项目成员 · ${proj.name}`, "仅项目所有者或平台管理员可以管理成员。"))
        : `<div class="card"><div class="card-head"><div class="card-title">项目成员</div></div><div class="card-body"><p class="muted">请先在左下角选择一个项目。</p></div></div>`}
      ${keysCardHtml(canProvision)}
    </div>`;
  wireUsersView();
  const tasks = [loadKeysTable()];
  if (canManageUsers) tasks.push(loadUsersTable());
  if (proj && canManageMembers) tasks.push(loadMembersTable());
  await Promise.all(tasks);
}

function wireUsersView() {
  const bind = (sel, ev, fn) => { const el = $(sel); if (el) el.addEventListener(ev, fn); };
  bind("#identity-change-password", "click", passwordModal);
  bind("#identity-login", "click", loginModal);
  bind("#demo-banner-login", "click", loginModal);
  if (hasPlatformCap("user.manage")) {
    bind("#users-new", "click", () => userModal());
    const q = $("#users-filter-q");
    if (q) {
      q.value = state.usersFilter.q;
      q.addEventListener("input", debounce(() => {
        state.usersFilter.q = q.value.trim();
        loadUsersTable();
      }, 300));
    }
    const st = $("#users-filter-status");
    if (st) {
      st.value = state.usersFilter.status;
      st.addEventListener("change", () => {
        state.usersFilter.status = st.value;
        loadUsersTable();
      });
    }
  }
  if (hasProjectCap("member.manage")) {
    bind("#members-add", "click", addMemberModal);
  }
  bind("#keys-create", "click", createKeyModal);
  bind("#keys-filter-user", "change", () => loadKeysTable());
}

// ---- 用户管理 ----

async function loadUsersTable() {
  const params = {};
  if (state.usersFilter.q) params.q = state.usersFilter.q;
  if (state.usersFilter.status) params.status = state.usersFilter.status;
  const qs = new URLSearchParams(params).toString();
  state.users = await api("/api/users" + (qs ? "?" + qs : ""));
  renderUsersTable();
}

function renderUsersTable() {
  const tbody = $("#users-table tbody");
  if (!tbody) return;
  const me = state.me || {};
  const activeAdmins = state.users.filter((u) => u.is_admin && u.status === "active").length;
  if (!state.users.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="muted">没有匹配的用户</td></tr>`;
  } else {
    tbody.innerHTML = state.users.map((u) => {
      const isSelf = u.user_id === me.user_id;
      const lastAdmin = u.is_admin && u.status === "active" && activeAdmins <= 1;
      const canToggle = !isSelf && !lastAdmin;
      const toggleTitle = isSelf ? "不能禁用/降级自己" : "最后一个平台管理员受保护";
      return `<tr>
        <td class="mono">${escapeHtml(u.user_key)}</td>
        <td>${escapeHtml(u.display_name)}</td>
        <td>${KIND_LABEL[u.kind] || escapeHtml(u.kind)}</td>
        <td>${u.is_admin ? `<span class="badge badge-admin">管理员</span>` : `<span class="muted">—</span>`}</td>
        <td><span class="badge ${u.status === "active" ? "badge-published" : "badge-error"}">${USER_STATUS_LABEL[u.status] || u.status}</span></td>
        <td>${u.project_count}</td>
        <td>${u.active_key_count}</td>
        <td>${fmtTime(u.created_at)}</td>
        <td class="users-ops">
          <button class="btn btn-ghost btn-sm" data-users-edit="${u.user_id}">编辑</button>
          ${u.status === "active"
            ? `<button class="btn btn-ghost btn-sm danger" data-users-disable="${u.user_id}" ${canToggle ? "" : `disabled title="${toggleTitle}"`}>禁用</button>`
            : `<button class="btn btn-ghost btn-sm" data-users-enable="${u.user_id}" ${isSelf ? "disabled" : ""}>启用</button>`}
        </td>
      </tr>`;
    }).join("");
  }
  const count = $("#users-count");
  if (count) count.textContent = `共 ${state.users.length} 个主体`;
  $$("#users-table [data-users-edit]").forEach((b) =>
    b.addEventListener("click", () => userModal(Number(b.dataset.usersEdit))));
  $$("#users-table [data-users-disable]").forEach((b) =>
    b.addEventListener("click", () => toggleUser(Number(b.dataset.usersDisable), "disabled")));
  $$("#users-table [data-users-enable]").forEach((b) =>
    b.addEventListener("click", () => toggleUser(Number(b.dataset.usersEnable), "active")));
}

function userModal(userId) {
  const u = userId ? state.users.find((x) => x.user_id === userId) : null;
  const editing = !!u;
  const activeAdmins = state.users.filter((x) => x.is_admin && x.status === "active").length;
  const isSelf = editing && u.user_id === (state.me || {}).user_id;
  const lastAdmin = editing && u.is_admin && u.status === "active" && activeAdmins <= 1;
  const fields = [
    { key: "user_key", label: "user_key", required: true, value: u ? u.user_key : "", placeholder: "如 u:alice / sa:dbt-importer" },
    { key: "display_name", label: "名称", required: true, value: u ? u.display_name : "" },
  ];
  if (!editing) {
    fields.push({ key: "kind", label: "账号类型", type: "select", value: "human",
      options: [{ value: "human", label: "自然人" }, { value: "service_account", label: "服务账号" }] });
  }
  fields.push({ key: "is_admin", label: "平台管理员", type: "select",
    value: u ? String(u.is_admin) : "false",
    options: [{ value: "false", label: "否" }, { value: "true", label: "是（用户管理/代建密钥/观测）" }] });
  fields.push({ key: "status", label: "状态", type: "select", value: u ? u.status : "active",
    options: [{ value: "active", label: "正常" }, { value: "disabled", label: "禁用" }] });
  formModal(editing ? `编辑用户 · ${u.user_key}` : "新建用户", fields, editing ? "保存" : "创建", async (v) => {
    try {
      if (editing) {
        await api(`/api/users/${userId}`, { method: "PUT", body: JSON.stringify({
          display_name: v.display_name,
          is_admin: v.is_admin === "true",
          status: v.status,
        }) });
        closeModal(); toast("用户已更新", "success");
      } else {
        await api("/api/users", { method: "POST", body: JSON.stringify({
          user_key: v.user_key, display_name: v.display_name,
          kind: v.kind, is_admin: v.is_admin === "true",
        }) });
        closeModal(); toast("用户已创建；请再为其创建 API Key 完成开通", "success");
      }
      await loadUsersTable();
    } catch (e) { showError(e); }
  });
  // user_key 是身份标识，编辑时只读
  if (editing) {
    const keyInput = $('#modal-form [data-key="user_key"]');
    if (keyInput) {
      keyInput.readOnly = true;
      keyInput.style.opacity = "0.6";
      keyInput.title = "user_key 为身份标识，不可修改";
    }
  }
  // 自操作 / 最后 admin 的服务端强制（409）在前端给出提示
  if (editing && (isSelf || lastAdmin)) {
    const adminSel = $('#modal-form [data-key="is_admin"]');
    if (adminSel) adminSel.title = isSelf ? "不能取消自己的管理员身份（服务端强制）" : "最后一个平台管理员受保护（服务端强制）";
    if (isSelf) {
      const stSel = $('#modal-form [data-key="status"]');
      if (stSel) stSel.title = "不能禁用自己（服务端强制）";
    }
  }
}

async function toggleUser(userId, status) {
  const u = state.users.find((x) => x.user_id === userId);
  if (!u) return;
  const action = status === "disabled" ? "禁用" : "启用";
  confirmModal(`${action}用户`,
    `确定要${action}主体 ${u.user_key} 吗？${status === "disabled" ? "禁用后其全部 API Key 即时失效。" : ""}`,
    action, async () => {
      try {
        await api(`/api/users/${userId}`, { method: "PUT", body: JSON.stringify({ status }) });
        closeModal(); toast(`已${action} ${u.user_key}`, "success");
        await loadUsersTable();
      } catch (e) { showError(e); }
    });
}

// ---- 项目成员 ----

async function loadMembersTable() {
  if (!state.projectId) return;
  state.members = await api(`/api/projects/${state.projectId}/members`);
  renderMembersTable();
}

function renderMembersTable() {
  const tbody = $("#members-table tbody");
  if (!tbody) return;
  const owners = state.members.filter((m) => m.role === "owner").length;
  tbody.innerHTML = state.members.map((m) => {
    const lastOwner = m.role === "owner" && owners <= 1;
    return `<tr>
      <td class="mono">${escapeHtml(m.user_key)}</td>
      <td>${escapeHtml(m.display_name)}</td>
      <td>${KIND_LABEL[m.kind] || escapeHtml(m.kind)}</td>
      <td><span class="badge badge-role-${m.role}">${ROLE_LABEL[m.role] || m.role}</span></td>
      <td>${fmtTime(m.created_at)}</td>
      <td class="users-ops">
        <button class="btn btn-ghost btn-sm" data-member-role="${m.user_id}" data-role="${m.role}">改角色</button>
        <button class="btn btn-ghost btn-sm danger" data-member-remove="${m.user_id}" ${lastOwner ? 'disabled title="最后一个所有者受保护，请先移交所有权"' : ""}>移除</button>
      </td>
    </tr>`;
  }).join("");
  $$("#members-table [data-member-role]").forEach((b) =>
    b.addEventListener("click", () => memberRoleModal(Number(b.dataset.memberRole), b.dataset.role)));
  $$("#members-table [data-member-remove]").forEach((b) =>
    b.addEventListener("click", () => removeMember(Number(b.dataset.memberRemove))));
}

function addMemberModal() {
  formModal("添加项目成员", [
    { key: "user_key", label: "user_key", required: true, placeholder: "主体需已存在，如 u:alice" },
    { key: "role", label: "角色", type: "select", value: "viewer",
      options: MEMBER_ROLES.map((r) => ({ value: r, label: ROLE_LABEL[r] })) },
  ], "添加", async (v) => {
    try {
      await api(`/api/projects/${state.projectId}/members`, {
        method: "POST",
        body: JSON.stringify({ user_key: v.user_key, role: v.role }),
      });
      closeModal(); toast("成员已添加", "success");
      await loadMembersTable();
    } catch (e) { showError(e); }
  });
}

function memberRoleModal(userId, currentRole) {
  const m = state.members.find((x) => x.user_id === userId);
  if (!m) return;
  const owners = state.members.filter((x) => x.role === "owner").length;
  const lastOwner = m.role === "owner" && owners <= 1;
  formModal(`成员角色 · ${m.user_key}`, [
    { key: "role", label: "角色", type: "select", value: currentRole,
      options: MEMBER_ROLES.map((r) => ({ value: r, label: ROLE_LABEL[r] })) },
  ], "保存", async (v) => {
    if (v.role === currentRole) { closeModal(); return; }
    try {
      await api(`/api/projects/${state.projectId}/members`, {
        method: "POST",
        body: JSON.stringify({ user_key: m.user_key, role: v.role }),
      });
      closeModal(); toast("角色已更新", "success");
      await loadMembersTable();
    } catch (e) { showError(e); }
  });
  if (lastOwner) {
    const sel = $('#modal-form [data-key="role"]');
    if (sel) {
      Array.from(sel.options).forEach((o) => { if (o.value !== "owner") o.disabled = true; });
      sel.title = "最后一个所有者不能降级（服务端强制 409）";
    }
  }
}

async function removeMember(userId) {
  const m = state.members.find((x) => x.user_id === userId);
  if (!m) return;
  confirmModal("移除成员", `确定将 ${m.user_key} 移出当前项目吗？`, "移除", async () => {
    try {
      await api(`/api/projects/${state.projectId}/members/${userId}`, { method: "DELETE" });
      closeModal(); toast("成员已移除", "success");
      await loadMembersTable();
    } catch (e) { showError(e); }
  });
}

// ---- API Key ----

async function loadKeysTable() {
  const input = $("#keys-filter-user");
  const userKey = input ? input.value.trim() : "";
  if (userKey && !hasPlatformCap("api_key.provision")) {
    toast("仅平台管理员可查询他人密钥", "error");
    return;
  }
  state.keys = await api("/api/auth/api-keys" + (userKey ? "?user_key=" + encodeURIComponent(userKey) : ""));
  renderKeysTable();
}

function renderKeysTable() {
  const tbody = $("#keys-table tbody");
  if (!tbody) return;
  if (!state.keys.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="muted">暂无密钥</td></tr>`;
    return;
  }
  tbody.innerHTML = state.keys.map((k) => `<tr>
    <td>${escapeHtml(k.name)}</td>
    <td class="mono">${escapeHtml(k.key_id)}</td>
    <td><span class="badge ${k.status === "active" ? "badge-published" : "badge-error"}">${k.status === "active" ? "有效" : "已吊销"}</span></td>
    <td>${fmtTime(k.created_at)}</td>
    <td>${k.last_used_at ? fmtTime(k.last_used_at) : "从未使用"}</td>
    <td>${k.expires_at ? fmtTime(k.expires_at) : "不过期"}</td>
    <td class="users-ops">${k.status === "active"
      ? `<button class="btn btn-ghost btn-sm danger" data-key-revoke="${escapeHtml(k.key_id)}">吊销</button>`
      : ""}</td>
  </tr>`).join("");
  $$("#keys-table [data-key-revoke]").forEach((b) =>
    b.addEventListener("click", () => revokeKey(b.dataset.keyRevoke)));
}

function createKeyModal() {
  const canProvision = hasPlatformCap("api_key.provision");
  const fields = [
    { key: "name", label: "名称", required: true, placeholder: "如 alice-laptop" },
    { key: "expires_in_days", label: "有效期（天）", placeholder: "留空不过期" },
  ];
  if (canProvision) {
    fields.push({ key: "user_key", label: "为他人代建（user_key）", placeholder: "留空为本人创建；管理员可代成员/服务账号创建" });
  }
  formModal("创建 API Key", fields, "创建", async (v) => {
    try {
      const body = { name: v.name };
      if (v.expires_in_days) body.expires_in_days = Number(v.expires_in_days);
      if (canProvision && v.user_key) body.user_key = v.user_key;
      const r = await api("/api/auth/api-keys", { method: "POST", body: JSON.stringify(body) });
      closeModal();
      showSecretModal(r);
      await loadKeysTable();
    } catch (e) { showError(e); }
  });
}

function showSecretModal(r) {
  const token = r.secret;
  openModal("密钥已创建（secret 仅此一次展示）", `
    <p>请立即复制并妥善保存；关闭此窗口后无法再次查看。</p>
    <div class="secret-box"><code id="secret-text">${escapeHtml(token)}</code>
      <button class="btn btn-sm" id="secret-copy">复制</button></div>
    <p class="muted mt14">归属主体：<b>${escapeHtml(r.user_key)}</b> · 有效期：${r.expires_at ? fmtTime(r.expires_at) : "不过期"}</p>`,
    [{ label: "关闭", cls: "btn-primary", onClick: closeModal }]);
  $("#secret-copy").onclick = async () => {
    try {
      await navigator.clipboard.writeText(token);
      toast("已复制到剪贴板", "success");
    } catch (e) {
      const el = $("#secret-text");
      const range = document.createRange();
      range.selectNodeContents(el);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      toast("请按 Ctrl/Cmd+C 手动复制", "error");
    }
  };
}

async function revokeKey(keyId) {
  confirmModal("吊销密钥", `确定吊销密钥 ${keyId} 吗？吊销后旧令牌即时失效（401）。`, "吊销", async () => {
    try {
      await api(`/api/auth/api-keys/${encodeURIComponent(keyId)}/revoke`, { method: "POST" });
      closeModal(); toast("密钥已吊销", "success");
      await loadKeysTable();
    } catch (e) { showError(e); }
  });
}

// ---------------------------------------------------------------- 初始化

async function init() {
  $$(".nav-item").forEach((b) => b.addEventListener("click", () => {
    // 任何导航点击都退出编辑器回到对应视图的列表（编辑器仅经模型链接进入）
    state.editor = null;
    setView(b.dataset.view);
  }));
  $("#project-select").addEventListener("change", async (e) => {
    state.projectId = Number(e.target.value);
    state.editor = null;
    await loadProjectContext();
    renderIdentityChip(); // V1.2：项目角色徽标随当前项目切换
    await renderView();
  });
  $("#btn-new-project").addEventListener("click", newProjectModal);

  try {
    await loadProjects();
    await loadProjectContext();
    await loadMe();
    setView("dashboard");
  } catch (e) { showError(e); }
}

document.addEventListener("DOMContentLoaded", init);
