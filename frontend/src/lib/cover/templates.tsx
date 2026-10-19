"use client";

/**
 * 封面模板（自 `D:\项目1\cover_spike\templates.js` 迁移为 React 组件）。
 *
 * 迁移原则（与 spike 保持等价）：
 * - 4 套模板、class 名、DOM 结构、字段契约（`cover_data`）全部一致；
 * - 防注入：spike 用 `esc()` 转义 HTML 字符串，React 的 JSX 文本插值天然转义，
 *   故不再需要 `esc()`（这也是「不用 dangerouslySetInnerHTML」的原因）；
 * - 非法 `template` / `theme` 回退第一项（`getTemplate` / `getTheme`），不白屏；
 * - 颜色一律走主题 CSS 变量（由 `CoverCanvas` 写到 `.cover` 上），
 *   字号自适应交给 `fitTitle`（见 `fitTitle.ts`），模板本身不碰样式逻辑。
 */

import type { CSSProperties, ReactNode, RefObject } from "react";
import { useEffect, useLayoutEffect, useRef } from "react";

import { SAFE_BOTTOM_EXTRA, SAFE_PADDING } from "./constants";
import { fitTitle } from "./fitTitle";
import { getTheme } from "./themes";

/** 封面编辑数据（字段与 cover_data.example.json / covers.config_json 对齐） */
export interface CoverDraft {
  title: string;
  subtitle: string;
  tags: string[];
  bullets: string[];
  author: string;
  page_no: string;
  /** 模板标识（非法值回退第一项） */
  template: string;
  /** 配色标识（非法值回退第一项） */
  theme: string;
  /** 高亮关键词（仅 highlight 模板使用） */
  highlight_word: string;
  /**
   * 背景图 dataURL —— **仅当前编辑会话内存中使用**：
   * 不写 config_json、不落库、不上传；重新打开需重新上传（同 spike 口径）。
   */
  bg_image: string | null;
}

/** 初始示例草稿（同 spike 的 state 默认值） */
export const DEFAULT_DRAFT: CoverDraft = {
  title: "3个表格函数，让你准时下班",
  subtitle: "打工人效率自救指南，第 1 期",
  tags: ["职场效率", "Excel", "干货"],
  bullets: ["VLOOKUP 跨表取数", "SUMIFS 多条件汇总", "TEXT 一键格式化"],
  author: "@你的昵称",
  page_no: "01/06",
  template: "gradient_hero",
  theme: "coral",
  highlight_word: "",
  bg_image: null,
};

/* ------------------------------------------------------------------ 共享子块 */

/** 主标题：highlight 模板把指定关键词包进 `<mark class="cv-hl">`（与 spike 同口径） */
function TitleText({ title, keyword }: { title: string; keyword?: string }) {
  const text = String(title ?? "");
  if (keyword && text.includes(keyword)) {
    const index = text.indexOf(keyword);
    return (
      <>
        {text.slice(0, index)}
        <mark className="cv-hl">{keyword}</mark>
        {text.slice(index + keyword.length)}
      </>
    );
  }
  return <>{text}</>;
}

/** 标签胶囊组（空则不渲染） */
function TagsBlock({ tags }: { tags: string[] }) {
  const list = (tags ?? []).filter((tag) => String(tag).trim() !== "");
  if (list.length === 0) return null;
  return (
    <div className="cv-tags">
      {list.map((tag, index) => (
        <span className="cv-tag" key={index}>
          {tag}
        </span>
      ))}
    </div>
  );
}

/** 底部署名 + 页码 */
function MetaBlock({ draft }: { draft: CoverDraft }) {
  return (
    <div className="cv-meta">
      <span className="cv-author">{draft.author ?? ""}</span>
      {draft.page_no ? <span className="cv-page">{draft.page_no}</span> : null}
    </div>
  );
}

/** 标准底部（标签 + 署名） */
function FootBlock({ draft }: { draft: CoverDraft }) {
  return (
    <div className="cv-foot">
      <TagsBlock tags={draft.tags} />
      <MetaBlock draft={draft} />
    </div>
  );
}

/** 副标题（钩子；空则不渲染） */
function SubtitleBlock({ draft }: { draft: CoverDraft }) {
  const subtitle = String(draft.subtitle ?? "").trim();
  return subtitle ? <p className="cv-subtitle">{subtitle}</p> : null;
}

/* ------------------------------------------------------------------ 4 套模板 */

/** 渐变大字报：眉题 + 大主标题 + 副标题钩子 + 标签胶囊 + 署名 */
function GradientHero({ draft }: { draft: CoverDraft }) {
  return (
    <>
      <div className="cover-bg" />
      <div className="safe-area">
        <div className="cv-head">
          <p className="cv-eyebrow">{draft.page_no ? "小红书 · 干货" : "小红书"}</p>
          <h1 className="cv-title">
            <TitleText title={draft.title} />
          </h1>
          <SubtitleBlock draft={draft} />
        </div>
        <FootBlock draft={draft} />
      </div>
    </>
  );
}

/** 纯色清单体：主标题 + 01/02/03 编号要点 + 标签署名 */
function ListClean({ draft }: { draft: CoverDraft }) {
  const bullets = (draft.bullets ?? []).filter((bullet) => String(bullet).trim() !== "");
  return (
    <>
      <div className="cover-bg" />
      <div className="safe-area">
        <div className="cv-head">
          <h1 className="cv-title">
            <TitleText title={draft.title} />
          </h1>
          <SubtitleBlock draft={draft} />
        </div>
        <div className="cv-body">
          {bullets.length > 0 ? (
            <ol className="cv-list">
              {bullets.map((bullet, index) => (
                <li key={index}>
                  <span className="cv-num">{String(index + 1).padStart(2, "0")}</span>
                  <span className="cv-text">{bullet}</span>
                </li>
              ))}
            </ol>
          ) : null}
        </div>
        <FootBlock draft={draft} />
      </div>
    </>
  );
}

/** 背景图风：本地背景图 + 渐变遮罩保证白字可读 + 主副标题 */
function BgImage({ draft }: { draft: CoverDraft }) {
  return (
    <>
      <div className="cover-bg" />
      {draft.bg_image ? (
        // 背景图恒为 FileReader 读出的 dataURL（不用 blob / 外链），canvas 不会被跨域污染
        // eslint-disable-next-line @next/next/no-img-element -- 导出画布需要真实 <img> 节点
        <img className="cover-bg-img" alt="" src={draft.bg_image} />
      ) : null}
      <div className="cover-mask" />
      <div className="safe-area">
        <div className="cv-head">
          <h1 className="cv-title">
            <TitleText title={draft.title} />
          </h1>
          <SubtitleBlock draft={draft} />
        </div>
        <FootBlock draft={draft} />
      </div>
    </>
  );
}

/** 关键词高亮：主标题中指定关键词用高亮色块强调 + 副标题 + 标签 */
function Highlight({ draft }: { draft: CoverDraft }) {
  return (
    <>
      <div className="cover-bg" />
      <div className="safe-area">
        <div className="cv-head">
          <h1 className="cv-title">
            <TitleText title={draft.title} keyword={draft.highlight_word} />
          </h1>
          <SubtitleBlock draft={draft} />
        </div>
        <FootBlock draft={draft} />
      </div>
    </>
  );
}

export interface CoverTemplate {
  id: string;
  name: string;
  desc: string;
  /** 该模板是否有「清单要点」字段 */
  needBullets: boolean;
  /** 该模板是否有「高亮关键词」字段 */
  needKeyword: boolean;
  render: (draft: CoverDraft) => ReactNode;
}

/** 4 套模板（数据驱动；顺序即下拉顺序，第一项为回退项） */
export const COVER_TEMPLATES: CoverTemplate[] = [
  {
    id: "gradient_hero",
    name: "渐变大字报",
    desc: "大主标题 + 副标题钩子 + 标签胶囊 + 署名",
    needBullets: false,
    needKeyword: false,
    render: (draft) => <GradientHero draft={draft} />,
  },
  {
    id: "list_clean",
    name: "纯色清单体",
    desc: "主标题 + 01/02/03 编号要点 + 标签署名",
    needBullets: true,
    needKeyword: false,
    render: (draft) => <ListClean draft={draft} />,
  },
  {
    id: "bg_image",
    name: "背景图风",
    desc: "本地背景图 + 渐变遮罩 + 主副标题（白字可读）",
    needBullets: false,
    needKeyword: false,
    render: (draft) => <BgImage draft={draft} />,
  },
  {
    id: "highlight",
    name: "关键词高亮",
    desc: "主标题中指定关键词用高亮色块强调 + 副标题 + 标签",
    needBullets: false,
    needKeyword: true,
    render: (draft) => <Highlight draft={draft} />,
  },
];

/** 按 id 取模板，取不到返回第一套（脏数据不白屏） */
export function getTemplate(id: string | null | undefined): CoverTemplate {
  return COVER_TEMPLATES.find((template) => template.id === id) ?? COVER_TEMPLATES[0];
}

/* ------------------------------------------------------------------ 渲染外壳 */

// SSR 期间没有 DOM：服务端用 useEffect 占位（不执行），客户端用 useLayoutEffect
// 保证「渲染提交后、浏览器绘制前」完成测量（与 spike 的时序一致）。
const useIsomorphicLayoutEffect =
  typeof window !== "undefined" ? useLayoutEffect : useEffect;

/**
 * 封面画布（导出目标节点，恒定 1242×1656）。
 *
 * - 主题变量与安全区尺寸写到根节点（等价于 spike 的 `applyThemeVars` + `--cv-*`）；
 * - 每次渲染后跑 `fitTitle` 做标题自适应（真实 DOM 测量）；
 * - 预览的 `transform: scale()` 施加在**外层包装**上，不改变本节点尺寸，
 *   因此导出像素恒为 1242×1656。
 */
export function CoverCanvas({
  draft,
  className,
  canvasRef,
}: {
  draft: CoverDraft;
  className?: string;
  /** 可选外部 ref：页面据此拿导出节点（不传则仅内部使用） */
  canvasRef?: RefObject<HTMLDivElement | null>;
}) {
  const localRef = useRef<HTMLDivElement>(null);
  const ref = canvasRef ?? localRef;
  const template = getTemplate(draft.template);
  const theme = getTheme(draft.theme);

  useIsomorphicLayoutEffect(() => {
    fitTitle(ref.current);
  });

  const vars = {
    "--cv-safe": `${SAFE_PADDING}px`,
    "--cv-safe-bottom": `${SAFE_PADDING + SAFE_BOTTOM_EXTRA}px`,
    ...theme.vars,
  } as CSSProperties;

  return (
    <div
      ref={ref}
      className={`cover tpl-${template.id}${className ? ` ${className}` : ""}`}
      style={vars}
      data-template={template.id}
      data-theme={theme.id}
    >
      {template.render(draft)}
    </div>
  );
}
