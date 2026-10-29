/** 业务总线矩阵 E2E（V1.3）：登录 → 业务需求视图 → 企业总览热力图 → 域内下钻 → 字段映射明细 */
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

  const waitBus = (fn, timeout = 15000) => page.waitForFunction(fn, { timeout });

  try {
    await page.goto(BASE, { waitUntil: "networkidle" });
    await page.waitForSelector("text=最近模型", { timeout: 20000 });

    // ---------- 登录（admin 账号密码）：身份芯片内的登录按钮 ----------
    await page.waitForSelector("#btn-login", { timeout: 10000 });
    await page.click("#btn-login");
    await page.waitForSelector("#login-user", { timeout: 8000 });
    await page.fill("#login-user", "admin");
    await page.fill("#login-pass", "Admin@123456");
    await page.click("#login-submit");
    await waitBus(() => {
      const chip = document.querySelector("#identity-chip .identity-name");
      return chip && chip.textContent.includes("平台管理员");
    });
    check("U1 管理员登录成功", true);

    // ---------- 进入业务需求视图 ----------
    await page.click('.nav-item[data-view="requirements"]');
    await waitBus(() => document.querySelector("#bus-matrix .bm-heat"), 20000);
    check("U2 总线矩阵卡片渲染", true);

    // ---------- 企业级总览 ----------
    const ov = await page.evaluate(() => {
      const cards = Array.from(document.querySelectorAll("#bus-matrix .stat-card"))
        .map((c) => c.querySelector(".stat-label").textContent.trim() + "=" +
                    c.querySelector(".stat-value").textContent.trim());
      const table = document.querySelector("#bus-matrix table.bm-heat");
      const rows = table ? table.querySelectorAll("tbody tr").length : 0;
      const dimCols = table ? table.querySelectorAll("thead .bm-dim-col").length : 0;
      const sharedRows = Array.from(
        document.querySelectorAll("#bus-matrix .card .data tbody tr"))
        .map((tr) => tr.cells[0].textContent.trim());
      const notes = Array.from(document.querySelectorAll("#bus-matrix .bm-note"))
        .map((n) => n.textContent.trim().slice(0, 60));
      const reuseSvg = !!document.querySelector('#bus-matrix svg[aria-label="维度复用度条形图"]');
      return { cards, rows, dimCols, sharedRows, notes, reuseSvg };
    });
    check("U3 总览统计卡", ov.cards.length === 4, ov.cards.join(" · "));
    check("U4 热力行=主题域数(8)", ov.rows === 8, "rows=" + ov.rows);
    check("U5 热力图列=维度数(14)", ov.dimCols === 14, "cols=" + ov.dimCols);
    check("U6 一致性维度榜含 dim_date/dim_user",
      ov.sharedRows.some((t) => t.includes("dim_date")) &&
      ov.sharedRows.some((t) => t.includes("dim_user")),
      ov.sharedRows.slice(0, 4).join(" / "));
    check("U7 维度复用条形图", ov.reuseSvg);
    check("U8 治理提示渲染", ov.notes.length >= 1, ov.notes[0] || "");
    await page.screenshot({ path: `${SHOT_DIR}/bus_matrix_overview.png`, fullPage: false });

    // ---------- 下钻域内矩阵（点击热力图首行 = TRADE 交易域） ----------
    await page.evaluate(() => {
      document.querySelector('#bus-matrix table.bm-heat tbody tr[data-bm-area]').click();
    });
    await waitBus(() => document.querySelector("#bm-back"), 15000);
    const area = await page.evaluate(() => ({
      back: !!document.querySelector("#bm-back"),
      title: document.querySelector("#bus-matrix").textContent.includes("交易域"),
      procRows: document.querySelectorAll("#bus-matrix table.bm-area-matrix tbody tr").length,
      dimCols: document.querySelectorAll("#bus-matrix table.bm-area-matrix thead .bm-dim-col").length,
      filled: document.querySelectorAll("#bus-matrix [data-bm-cell]").length,
      reuseSvg: !!document.querySelector('#bus-matrix svg[aria-label="维度复用对比条形图"]'),
      detailAutoFilled: !!document.querySelector("#bm-detail .bm-edge"),
    }));
    check("U9 域内矩阵渲染（交易域 10 过程 × 14 维度）",
      area.back && area.title && area.procRows === 10 && area.dimCols === 14,
      `rows=${area.procRows} cols=${area.dimCols}`);
    check("U10 映射单元格存在（9 个）", area.filled === 9, "filled=" + area.filled);
    check("U11 维度复用对比图 + 明细自动填充首单元格",
      area.reuseSvg && area.detailAutoFilled);

    // ---------- 点击单元格 → 字段映射明细 ----------
    await page.evaluate(() => {
      document.querySelector("#bus-matrix [data-bm-cell]").click();
    });
    await waitBus(() => document.querySelector("#bm-detail .bm-edge"), 8000);
    const detail = await page.evaluate(() => {
      const edges = document.querySelectorAll("#bm-detail .bm-edge").length;
      const text = document.querySelector("#bm-detail").textContent;
      return {
        edges,
        hasFields: text.includes("⇒"),
        hasRelBadge: !!document.querySelector("#bm-detail .bm-edge .badge"),
        selected: !!document.querySelector("#bus-matrix .bm-cell-selected"),
      };
    });
    check("U12 字段映射明细（边/字段对/关系徽标/选中态）",
      detail.edges >= 1 && detail.hasFields && detail.hasRelBadge && detail.selected,
      `edges=${detail.edges}`);
    await page.screenshot({ path: `${SHOT_DIR}/bus_matrix_area.png`, fullPage: false });

    // ---------- 返回总览 ----------
    await page.click("#bm-back");
    await waitBus(() => document.querySelector('#bus-matrix svg[aria-label="维度复用度条形图"]'), 15000);
    check("U13 返回企业总览", true);

    // ---------- 主题域卡片下钻入口 ----------
    await page.evaluate(() => {
      const btn = document.querySelector("[data-bm-area].btn");
      if (btn) btn.click();
    });
    await waitBus(() => document.querySelector("#bm-back"), 15000);
    check("U14 主题域卡片「总线矩阵」按钮可下钻", true);
  } catch (e) {
    check("E2E 执行异常", false, String(e.message).slice(0, 300));
  } finally {
    check("U15 无控制台错误", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" | "));
    await browser.close();
  }

  console.log(`\n${pass} passed, ${fail} failed`);
  if (fail > 0) { console.log("FAILURES:\n - " + failures.join("\n - ")); process.exit(1); }
})();
