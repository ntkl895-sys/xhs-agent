"use client";

/**
 * 「单篇诊断」页面（第 16 步）。
 *
 * 全栈链路：本页（浏览器）-> 本站 /api/v1/**（Route Handler 服务端代理）-> FastAPI。
 *
 * - 顶部：笔记选择器（GET /api/v1/notes?status=published 只列**已发布**笔记；
 *   支持 URL ?note_id= 预选）+ 诊断类型选择（自动 / 冷启动 / 复盘 / 异常）；
 * - 中部：点「生成诊断」-> POST /api/v1/diagnostics/generate（同步，BFF 超时 120s），
 *   结果分块展示：①数据事实（**由程序计算**：最新六指标 / 互动率 / 环比 / 覆盖时长）；
 *   ②综合评分（数据不足时显示「数据不足，未评分」，不硬打分）；
 *   ③亮点 / 问题 / 原因 / 建议（建议逐条可复制）；
 *   ④诊断类型与判定来源（自动 / 手动）；
 *   ⑤基准与数据缺口（显著声明「仅基于该笔记自身数据、无行业 / 爆款基准」）；
 * - 底部：诊断历史（GET /api/v1/diagnostics?note_id=，倒序），点「查看」拉单条详情。
 *
 * 红线：诊断 = **只读 + 建议**（不改笔记 / 选题 / 数据、不自动发布、不登录小红书）；
 * 本期没有任何外部基准（跨篇基准属第 18 步 RAG）；接口不返回 ``llm_raw``。
 *
 * ⚠️ Next 16：`useSearchParams()` 必须包在 `<Suspense>` 中（否则 build 失败），
 * 故把用到它的部分拆成子组件 `DiagnosticsInner`，外层用 `<Suspense>` 包裹。
 */

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  ErrorText,
  Notice,
  formatDateTime,
  formatNumber,
  formatScore,
  toApiError,
} from "@/components/shared";
import { ApiError, apiClient, toQuery } from "@/lib/api-client";
import type {
  Diagnostic,
  DiagnosticBenchmark,
  DiagnosticList,
  DiagnosticType,
  NoteListItem,
  Page,
} from "@/lib/types";

/** 笔记选择器里「未选择」的占位值（与数据回填页同款约定） */
const NOTE_NONE = "__none__";

/** 「自动判定」在下拉里的占位值（区别于三个显式类型） */
const TYPE_AUTO = "auto";

/** 六个指标（顺序即展示顺序，与后端 METRIC_FIELDS 一致） */
const METRIC_COLUMNS: { key: string; label: string }[] = [
  { key: "views", label: "浏览量" },
  { key: "likes", label: "点赞" },
  { key: "collects", label: "收藏" },
  { key: "comments", label: "评论" },
  { key: "shares", label: "分享" },
  { key: "new_followers", label: "新增粉丝" },
];

/** 六条互动率（顺序即展示顺序，与后端 RATE_FIELDS 一致） */
const RATE_COLUMNS: { key: string; label: string }[] = [
  { key: "like_rate", label: "点赞率" },
  { key: "collect_rate", label: "收藏率" },
  { key: "comment_rate", label: "评论率" },
  { key: "share_rate", label: "分享率" },
  { key: "follow_rate", label: "涨粉率" },
  { key: "engagement_rate", label: "互动率" },
];

const TYPE_META: Record<DiagnosticType, { text: string; cls: string }> = {
  cold_start: { text: "冷启动", cls: "border-sky-600/30 bg-sky-500/15 text-sky-700" },
  review: { text: "复盘", cls: "border-border bg-muted text-muted-foreground" },
  anomaly: { text: "异常", cls: "border-red-600/30 bg-red-500/15 text-red-700" },
};

/** 判定来源 -> 中文（auto / manual） */
function sourceText(source: string | null | undefined): string {
  if (source === "manual") return "手动指定";
  if (source === "auto") return "自动判定";
  return "—";
}

/** 比率（0~1 小数）-> 百分比展示；null -> 「—」 */
function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(2)}%`;
}

/** 比率带符号（正数补 +）——增长率展示用 */
function formatPercentSigned(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const text = `${(value * 100).toFixed(2)}%`;
  return value > 0 ? `+${text}` : text;
}

/** 增量带符号（正数补 +）——环比展示用 */
function formatDelta(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value > 0 ? `+${formatNumber(value)}` : formatNumber(value);
}

/** 从 benchmark.trends 里安全读出总增量 / 增长率 / 覆盖时长（结构由后端固定生成） */
function readTrends(benchmark: DiagnosticBenchmark | null): {
  totalDeltas: Record<string, number | null>;
  growthRates: Record<string, number | null>;
  coverageHours: number | null;
} {
  const raw = benchmark?.trends;
  if (!raw || typeof raw !== "object") {
    return { totalDeltas: {}, growthRates: {}, coverageHours: null };
  }
  const record = raw as Record<string, unknown>;
  const deltas = record.total_deltas;
  const rates = record.growth_rates;
  const hours = record.coverage_hours;
  return {
    totalDeltas: (deltas && typeof deltas === "object" ? deltas : {}) as Record<
      string,
      number | null
    >,
    growthRates: (rates && typeof rates === "object" ? rates : {}) as Record<
      string,
      number | null
    >,
    coverageHours: typeof hours === "number" ? hours : null,
  };
}

/** 复制文本：优先 Clipboard API，失败时回退到隐藏 textarea（兼容非安全上下文） */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // 忽略：走下面的兜底
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}

function DiagnosticsInner() {
  const searchParams = useSearchParams();
  const urlNoteId = searchParams.get("note_id");

  /* ------------------------------------------------------------ 笔记列表 */
  const [notes, setNotes] = useState<NoteListItem[]>([]);
  const [notesLoading, setNotesLoading] = useState(true);
  const [notesError, setNotesError] = useState<ApiError | null>(null);

  /* ------------------------------------------------------------ 当前笔记 */
  const [noteId, setNoteId] = useState(urlNoteId ?? "");

  /* ------------------------------------------------------------ 生成参数 / 状态 */
  const [generateType, setGenerateType] = useState<string>(TYPE_AUTO);
  const [generating, setGenerating] = useState(false);
  const [genError, setGenError] = useState<ApiError | null>(null);
  const [formError, setFormError] = useState("");
  const [notice, setNotice] = useState("");
  /** 当前展示的诊断（刚生成的 / 历史里点开查看的） */
  const [active, setActive] = useState<Diagnostic | null>(null);
  /** 建议复制反馈：记录被复制建议的下标 */
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null);

  /* ------------------------------------------------------------ 诊断历史 */
  const [history, setHistory] = useState<DiagnosticList | null>(null);
  const [historyLoading, setHistoryLoading] = useState(Boolean(urlNoteId));
  const [historyError, setHistoryError] = useState<ApiError | null>(null);
  const [historyToken, setHistoryToken] = useState(0);
  const [detailBusyId, setDetailBusyId] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<ApiError | null>(null);

  // 笔记列表（挂载时一次；**只展示已发布** —— 诊断针对已发布作品）
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<Page<NoteListItem>>(
          `/api/v1/notes${toQuery({ page: 1, page_size: 100, status: "published" })}`,
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

  // 诊断历史（选择笔记后拉取；token 变化时强制重拉）
  useEffect(() => {
    if (!noteId) return;
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<DiagnosticList>(
          `/api/v1/diagnostics${toQuery({ note_id: noteId })}`,
        );
        if (!cancelled) {
          setHistory(data);
          setHistoryError(null);
        }
      } catch (err) {
        if (!cancelled) setHistoryError(toApiError(err));
      } finally {
        if (!cancelled) setHistoryLoading(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [noteId, historyToken]);

  const chooseNote = useCallback((next: string) => {
    setNoteId(next);
    setHistory(null);
    setHistoryError(null);
    setHistoryLoading(Boolean(next));
    setHistoryToken((token) => token + 1);
    setNotice("");
    setFormError("");
    setGenError(null);
    setDetailError(null);
    setActive(null);
    setCopiedIndex(null);
  }, []);

  const reloadHistory = useCallback(() => {
    setHistoryLoading(true);
    setHistoryToken((token) => token + 1);
  }, []);

  /* ------------------------------------------------------------ 生成诊断 */
  const onGenerate = async () => {
    setGenError(null);
    setNotice("");
    setFormError("");
    setDetailError(null);
    if (!noteId) {
      setFormError("请先选择要诊断的笔记");
      return;
    }
    setGenerating(true);
    try {
      const result = await apiClient.post<Diagnostic>("/api/v1/diagnostics/generate", {
        note_id: noteId,
        diagnosis_type: generateType === TYPE_AUTO ? null : generateType,
      });
      setActive(result);
      setCopiedIndex(null);
      const typeText = result.diagnosis_type ? TYPE_META[result.diagnosis_type].text : "诊断";
      setNotice(
        result.score === null
          ? `已生成「${typeText}」诊断：数据不足，未评分（详见下方数据缺口）`
          : `已生成「${typeText}」诊断：综合评分 ${formatScore(result.score)}`,
      );
      reloadHistory();
    } catch (err) {
      setGenError(toApiError(err));
    } finally {
      setGenerating(false);
    }
  };

  /* ------------------------------------------------------------ 历史详情 */
  const openDetail = async (id: string) => {
    setDetailBusyId(id);
    setDetailError(null);
    try {
      const data = await apiClient.get<Diagnostic>(
        `/api/v1/diagnostics/${encodeURIComponent(id)}`,
      );
      setActive(data);
      setCopiedIndex(null);
    } catch (err) {
      setDetailError(toApiError(err));
    } finally {
      setDetailBusyId(null);
    }
  };

  /* ------------------------------------------------------------ 复制建议 */
  const onCopySuggestion = async (index: number, text: string) => {
    const ok = await copyText(text);
    setCopiedIndex(ok ? index : null);
    setNotice(ok ? "建议已复制到剪贴板" : "复制失败：请手动选中文本复制");
  };

  /* ------------------------------------------------------------ 派生数据 */
  const noteItems = useMemo(() => {
    const entries: Record<string, string> = { [NOTE_NONE]: "请选择笔记…" };
    for (const note of notes) {
      const short = note.title.length > 28 ? `${note.title.slice(0, 28)}…` : note.title;
      entries[note.id] = `${short}（已发布）`;
    }
    if (noteId && !entries[noteId]) {
      // URL 预选的笔记不在已发布列表中：仍允许操作，但如实标注
      entries[noteId] = "当前笔记（不在已发布列表）";
    }
    return entries;
  }, [notes, noteId]);

  const currentNote = notes.find((note) => note.id === noteId) ?? null;
  const historyRows = history?.items ?? [];
  const trends = readTrends(active?.benchmark ?? null);
  const dataGaps = active?.llm_meta?.data_gaps ?? [];

  return (
    <>
      {/* ---------------- 顶部：笔记选择 + 类型 + 生成 ---------------- */}
      <Card>
        <CardContent className="flex flex-wrap items-end gap-4 pt-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="dg-note">选择笔记</Label>
            <Select
              items={noteItems}
              value={noteId || NOTE_NONE}
              onValueChange={(value) => {
                const next = String(value);
                chooseNote(next === NOTE_NONE ? "" : next);
              }}
            >
              <SelectTrigger id="dg-note" className="w-96">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NOTE_NONE}>请选择笔记…</SelectItem>
                {notes.map((note) => {
                  const short =
                    note.title.length > 28 ? `${note.title.slice(0, 28)}…` : note.title;
                  return (
                    <SelectItem key={note.id} value={note.id}>
                      {short}（已发布）
                    </SelectItem>
                  );
                })}
                {noteId && !notes.some((note) => note.id === noteId) ? (
                  <SelectItem value={noteId}>当前笔记（不在已发布列表）</SelectItem>
                ) : null}
              </SelectContent>
            </Select>
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="dg-type">诊断类型</Label>
            <Select
              items={{
                [TYPE_AUTO]: "自动判定（推荐）",
                cold_start: "冷启动（数据不足）",
                review: "复盘（常规）",
                anomaly: "异常（明显负增长）",
              }}
              value={generateType}
              onValueChange={(value) => setGenerateType(String(value))}
            >
              <SelectTrigger id="dg-type" className="w-56">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={TYPE_AUTO}>自动判定（推荐）</SelectItem>
                <SelectItem value="cold_start">冷启动（数据不足）</SelectItem>
                <SelectItem value="review">复盘（常规）</SelectItem>
                <SelectItem value="anomaly">异常（明显负增长）</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <Button type="button" disabled={generating || !noteId} onClick={() => void onGenerate()}>
            {generating ? "诊断中……（最长约 2 分钟）" : "生成诊断"}
          </Button>

          <div className="flex flex-col gap-1 text-sm text-muted-foreground">
            {notesLoading ? (
              <span>加载笔记列表…</span>
            ) : notesError ? (
              <span className="text-red-600">笔记列表加载失败：{notesError.message}</span>
            ) : noteId ? (
              <span>
                当前笔记：{currentNote ? currentNote.title : "（不在已发布列表）"}；已有诊断{" "}
                {history?.items.length ?? 0} 条
              </span>
            ) : (
              <span className="text-amber-700">
                请先选择一篇「已发布」的笔记；选择器只列出已发布作品
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* ---------------- 生成状态 ---------------- */}
      <Card>
        <CardContent className="flex flex-col gap-2 pt-4">
          {formError ? <p className="text-sm text-red-600">{formError}</p> : null}
          <Notice text={notice} />
          {genError ? (
            <div className="flex flex-col gap-1">
              <ErrorText error={genError} />
              {genError.status === 400 ? (
                <p className="text-xs text-amber-700">
                  提示：400 通常表示后端未就绪（未开 LLM 开关 / 未填 key / 模型名仍是占位符）或
                  连续结构校验超限；可稍后重试或在 backend/.env 检查配置。
                </p>
              ) : null}
            </div>
          ) : null}
        </CardContent>
      </Card>

      {/* ---------------- 结果区 ---------------- */}
      {active ? (
        <>
          {/* ① 数据事实（程序计算） + ② 评分 */}
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader>
                <CardTitle>数据事实（由程序计算，非模型生成）</CardTitle>
                <p className="text-sm text-muted-foreground">
                  最新值取时间线中每个指标的最后一个非空值；互动率按**同一条快照**配对计算；
                  环比与增长基于快照序列本地计算。缺失一律显示「—」，绝不臆造。
                </p>
              </CardHeader>
              <CardContent className="flex flex-col gap-4">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>指标</TableHead>
                      <TableHead className="text-right">最新值</TableHead>
                      <TableHead className="text-right">总增量</TableHead>
                      <TableHead className="text-right">增长率</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {METRIC_COLUMNS.map((item) => (
                      <TableRow key={item.key}>
                        <TableCell>{item.label}</TableCell>
                        <TableCell className="text-right">
                          {formatNumber(active.benchmark?.latest_metrics?.[item.key])}
                        </TableCell>
                        <TableCell className="text-right">
                          {formatDelta(trends.totalDeltas[item.key])}
                        </TableCell>
                        <TableCell className="text-right">
                          {formatPercentSigned(trends.growthRates[item.key])}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>

                <div>
                  <p className="mb-2 text-sm font-medium">互动率（同快照配对）</p>
                  <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-3">
                    {RATE_COLUMNS.map((item) => (
                      <div key={item.key} className="flex items-center justify-between gap-2">
                        <span className="text-muted-foreground">{item.label}</span>
                        <span>{formatPercent(active.benchmark?.rates?.[item.key])}</span>
                      </div>
                    ))}
                  </div>
                </div>

                <p className="text-sm text-muted-foreground">
                  数据覆盖时长：
                  {trends.coverageHours === null ? "—（无快照或缺少发布时间）" : `约 ${trends.coverageHours} 小时`}
                  ；依据快照：
                  {active.based_on_metric_id ? "最新一条（based_on_metric_id 已记录）" : "无（该笔记暂无快照）"}
                </p>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>综合评分</CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                {active.score === null ? (
                  <p className="text-lg font-medium text-amber-700">数据不足，未评分</p>
                ) : (
                  <p className="text-3xl font-medium">
                    {formatScore(active.score)}
                    <span className="ml-1 text-base text-muted-foreground">/ 100</span>
                  </p>
                )}
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  {active.diagnosis_type ? (
                    <Badge className={TYPE_META[active.diagnosis_type].cls}>
                      {TYPE_META[active.diagnosis_type].text}
                    </Badge>
                  ) : null}
                  <span className="text-muted-foreground">
                    判定来源：{sourceText(active.llm_meta?.diagnosis_type_source)}
                  </span>
                </div>
                <p className="text-xs text-muted-foreground">
                  评分口径：仅依据该笔记自身数据的表现健康度（0~100）；没有足够数据时不评分，
                  这是**如实声明**而非失败。
                </p>
              </CardContent>
            </Card>
          </div>

          {/* ③ 亮点 / 问题 / 原因 / 建议 */}
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>亮点</CardTitle>
              </CardHeader>
              <CardContent>
                {active.strengths.length === 0 ? (
                  <p className="text-sm text-muted-foreground">未给出（可能数据不足以判断）</p>
                ) : (
                  <ul className="list-inside list-disc text-sm">
                    {active.strengths.map((text) => (
                      <li key={text}>{text}</li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>问题</CardTitle>
              </CardHeader>
              <CardContent>
                {active.weaknesses.length === 0 ? (
                  <p className="text-sm text-muted-foreground">未给出（可能数据不足以判断）</p>
                ) : (
                  <ul className="list-inside list-disc text-sm">
                    {active.weaknesses.map((text) => (
                      <li key={text}>{text}</li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>原因分析</CardTitle>
              </CardHeader>
              <CardContent>
                {active.reasons.length === 0 ? (
                  <p className="text-sm text-muted-foreground">未给出</p>
                ) : (
                  <ul className="list-inside list-disc text-sm">
                    {active.reasons.map((text) => (
                      <li key={text}>{text}</li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>建议（按重要性排序，可逐条复制）</CardTitle>
              </CardHeader>
              <CardContent>
                {active.suggestions.length === 0 ? (
                  <p className="text-sm text-muted-foreground">未给出</p>
                ) : (
                  <ol className="flex flex-col gap-2 text-sm">
                    {active.suggestions.map((text, index) => (
                      <li key={text} className="flex items-start gap-2">
                        <span className="mt-0.5 text-muted-foreground">{index + 1}.</span>
                        <span className="flex-1">{text}</span>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() => void onCopySuggestion(index, text)}
                        >
                          {copiedIndex === index ? "已复制" : "复制"}
                        </Button>
                      </li>
                    ))}
                  </ol>
                )}
              </CardContent>
            </Card>
          </div>

          {/* ④⑤ 基准与数据缺口 + 元信息 */}
          <Card>
            <CardHeader>
              <CardTitle>基准与数据缺口</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <div className="rounded-lg border border-amber-600/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-800">
                <p className="font-medium">
                  {active.benchmark?.benchmark_note ??
                    "本期无同类/历史爆款基准，仅基于该笔记自身数据。"}
                </p>
                <p className="mt-1 text-xs">
                  外部基准：无（external_benchmark = null，绝不杜撰行业均值 / 爆款阈值；
                  跨篇基准属第 18 步 RAG）。基准口径：
                  {active.benchmark?.basis === "self_only" ? "仅该笔记自身（self_only）" : "—"}。
                </p>
              </div>
              {dataGaps.length > 0 ? (
                <div>
                  <p className="mb-1 text-sm font-medium">数据缺口（如实声明）：</p>
                  <ul className="list-inside list-disc text-sm text-muted-foreground">
                    {dataGaps.map((text) => (
                      <li key={text}>{text}</li>
                    ))}
                  </ul>
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">未发现数据缺口。</p>
              )}
              <p className="text-xs text-muted-foreground">
                生成时间：{formatDateTime(active.created_at)}；模型：{active.llm_model ?? "—"}；
                提示词版本：{active.llm_meta?.prompt_version ?? "—"}；JSON 解析尝试：
                {active.llm_meta?.json_parse_attempts ?? "—"} 次
              </p>
            </CardContent>
          </Card>
        </>
      ) : null}

      {/* ---------------- 底部：诊断历史 ---------------- */}
      <Card>
        <CardHeader>
          <CardTitle>诊断历史</CardTitle>
          <p className="text-sm text-muted-foreground">
            诊断历史只增不改（不提供编辑 / 删除）；点「查看」拉取单条详情并展示在上方结果区。
          </p>
        </CardHeader>
        <CardContent>
          {detailError ? (
            <div className="mb-3">
              <ErrorText error={detailError} />
            </div>
          ) : null}
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>生成时间</TableHead>
                <TableHead>类型</TableHead>
                <TableHead className="text-right">评分</TableHead>
                <TableHead>判定来源</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {!noteId ? (
                <TableRow>
                  <TableCell colSpan={5} className="py-6 text-center text-muted-foreground">
                    请先在上方选择一篇笔记
                  </TableCell>
                </TableRow>
              ) : historyLoading ? (
                <TableRow>
                  <TableCell colSpan={5} className="py-6 text-center text-muted-foreground">
                    加载中……
                  </TableCell>
                </TableRow>
              ) : historyError ? (
                <TableRow>
                  <TableCell colSpan={5} className="py-6 text-center">
                    <ErrorText error={historyError} />
                  </TableCell>
                </TableRow>
              ) : historyRows.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={5} className="py-6 text-center text-muted-foreground">
                    暂无诊断（点上方「生成诊断」生成第一条）
                  </TableCell>
                </TableRow>
              ) : (
                historyRows.map((item) => {
                  const typeMeta = item.diagnosis_type
                    ? TYPE_META[item.diagnosis_type]
                    : null;
                  return (
                    <TableRow key={item.id}>
                      <TableCell className="text-muted-foreground">
                        {formatDateTime(item.created_at)}
                      </TableCell>
                      <TableCell>
                        {typeMeta ? (
                          <Badge className={typeMeta.cls}>{typeMeta.text}</Badge>
                        ) : (
                          "—"
                        )}
                      </TableCell>
                      <TableCell className="text-right">
                        {item.score === null ? "未评分" : formatScore(item.score)}
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        {sourceText(item.llm_meta?.diagnosis_type_source)}
                      </TableCell>
                      <TableCell className="text-right">
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          disabled={detailBusyId === item.id}
                          onClick={() => void openDetail(item.id)}
                        >
                          {detailBusyId === item.id ? "加载中……" : "查看"}
                        </Button>
                      </TableCell>
                    </TableRow>
                  );
                })
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </>
  );
}

export default function DiagnosticsPage() {
  return (
    <main className="mx-auto flex w-full max-w-7xl flex-1 flex-col gap-6 px-6 py-10">
      <header className="flex flex-col gap-2">
        <h1 className="text-2xl font-medium tracking-tight">单篇诊断</h1>
        <p className="text-sm text-muted-foreground">
          第 16 步：对一篇已发布笔记做「只读 + 建议」诊断 —— 程序先把最新指标 / 互动率 /
          环比 / 数据缺口算清楚，再由模型给出亮点 / 问题 / 原因 / 可执行建议。
          <strong>本期无外部基准</strong>（仅基于该笔记自身数据，跨篇基准见第 18 步）；
          数据不足会如实声明、不硬打分。不修改任何笔记数据、不自动发布。
        </p>
      </header>

      <Suspense fallback={<p className="text-sm text-muted-foreground">加载中……</p>}>
        <DiagnosticsInner />
      </Suspense>
    </main>
  );
}
