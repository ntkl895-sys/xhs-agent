// 度量专项 · 合成截图夹具渲染器（node + playwright-core + 本机 Chrome）
// 用法:
//   node render_fixture.cjs <htmlPath> <outPngPath> [width] [height]
// 说明:
//   - 仅用于 benchmark 合成夹具的本地渲染；不访问任何网络（file:// 加载本地 HTML）；
//   - 需要环境变量 NODE_PATH 指向含 playwright-core 的 node_modules
//     （bench_vision_synth.py 会自动设置）。
"use strict";

const fs = require("fs");
const { pathToFileURL } = require("url");
const { chromium } = require("playwright-core");

const CHROME_CANDIDATES = [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
];

function findChrome() {
  for (const candidate of CHROME_CANDIDATES) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return null;
}

(async () => {
  const [htmlPath, outPath, widthArg, heightArg] = process.argv.slice(2);
  if (!htmlPath || !outPath) {
    console.error("usage: node render_fixture.cjs <htmlPath> <outPngPath> [width] [height]");
    process.exit(2);
  }
  const width = parseInt(widthArg || "920", 10);
  const height = parseInt(heightArg || "640", 10);
  const chrome = findChrome();
  let browser = null;
  try {
    browser = await chromium.launch(chrome ? { executablePath: chrome } : {});
    const page = await browser.newPage({
      viewport: { width, height },
      deviceScaleFactor: 2, // 2x 缩放，近似真实截图清晰度
    });
    await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "load" });
    await page.waitForTimeout(120); // 等字体/布局稳定
    await page.screenshot({ path: outPath, fullPage: false });
    console.log("RENDER_OK " + outPath);
    process.exit(0);
  } catch (err) {
    console.error("RENDER_FAIL " + (err && err.message ? err.message : String(err)));
    process.exit(1);
  } finally {
    if (browser) {
      try {
        await browser.close();
      } catch (e) {
        /* 忽略关闭异常 */
      }
    }
  }
})();
