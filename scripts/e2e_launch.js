/** 浏览器 E2E 公共启动 helper：跨平台解析 Chrome / Chromium。
 *
 * 解析顺序：环境变量 CHROME_PATH → 各平台常见安装路径 → 都没有时返回 null，
 * 由 playwright 使用自带的 Chromium（需先执行 npx playwright install chromium）。
 *
 * 用法：
 *   const { chromium } = require("playwright");
 *   const { launchBrowser } = require("./e2e_launch.js");
 *   const browser = await launchBrowser(chromium);
 */
const fs = require("fs");

const CANDIDATES = [
  // Windows
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  process.env.LOCALAPPDATA
    ? `${process.env.LOCALAPPDATA}/Google/Chrome/Application/chrome.exe`
    : null,
  // macOS
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  // Linux
  "/usr/bin/google-chrome",
  "/usr/bin/google-chrome-stable",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
].filter(Boolean);

function resolveChrome() {
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) {
    return process.env.CHROME_PATH;
  }
  return CANDIDATES.find((p) => fs.existsSync(p)) || null;
}

/** 启动 chromium：有系统 Chrome 用之；否则回退 playwright 自带 Chromium。 */
async function launchBrowser(chromium) {
  const executablePath = resolveChrome();
  const options = {
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  };
  if (executablePath) options.executablePath = executablePath;
  return chromium.launch(options);
}

module.exports = { launchBrowser, resolveChrome };
