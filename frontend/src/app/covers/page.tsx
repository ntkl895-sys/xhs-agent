"use client";

/**
 * 「封面工作室」页面（第 13 步）。
 *
 * 全栈链路：本页（浏览器）-> 本站 /api/v1/**（Route Handler 服务端代理）-> FastAPI。
 *
 * - 顶部：笔记选择器（GET /api/v1/notes 拉列表；支持 URL ?note_id= 预选）；
 * - 左侧：编辑表单（主标题 / 副标题 / 标签 / 清单要点 / 署名 / 页码 / 模板 / 配色 /
 *   高亮关键词 / 背景图）；
 * - 中部：1242×1656 实时预览（外层 `transform: scale()` 适配视口，**不影响导出像素**）；
 * - 右侧：保存为候选（POST）/ 下载 PNG（浏览器下载）/ 候选库（缩略图按 config_json
 *   实时小尺寸渲染、点击互斥选中、可载入编辑）/ 封面自检（离线、浏览器内完成）。
 *
 * 红线：封面纯前端生成 —— 零 LLM、零 API Key；背景图 dataURL **仅内存**（不写
 * config_json、不落库、不上传）；导出 PNG 由浏览器下载（不落服务器磁盘）。
 *
 * ⚠️ Next 16：`useSearchParams()` 必须包在 `<Suspense>` 中（否则 build 失败），
 * 故把用到它的部分拆成子组件 `CoversInner`，外层用 `<Suspense>` 包裹。
 */

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";

import "./covers.css";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ErrorText, Field, Notice, TagInput, toApiError } from "@/components/shared";
import { ApiError, apiClient, toQuery } from "@/lib/api-client";
import {
  COVER_HEIGHT,
  COVER_WIDTH,
  MAX_PNG_BYTES,
} from "@/lib/cover/constants";
import {
  downloadDataUrl,
  exportNode,
  formatBytes,
  makeTestImageDataUrl,
  stamp,
  waitFontsReady,
} from "@/lib/cover/exportPng";
import { fitTitle, nextFrame } from "@/lib/cover/fitTitle";
import {
  COVER_TEMPLATES,
  CoverCanvas,
  DEFAULT_DRAFT,
  getTemplate,
  type CoverDraft,
} from "@/lib/cover/templates";
import { COVER_THEMES, getTheme } from "@/lib/cover/themes";
import type { Cover, CoverCreateRequest, CoverList, NoteListItem, Page } from "@/lib/types";

/** 候选缩略图宽度（px）；高度按 3:4 换算 */
const THUMB_WIDTH = 110;
const THUMB_HEIGHT = Math.round((THUMB_WIDTH * COVER_HEIGHT) / COVER_WIDTH);

/** 笔记选择器里「未选择」的占位值（避免空串与 base-ui 的空值语义冲突） */
const NOTE_NONE = "__none__";

/** 自检样本：精确 2 / 20 / 40 字（与 spike 逐字一致，勿凭感觉改短） */
const TITLE_SAMPLES = [
  "爆款",
  "每天早下班一小时的Excel效率技巧合集",
  "打工人必看的十个Excel效率技巧合集让你每天早下班一小时真的很实用值得一试推荐",
];

interface SelfTestItem {
  name: string;
  pass: boolean;
  level: "ok" | "warn" | "bad";
  detail: string;
}

function mkItem(
  name: string,
  pass: boolean,
  detail: string,
  level?: "ok" | "warn" | "bad",
): SelfTestItem {
  return { name, pass, level: level ?? (pass ? "ok" : "bad"), detail };
}

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** 由候选记录还原编辑草稿（背景图不随候选保存，恒为 null） */
function draftFromCover(cover: Cover): CoverDraft {
  const config = (cover.config_json ?? {}) as Record<string, unknown>;
  const strList = (value: unknown): string[] =>
    Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
  const str = (value: unknown): string => (typeof value === "string" ? value : "");
  return {
    title: cover.title_text ?? "",
    subtitle: cover.subtitle_text ?? "",
    tags: strList(config.tags),
    bullets: strList(config.bullets),
    author: str(config.author),
    page_no: str(config.page_no),
    highlight_word: str(config.highlight_word),
    template: cover.template_key,
    theme: cover.color_scheme,
    bg_image: null,
  };
}

/** 候选缩略图：按 config_json 实时小尺寸渲染（不存图片） */
function CoverThumb({ cover }: { cover: Cover }) {
  const draft = draftFromCover(cover);
  const scale = THUMB_WIDTH / COVER_WIDTH;
  return (
    <div
      className={`cover-thumb relative ${cover.is_selected ? "cover-thumb-selected" : ""}`}
      style={{ width: THUMB_WIDTH, height: THUMB_HEIGHT }}
    >
      <div
        className="absolute top-0 left-0"
        style={{
          width: COVER_WIDTH,
          height: COVER_HEIGHT,
          transform: `scale(${scale})`,
          transformOrigin: "top left",
        }}
      >
        <CoverCanvas draft={draft} />
      </div>
    </div>
  );
}

function CoversInner() {
  const searchParams = useSearchParams();
  const urlNoteId = searchParams.get("note_id");

  /* ------------------------------------------------------------ 笔记列表 */
  const [notes, setNotes] = useState<NoteListItem[]>([]);
  const [notesLoading, setNotesLoading] = useState(true);
  const [notesError, setNotesError] = useState<ApiError | null>(null);

  /* -------------------------------------------------------- 当前笔记与候选 */
  const [noteId, setNoteId] = useState(urlNoteId ?? "");
  const [covers, setCovers] = useState<CoverList | null>(null);
  const [coversLoading, setCoversLoading] = useState(Boolean(urlNoteId));
  const [coversError, setCoversError] = useState<ApiError | null>(null);
  const [coversToken, setCoversToken] = useState(0);

  /* ------------------------------------------------------------ 编辑草稿 */
  const [draft, setDraft] = useState<CoverDraft>({ ...DEFAULT_DRAFT });
  const draftRef = useRef(draft);

  /* ------------------------------------------------------------ 操作状态 */
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [selectingId, setSelectingId] = useState<string | null>(null);

  /* --------------------------------------------------- 舞台（预览缩放） */
  const stageWrapRef = useRef<HTMLDivElement>(null);
  const coverRef = useRef<HTMLDivElement>(null);
  const [stageScale, setStageScale] = useState(0.32);

  /* ------------------------------------------------------- 背景图（内存） */
  const bgInputRef = useRef<HTMLInputElement>(null);
  const [bgStatus, setBgStatus] = useState("未选择背景图（仅本机内存，不入库、不上传）");

  /* -------------------------------------------------------------- 自检 */
  const [selfTestRunning, setSelfTestRunning] = useState(false);
  const [selfTestStage, setSelfTestStage] = useState("");
  const [selfTestResults, setSelfTestResults] = useState<SelfTestItem[] | null>(null);
  const [selfTestLog, setSelfTestLog] = useState("");
  const runSelfTestRef = useRef<() => Promise<void>>(async () => {});

  // 渲染后同步最新草稿（自检开始/恢复时读它取快照）
  useEffect(() => {
    draftRef.current = draft;
  });

  // 舞台缩放：ResizeObserver 回调里 setState（同源外部系统订阅；初始测量由首次回调完成）
  useEffect(() => {
    const wrap = stageWrapRef.current;
    if (!wrap) return;
    const observer = new ResizeObserver(() => {
      const available = wrap.clientWidth - 16;
      if (available > 0) {
        setStageScale(Math.min(1, available / COVER_WIDTH));
      }
    });
    observer.observe(wrap);
    return () => observer.disconnect();
  }, []);

  // 笔记列表（挂载时一次）
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<Page<NoteListItem>>(
          `/api/v1/notes${toQuery({ page: 1, page_size: 100, status: "all" })}`,
        );
        if (!cancelled) {
          setNotes(data.items);
          setNotesError(null);
        }
      } catch (err) {
        if (!cancelled) setNotesError(toApiError(err));
      } finally {
        if (!cancelled) setNotesLoading(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, []);

  // 候选列表（选择笔记后拉取；token 变化时强制重拉）
  useEffect(() => {
    if (!noteId) return;
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<CoverList>(
          `/api/v1/covers${toQuery({ note_id: noteId })}`,
        );
        if (!cancelled) {
          setCovers(data);
          setCoversError(null);
        }
      } catch (err) {
        if (!cancelled) setCoversError(toApiError(err));
      } finally {
        if (!cancelled) setCoversLoading(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [noteId, coversToken]);

  /* ---------------------------------------------------------- 事件处理 */

  const chooseNote = useCallback((next: string) => {
    setNoteId(next);
    setCovers(null);
    setCoversError(null);
    setCoversLoading(Boolean(next));
    setNotice("");
    setActionError(null);
    setCoversToken((token) => token + 1);
  }, []);

  const reloadCovers = useCallback(() => {
    setCoversLoading(true);
    setCoversToken((token) => token + 1);
  }, []);

  const onBgFile = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!/\.(jpe?g|png|webp)$/i.test(file.name)) {
      setBgStatus("仅支持 jpg/jpeg/png/webp，请转换后再试");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      setDraft((prev) => ({ ...prev, bg_image: String(reader.result) }));
      setBgStatus("已选择本地图片（仅在本机内存中使用；配合「背景图风」模板显示）");
    };
    reader.onerror = () => setBgStatus("读取本地图片失败");
    reader.readAsDataURL(file);
  };

  const clearBg = () => {
    if (bgInputRef.current) bgInputRef.current.value = "";
    setDraft((prev) => ({ ...prev, bg_image: null }));
    setBgStatus("未选择背景图（仅本机内存，不入库、不上传）");
  };

  const saveCover = async () => {
    if (!noteId) {
      setActionError(new ApiError(0, "no_note", "请先选择一篇笔记，候选要挂在笔记下。"));
      return;
    }
    const errors: Record<string, string> = {};
    if (!draft.title.trim()) errors.title = "主标题不能为空";
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) return;

    const config: Record<string, unknown> = {
      tags: draft.tags,
      bullets: draft.bullets,
      author: draft.author,
      page_no: draft.page_no,
    };
    if (draft.highlight_word) config.highlight_word = draft.highlight_word;

    const payload: CoverCreateRequest = {
      note_id: noteId,
      template_key: getTemplate(draft.template).id,
      color_scheme: getTheme(draft.theme).id,
      title_text: draft.title.trim() || null,
      subtitle_text: draft.subtitle.trim() || null,
      config_json: config,
    };

    setSaving(true);
    setActionError(null);
    setNotice("");
    try {
      await apiClient.post<Cover>("/api/v1/covers", payload);
      setNotice("已保存为候选（背景图不随候选保存，重新打开需重新上传）");
      reloadCovers();
    } catch (err) {
      setActionError(toApiError(err));
    } finally {
      setSaving(false);
    }
  };

  const downloadPng = async () => {
    const cover = coverRef.current;
    if (!cover) return;
    setDownloading(true);
    setActionError(null);
    setNotice("");
    try {
      const theme = getTheme(draft.theme);
      const info = await exportNode(cover, theme.bgColor);
      downloadDataUrl(
        info.dataUrl,
        `cover_${getTemplate(draft.template).id}_${theme.id}_${stamp()}.png`,
      );
      setNotice(
        `已导出 ${info.width}×${info.height}，体积 ${formatBytes(info.bytes)}（耗时 ${info.ms}ms）`,
      );
    } catch (err) {
      setActionError(toApiError(err));
    } finally {
      setDownloading(false);
    }
  };

  const selectCandidate = async (cover: Cover) => {
    setSelectingId(cover.id);
    setActionError(null);
    setNotice("");
    try {
      await apiClient.patch<Cover>(`/api/v1/covers/${cover.id}`, { is_selected: true });
      setNotice("已设为当前封面（同笔记其余候选自动取消选中；笔记状态不变）");
      reloadCovers();
    } catch (err) {
      setActionError(toApiError(err));
    } finally {
      setSelectingId(null);
    }
  };

  const loadCandidate = (cover: Cover) => {
    setDraft(draftFromCover(cover));
    setBgStatus("未选择背景图（仅本机内存，不入库、不上传）");
    if (bgInputRef.current) bgInputRef.current.value = "";
    setActionError(null);
    setNotice("已载入该候选到编辑区（背景图不随候选保存，需重新上传）");
  };

  /* -------------------------------------------------------------- 自检 */

  const runSelfTest = async () => {
    const cover = coverRef.current;
    if (!cover) return;
    const snapshot: CoverDraft = { ...draftRef.current };

    setSelfTestRunning(true);
    setSelfTestResults(null);
    setSelfTestLog("");
    setSelfTestStage("准备中");
    const results: SelfTestItem[] = [];
    const logs: string[] = [];
    const log = (text: string) => logs.push(text);

    try {
      // ---------- 1 逻辑画布尺寸 ----------
      setSelfTestStage("1/7 画布尺寸");
      await nextFrame();
      const width = cover.offsetWidth;
      const height = cover.offsetHeight;
      log(`canvas ${width}x${height}`);
      results.push(
        mkItem(
          "1 逻辑画布尺寸",
          width === COVER_WIDTH && height === COVER_HEIGHT,
          `实测 ${width}×${height}，目标 ${COVER_WIDTH}×${COVER_HEIGHT}；舞台缩放不影响导出（导出直接读画布节点）`,
        ),
      );

      // ---------- 2 字体就绪 ----------
      setSelfTestStage("2/7 字体就绪");
      const fontStatus = await waitFontsReady();
      log(`fonts ${fontStatus}`);
      results.push(
        mkItem(
          "2 字体就绪",
          fontStatus === "loaded",
          `document.fonts.status=${fontStatus}；实际 font-family=${getComputedStyle(cover).fontFamily.slice(0, 60)}`,
        ),
      );

      // ---------- 3 标题 2/20/40 字自适应 ----------
      setSelfTestStage("3/7 标题 2/20/40 字自适应");
      const fitLogs: string[] = [];
      let allFit = true;
      for (const sample of TITLE_SAMPLES) {
        setDraft((prev) => ({ ...prev, title: sample }));
        await nextFrame();
        await nextFrame();
        const fit = fitTitle(coverRef.current);
        if (fit.overflow) allFit = false;
        fitLogs.push(
          `${sample.length}字→字号${fit.fontSize}px/${fit.lines}行${fit.overflow ? "溢出✗" : "不溢出✓"}`,
        );
      }
      setDraft((prev) => ({ ...prev, title: snapshot.title }));
      await nextFrame();
      log(`fit ${fitLogs.join("；")}`);
      results.push(mkItem("3 标题 2/20/40 字自适应", allFit, fitLogs.join("；")));

      // ---------- 4 单张导出 ----------
      setSelfTestStage("4/7 导出像素与体积");
      try {
        const info = await exportNode(cover, getTheme(snapshot.theme).bgColor);
        log(`export ${info.width}x${info.height} ${info.ms}ms ${Math.round(info.bytes / 1024)}KB`);
        results.push(
          mkItem(
            "4 导出像素与体积",
            info.width === COVER_WIDTH && info.height === COVER_HEIGHT && info.bytes <= MAX_PNG_BYTES,
            `naturalWidth×Height=${info.width}×${info.height}；体积=${formatBytes(info.bytes)}（上限 ${formatBytes(MAX_PNG_BYTES)}）；耗时=${info.ms}ms`,
          ),
        );
      } catch (err) {
        results.push(mkItem("4 导出像素与体积", false, `导出失败：${messageOf(err)}`));
      }

      // ---------- 5 模板 × 配色全矩阵 ----------
      setSelfTestStage("5/7 模板×配色矩阵导出（20 次）");
      const matrix: string[] = [];
      let matrixOk = true;
      for (const template of COVER_TEMPLATES) {
        for (const theme of COVER_THEMES) {
          setDraft((prev) => ({ ...prev, template: template.id, theme: theme.id }));
          await nextFrame();
          await nextFrame();
          try {
            const info = await exportNode(cover, theme.bgColor);
            const ok = info.width === COVER_WIDTH && info.height === COVER_HEIGHT;
            if (!ok) matrixOk = false;
            matrix.push(`${template.id}×${theme.id}${ok ? "✓" : `✗(${info.width}×${info.height})`}`);
            log(
              `m ${template.id}×${theme.id} ${info.width}x${info.height} ${info.ms}ms ${Math.round(info.bytes / 1024)}KB`,
            );
          } catch (err) {
            matrixOk = false;
            matrix.push(`${template.id}×${theme.id}✗(${messageOf(err)})`);
          }
        }
      }
      setDraft((prev) => ({ ...prev, template: snapshot.template, theme: snapshot.theme }));
      await nextFrame();
      results.push(
        mkItem(
          "5 模板×配色全矩阵导出",
          matrixOk,
          `${COVER_TEMPLATES.length} 套模板 × ${COVER_THEMES.length} 组配色 = ${matrix.length} 次导出：${matrix.join("，")}`,
        ),
      );

      // ---------- 6 背景图链路（内存测试图） ----------
      setSelfTestStage("6/7 背景图导出（内存测试图）");
      setDraft((prev) => ({ ...prev, template: "bg_image", bg_image: makeTestImageDataUrl() }));
      await nextFrame();
      await nextFrame();
      try {
        const info = await exportNode(cover, getTheme(snapshot.theme).bgColor);
        log(`bg ${info.width}x${info.height} ${Math.round(info.bytes / 1024)}KB`);
        results.push(
          mkItem(
            "6 背景图导出（dataURL 注入）",
            info.width === COVER_WIDTH && info.height === COVER_HEIGHT,
            `尺寸=${info.width}×${info.height}，体积=${formatBytes(info.bytes)}，无 SecurityError；用户真实大图请自行另测`,
          ),
        );
      } catch (err) {
        results.push(mkItem("6 背景图导出（dataURL 注入）", false, `失败：${messageOf(err)}`));
      }
      setDraft((prev) => ({ ...prev, template: snapshot.template, bg_image: snapshot.bg_image }));
      await nextFrame();

      // ---------- 7 离线可导出 ----------
      setSelfTestStage("7/7 离线可导出");
      const externalScripts = Array.from(
        document.querySelectorAll<HTMLScriptElement>("script[src]"),
      ).filter((node) => {
        try {
          return new URL(node.src, window.location.href).origin !== window.location.origin;
        } catch {
          return false;
        }
      });
      results.push(
        mkItem(
          "7 离线可导出（bundle 依赖）",
          externalScripts.length === 0,
          externalScripts.length === 0
            ? "html-to-image 来自 npm bundle；页面无任何外链脚本 / 在线字体；以上 22 次导出均在本地完成"
            : `发现外链脚本：${externalScripts.map((node) => node.src).join("，")}`,
          externalScripts.length === 0 ? "ok" : "warn",
        ),
      );

      // 收尾：完整恢复草稿
      setDraft(snapshot);
      await nextFrame();
    } catch (err) {
      results.push(mkItem("自检异常", false, messageOf(err)));
    } finally {
      setSelfTestResults(results);
      setSelfTestStage("");
      setSelfTestLog(logs.join("\n"));
      setSelfTestRunning(false);
    }
  };

  // 渲染后同步最新自检函数（供 #autotest 自动触发使用）
  useEffect(() => {
    runSelfTestRef.current = runSelfTest;
  });

  // 自动化钩子：访问 /covers#autotest 自动跑一遍自检，结果写入 DOM
  // （自检不依赖笔记与后端，可离线核验；面板带 data-selftest-done / data-selftest-summary）
  useEffect(() => {
    if (typeof window === "undefined" || window.location.hash !== "#autotest") return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      if (!cancelled) void runSelfTestRef.current();
    }, 400);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, []);

  /* ------------------------------------------------------------ 派生数据 */

  const noteItems = useMemo(() => {
    const entries: Record<string, string> = { [NOTE_NONE]: "请选择笔记…" };
    for (const note of notes) {
      const short = note.title.length > 28 ? `${note.title.slice(0, 28)}…` : note.title;
      entries[note.id] = `${short}（${note.status}）`;
    }
    return entries;
  }, [notes]);

  const templateItems = useMemo(() => {
    const entries: Record<string, string> = {};
    for (const template of COVER_TEMPLATES) {
      entries[template.id] = `${template.name}（${template.desc}）`;
    }
    return entries;
  }, []);

  const activeTemplate = getTemplate(draft.template);
  const currentNote = notes.find((note) => note.id === noteId) ?? null;
  const selfTestSummary = selfTestResults
    ? {
        total: selfTestResults.length,
        pass: selfTestResults.filter((item) => item.pass && item.level === "ok").length,
        warn: selfTestResults.filter((item) => item.level === "warn").length,
        fail: selfTestResults.filter((item) => !item.pass).length,
      }
    : null;

  return (
    <>
      {/* ---------------- 顶部：笔记选择器 ---------------- */}
      <Card>
        <CardContent className="flex flex-wrap items-end gap-4 pt-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="cv-note">选择笔记</Label>
            <Select
              items={noteItems}
              value={noteId || NOTE_NONE}
              onValueChange={(value) => {
                const next = String(value);
                chooseNote(next === NOTE_NONE ? "" : next);
              }}
            >
              <SelectTrigger id="cv-note" className="w-80">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NOTE_NONE}>请选择笔记…</SelectItem>
                {notes.map((note) => {
                  const short =
                    note.title.length > 28 ? `${note.title.slice(0, 28)}…` : note.title;
                  return (
                    <SelectItem key={note.id} value={note.id}>
                      {short}（{note.status}）
                    </SelectItem>
                  );
                })}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1 text-sm text-muted-foreground">
            {notesLoading ? (
              <span>加载笔记列表…</span>
            ) : notesError ? (
              <span className="text-red-600">笔记列表加载失败：{notesError.message}</span>
            ) : noteId ? (
              <span>
                当前笔记：{currentNote ? currentNote.title : noteId}；候选 {covers?.items.length ?? 0} 个
                {covers?.selected_id ? "，已选中 1 个" : "，未选中"}
              </span>
            ) : (
              <span className="text-amber-700">
                请先选择一篇笔记：候选的保存与选中都挂在笔记下（预览与「封面自检」无需笔记，可离线使用）
              </span>
            )}
            {notes.length === 0 && !notesLoading && !notesError ? (
              <span>暂无笔记：请先在「选题库」把选题通过，再用创作 Agent 生成笔记草稿。</span>
            ) : null}
          </div>
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[380px_minmax(0,1fr)_380px]">
        {/* ---------------- 左：编辑表单 ---------------- */}
        <Card>
          <CardHeader>
            <CardTitle>编辑内容</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <Field
              label="主标题 *"
              htmlFor="cv-title"
              error={fieldErrors.title}
              hint="100 字以内；标题会按 150→48px 自动降档，最多 3 行不溢出"
            >
              <Input
                id="cv-title"
                maxLength={100}
                value={draft.title}
                onChange={(event) => setDraft({ ...draft, title: event.target.value })}
              />
            </Field>

            <Field label="副标题 / 钩子" htmlFor="cv-subtitle" hint="100 字以内">
              <Input
                id="cv-subtitle"
                maxLength={100}
                value={draft.subtitle}
                onChange={(event) => setDraft({ ...draft, subtitle: event.target.value })}
              />
            </Field>

            <Field label="标签" hint="最多 8 个；以胶囊展示">
              <TagInput
                value={draft.tags}
                onChange={(tags) => setDraft({ ...draft, tags })}
                placeholder="输入后回车或点「添加」"
                maxItems={8}
                maxLength={20}
              />
            </Field>

            {activeTemplate.needBullets ? (
              <Field label="清单要点" hint="仅「纯色清单体」使用；编号 01/02/03 自动生成">
                <TagInput
                  value={draft.bullets}
                  onChange={(bullets) => setDraft({ ...draft, bullets })}
                  placeholder="输入后回车或点「添加」"
                  maxItems={8}
                  maxLength={40}
                />
              </Field>
            ) : null}

            <div className="flex gap-3">
              <div className="flex-1">
                <Field label="署名" htmlFor="cv-author">
                  <Input
                    id="cv-author"
                    maxLength={50}
                    value={draft.author}
                    onChange={(event) => setDraft({ ...draft, author: event.target.value })}
                  />
                </Field>
              </div>
              <div className="flex-1">
                <Field label="页码" htmlFor="cv-page">
                  <Input
                    id="cv-page"
                    maxLength={20}
                    value={draft.page_no}
                    onChange={(event) => setDraft({ ...draft, page_no: event.target.value })}
                  />
                </Field>
              </div>
            </div>

            {activeTemplate.needKeyword ? (
              <Field
                label="高亮关键词"
                htmlFor="cv-keyword"
                hint="仅「关键词高亮」模板使用；填主标题里出现的词"
              >
                <Input
                  id="cv-keyword"
                  maxLength={30}
                  value={draft.highlight_word}
                  onChange={(event) =>
                    setDraft({ ...draft, highlight_word: event.target.value })
                  }
                />
              </Field>
            ) : null}

            <Field label="模板">
              <Select
                items={templateItems}
                value={activeTemplate.id}
                onValueChange={(value) => setDraft({ ...draft, template: String(value) })}
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {COVER_TEMPLATES.map((template) => (
                    <SelectItem key={template.id} value={template.id}>
                      {template.name}（{template.desc}）
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>

            <Field label="配色">
              <div className="flex flex-wrap gap-2">
                {COVER_THEMES.map((theme) => {
                  const active = getTheme(draft.theme).id === theme.id;
                  return (
                    <button
                      key={theme.id}
                      type="button"
                      onClick={() => setDraft({ ...draft, theme: theme.id })}
                      className={
                        active
                          ? "flex items-center gap-1.5 rounded-full border border-primary bg-muted px-2.5 py-1 text-xs font-medium"
                          : "flex items-center gap-1.5 rounded-full border border-border px-2.5 py-1 text-xs text-muted-foreground hover:text-foreground"
                      }
                    >
                      <i
                        className="block h-4 w-4 rounded-full border border-black/10"
                        style={{ background: theme.bgColor }}
                      />
                      {theme.name}
                    </button>
                  );
                })}
              </div>
            </Field>

            <Field label="背景图" hint="仅本机浏览器内存使用：不入库、不上传；选「背景图风」模板显示">
              <div className="flex items-center gap-2">
                <input
                  ref={bgInputRef}
                  type="file"
                  accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp"
                  className="min-w-0 flex-1 text-xs"
                  onChange={onBgFile}
                />
                <Button type="button" variant="outline" size="sm" onClick={clearBg}>
                  清除
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">{bgStatus}</p>
            </Field>
          </CardContent>
        </Card>

        {/* ---------------- 中：实时预览 ---------------- */}
        <Card className="self-start">
          <CardHeader>
            <CardTitle>
              实时预览（1242×1656）
              <span className="ml-2 text-xs font-normal text-muted-foreground">
                缩放 {Math.round(stageScale * 100)}%
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div ref={stageWrapRef} className="cover-stage-wrap flex justify-center">
              <div
                style={{
                  width: Math.round(COVER_WIDTH * stageScale),
                  height: Math.round(COVER_HEIGHT * stageScale),
                  position: "relative",
                }}
              >
                <div
                  style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    transform: `scale(${stageScale})`,
                    transformOrigin: "top left",
                  }}
                >
                  <CoverCanvas draft={draft} canvasRef={coverRef} />
                </div>
              </div>
            </div>
            <p className="mt-2 text-xs text-muted-foreground">
              预览为等比缩放；导出直接读 1242×1656 画布节点，缩放不影响导出像素。文字均在安全区内
              （边距 {`96px`}，底部额外 48px）。
            </p>
          </CardContent>
        </Card>

        {/* ---------------- 右：操作 + 候选库 + 自检 ---------------- */}
        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader>
              <CardTitle>操作</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-2">
              <ErrorText error={actionError} />
              <Notice text={notice} />
              <Button onClick={() => void saveCover()} disabled={saving || !noteId}>
                {saving ? "保存中…" : "保存为候选"}
              </Button>
              <Button variant="outline" onClick={() => void downloadPng()} disabled={downloading}>
                {downloading ? "导出中…" : "下载 PNG"}
              </Button>
              <Button
                variant="outline"
                onClick={() => void runSelfTestRef.current()}
                disabled={selfTestRunning}
              >
                {selfTestRunning ? `封面自检中…（${selfTestStage}）` : "封面自检"}
              </Button>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>候选库</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              {coversError ? <ErrorText error={coversError} /> : null}
              {!noteId ? (
                <p className="text-sm text-muted-foreground">先选择笔记以查看候选。</p>
              ) : coversLoading ? (
                <p className="text-sm text-muted-foreground">加载中……</p>
              ) : (covers?.items.length ?? 0) === 0 ? (
                <p className="text-sm text-muted-foreground">
                  暂无候选，点「保存为候选」创建第一个。
                </p>
              ) : (
                <div className="grid grid-cols-2 gap-3">
                  {covers!.items.map((cover) => (
                    <div key={cover.id} className="flex flex-col gap-1.5">
                      <button
                        type="button"
                        title="点击设为当前封面（同笔记互斥）"
                        className="cursor-pointer disabled:opacity-50"
                        disabled={selectingId === cover.id}
                        onClick={() => void selectCandidate(cover)}
                      >
                        <CoverThumb cover={cover} />
                      </button>
                      <div className="flex items-center justify-between text-xs text-muted-foreground">
                        <span>{cover.template_key}</span>
                        {cover.is_selected ? (
                          <Badge className="border-emerald-600/30 bg-emerald-500/15 text-emerald-700">
                            当前
                          </Badge>
                        ) : null}
                      </div>
                      <Button variant="outline" size="sm" onClick={() => loadCandidate(cover)}>
                        载入编辑
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>封面自检（离线 · 浏览器内）</CardTitle>
            </CardHeader>
            <CardContent>
              <div
                data-selftest-done={selfTestResults ? "1" : "0"}
                data-selftest-summary={selfTestSummary ? JSON.stringify(selfTestSummary) : ""}
              >
                {selfTestResults ? (
                  <>
                    <ul className="flex flex-col gap-2 text-xs">
                      {selfTestResults.map((item) => (
                        <li key={item.name}>
                          <span
                            className={
                              item.level === "warn"
                                ? "text-amber-700"
                                : item.pass
                                  ? "text-emerald-700"
                                  : "text-red-600"
                            }
                          >
                            {item.level === "warn" ? "⚠" : item.pass ? "✓" : "✗"}
                          </span>{" "}
                          <span className="font-medium">{item.name}</span>
                          <p className="whitespace-normal break-all text-muted-foreground">
                            {item.detail}
                          </p>
                        </li>
                      ))}
                    </ul>
                    {selfTestSummary ? (
                      <p className="mt-2 text-xs text-muted-foreground">
                        共 {selfTestSummary.total} 项，通过 {selfTestSummary.pass}，警告{" "}
                        {selfTestSummary.warn}，失败 {selfTestSummary.fail}
                      </p>
                    ) : null}
                  </>
                ) : selfTestRunning ? (
                  <p className="text-sm text-muted-foreground">自检进行中…（{selfTestStage}）</p>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    点「封面自检」在浏览器内离线验证：画布尺寸 / 三档标题自适应 / 4×5 全矩阵导出 /
                    单张体积 / 背景图链路 / 离线可导出。
                  </p>
                )}
              </div>
              {selfTestLog ? (
                <details className="mt-2 text-xs">
                  <summary className="cursor-pointer text-muted-foreground">
                    自检日志（{selfTestLog.split("\n").length} 行）
                  </summary>
                  <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all text-muted-foreground">
                    {selfTestLog}
                  </pre>
                </details>
              ) : null}
              <pre data-selftest-json hidden>
                {selfTestResults ? JSON.stringify(selfTestResults) : ""}
              </pre>
            </CardContent>
          </Card>
        </div>
      </div>
    </>
  );
}

export default function CoversPage() {
  return (
    <main className="mx-auto flex w-full max-w-[1800px] flex-1 flex-col gap-6 px-6 py-10">
      <header className="flex flex-col gap-2">
        <h1 className="text-2xl font-medium tracking-tight">封面工作室</h1>
        <p className="text-sm text-muted-foreground">
          第 13 步：程序化封面（4 套模板 × 5 组配色，1242×1656 严格 3:4）。封面完全由浏览器本地渲染导出
          —— 不调用任何大模型、不需要 API Key；候选只存文字配置（背景图与大图不入库），导出 PNG 由浏览器直接下载。
        </p>
      </header>

      <Suspense fallback={<p className="text-sm text-muted-foreground">加载中……</p>}>
        <CoversInner />
      </Suspense>
    </main>
  );
}
