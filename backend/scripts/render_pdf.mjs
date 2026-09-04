// Playwright 原生 PDF 渲染器。
// 用法：node render_pdf.mjs <input.json> <output.pdf>
// input.json: { "html": "<!doctype html>..." }
import { existsSync, readFileSync } from "node:fs";
import { chromium } from "playwright";

const [, , inputPath, outputPath] = process.argv;
if (!inputPath || !outputPath) {
  console.error("用法：node render_pdf.mjs <input.json> <output.pdf>");
  process.exit(2);
}

let browser;
try {
  const payload = JSON.parse(readFileSync(inputPath, "utf8"));
  if (!payload || typeof payload.html !== "string" || !payload.html.trim()) {
    throw new Error("缺少 html");
  }

  const chromeCandidates = [
    process.env.MIAOYU_CHROMIUM_PATH,
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    process.env.LOCALAPPDATA ? `${process.env.LOCALAPPDATA}/Google/Chrome/Application/chrome.exe` : "",
  ].filter(Boolean);
  const executablePath = chromeCandidates.find(path => existsSync(path));
  // 某些 Windows 安全策略会阻止 Playwright headless-shell 的 spawn；完整 Chromium
  // 进程可通过 executablePath 覆盖，未配置时仍使用 Playwright 自带浏览器。
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  // PDF HTML 由前端已转义的报告内容和本地 CSS 组成；关闭 JS，避免提交内容被执行。
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  await page.setContent(payload.html, { waitUntil: "load", timeout: 30000 });
  await page.emulateMedia({ media: "print" });
  await page.evaluate(async () => {
    if (document.fonts?.ready) await document.fonts.ready;
    await Promise.all([...document.images].map(img => img.complete
      ? Promise.resolve()
      : new Promise(resolve => { img.addEventListener("load", resolve, { once: true }); img.addEventListener("error", resolve, { once: true }); })));
  });
  await page.pdf({
    path: outputPath,
    format: "A4",
    printBackground: true,
    preferCSSPageSize: true,
    margin: { top: "12mm", right: "12mm", bottom: "14mm", left: "12mm" },
  });
  await context.close();
} catch (error) {
  console.error(error?.stack || String(error));
  process.exitCode = 1;
} finally {
  await browser?.close();
}
