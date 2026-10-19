"use client";

/**
 * 主标题自适应（自 `D:\项目1\cover_spike\app.js` 的 `fitTitle()` 逐行等价迁移）。
 *
 * 口径要点（spike 实测踩坑，勿改）：
 *
 * 1. 用**真实 DOM 测量**（自然高度 / 行数 / 横向溢出）判定，不靠目测；
 * 2. 判定用 `offsetHeight`（自然高度），**不能**用 `scrollHeight vs clientHeight`
 *    —— CJK 字形会超出 line box 约 5px，那个差值会把所有档位误判为溢出；
 * 3. 在字号档位（150→48）间降档，最多 `TITLE_MAX_LINES` 行且不溢出安全区；
 * 4. 最小档仍放不下时交给 `-webkit-line-clamp` 截断并显示省略号。
 */

import { TITLE_FONT_STEPS, TITLE_LINE_HEIGHT, TITLE_MAX_LINES } from "./constants";

export interface FitResult {
  /** 最终选定的字号（px） */
  fontSize: number;
  /** 是否仍溢出（已交给 line-clamp 省略） */
  overflow: boolean;
  /** 占用的行数 */
  lines: number;
  /** 标题可用高度（px） */
  avail: number;
}

/**
 * 等一帧，确保样式已回流后再测量。
 * 同时用 setTimeout 兜底：某些环境（后台标签页、无头模式）不触发 rAF（spike 实测踩坑）。
 */
export function nextFrame(): Promise<void> {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      resolve();
    };
    requestAnimationFrame(finish);
    setTimeout(finish, 50);
  });
}

/**
 * 在字号档位间降档，保证主标题在安全区内最多 `TITLE_MAX_LINES` 行且不溢出。
 *
 * @param cover `.cover` 根节点（内部需含 `.safe-area` 与 `.cv-title`）
 */
export function fitTitle(cover: HTMLElement | null): FitResult {
  const title = cover?.querySelector<HTMLElement>(".cv-title") ?? null;
  const safe = cover?.querySelector<HTMLElement>(".safe-area") ?? null;
  if (!title || !safe) return { fontSize: 0, overflow: false, lines: 0, avail: 0 };
  const head = title.parentElement;
  if (!head) return { fontSize: 0, overflow: false, lines: 0, avail: 0 };

  // 安全区可用高度 = 安全区高度 - 除「标题所在块」之外其它块的实测高度 - 余量
  const siblings = Array.from(safe.children).filter((child) => child !== head);
  const othersH = siblings.reduce(
    (sum, child) => sum + (child as HTMLElement).offsetHeight,
    0,
  );
  const inHeadH = Array.from(head.children)
    .filter((child) => child !== title)
    .reduce((sum, child) => sum + (child as HTMLElement).offsetHeight, 0);
  const safeH = safe.clientHeight;
  const gap = 24;
  const avail = Math.max(120, safeH - othersH - inHeadH - gap);

  let chosen = TITLE_FONT_STEPS[TITLE_FONT_STEPS.length - 1];
  let overflow = true;
  let naturalH = 0;
  let chosenLines = TITLE_MAX_LINES;

  for (const size of TITLE_FONT_STEPS) {
    // 测量阶段：取消高度限制与 line-clamp，用「自然高度」判定
    title.style.display = "block";
    title.style.webkitLineClamp = "none";
    title.style.fontSize = `${size}px`;
    title.style.maxHeight = "none";

    const lineH = size * TITLE_LINE_HEIGHT;
    const h = title.offsetHeight;
    const lines = Math.max(1, Math.round(h / lineH));
    const maxH = Math.min(avail, Math.round(lineH * TITLE_MAX_LINES));
    const fits =
      h <= maxH + 2 &&
      lines <= TITLE_MAX_LINES &&
      title.scrollWidth <= title.clientWidth + 1;

    if (fits) {
      chosen = size;
      naturalH = h;
      chosenLines = lines;
      overflow = false;
      break;
    }
  }

  // 定档
  title.style.fontSize = `${chosen}px`;
  if (overflow) {
    // 最小档仍超出 → 交给 line-clamp 截断并加省略号
    const finalMax = Math.min(avail, Math.round(chosen * TITLE_LINE_HEIGHT * TITLE_MAX_LINES));
    title.style.maxHeight = `${finalMax}px`;
    title.style.display = "-webkit-box";
    title.style.webkitBoxOrient = "vertical";
    title.style.webkitLineClamp = String(
      Math.max(1, Math.floor(finalMax / (chosen * TITLE_LINE_HEIGHT))),
    );
    chosenLines = Number(title.style.webkitLineClamp);
  } else {
    // 已确认放得下：留一点余量，避免 overflow:hidden 裁掉字形底部
    title.style.maxHeight = `${Math.round(naturalH + 12)}px`;
  }

  return { fontSize: chosen, overflow, lines: chosenLines, avail };
}
