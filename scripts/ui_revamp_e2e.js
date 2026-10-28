/** UI 改版验证（任务 #55/#56/#57）：工作台图表 + 需求筛选 + 布局质感 */
const { chromium } = require("playwright");
const fs = require("fs");
const BASE = "http://127.0.0.1:8000";
const { launchBrowser } = require("./e2e_launch.js");
const SHOT_DIR = process.env.E2E_SHOT_DIR || ".playwright-cli-e2e";

let pass = 0, fail = 0;
const failures = [];
function check(name, ok, detail) {
  console.log(`${ok ? "PASS" : "FAIL"} | ${name}${detail ? " | " + detail : ""}`);
  if (ok) pass++; else { fail++; failures.push(name + (detail ? " | " + detail : "")); }
}

(async () => {
  if (!fs.existsSync(SHOT_DIR)) fs.mkdirSync(SHOT_DIR, { recursive: true });
  const browser = await launchBrowser(chromium);
  const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 200)); });
  page.on("pageerror", (e) => consoleErrors.push("PAGEERROR: " + String(e.message).slice(0, 200)));

  try {
    await page.goto(BASE, { waitUntil: "networkidle" });
    await page.waitForSelector("text=最近模型", { timeout: 20000 });

    // ---------- #55 工作台图表 ----------
    const areaBars = await page.evaluate(() => document.querySelectorAll("#chart-area-bar rect.chart-bar-anim").length);
    check("C1. 业务域柱状图渲染（8 根柱）", areaBars === 8, `bars=${areaBars}`);
    const donutSegs = await page.evaluate(() => document.querySelectorAll("#chart-layer-donut circle.chart-donut-seg").length);
    check("C2. 分层环形图渲染（5 段）", donutSegs === 5, `segs=${donutSegs}`);
    const stackSegs = await page.evaluate(() => document.querySelectorAll("#chart-status-stack rect.chart-bar-anim").length);
    check("C3. 状态堆叠图渲染（4 段）", stackSegs === 4, `segs=${stackSegs}`);
    const donutCenter = await page.evaluate(() => {
      const t = document.querySelector("#chart-layer-donut .chart-donut-num");
      return t ? t.textContent : "";
    });
    check("C4. 环形图中心总数=114", donutCenter === "114", `center=${donutCenter}`);
    const barVals = await page.evaluate(() =>
      Array.from(document.querySelectorAll("#chart-area-bar .chart-value")).map((t) => t.textContent).join(","));
    check("C5. 柱状图数值按降序", barVals === "21,21,20,15,13,9,8,7", `vals=${barVals}`);
    await page.waitForTimeout(900);
    await page.screenshot({ path: `${SHOT_DIR}/ui-01-dashboard-charts.png` });

    // ---------- #56 布局 ----------
    const layout = await page.evaluate(() => {
      const content = document.getElementById("content");
      const cs = getComputedStyle(content);
      const card = document.querySelector(".card");
      const cardCs = getComputedStyle(card);
      return {
        contentMaxW: cs.maxWidth, contentPad: cs.padding,
        cardRadius: cardCs.borderRadius, cardShadow: cardCs.boxShadow !== "none",
        viewMeta: document.getElementById("view-meta").textContent,
        statCardBg: getComputedStyle(document.querySelector(".stat-card")).backgroundImage !== "none",
      };
    });
    check("L1. 内容区限宽居中+呼吸感", layout.contentMaxW === "1560px" && layout.contentPad.startsWith("28px 32px"), JSON.stringify(layout));
    check("L2. 卡片圆角+阴影", layout.cardRadius === "14px" && layout.cardShadow, `radius=${layout.cardRadius}`);
    check("L3. 顶栏项目元数据", layout.viewMeta.includes("PRJ_ECOM"), layout.viewMeta);
    check("L4. 统计卡渐变底色", layout.statCardBg);

    // ---------- #57 需求筛选 ----------
    await page.click(".nav-item[data-view='requirements']");
    await page.waitForSelector("#req-toolbar", { timeout: 20000 });
    const totalRows = await page.evaluate(() => document.querySelectorAll("#req-table tbody tr").length);
    check("R1. 需求表全量渲染", totalRows === 26, `rows=${totalRows}`);
    const countText0 = await page.evaluate(() => document.getElementById("req-filter-count").textContent);
    check("R2. 计数初始态", countText0 === "共 26 条", countText0);

    // 关键词筛选（标题/编码）
    await page.fill("#req-search", "订单");
    await page.waitForTimeout(200);
    const kwRows = await page.evaluate(() => document.querySelectorAll("#req-table tbody tr").length);
    check("R3. 关键词筛选生效", kwRows > 0 && kwRows < 26, `rows=${kwRows}`);
    const countText1 = await page.evaluate(() => document.getElementById("req-filter-count").textContent);
    check("R4. 筛选计数提示", countText1.startsWith("筛选出"), countText1);

    // 组合筛选：关键词 + 状态
    await page.selectOption("#req-filter-status", "draft");
    await page.waitForTimeout(200);
    const comboRows = await page.evaluate(() => document.querySelectorAll("#req-table tbody tr").length);
    check("R5. 组合筛选（关键词+状态）", comboRows >= 0 && comboRows <= kwRows, `rows=${comboRows}`);

    // 无结果空态
    await page.fill("#req-search", "zzz不存在的需求zzz");
    await page.waitForTimeout(200);
    const emptyOk = await page.evaluate(() => {
      const e = document.querySelector("#req-table .empty-title");
      return !!e && e.textContent === "没有匹配的需求";
    });
    check("R6. 无结果空态提示", emptyOk);

    // 清空筛选
    await page.click("#req-filter-reset");
    await page.waitForTimeout(200);
    const resetRows = await page.evaluate(() => document.querySelectorAll("#req-table tbody tr").length);
    check("R7. 清空筛选恢复全量", resetRows === 26, `rows=${resetRows}`);

    // 业务域下拉
    const domOpts = await page.evaluate(() => Array.from(document.querySelectorAll("#req-filter-domain option")).map((o) => o.value));
    check("R8. 业务域候选项来自数据", domOpts.length > 1 && domOpts[0] === "", domOpts.join("|").slice(0, 80));

    await page.screenshot({ path: `${SHOT_DIR}/ui-02-requirements-filter.png` });

    // ---------- 其它视图无回归 ----------
    await page.click(".nav-item[data-view='models']");
    await page.waitForSelector("#model-list tbody tr", { timeout: 20000 });
    check("M1. 模型中心正常", true);
    await page.screenshot({ path: `${SHOT_DIR}/ui-03-models.png` });

    check("Z. 全程控制台零错误", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" ／ "));
  } catch (e) {
    check("执行异常", false, String(e && e.message ? e.message : e).slice(0, 300));
    try { await page.screenshot({ path: `${SHOT_DIR}/ui-99-error.png` }); } catch (_) {}
  } finally {
    await browser.close();
  }
  console.log(`\n===== UI 改版验证：${pass} 通过 / ${fail} 失败 =====`);
  if (failures.length) { console.log("失败项："); failures.forEach((f) => console.log("  - " + f)); process.exitCode = 1; }
})();
