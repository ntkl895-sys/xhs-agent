/**
 * 封面规格常量（自 `D:\项目1\cover_spike\app.js` 顶部逐项迁移，值保持完全一致）。
 *
 * 迁移原则：这些常量决定导出像素与安全区，**不要在别处重复定义**；
 * 后端 `covers.width/height` 的默认值（1242 / 1656）与这里对齐。
 */

/** 封面宽度（px）；改 1080 即为通用档，仍是 3:4 */
export const COVER_WIDTH = 1242;

/** 封面高度（px）；改 1440 即为通用档，仍是 3:4 */
export const COVER_HEIGHT = 1656;

/** 安全区内边距（px）：文字与关键元素不得越界 */
export const SAFE_PADDING = 96;

/** 底部额外留白（px）：避开小红书信息流 UI 可能遮挡的区域 */
export const SAFE_BOTTOM_EXTRA = 48;

/** 主标题字号档位（px），从大到小自动降档 */
export const TITLE_FONT_STEPS = [150, 132, 116, 100, 88, 76, 64, 56, 48];

/** 主标题行高倍数，用于把「最多 N 行」换算成像素高度 */
export const TITLE_LINE_HEIGHT = 1.25;

/** 主标题最多允许的行数，超出则省略 */
export const TITLE_MAX_LINES = 3;

/** 导出 PNG 的目标大小上限（字节） */
export const MAX_PNG_BYTES = 5 * 1024 * 1024;
