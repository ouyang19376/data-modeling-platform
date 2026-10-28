/**
 * Neo4j 画布图谱（neovis.js）浏览器 E2E 实测
 * 覆盖：画布渲染 / 数据入 DataSet / hover tooltip / 单击详情 / 双击聚焦 /
 *       节点拖动 / 边点击详情 / 边「聚焦两端」/ 空白取消 / 边标签开关 /
 *       布局切换 / 缩放 / 适应 / 视图切换（current/published_latest/snapshot/lineage 外部血缘）/
 *       跳转交互（关系列表点击跳转聚焦 / 聚焦邻居 / 打开编辑器跨视图跳转 / 返回图谱）/
 *       路径查询高亮聚焦 / 重载 / 控制台零错误
 * 运行：node scripts/neo4j_graph_e2e.js（需后端已在 127.0.0.1:8000 运行）
 *
 * 数据期望值全部从 API 动态拉取（EXP），不硬编码节点/边/快照数量，
 * 适配任意演示数据集（当前库：PRJ_ECOM 电商数仓 114 模型）。
 */
const { chromium } = require("playwright");
const fs = require("fs");

const BASE = "http://127.0.0.1:8000";
const { launchBrowser } = require("./e2e_launch.js");
const SHOT_DIR = process.env.E2E_SHOT_DIR || ".playwright-cli-e2e";

/** 从 API 拉取演示数据期望值（视图规模 / 默认快照 / 路径对） */
async function loadExpectations() {
  const get = async (p) => {
    const r = await fetch(BASE + p);
    if (!r.ok) throw new Error(`GET ${p} -> ${r.status}`);
    return r.json();
  };
  const cur = await get("/api/projects/1/model-graph?view=current");
  const pub = await get("/api/projects/1/model-graph?view=published_latest");
  const lin = await get("/api/projects/1/model-graph?view=lineage");
  const snaps = await get("/api/projects/1/model-graph/snapshots");
  const models = await get("/api/projects/1/models");
  const byKey = {};
  models.forEach((m) => { byKey[m.model_key] = m.id; });
  // 路径对：报表 → 维度（跨 ADS/DWS/DWD/DIM 的下游链路）
  const pathSrc = byKey["ads_gmv_daily"], pathTgt = byKey["dim_category"];
  const pathRes = await get(`/api/projects/1/model-graph/path`
    + `?source_model_id=${pathSrc}&target_model_id=${pathTgt}&direction=downstream`);
  return {
    current: { nodes: cur.nodes.length, edges: cur.edges.length },
    publishedLatest: { nodes: pub.nodes.length, edges: pub.edges.length },
    lineage: {
      nodes: lin.nodes.length,
      edges: lin.edges.length + (lin.external_edges || []).length,
      ext: (lin.external_edges || []).length,
    },
    snapshot: {
      total: snaps.length,
      defaultNo: snaps[0].snapshot_no,
      nodeCount: snaps[0].node_count,
      edgeCount: snaps[0].edge_count,
    },
    path: {
      src: pathSrc, tgt: pathTgt,
      found: pathRes.found, distance: pathRes.distance,
    },
  };
}

let pass = 0, fail = 0;
const failures = [];
function check(name, ok, detail) {
  const line = `${ok ? "PASS" : "FAIL"} | ${name}${detail !== undefined && detail !== null && detail !== "" ? " | " + detail : ""}`;
  console.log(line);
  if (ok) pass++; else { fail++; failures.push(name + (detail ? " | " + detail : "")); }
}

/** 等待图谱新实例布局完成（loading 隐藏 + network 就绪） */
async function waitGraphReady(page, expectNodes) {
  await page.waitForSelector("#neo-canvas canvas", { timeout: 20000 });
  await page.waitForFunction(
    () => {
      const el = document.getElementById("neo-loading");
      const st = window.state;
      return !!(el && el.hidden && st && st.graphViz && st.graphViz.network);
    },
    null,
    { timeout: 40000 }
  );
  if (expectNodes != null) {
    await page.waitForFunction(
      (n) => window.state.graphViz.network.body.data.nodes.length === n,
      expectNodes,
      { timeout: 20000 }
    );
  }
  // 物理布局首帧后节点才有 x/y（completed 事件只代表 Network 已创建）
  await page.waitForFunction(
    () => {
      const net = window.state.graphViz && window.state.graphViz.network;
      if (!net) return false;
      const ids = net.body.data.nodes.getIds();
      if (!ids.length) return false;
      const pos = net.getPositions(ids);
      return ids.every((id) => pos[id] && Number.isFinite(pos[id].x) && Number.isFinite(pos[id].y));
    },
    null,
    { timeout: 20000 }
  );
  await page.waitForTimeout(600); // 等 stabilization.fit 落定
}

/** 等待图谱视图落定：fit 动画结束 + 物理模拟停止。
 *  判定：scale 与任一节点位置在连续两次采样（间隔 150ms）内不变。
 *  必要性：fit 动画（350ms）在 headless 下实测 +800ms 仍未落定，
 *  物理模拟重启后节点也在漂移；视图移动中固定 DOM 点的命中测试会
 *  从「空白」翻转为「命中节点/边」，导致后续点击语义不确定。 */
async function settleGraphView(page) {
  await page.evaluate(() => { window.__viewProbe = null; });
  await page.waitForFunction(() => {
    const net = window.state.graphViz && window.state.graphViz.network;
    if (!net) return false;
    const ids = net.body.data.nodes.getIds();
    if (!ids.length) return true;
    const p = net.getPositions([ids[0]])[ids[0]];
    const now = { s: net.getScale(), x: p ? p.x : 0, y: p ? p.y : 0 };
    const prev = window.__viewProbe;
    window.__viewProbe = now;
    if (!prev) return false;
    return Math.abs(prev.s - now.s) < 1e-4
      && Math.abs(prev.x - now.x) < 0.01 && Math.abs(prev.y - now.y) < 0.01;
  }, null, { timeout: 15000, polling: 150 });
}

/** 取一个不在详情卡（右上 272px）下方、且尽量靠画布中心的节点 id 与页面坐标
 *  onlyWithRel=true 时只选至少关联一条内部边的节点（详情卡才有关系列表可跳转） */
async function pickNode(page, onlyWithRel) {
  return page.evaluate((onlyWithRel) => {
    const st = window.state;
    const net = st.graphViz.network;
    let relIds = null;
    if (onlyWithRel) {
      relIds = new Set();
      (st.graphEdges || []).forEach((e) => { relIds.add(e.source_model_id); relIds.add(e.target_model_id); });
    }
    const detail = document.getElementById("graph-detail");
    const dr = detail && !detail.hidden ? detail.getBoundingClientRect() : null;
    const r = net.canvas.frame.canvas.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    let best = null, bestScore = -Infinity;
    net.body.data.nodes.forEach((item) => {
      if (relIds && !relIds.has(item.id)) return;
      const p = net.getPositions([item.id])[item.id];
      const d = net.canvasToDOM(p);
      const px = r.left + d.x, py = r.top + d.y;
      if (dr && px > dr.left - 30 && px < dr.right + 30 && py > dr.top - 30 && py < dr.bottom + 30) return;
      const score = -Math.hypot(px - cx, py - cy); // 越靠中心越好
      if (score > bestScore) {
        bestScore = score;
        best = { id: item.id, x: px, y: py };
      }
    });
    return best;
  }, onlyWithRel);
}

/** 计算给定 vis 节点中有多少个的屏幕位置落在画布可视区内（语义：跳转/聚焦后目标可见） */
async function countVisibleNodes(page, ids) {
  return page.evaluate((ids) => {
    const net = window.state.graphViz.network;
    const r = net.canvas.frame.canvas.getBoundingClientRect();
    let visible = 0;
    ids.forEach((id) => {
      const p = net.getPositions([id])[id];
      if (!p) return;
      const d = net.canvasToDOM(p);
      if (d.x >= 0 && d.x <= r.width && d.y >= 0 && d.y <= r.height) visible++;
    });
    return { visible, total: ids.length };
  }, ids);
}

(async () => {
  if (!fs.existsSync(SHOT_DIR)) fs.mkdirSync(SHOT_DIR, { recursive: true });
  const browser = await launchBrowser(chromium);
  const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 240)); });
  page.on("pageerror", (e) => consoleErrors.push("PAGEERROR: " + String(e.message).slice(0, 240)));

  try {
    const EXP = await loadExpectations();
    console.log(`[期望值] current ${EXP.current.nodes}节点/${EXP.current.edges}边 · `
      + `published_latest ${EXP.publishedLatest.nodes}/${EXP.publishedLatest.edges} · `
      + `lineage ${EXP.lineage.nodes}/${EXP.lineage.edges}(外部${EXP.lineage.ext}) · `
      + `快照 ${EXP.snapshot.total} 个(默认#${EXP.snapshot.defaultNo}) · `
      + `路径 ${EXP.path.src}→${EXP.path.tgt} 距离${EXP.path.distance}`);

    // ---------- 1. 打开工作台 ----------
    await page.goto(BASE, { waitUntil: "networkidle" });
    await page.waitForSelector("text=最近模型", { timeout: 20000 });
    check("1. 工作台打开", true);

    // ---------- 2. 进入模型关系图 ----------
    await page.click("button[data-view='graph']");
    await page.waitForSelector("#neo-canvas canvas", { timeout: 20000 });
    check("2. 画布容器渲染（canvas 存在）", true);

    // ---------- 3. 布局计算完成 ----------
    await waitGraphReady(page, EXP.current.nodes);
    check("3. 布局计算完成（loading 隐藏）", true);

    // ---------- 4-6. neovis 实例 + 数据入 DataSet ----------
    const counts = await page.evaluate(() => {
      const st = window.state;
      const net = st.graphViz && st.graphViz.network;
      if (!net) return { hasViz: false, nodes: -1, edges: -1, ext: -1 };
      const ext = net.body.data.edges.get().filter((e) => (e.raw && e.raw.properties && e.raw.properties.external) === true).length;
      return {
        hasViz: true,
        nodes: net.body.data.nodes.length,
        edges: net.body.data.edges.length,
        ext,
        layout: st.graphLayoutMode,
      };
    });
    check("4. neovis 实例创建（Neo4j 组件）", counts.hasViz === true);
    check(`5. ${EXP.current.nodes} 个模型节点入画布`, counts.nodes === EXP.current.nodes, `实际 ${counts.nodes}`);
    check(`6. ${EXP.current.edges} 条关系边入画布`, counts.edges === EXP.current.edges, `实际 ${counts.edges}`);
    await page.screenshot({ path: `${SHOT_DIR}/neo-01-canvas.png` });

    // ---------- 7. hover 节点 → tooltip ----------
    const node = await pickNode(page);
    check("7a. 选取可交互节点", !!node, node ? `vis id=${node.id}` : "无候选");
    await page.mouse.move(node.x, node.y);
    await page.waitForTimeout(500);
    const tip = await page.evaluate(() => {
      const t = document.getElementById("neo-tip");
      return { visible: !!(t && !t.hidden), text: t && t.textContent ? t.textContent.trim() : "" };
    });
    check("7b. hover 节点出 tooltip", tip.visible && tip.text.length > 4, tip.text.slice(0, 40).replace(/\s+/g, " "));
    await page.screenshot({ path: `${SHOT_DIR}/neo-02-hover-tip.png` });

    // ---------- 8. 单击节点 → 详情卡 ----------
    await page.mouse.click(node.x, node.y);
    await page.waitForTimeout(500);
    const detail = await page.evaluate(() => {
      const d = document.getElementById("graph-detail");
      const name = d.querySelector(".graph-detail-name");
      return { visible: !d.hidden, name: name ? name.textContent : "", selected: window.state.graphSelectedNode };
    });
    check("8. 单击节点出详情卡", detail.visible && detail.name.length > 0 && detail.selected === node.id,
      `${detail.name}（选中 ${detail.selected}）`);
    await page.screenshot({ path: `${SHOT_DIR}/neo-03-node-detail.png` });

    // ---------- 9. 双击节点 → 聚焦（fit + 选中态） ----------
    const scaleBefore = await page.evaluate(() => window.state.graphViz.network.getScale());
    await page.mouse.dblclick(node.x, node.y);
    await page.waitForTimeout(1400);
    const focus = await page.evaluate(() => ({
      selected: window.state.graphSelectedNode,
      scale: window.state.graphViz.network.getScale(),
    }));
    check("9. 双击节点聚焦（视图适配）", focus.selected === node.id && Math.abs(focus.scale - scaleBefore) > 0.02,
      `scale ${scaleBefore.toFixed(3)} → ${focus.scale.toFixed(3)}`);
    await page.screenshot({ path: `${SHOT_DIR}/neo-04-dblclick-focus.png` });

    // ---------- 10. 拖动节点 → 位置变化 ----------
    // 两个坑：① 9 聚焦后视图已缩放，node 的屏幕坐标失效，mousedown 会落空；
    // ② vis 拖拽在按下/松手时都会 emit("startSimulation")，物理循环会把
    // 拖拽的节点拉回平衡位（密集图回弹明显，实测净位移仅 31px）。
    // 先关闭物理保证位移可确定测量，测完立即恢复。
    await page.evaluate(() => { window.state.graphViz.network.setOptions({ physics: false }); });
    const dragNode = await pickNode(page);
    check("10a. 重新选取可拖动节点", !!dragNode, dragNode ? `vis id=${dragNode.id}` : "无候选");
    if (dragNode) {
      const posBefore = await page.evaluate((nid) => {
        const p = window.state.graphViz.network.getPositions([nid])[nid];
        return { x: p.x, y: p.y };
      }, dragNode.id);
      await page.mouse.move(dragNode.x, dragNode.y);
      await page.mouse.down();
      await page.mouse.move(dragNode.x + 220, dragNode.y + 130, { steps: 14 });
      await page.mouse.up();
      await page.waitForTimeout(300);
      const posAfter = await page.evaluate((nid) => window.state.graphViz.network.getPositions([nid])[nid], dragNode.id);
      const moved = Math.hypot(posAfter.x - posBefore.x, posAfter.y - posBefore.y);
      check("10. 节点可拖动", moved > 60, `位移 ${moved.toFixed(0)}px`);
    } else {
      check("10. 节点可拖动", false, "无候选节点");
    }
    await page.screenshot({ path: `${SHOT_DIR}/neo-05-drag-node.png` });
    // 恢复物理布局（应用常规状态）
    await page.evaluate(() => { window.state.graphViz.network.setOptions({ physics: true }); });
    await page.waitForTimeout(400);

    // ---------- 11. 点击边 → 关系详情（带重试，容忍物理布局漂移） ----------
    // 密集图（159 边）下两个坑：① getEdgeAt 的命中半径是 10 个「画布单位」，
    // fit 缩放下（scale≈0.03）只有约 0.3 DOM px，直线插值采样必空；
    // ② 曲线边（cubicBezier）偏离直线插值位置。
    // 对策：先把视图放大到 scale≈1.0 并平移到最长边中点，再对可见画布做
    // 网格扫描，找「getEdgeAt 命中且该点无节点」的真实可点击像素。
    let edgeOk = false, edgeInfo = "";
    for (let attempt = 1; attempt <= 3 && !edgeOk; attempt++) {
      const edgePt = await page.evaluate(() => {
        const net = window.state.graphViz.network;
        const detail = document.getElementById("graph-detail");
        const dr = detail && !detail.hidden ? detail.getBoundingClientRect() : null;
        const r = net.canvas.frame.canvas.getBoundingClientRect();
        const isExt = (e) => !!(e.raw && e.raw.properties && e.raw.properties.external);
        // 按边长度降序取前几条作放大锚点（长边中点附近最可能有裸露像素）
        const cands = [];
        net.body.data.edges.get().forEach((e) => {
          if (isExt(e)) return;
          const pa = net.getPositions([e.from])[e.from];
          const pb = net.getPositions([e.to])[e.to];
          if (!pa || !pb) return;
          cands.push({ eid: e.id, d: Math.hypot(pa.x - pb.x, pa.y - pb.y), mx: (pa.x + pb.x) / 2, my: (pa.y + pb.y) / 2 });
        });
        cands.sort((a, b) => b.d - a.d);
        const scales = [1.0, 1.6, 2.4];
        for (let si = 0; si < scales.length; si++) {
          const a = cands[si % Math.max(1, cands.length)];
          if (!a) break;
          net.moveTo({ position: { x: a.mx, y: a.my }, scale: scales[si], animation: false });
          const step = 8; // 命中带宽 10 画布单位 ≈ 10 DOM px，步长 8 保证覆盖
          for (let y = step; y < r.height - step; y += step) {
            for (let x = step; x < r.width - step; x += step) {
              // 避开右上角详情卡覆盖区（点在卡片上则点击落在卡片而非画布）
              if (dr && r.left + x > dr.left - 8 && r.left + x < dr.right + 8
                && r.top + y > dr.top - 8 && r.top + y < dr.bottom + 8) continue;
              const dom = { x, y };
              const eid = net.getEdgeAt(dom);
              if (eid == null || net.getNodeAt(dom) != null) continue;
              const e = net.body.data.edges.get(eid);
              if (!e || isExt(e)) continue;
              return { x: r.left + x, y: r.top + y, edgeId: eid };
            }
          }
        }
        return null;
      });
      if (!edgePt) { edgeInfo = "未找到边上可点击点"; break; }
      await page.mouse.move(edgePt.x, edgePt.y);
      await page.mouse.click(edgePt.x, edgePt.y);
      await page.waitForTimeout(500);
      const ed = await page.evaluate(() => {
        const d = document.getElementById("graph-detail");
        const name = d.querySelector(".graph-detail-name");
        return { visible: !d.hidden, name: name ? name.textContent : "", selected: window.state.graphSelectedEdge };
      });
      if (ed.visible && ed.selected === edgePt.edgeId) {
        edgeOk = true;
        edgeInfo = `${ed.name}（边 ${ed.selected}）`;
        await page.screenshot({ path: `${SHOT_DIR}/neo-06-edge-detail.png` });
      } else {
        edgeInfo = `第 ${attempt} 次点击未命中边（selected=${ed.selected}）`;
      }
    }
    check("11. 点击边出关系详情", edgeOk, edgeInfo);

    // 探测边像素时把视图放大过，先还原全局视图，保证 11b 的「聚焦两端」
    // 有可观测的视图变化（fit 的 maxZoomLevel 默认为 1，若停留在 scale 1.0
    // 的探测视图上，聚焦两端会被钳位回 1.0 而看不出变化）
    await page.click("#graph-fit");
    await page.waitForTimeout(1300); // fit 动画默认 1s + 余量

    // ---------- 11b. 边详情「聚焦两端」→ 视图适配跳转 ----------
    if (edgeOk) {
      const es0 = await page.evaluate(() => window.state.graphViz.network.getScale());
      await page.click("#graph-edge-focus");
      await page.waitForTimeout(800); // fit 动画 350ms + 余量
      const es1 = await page.evaluate(() => window.state.graphViz.network.getScale());
      check("11b. 边详情「聚焦两端」视图适配", Math.abs(es1 - es0) > 0.02,
        `scale ${es0.toFixed(3)} → ${es1.toFixed(3)}`);
      await page.screenshot({ path: `${SHOT_DIR}/neo-06b-edge-focus.png` });
    }

    // ---------- 12. 空白单击 → 取消选择 ----------
    // 时序坑（实测偶发失败）：11b 的 fit 动画与测试 10 恢复的物理模拟都会让
    // 视图/节点在扫描后继续移动，固定 DOM 点的命中测试会从「空白」翻转为
    // 「命中边」——点击边 → selectNeoEdge → 详情卡不关、selected 保持 null，
    // 与「未清除」的失败签名一致（detail=false selected=null）。
    // 对策：先等视图落定（settleGraphView），再螺旋扫描空白点；点击后未清除
    // 则按当前视图重新扫描重试（与测试 11 的多 attempt 模式一致）。
    await settleGraphView(page);
    let cleared = { detail: false, selected: "x" };
    for (let attempt = 1; attempt <= 3 && !(cleared.detail && cleared.selected === null); attempt++) {
      const blank = await page.evaluate(() => {
        const net = window.state.graphViz.network;
        const r = net.canvas.frame.canvas.getBoundingClientRect();
        // 从左下角起螺旋扫描一个空白点（无节点、无边、不在详情卡上）
        for (let ring = 0; ring < 8; ring++) {
          for (let a = 0; a < 12; a++) {
            const px = 20 + ring * 26 + Math.cos((a / 12) * Math.PI * 2) * ring * 20;
            const py = r.height - 20 - ring * 26 + Math.sin((a / 12) * Math.PI * 2) * ring * 20;
            const dom = { x: px, y: py };
            if (net.getNodeAt(dom) == null && net.getEdgeAt(dom) == null) {
              return { x: r.left + px, y: r.top + py };
            }
          }
        }
        return { x: r.left + 24, y: r.top + r.height - 24 };
      });
      await page.mouse.click(blank.x, blank.y);
      await page.waitForTimeout(400);
      cleared = await page.evaluate(() => ({
        detail: document.getElementById("graph-detail").hidden,
        selected: window.state.graphSelectedNode,
      }));
    }
    check("12. 空白单击取消选择", cleared.detail && cleared.selected === null, `detail=${cleared.detail} selected=${cleared.selected}`);

    // ---------- 12b. 详情卡关系列表点击 → 跳转聚焦目标模型（跳转交互①） ----------
    // 先「适应视图」重置：上一轮 11b 聚焦两端后 scale 已被 vis fit 的 1.0 上限钳位，
    // 不重置则 fit 无 observable 变化（fit 上限行为见探针：fit 2 节点=1.0，moveTo 5.0 不受限）
    await page.click("#graph-fit");
    await page.waitForTimeout(900);
    const jumpNode = await pickNode(page, true);
    check("12b-0. 选取带关系的节点", !!jumpNode, jumpNode ? `vis id=${jumpNode.id}` : "无候选");
    if (jumpNode) {
      await page.mouse.click(jumpNode.x, jumpNode.y);
      await page.waitForTimeout(500);
      const relTarget = await page.evaluate(() => {
        const li = document.querySelector("#graph-detail [data-neo-focus-node]");
        return li ? Number(li.dataset.neoFocusNode) : null;
      });
      check("12b-1. 详情卡渲染可点击关系列表", relTarget != null && relTarget !== jumpNode.id,
        `首个关系目标 #${relTarget}`);
      if (relTarget != null) {
        const js0 = await page.evaluate(() => window.state.graphViz.network.getScale());
        await page.click("#graph-detail [data-neo-focus-node]");
        await page.waitForTimeout(900); // focusNeoNode → fitNeoNodes 动画 350ms
        const jumped = await page.evaluate(() => ({
          selected: window.state.graphSelectedNode,
          scale: window.state.graphViz.network.getScale(),
          detailVisible: !document.getElementById("graph-detail").hidden,
          detailName: (document.querySelector("#graph-detail .graph-detail-name") || {}).textContent || "",
        }));
        const tgtVisible = await countVisibleNodes(page, [relTarget]);
        check("12b-2. 点击关系项跳转聚焦目标模型",
          jumped.selected === relTarget && jumped.detailVisible && tgtVisible.visible === 1,
          `选中 #${jumpNode.id} → #${jumped.selected}（${jumped.detailName}），scale ${js0.toFixed(3)} → ${jumped.scale.toFixed(3)}，目标在视区内=${tgtVisible.visible === 1}`);
        await page.screenshot({ path: `${SHOT_DIR}/neo-06c-rel-jump.png` });

        // ---------- 12c. 「聚焦邻居」→ 视图适配到 选中节点+全部邻居 ----------
        // 先缩小两档制造视差，再点聚焦邻居：fit 后 scale 必升、邻居必全部入视区
        await page.click("#graph-zoom-out");
        await page.click("#graph-zoom-out");
        await page.waitForTimeout(500);
        const fs0 = await page.evaluate(() => window.state.graphViz.network.getScale());
        await page.click("#graph-detail-focus");
        await page.waitForTimeout(800);
        const nbr = await page.evaluate(() => {
          const st = window.state;
          const sel = st.graphSelectedNode;
          const ids = new Set([sel]);
          st.graphEdges.forEach((e) => {
            if (e.source_model_id === sel) ids.add(e.target_model_id);
            if (e.target_model_id === sel) ids.add(e.source_model_id);
          });
          return { selected: sel, ids: [...ids] };
        });
        const fs1 = await page.evaluate(() => window.state.graphViz.network.getScale());
        const nbrVisible = await countVisibleNodes(page, nbr.ids);
        check("12c. 「聚焦邻居」视图适配到节点+全部邻居",
          fs1 > fs0 + 0.02 && nbrVisible.visible === nbrVisible.total,
          `scale ${fs0.toFixed(3)} → ${fs1.toFixed(3)}，节点+邻居 ${nbrVisible.visible}/${nbrVisible.total} 在视区内`);
        await page.screenshot({ path: `${SHOT_DIR}/neo-06e-neighbor-focus.png` });

        // ---------- 12d. 「打开编辑器」→ 跨视图跳转（跳转交互②） ----------
        await page.click("#graph-detail-open");
        await page.waitForFunction(
          () => window.state.view === "models" && !!(window.state.editor && window.state.editor.model),
          null,
          { timeout: 15000 }
        );
        const editorJump = await page.evaluate(() => {
          const nav = document.querySelector(".nav-item.active");
          return {
            view: window.state.view,
            modelId: window.state.editor ? window.state.editor.model.id : null,
            modelKey: window.state.editor && window.state.editor.model ? window.state.editor.model.model_key : "",
            viewTitle: document.getElementById("view-title").textContent,
            hasEditorHead: !!document.querySelector(".editor-head"),
            navActive: nav ? nav.dataset.view : null,
          };
        });
        check("12d. 「打开编辑器」跨视图跳转",
          editorJump.view === "models" && editorJump.modelId === relTarget
          && editorJump.viewTitle === "模型编辑器" && editorJump.hasEditorHead,
          `模型 #${editorJump.modelId} ${editorJump.modelKey}，view=${editorJump.view}，标题「${editorJump.viewTitle}」，导航=${editorJump.navActive}`);
        await page.screenshot({ path: `${SHOT_DIR}/neo-06d-editor-jump.png` });

        // ---------- 12e. 从编辑器返回图谱，画布重建后可继续交互 ----------
        await page.click("button[data-view='graph']");
        await waitGraphReady(page, EXP.current.nodes);
        check("12e. 编辑器返回图谱（画布重建）", true, "视图已回到模型关系图");
      }
    }

    // ---------- 13. 边标签开关 ----------
    await page.click("#graph-labels");
    await page.waitForTimeout(500);
    const labelsOn = await page.evaluate(() => {
      const st = window.state;
      const edges = st.graphViz.network.body.data.edges.get();
      const withLabel = edges.filter((e) => e.label !== undefined && e.label !== null && e.label !== "").length;
      return { flag: st.graphShowAllLabels, withLabel, total: edges.length };
    });
    check("13. 边标签开关（全显）", labelsOn.flag === true && labelsOn.withLabel === labelsOn.total,
      `${labelsOn.withLabel}/${labelsOn.total} 条边有标签`);
    await page.screenshot({ path: `${SHOT_DIR}/neo-07-edge-labels.png` });
    await page.click("#graph-labels"); // 关回去
    await page.waitForTimeout(300);

    // ---------- 14. 分层布局切换 ----------
    await page.click("#graph-layout");
    await page.waitForTimeout(6000); // hierarchicalRepulsion stabilization（节点多时更久）
    const layered = await page.evaluate(() => {
      const st = window.state;
      const net = st.graphViz.network;
      const ids = net.body.data.nodes.getIds();
      const pos = net.getPositions(ids);
      // LR 分层：节点按 level 分列，x 取值应聚集为少数几列（远少于力导向的离散值）
      const cols = new Set(ids.map((id) => Math.round(pos[id].x / 60)));
      return { mode: st.graphLayoutMode, btn: document.getElementById("graph-layout").textContent, cols: cols.size, n: ids.length };
    });
    check("14. 分层布局切换（LR 分列）",
      layered.mode === "layered" && layered.cols >= 2 && layered.cols <= 20 && layered.cols < layered.n,
      `mode=${layered.mode} btn=${layered.btn} 分列数=${layered.cols}/${layered.n}`);
    await page.screenshot({ path: `${SHOT_DIR}/neo-08-layered.png` });

    // ---------- 15. 切回力导向 ----------
    await page.click("#graph-layout");
    await page.waitForTimeout(5000);
    const backForce = await page.evaluate(() => window.state.graphLayoutMode);
    check("15. 切回力导向布局", backForce === "force", `mode=${backForce}`);
    await page.screenshot({ path: `${SHOT_DIR}/neo-09-force.png` });

    // ---------- 16. 缩放 + / - / 100% 标签 ----------
    // moveTo 动画默认时长 1s：每次点击后必须等动画落定再读 scale，
    // 否则连点时第二次读到的是动画中间值，缩放量互相抵消
    const zRead = () => page.evaluate(() => ({
      label: document.getElementById("graph-zoom-label").textContent,
      scale: window.state.graphViz.network.getScale(),
    }));
    const z0 = await zRead();
    await page.click("#graph-zoom-in");
    await page.waitForTimeout(1200);
    const z1 = await zRead();
    await page.click("#graph-zoom-out");
    await page.waitForTimeout(1200);
    await page.click("#graph-zoom-out");
    await page.waitForTimeout(1200);
    const z2 = await zRead();
    check("16. 缩放按钮 + 标签联动",
      z0.label !== z1.label && z1.label !== z2.label && Math.abs(z2.scale - z0.scale) > 0.001,
      `${z0.label}(${z0.scale.toFixed(3)}) → ${z1.label}(${z1.scale.toFixed(3)}) → ${z2.label}(${z2.scale.toFixed(3)})`);

    // ---------- 17. 缩放放大 + 适应视图 ----------
    const s0 = await page.evaluate(() => window.state.graphViz.network.getScale());
    for (let i = 0; i < 5; i++) {
      await page.click("#graph-zoom-in");
      await page.waitForTimeout(1150); // 等上一次动画落定，避免缩放量互相抵消
    }
    const s1 = await page.evaluate(() => window.state.graphViz.network.getScale());
    await page.click("#graph-fit");
    await page.waitForTimeout(1300); // fit 动画默认 1s + 余量
    const s2 = await page.evaluate(() => window.state.graphViz.network.getScale());
    check("17. 缩放放大 + 适应视图", s1 > s0 * 2 && s2 < s1 * 0.8,
      `${s0.toFixed(3)} → 放大×5 ${s1.toFixed(3)} → 适应 ${s2.toFixed(3)}`);

    // ---------- 18. 视图切换：published_latest ----------
    await page.click("[data-graph-view='published_latest']");
    await waitGraphReady(page, EXP.publishedLatest.nodes);
    const pub = await page.evaluate(() => {
      const net = window.state.graphViz.network;
      return { nodes: net.body.data.nodes.length, edges: net.body.data.edges.length };
    });
    check("18. 视图切换 published_latest",
      pub.nodes === EXP.publishedLatest.nodes && pub.edges === EXP.publishedLatest.edges,
      `节点 ${pub.nodes} / 边 ${pub.edges}`);

    // ---------- 19. 视图切换：snapshot（默认最新快照） ----------
    await page.click("[data-graph-view='snapshot']");
    await page.waitForSelector("#graph-snapshot-select", { timeout: 10000 });
    await waitGraphReady(page, EXP.snapshot.nodeCount);
    const snap = await page.evaluate(() => {
      const net = window.state.graphViz.network;
      const sel = document.getElementById("graph-snapshot-select");
      return { nodes: net.body.data.nodes.length, edges: net.body.data.edges.length, snapNo: sel ? sel.value : null };
    });
    check(`19. 视图切换 snapshot（默认 #${EXP.snapshot.defaultNo}）`,
      snap.nodes === EXP.snapshot.nodeCount && snap.edges === EXP.snapshot.edgeCount
      && snap.snapNo === String(EXP.snapshot.defaultNo),
      `快照 #${snap.snapNo}，节点 ${snap.nodes} / 边 ${snap.edges}`);

    // ---------- 20. 视图切换：lineage（设计边 + 外部血缘边） ----------
    await page.click("[data-graph-view='lineage']");
    await waitGraphReady(page, EXP.lineage.nodes);
    const lin = await page.evaluate(() => {
      const st = window.state;
      const net = st.graphViz.network;
      const ext = net.body.data.edges.get().filter((e) => (e.raw && e.raw.properties && e.raw.properties.external) === true);
      return {
        nodes: net.body.data.nodes.length,
        edges: net.body.data.edges.length,
        ext: ext.length,
        extDashed: ext.every((e) => e.dashes === true),
      };
    });
    check("20. lineage 外部血缘边（紫色虚线）",
      lin.nodes === EXP.lineage.nodes && lin.edges === EXP.lineage.edges
      && lin.ext === EXP.lineage.ext && lin.extDashed,
      `节点 ${lin.nodes} / 边 ${lin.edges} / 外部 ${lin.ext} 条，dashes=${lin.extDashed}`);
    await page.screenshot({ path: `${SHOT_DIR}/neo-10-lineage.png` });

    // ---------- 21. 路径查询：报表 → 维度 downstream（动态取路径对） ----------
    await page.click("[data-graph-view='current']");
    await waitGraphReady(page, EXP.current.nodes);
    await page.selectOption("#path-source", String(EXP.path.src));
    await page.selectOption("#path-target", String(EXP.path.tgt));
    await page.selectOption("#path-direction", "downstream");
    await page.click("#btn-query-path");
    await page.waitForFunction(
      () => {
        const el = document.getElementById("path-result");
        return !!(el && (el.textContent.includes("找到路径") || el.textContent.includes("未找到")));
      },
      null,
      { timeout: 20000 }
    );
    await page.waitForTimeout(900);
    const path = await page.evaluate(() => {
      const st = window.state;
      const res = document.getElementById("path-result");
      const net = st.graphViz && st.graphViz.network;
      let dimCount = 0, total = 0;
      if (net) {
        net.body.data.nodes.forEach((n) => {
          total++;
          const c = n.color && n.color.background;
          if (c === "rgba(20,27,47,0.35)") dimCount++;
        });
      }
      return {
        found: res.textContent.includes("找到路径"),
        distance: (res.textContent.match(/距离 (\d+)/) || [])[1] || null,
        focusNodes: st.graphPathFocus ? st.graphPathFocus.nodes.size : 0,
        focusEdges: st.graphPathFocus ? st.graphPathFocus.edges.size : 0,
        dimCount, total,
      };
    });
    check("21. 路径查询高亮 + 聚焦",
      path.found && path.distance === String(EXP.path.distance)
      && path.focusNodes === EXP.path.distance + 1 && path.focusEdges === EXP.path.distance,
      `距离=${path.distance} 聚焦节点=${path.focusNodes} 聚焦边=${path.focusEdges} 淡化节点=${path.dimCount}/${path.total}`);
    await page.screenshot({ path: `${SHOT_DIR}/neo-11-path-focus.png` });

    // ---------- 21b. 路径结果分页（默认每页 10 条，筛选栏可调） ----------
    const page1 = await page.evaluate(() => {
      const rows = document.querySelectorAll("#path-table tbody tr");
      return {
        rowCount: rows.length,
        info: (document.getElementById("path-page-info") || {}).textContent || "",
        label: (document.getElementById("path-page-label") || {}).textContent || "",
        sizeDefault: (document.getElementById("path-page-size") || {}).value || "",
        prevDisabled: document.getElementById("path-prev").disabled,
        nextDisabled: document.getElementById("path-next").disabled,
        firstNode: rows[0] ? rows[0].textContent : "",
      };
    });
    check("21b-1. 结果分页渲染（默认每页 10）",
      page1.rowCount === EXP.path.distance + 1
      && page1.info === `共 ${EXP.path.distance + 1} 个路径节点`
      && page1.label === `第 1 / 1 页` && page1.sizeDefault === "10",
      `行数=${page1.rowCount} ${page1.info} ${page1.label} 默认每页=${page1.sizeDefault}`);
    check("21b-2. 单页时分页按钮禁用", page1.prevDisabled && page1.nextDisabled);
    // 每页数量调整为「全部」→ 行数不变、页码仍 1
    await page.selectOption("#path-page-size", "0");
    await page.waitForTimeout(200);
    const pageAll = await page.evaluate(() => ({
      rows: document.querySelectorAll("#path-table tbody tr").length,
      label: (document.getElementById("path-page-label") || {}).textContent || "",
    }));
    check("21b-3. 每页数量切「全部」", pageAll.rows === EXP.path.distance + 1 && pageAll.label === "第 1 / 1 页",
      `行数=${pageAll.rows} ${pageAll.label}`);
    // 切回 10 条/页
    await page.selectOption("#path-page-size", "10");
    await page.waitForTimeout(150);

    // ---------- 21c. 布局：路径查询独立横排，快照/血缘两列并列 ----------
    const layout = await page.evaluate(() => {
      const pathCard = document.getElementById("graph-path-card").getBoundingClientRect();
      const snap = document.getElementById("graph-snapshot-card").getBoundingClientRect();
      const lineage = document.getElementById("graph-lineage-card").getBoundingClientRect();
      return {
        pathFullWidth: pathCard.width > snap.width + lineage.width,
        pathAbovePanels: pathCard.bottom <= snap.top + 1,
        panelsSideBySide: Math.abs(snap.top - lineage.top) < 2 && snap.right <= lineage.left + 1,
      };
    });
    check("21c. 路径横排 + 两列并列",
      layout.pathFullWidth && layout.pathAbovePanels && layout.panelsSideBySide,
      JSON.stringify(layout));
    await page.screenshot({ path: `${SHOT_DIR}/neo-11b-path-panels.png` });

    // ---------- 22. 清除路径高亮 ----------
    const hasClear = await page.evaluate(() => !!document.getElementById("btn-clear-path-focus"));
    if (hasClear) {
      await page.click("#btn-clear-path-focus");
      await page.waitForTimeout(500);
      const clearedFocus = await page.evaluate(() => window.state.graphPathFocus === null);
      check("22. 清除路径高亮", clearedFocus);
    } else {
      check("22. 清除路径高亮", false, "未找到清除按钮");
    }

    // ---------- 23. 重载按钮 ----------
    await page.click("#graph-reload");
    await waitGraphReady(page, EXP.current.nodes);
    check("23. 重载图谱数据", true);

    // ---------- 24. 控制台零错误 ----------
    check("24. 控制台零错误", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" ／ "));

    await page.screenshot({ path: `${SHOT_DIR}/neo-12-final.png` });
  } catch (e) {
    check("E2E 执行异常", false, String(e && e.message ? e.message : e).slice(0, 300));
    try {
      const dump = await page.evaluate(() => {
        const st = window.state || {};
        const el = document.getElementById("neo-loading");
        const canvas = document.querySelector("#neo-canvas canvas");
        return {
          view: st.view, graphView: st.graphView,
          loadingHidden: el ? el.hidden : "no-el",
          loadingHtml: el ? el.innerHTML.slice(0, 160) : "",
          hasViz: !!st.graphViz,
          hasNet: !!(st.graphViz && st.graphViz.network),
          nodes: st.graphViz && st.graphViz.network ? st.graphViz.network.body.data.nodes.length : -1,
          edges: st.graphViz && st.graphViz.network ? st.graphViz.network.body.data.edges.length : -1,
          canvas: !!canvas,
          contentHead: (document.getElementById("content").textContent || "").slice(0, 120),
        };
      });
      console.log("[失败现场]", JSON.stringify(dump));
    } catch (_) { /* 忽略 */ }
    try { await page.screenshot({ path: `${SHOT_DIR}/neo-99-error.png` }); } catch (_) { /* 忽略 */ }
  } finally {
    await browser.close();
  }

  console.log(`\n===== 结果：${pass} 通过 / ${fail} 失败 =====`);
  if (failures.length) { console.log("失败项："); failures.forEach((f) => console.log("  - " + f)); process.exitCode = 1; }
})();
