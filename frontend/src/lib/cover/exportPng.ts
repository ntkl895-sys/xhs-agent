"use client";

/**
 * 封面导出封装（自 `D:\项目1\cover_spike\app.js` 的 `captureToPng()` / 导出工具迁移）。
 *
 * 设计约定：
 * - `captureToPng(node, { width, height, backgroundColor })` 是**全站唯一**绑定
 *   截图库（`html-to-image`）的位置；上层（页面 / 自检）不直接 import 截图库，
 *   将来换库只改这一个函数；
 * - `html-to-image@1.11.13` 由 npm 打进 bundle（**不再**用 vendor UMD / CDN），
 *   打包后断网可导出（spike 的 CDN 回退逻辑随之删除）；
 * - `pixelRatio=1` 且显式传 1242×1656：导出像素严格等于画布尺寸，
 *   与预览的 `transform: scale()` 无关。
 */

import { toPng } from "html-to-image";

import { COVER_HEIGHT, COVER_WIDTH } from "./constants";

export interface CaptureOptions {
  width?: number;
  height?: number;
  backgroundColor?: string;
}

/**
 * 统一的 DOM → PNG 封装（上层唯一出口）。
 *
 * @param node 导出目标节点（恒为 1242×1656 的画布节点，不受预览缩放影响）
 * @returns PNG data URL
 */
export async function captureToPng(
  node: HTMLElement,
  options: CaptureOptions = {},
): Promise<string> {
  return toPng(node, {
    pixelRatio: 1,
    width: options.width ?? COVER_WIDTH,
    height: options.height ?? COVER_HEIGHT,
    cacheBust: false,
    backgroundColor: options.backgroundColor,
  });
}

/** 系统中文字体栈（与 CSS 的 font-family 对齐；不打包、不联网加载任何字体） */
const FONT_FAMILIES = [
  '"PingFang SC"',
  '"Microsoft YaHei"',
  '"Source Han Sans SC"',
  '"Noto Sans CJK SC"',
];

/**
 * 导出前等待字体就绪，避免导出 PNG 与预览字体不一致（方框 / 回退）。
 */
export async function waitFontsReady(): Promise<string> {
  const jobs: Promise<unknown>[] = [];
  for (const family of FONT_FAMILIES) {
    for (const weight of [800, 600, 500]) {
      jobs.push(document.fonts.load(`${weight} 100px ${family}`).catch(() => null));
    }
  }
  await Promise.all(jobs);
  await document.fonts.ready;
  return document.fonts.status;
}

/** data URL → 字节数（base64 部分按 3/4 折算） */
export function dataUrlBytes(dataUrl: string): number {
  const index = dataUrl.indexOf(",");
  if (index < 0) return 0;
  const base64 = dataUrl.slice(index + 1);
  return Math.floor((base64.length * 3) / 4);
}

/** 字节数转人类可读 */
export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(2)} MB`;
}

/** 生成文件名时间戳 20261011_193045 */
export function stamp(): string {
  const date = new Date();
  const pad = (value: number) => String(value).padStart(2, "0");
  return (
    `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}` +
    `_${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`
  );
}

/** 触发浏览器下载（data URL） */
export function downloadDataUrl(dataUrl: string, filename: string): void {
  const anchor = document.createElement("a");
  anchor.href = dataUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

/**
 * 生成一张纯色测试图（dataURL）。
 * 目的：让「背景图导出不污染 canvas」这条链路在**没有本地照片**时也能被自检覆盖。
 */
export function makeTestImageDataUrl(): string {
  const canvas = document.createElement("canvas");
  canvas.width = 800;
  canvas.height = 600;
  const context = canvas.getContext("2d");
  if (!context) return "";
  const gradient = context.createLinearGradient(0, 0, 800, 600);
  gradient.addColorStop(0, "#3a8dff");
  gradient.addColorStop(1, "#ff6b4a");
  context.fillStyle = gradient;
  context.fillRect(0, 0, 800, 600);
  context.fillStyle = "#ffffff";
  context.font = "bold 64px sans-serif";
  context.fillText("BG TEST", 220, 320);
  return canvas.toDataURL("image/png");
}

export interface ExportInfo {
  dataUrl: string;
  width: number;
  height: number;
  bytes: number;
  ms: number;
  fontStatus: string;
}

/**
 * 执行一次真实导出并统计像素 / 体积 / 耗时（调用前请确认画布已渲染到目标状态）。
 */
export async function exportNode(
  node: HTMLElement,
  backgroundColor: string,
): Promise<ExportInfo> {
  const started = performance.now();
  const fontStatus = await waitFontsReady();
  const dataUrl = await captureToPng(node, { backgroundColor });
  const image = new Image();
  await new Promise<void>((resolve, reject) => {
    image.onload = () => resolve();
    image.onerror = () => reject(new Error("导出的 PNG 无法载入"));
    image.src = dataUrl;
  });
  return {
    dataUrl,
    width: image.naturalWidth,
    height: image.naturalHeight,
    bytes: dataUrlBytes(dataUrl),
    ms: Math.round(performance.now() - started),
    fontStatus,
  };
}
