/** 全页面走查：所有视图 + 编辑器全部页签，零控制台错误 + 截图（只读，不造数据） */
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
    check("W1. 工作台渲染", true);
    await page.screenshot({ path: `${SHOT_DIR}/walk-01-dashboard.png` });

    // ---------- 业务需求 ----------
    await page.click(".nav-item[data-view='requirements']");
    await page.waitForTimeout(1200);
    const reqOk = await page.evaluate(() => document.getElementById("content").textContent.length > 50);
    check("W2. 业务需求视图", reqOk);
    await page.screenshot({ path: `${SHOT_DIR}/walk-02-requirements.png` });

    // ---------- 模型中心列表 ----------
    await page.click(".nav-item[data-view='models']");
    await page.waitForSelector("#model-list tbody tr", { timeout: 20000 }); // renderModels 先 await loadProjectContext
    const listOk = await page.evaluate(() => document.querySelectorAll("#model-list tbody tr").length > 0);
    check("W3. 模型中心列表", listOk);
    await page.screenshot({ path: `${SHOT_DIR}/walk-03-models.png` });

    // ---------- 打开第一个模型编辑器，逐个页签 ----------
    await page.click("#model-list [data-open-model]");
    await page.waitForSelector(".tab[data-tab='basic']", { timeout: 20000 });
    const inEditor = await page.evaluate(() => !!document.querySelector(".tab[data-tab='basic']"));
    check("W4. 进入模型编辑器", inEditor);
    await page.screenshot({ path: `${SHOT_DIR}/walk-04-editor-basic.png` });

    const tabs = ["fields", "relations", "constraints", "indexes", "impact", "versions", "ddl"];
    for (const t of tabs) {
      await page.click(`.tab[data-tab='${t}']`);
      await page.waitForTimeout(1200);
      const tabOk = await page.evaluate(() => document.getElementById("editor-body").textContent.length > 30);
      check(`W5. 编辑器页签 ${t}`, tabOk);
      await page.screenshot({ path: `${SHOT_DIR}/walk-05-editor-${t}.png` });
    }

    // ---------- 模型关系图（Neo4j 画布） ----------
    await page.click(".nav-item[data-view='graph']");
    await page.waitForFunction(() => {
      const st = window.state;
      return !!(st.graphViz && st.graphViz.network && st.graphViz.network.body.data.nodes.length > 0);
    }, null, { timeout: 40000 });
    await page.waitForTimeout(1200);
    check("W6. 模型关系图画布", true);
    await page.screenshot({ path: `${SHOT_DIR}/walk-06-graph.png` });

    // ---------- 运行事件 ----------
    await page.click(".nav-item[data-view='events']");
    await page.waitForTimeout(1500);
    const evOk = await page.evaluate(() => document.getElementById("content").textContent.length > 50);
    check("W7. 运行事件视图", evOk);
    await page.screenshot({ path: `${SHOT_DIR}/walk-07-events.png` });

    check("W8. 全程控制台零错误", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" ／ "));
  } catch (e) {
    check("走查执行异常", false, String(e && e.message ? e.message : e).slice(0, 300));
    try { await page.screenshot({ path: `${SHOT_DIR}/walk-99-error.png` }); } catch (_) {}
  } finally {
    await browser.close();
  }
  console.log(`\n===== 走查结果：${pass} 通过 / ${fail} 失败 =====`);
  if (failures.length) { console.log("失败项："); failures.forEach((f) => console.log("  - " + f)); process.exitCode = 1; }
})();
