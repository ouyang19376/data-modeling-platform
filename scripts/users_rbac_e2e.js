/** 用户与权限 E2E（V1.2 RBAC）：身份芯片 / 能力显隐 / 用户管理 / 成员管理 / API Key */
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

// 每次运行使用独立 user_key（用户无删除端点，避免重复创建 409）
const RUN = String(Date.now()).slice(-6);
const E2E_USER = "u:e2e.temp" + RUN;
const E2E_KEY_NAME = "e2e-temp-key-" + RUN;

(async () => {
  if (!fs.existsSync(SHOT_DIR)) fs.mkdirSync(SHOT_DIR, { recursive: true });
  const browser = await launchBrowser(chromium);
  const page = await browser.newPage({ viewport: { width: 1680, height: 1000 } });
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 200)); });
  page.on("pageerror", (e) => consoleErrors.push("PAGEERROR: " + String(e.message).slice(0, 200)));

  // 等待指定用户行的某列出现目标文本（弹窗关闭与表格刷新之间存在竞态，
  // 不能只等 modal-mask hidden；且必须锁定目标行，避免其他行误满足）
  const waitUserCell = (col, text) => page.waitForFunction(
    ({ userKey, col, text }) => {
      const tr = Array.from(document.querySelectorAll("#users-table tbody tr"))
        .find((t) => t.cells[0].textContent === userKey);
      return !!tr && !!tr.cells[col] && tr.cells[col].textContent.trim() === text;
    }, { userKey: E2E_USER, col, text }, { timeout: 8000 });
  const clickUserRowBtn = (userKey, attr) => page.evaluate(
    ({ userKey, attr }) => {
      const tr = Array.from(document.querySelectorAll("#users-table tbody tr"))
        .find((t) => t.cells[0].textContent === userKey);
      if (!tr) throw new Error("row not found: " + userKey);
      tr.querySelector(`[${attr}]`).click();
    }, { userKey, attr });
  const userRowCell = (userKey, col) => page.evaluate(
    ({ userKey, col }) => {
      const tr = Array.from(document.querySelectorAll("#users-table tbody tr"))
        .find((t) => t.cells[0].textContent === userKey);
      return tr ? tr.cells[col].textContent.trim() : null;
    }, { userKey, col });

  try {
    await page.goto(BASE, { waitUntil: "networkidle" });
    await page.waitForSelector("text=最近模型", { timeout: 20000 });

    // ---------- U1 身份芯片 ----------
    await page.waitForSelector("#identity-chip .identity-name", { timeout: 10000 });
    const chip = await page.evaluate(() => ({
      name: document.querySelector("#identity-chip .identity-name").textContent,
      badge: (document.querySelector("#identity-chip .badge") || {}).textContent,
      mode: document.querySelector("#identity-chip .identity-mode").textContent,
      hasLogout: !!document.getElementById("btn-logout"),
    }));
    check("U1. 顶栏身份芯片（演示管理员/平台管理员/演示模式）",
      chip.name === "演示管理员" && chip.badge === "平台管理员" && chip.mode === "演示模式" && !chip.hasLogout,
      JSON.stringify(chip));

    // ---------- U2 进入用户与权限视图 ----------
    await page.click(".nav-item[data-view='users']");
    await page.waitForSelector("#users-identity-card", { timeout: 10000 });
    const viewTitle = await page.evaluate(() => document.getElementById("view-title").textContent);
    check("U2. 视图标题=用户与权限", viewTitle === "用户与权限", viewTitle);

    const banner = await page.evaluate(() => {
      const b = document.querySelector(".demo-banner");
      return b ? b.textContent : "";
    });
    check("U3. 演示模式横幅提示", banner.includes("演示模式"), banner.slice(0, 40));

    // ---------- U4 能力显隐（demo admin：全部管理卡可见） ----------
    const cards = await page.evaluate(() => ({
      users: !!document.getElementById("users-manage-card"),
      members: !!document.getElementById("users-members-card"),
      keys: !!document.getElementById("users-keys-card"),
      platCaps: Array.from(
        document.querySelectorAll("#users-identity-card .identity-caps")[0].querySelectorAll(".cap-badge")
      ).map((e) => e.textContent).join(","),
      projRole: (document.querySelector("#users-identity-card .badge-role-owner") || {}).textContent,
    }));
    check("U4. 管理卡按权限显隐（admin 全可见）",
      cards.users && cards.members && cards.keys, JSON.stringify(cards));
    check("U5. 平台能力徽标（用户管理/代建密钥/观测指标）",
      cards.platCaps === "用户管理,代建密钥,观测指标", cards.platCaps);
    check("U6. 项目角色=所有者", cards.projRole === "所有者", cards.projRole);

    // ---------- U7 用户列表 ----------
    await page.waitForSelector("#users-table tbody tr", { timeout: 10000 });
    const userRows = await page.evaluate(() =>
      Array.from(document.querySelectorAll("#users-table tbody tr")).map((tr) => tr.cells[0].textContent));
    check("U7. 用户列表含演示管理员", userRows.includes("u:demo.admin"), userRows.join(","));
    const selfDisable = await page.evaluate(() => {
      const tr = Array.from(document.querySelectorAll("#users-table tbody tr"))
        .find((t) => t.cells[0].textContent === "u:demo.admin");
      const btn = tr.querySelector("[data-users-disable]");
      return btn ? { disabled: btn.disabled, title: btn.title } : null;
    });
    check("U8. 自禁用/最后 admin 前端防护（禁用按钮置灰）",
      !!selfDisable && selfDisable.disabled && selfDisable.title.length > 0, JSON.stringify(selfDisable));

    // ---------- U9 新建用户 ----------
    await page.click("#users-new");
    await page.waitForSelector("#modal-form", { timeout: 5000 });
    await page.fill('#modal-form [data-key="user_key"]', E2E_USER);
    await page.fill('#modal-form [data-key="display_name"]', "E2E 临时用户");
    await page.selectOption('#modal-form [data-key="kind"]', "human");
    await page.selectOption('#modal-form [data-key="is_admin"]', "false");
    await page.click('#modal-footer button:has-text("创建")');
    await waitUserCell(0, E2E_USER);
    const newRow = await userRowCell(E2E_USER, 1) + "/" + await userRowCell(E2E_USER, 3) + "/" + await userRowCell(E2E_USER, 4);
    check("U9. 新建用户进入列表（默认非 admin/正常）",
      newRow === "E2E 临时用户/—/正常", newRow);

    // ---------- U10 编辑用户 ----------
    await clickUserRowBtn(E2E_USER, "data-users-edit");
    await page.waitForSelector("#modal-form", { timeout: 5000 });
    const keyReadonly = await page.evaluate(() =>
      document.querySelector('#modal-form [data-key="user_key"]').readOnly);
    await page.fill('#modal-form [data-key="display_name"]', "E2E 改名用户");
    await page.click('#modal-footer button:has-text("保存")');
    await waitUserCell(1, "E2E 改名用户");
    check("U10. 编辑用户（user_key 只读 + 名称更新）", keyReadonly, `readonly=${keyReadonly}`);

    // ---------- U11 筛选 ----------
    await page.fill("#users-filter-q", E2E_USER);
    await page.waitForFunction(
      (uk) => Array.from(document.querySelectorAll("#users-table tbody tr")).length === 1
        && document.querySelector("#users-table tbody tr").cells[0].textContent === uk,
      E2E_USER, { timeout: 8000 });
    check("U11. 关键词筛选", true);
    await page.fill("#users-filter-q", "");
    await page.waitForFunction(
      () => Array.from(document.querySelectorAll("#users-table tbody tr")).length > 1,
      { timeout: 8000 });

    // ---------- U12 禁用用户 ----------
    await clickUserRowBtn(E2E_USER, "data-users-disable");
    await page.waitForSelector('#modal-footer button:has-text("禁用")', { state: "visible", timeout: 5000 });
    await page.click('#modal-footer button:has-text("禁用")');
    await waitUserCell(4, "已禁用");
    check("U12. 禁用用户（状态徽标变更）", true);

    // ---------- U13 重新启用 ----------
    await clickUserRowBtn(E2E_USER, "data-users-enable");
    await page.waitForSelector('#modal-footer button:has-text("启用")', { state: "visible", timeout: 5000 });
    await page.click('#modal-footer button:has-text("启用")');
    await waitUserCell(4, "正常");
    check("U13. 重新启用用户", true);

    // ---------- U14 成员管理（PRJ_ECOM：demo admin 是唯一 owner） ----------
    await page.waitForSelector("#members-table tbody tr", { timeout: 10000 });
    const memberInfo = await page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll("#members-table tbody tr"));
      const admin = rows.find((tr) => tr.cells[0].textContent === "u:demo.admin");
      return {
        total: rows.length,
        adminRole: admin ? admin.cells[3].textContent.trim() : "",
        adminRemoveDisabled: admin ? admin.querySelector("[data-member-remove]").disabled : null,
      };
    });
    check("U14. 成员列表（唯一 owner 的移除受前端保护）",
      memberInfo.adminRole === "所有者" && memberInfo.adminRemoveDisabled === true, JSON.stringify(memberInfo));

    // 添加 e2e 用户为成员
    await page.click("#members-add");
    await page.waitForSelector("#modal-form", { timeout: 5000 });
    await page.fill('#modal-form [data-key="user_key"]', E2E_USER);
    await page.selectOption('#modal-form [data-key="role"]', "viewer");
    await page.click('#modal-footer button:has-text("添加")');
    await page.waitForFunction(
      (uk) => Array.from(document.querySelectorAll("#members-table tbody tr"))
        .some((tr) => tr.cells[0].textContent === uk),
      E2E_USER, { timeout: 8000 });
    check("U15. 添加项目成员", true);

    // 改角色 viewer → editor
    await page.evaluate((uk) => {
      const tr = Array.from(document.querySelectorAll("#members-table tbody tr"))
        .find((t) => t.cells[0].textContent === uk);
      tr.querySelector("[data-member-role]").click();
    }, E2E_USER);
    await page.waitForSelector("#modal-form", { timeout: 5000 });
    await page.selectOption('#modal-form [data-key="role"]', "editor");
    await page.click('#modal-footer button:has-text("保存")');
    const newRole = await page.waitForFunction((uk) => {
      const tr = Array.from(document.querySelectorAll("#members-table tbody tr"))
        .find((t) => t.cells[0].textContent === uk);
      return tr && tr.cells[3].textContent.trim() === "编辑者";
    }, E2E_USER, { timeout: 8000 });
    check("U16. 修改成员角色 viewer→editor", !!newRole);

    // 移除成员
    await page.evaluate((uk) => {
      const tr = Array.from(document.querySelectorAll("#members-table tbody tr"))
        .find((t) => t.cells[0].textContent === uk);
      tr.querySelector("[data-member-remove]").click();
    }, E2E_USER);
    await page.waitForSelector('#modal-footer button:has-text("移除")', { state: "visible", timeout: 5000 });
    await page.click('#modal-footer button:has-text("移除")');
    await page.waitForFunction(
      (uk) => !Array.from(document.querySelectorAll("#members-table tbody tr"))
        .some((tr) => tr.cells[0].textContent === uk),
      E2E_USER, { timeout: 8000 });
    check("U17. 移除项目成员", true);

    // ---------- U18 API Key 创建 / secret 一次性展示 ----------
    await page.click("#keys-create");
    await page.waitForSelector("#modal-form", { timeout: 5000 });
    await page.fill('#modal-form [data-key="name"]', E2E_KEY_NAME);
    await page.click('#modal-footer button:has-text("创建")');
    await page.waitForSelector("#secret-text", { timeout: 5000 });
    const secret = await page.evaluate(() => document.getElementById("secret-text").textContent);
    check("U18. secret 仅创建时展示一次（dmc_ 前缀）", secret.startsWith("dmc_ak_"), secret.slice(0, 18) + "…");
    await page.screenshot({ path: `${SHOT_DIR}/users-01-secret.png` });
    await page.click('#modal-footer button:has-text("关闭")');
    await page.waitForFunction(
      (kn) => Array.from(document.querySelectorAll("#keys-table tbody tr"))
        .some((tr) => tr.cells[0].textContent === kn),
      E2E_KEY_NAME, { timeout: 8000 });
    check("U19. 密钥进入列表", true);

    // ---------- U20 吊销密钥 ----------
    await page.evaluate((kn) => {
      const tr = Array.from(document.querySelectorAll("#keys-table tbody tr"))
        .find((t) => t.cells[0].textContent === kn);
      tr.querySelector("[data-key-revoke]").click();
    }, E2E_KEY_NAME);
    await page.waitForSelector('#modal-footer button:has-text("吊销")', { state: "visible", timeout: 5000 });
    await page.click('#modal-footer button:has-text("吊销")');
    const revokedOk = await page.waitForFunction((kn) => {
      const tr = Array.from(document.querySelectorAll("#keys-table tbody tr"))
        .find((t) => t.cells[0].textContent === kn);
      return tr && tr.cells[2].textContent.trim() === "已吊销";
    }, E2E_KEY_NAME, { timeout: 8000 });
    check("U20. 吊销密钥（徽标变更）", !!revokedOk);

    // ---------- U21 收尾：禁用 e2e 用户，保持演示库整洁 ----------
    await clickUserRowBtn(E2E_USER, "data-users-disable");
    await page.waitForSelector('#modal-footer button:has-text("禁用")', { state: "visible", timeout: 5000 });
    await page.click('#modal-footer button:has-text("禁用")');
    await waitUserCell(4, "已禁用");
    check("U21. 收尾：e2e 用户置为禁用", true);

    // ---------- U22 截图 + 控制台零错误 ----------
    await page.evaluate(() => { document.getElementById("content").scrollTop = 0; });
    await page.screenshot({ path: `${SHOT_DIR}/users-02-manage.png` });
    check("U22. 控制台零错误", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" ; "));

  } catch (e) {
    check("E2E 执行异常", false, String(e.message).slice(0, 300));
  } finally {
    await browser.close();
  }

  console.log(`\n===== users_rbac_e2e: ${pass} passed, ${fail} failed =====`);
  if (failures.length) { console.log("FAILED:"); failures.forEach((f) => console.log("  - " + f)); }
  process.exit(fail ? 1 : 0);
})();
