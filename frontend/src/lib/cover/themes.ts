/**
 * 封面配色主题（自 `D:\项目1\cover_spike\themes.js` 逐项迁移）。
 *
 * 每个主题用一组 CSS 自定义属性描述，套用时把变量写到 `.cover` 元素上
 * （见 `CoverCanvas`）；`bgColor` 是纯色兜底值，导出 PNG 时作为 `backgroundColor`。
 * 本文件与框架无关，只有数据与纯函数。
 */

export interface CoverTheme {
  id: string;
  name: string;
  /** 纯色兜底（导出 PNG 的 backgroundColor） */
  bgColor: string;
  vars: Record<string, string>;
}

/** 5 组配色（珊瑚橙 / 奶白暖调 / 墨绿奶黄 / 黑底荧光 / 深海蓝） */
export const COVER_THEMES: CoverTheme[] = [
  {
    id: "coral",
    name: "珊瑚橙",
    bgColor: "#FF6B4A",
    vars: {
      "--cv-bg": "linear-gradient(160deg, #FF6B4A 0%, #FF9E7D 55%, #FFC4A3 100%)",
      "--cv-fg": "#FFFFFF",
      "--cv-muted": "rgba(255,255,255,0.88)",
      "--cv-accent": "#FFE45C",
      "--cv-hl-fg": "#8A2B00",
      "--cv-tag-bg": "rgba(255,255,255,0.22)",
      "--cv-tag-fg": "#FFFFFF",
      "--cv-tag-border": "rgba(255,255,255,0.5)",
      "--cv-divider": "rgba(255,255,255,0.35)",
    },
  },
  {
    id: "cream",
    name: "奶白暖调",
    bgColor: "#FFF8F0",
    vars: {
      "--cv-bg": "linear-gradient(170deg, #FFFDF9 0%, #FFF1E2 100%)",
      "--cv-fg": "#23201C",
      "--cv-muted": "#6B6157",
      "--cv-accent": "#FF6B4A",
      "--cv-hl-fg": "#FFFFFF",
      "--cv-tag-bg": "#FFF1E8",
      "--cv-tag-fg": "#C2410C",
      "--cv-tag-border": "#FFD9C4",
      "--cv-divider": "#EADFD2",
    },
  },
  {
    id: "forest",
    name: "墨绿奶黄",
    bgColor: "#14452F",
    vars: {
      "--cv-bg": "linear-gradient(155deg, #0E3A28 0%, #1C6B45 60%, #2E8B57 100%)",
      "--cv-fg": "#FFFBEA",
      "--cv-muted": "rgba(255,251,234,0.82)",
      "--cv-accent": "#F5D76E",
      "--cv-hl-fg": "#3A2A00",
      "--cv-tag-bg": "rgba(245,215,110,0.18)",
      "--cv-tag-fg": "#F5D76E",
      "--cv-tag-border": "rgba(245,215,110,0.55)",
      "--cv-divider": "rgba(255,251,234,0.28)",
    },
  },
  {
    id: "neon",
    name: "黑底荧光",
    bgColor: "#101014",
    vars: {
      "--cv-bg": "linear-gradient(165deg, #0B0B0F 0%, #1A1A22 55%, #26262F 100%)",
      "--cv-fg": "#FFFFFF",
      "--cv-muted": "rgba(255,255,255,0.72)",
      "--cv-accent": "#C6FF3D",
      "--cv-hl-fg": "#101014",
      "--cv-tag-bg": "rgba(198,255,61,0.16)",
      "--cv-tag-fg": "#C6FF3D",
      "--cv-tag-border": "rgba(198,255,61,0.5)",
      "--cv-divider": "rgba(255,255,255,0.2)",
    },
  },
  {
    id: "ocean",
    name: "深海蓝",
    bgColor: "#0F4C81",
    vars: {
      "--cv-bg": "linear-gradient(160deg, #0B3C6B 0%, #1E6FB8 60%, #3A8DFF 100%)",
      "--cv-fg": "#FFFFFF",
      "--cv-muted": "rgba(255,255,255,0.85)",
      "--cv-accent": "#FFD166",
      "--cv-hl-fg": "#5A3A00",
      "--cv-tag-bg": "rgba(255,255,255,0.2)",
      "--cv-tag-fg": "#FFFFFF",
      "--cv-tag-border": "rgba(255,255,255,0.48)",
      "--cv-divider": "rgba(255,255,255,0.32)",
    },
  },
];

/** 按 id 取主题，取不到则返回第一组（脏数据不白屏） */
export function getTheme(id: string | null | undefined): CoverTheme {
  return COVER_THEMES.find((theme) => theme.id === id) ?? COVER_THEMES[0];
}
