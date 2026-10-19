"use client";

/**
 * 「数据回填」页面（第 15 步）。
 *
 * 全栈链路：本页（浏览器）-> 本站 /api/v1/**（Route Handler 服务端代理）-> FastAPI。
 *
 * - 顶部：笔记选择器（GET /api/v1/notes?status=published 拉**已发布**笔记；
 *   支持 URL ?note_id= 预选）；
 * - 中部：两种录入方式（Tabs）——①手工录入（采集时刻 + 六指标数字输入，可留空）；
 *   ②截图识别（上传 png/jpeg/webp，POST /metrics/recognize 返回**候选**并回填同一
 *   可编辑表单，提示条列出缺失 / 不确定字段）；
 *   两种方式最终都走 POST /api/v1/metrics 落库（同笔记同采集时刻冲突时 409，
 *   页面提示改「采集时刻」）；
 * - 底部：数据时间线（GET /api/v1/metrics?note_id=，采集时刻倒序表格）。
 *
 * 红线：识别结果**必须人工核对**后才落库（识别接口不写库）；不自动发布、不登录
 * 小红书、不保存账号密码；截图仅本机流转（默认不落盘）。
 * 本步不做趋势图（第 17 步）与单篇诊断（第 16 步）。
 *
 * ⚠️ Next 16：`useSearchParams()` 必须包在 `<Suspense>` 中（否则 build 失败），
 * 故把用到它的部分拆成子组件 `MetricsInner`，外层用 `<Suspense>` 包裹。
 */

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";

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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  ErrorText,
  Field,
  Notice,
  formatDateTime,
  formatNumber,
  toApiError,
} from "@/components/shared";
import { ApiError, apiClient, toQuery } from "@/lib/api-client";
import type {
  MetricDataSource,
  MetricRecognizeResult,
  MetricSnapshotCreate,
  MetricSnapshotList,
  NoteListItem,
  Page,
} from "@/lib/types";

/** 笔记选择器里「未选择」的占位值（与封面工作室同款约定） */
const NOTE_NONE = "__none__";

/** 六个指标的输入框定义（顺序即展示顺序，与后端 METRIC_FIELDS 一致） */
type MetricKey = "views" | "likes" | "collects" | "comments" | "shares" | "new_followers";

const METRIC_INPUTS: { key: MetricKey; label: string; hint?: string }[] = [
  { key: "views", label: "浏览量", hint: "小红书里的「小眼睛」；没有数据就留空" },
  { key: "likes", label: "点赞" },
  { key: "collects", label: "收藏" },
  { key: "comments", label: "评论" },
  { key: "shares", label: "分享" },
  { key: "new_followers", label: "新增粉丝", hint: "截图上的「涨粉」" },
];

const FIELD_LABELS: Record<MetricKey, string> = {
  views: "浏览量",
  likes: "点赞",
  collects: "收藏",
  comments: "评论",
  shares: "分享",
  new_followers: "新增粉丝",
};

const EMPTY_FORM: Record<MetricKey, string> = {
  views: "",
  likes: "",
  collects: "",
  comments: "",
  shares: "",
  new_followers: "",
};

const SOURCE_META: Record<MetricDataSource, { text: string; cls: string }> = {
  screenshot: { text: "截图识别", cls: "border-sky-600/30 bg-sky-500/15 text-sky-700" },
  manual: { text: "手工录入", cls: "border-border bg-muted text-muted-foreground" },
};

/** Date -> `datetime-local` 输入值（本地时区，YYYY-MM-DDTHH:mm） */
function toLocalInputValue(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

/** `datetime-local` 值（本地时间）-> ISO 8601（UTC 带 Z）；非法返回 null */
function localInputToIso(value: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString();
}

/** 数字输入框值 -> 非负整数 / null（留空）/ 非法（带错误文案） */
function parseCount(raw: string): { value: number | null; error?: string } {
  const text = raw.trim();
  if (!text) return { value: null };
  if (!/^\d+$/.test(text)) {
    return { value: null, error: "只能填非负整数（留空表示未获取，不臆造）" };
  }
  return { value: Number(text) };
}

/** 识别结果里的数值 -> 输入框字符串（null -> 空） */
function countToInput(value: number | null): string {
  return value === null || value === undefined ? "" : String(value);
}

function MetricsInner() {
  const searchParams = useSearchParams();
  const urlNoteId = searchParams.get("note_id");

  /* ------------------------------------------------------------ 笔记列表 */
  const [notes, setNotes] = useState<NoteListItem[]>([]);
  const [notesLoading, setNotesLoading] = useState(true);
  const [notesError, setNotesError] = useState<ApiError | null>(null);

  /* ------------------------------------------------------------ 当前笔记 */
  const [noteId, setNoteId] = useState(urlNoteId ?? "");

  /* ------------------------------------------------------------ 录入表单 */
  const [mode, setMode] = useState("manual");
  const [capturedAt, setCapturedAt] = useState(() => toLocalInputValue(new Date()));
  const [form, setForm] = useState<Record<MetricKey, string>>({ ...EMPTY_FORM });
  const [evidence, setEvidence] = useState<Record<string, unknown> | null>(null);
  const [saveScreenshot, setSaveScreenshot] = useState(false);

  /* ------------------------------------------------------------ 识别状态 */
  const [recognizeBusy, setRecognizeBusy] = useState(false);
  const [recognizeError, setRecognizeError] = useState<ApiError | null>(null);
  const [recognizeInfo, setRecognizeInfo] = useState<MetricRecognizeResult | null>(null);

  /* ------------------------------------------------------------ 提交状态 */
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState("");
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const [notice, setNotice] = useState("");

  /* ------------------------------------------------------------ 时间线 */
  const [timeline, setTimeline] = useState<MetricSnapshotList | null>(null);
  const [timelineLoading, setTimelineLoading] = useState(Boolean(urlNoteId));
  const [timelineError, setTimelineError] = useState<ApiError | null>(null);
  const [timelineToken, setTimelineToken] = useState(0);

  // 笔记列表（挂载时一次；**只展示已发布** —— 数据回填针对已发布作品）
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

  // 数据时间线（选择笔记后拉取；token 变化时强制重拉）
  useEffect(() => {
    if (!noteId) return;
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<MetricSnapshotList>(
          `/api/v1/metrics${toQuery({ note_id: noteId })}`,
        );
        if (!cancelled) {
          setTimeline(data);
          setTimelineError(null);
        }
      } catch (err) {
        if (!cancelled) setTimelineError(toApiError(err));
      } finally {
        if (!cancelled) setTimelineLoading(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [noteId, timelineToken]);

  const chooseNote = useCallback((next: string) => {
    setNoteId(next);
    setTimeline(null);
    setTimelineError(null);
    setTimelineLoading(Boolean(next));
    setTimelineToken((token) => token + 1);
    setNotice("");
    setActionError(null);
    setRecognizeError(null);
    setRecognizeInfo(null);
  }, []);

  const reloadTimeline = useCallback(() => {
    setTimelineLoading(true);
    setTimelineToken((token) => token + 1);
  }, []);

  /* ------------------------------------------------------------ 识别上传 */
  const onPickFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = ""; // 允许连续选择同一文件重试
    if (!file) return;

    setRecognizeBusy(true);
    setRecognizeError(null);
    setRecognizeInfo(null);
    setActionError(null);
    setNotice("");
    try {
      const formData = new FormData();
      formData.append("file", file);
      if (noteId) formData.append("note_id", noteId);
      if (saveScreenshot) formData.append("save_screenshot", "true");
      const result = await apiClient.postForm<MetricRecognizeResult>(
        "/api/v1/metrics/recognize",
        formData,
      );
      setForm({
        views: countToInput(result.metrics.views),
        likes: countToInput(result.metrics.likes),
        collects: countToInput(result.metrics.collects),
        comments: countToInput(result.metrics.comments),
        shares: countToInput(result.metrics.shares),
        new_followers: countToInput(result.metrics.new_followers),
      });
      const recognizedAt = new Date(result.captured_at);
      if (!Number.isNaN(recognizedAt.getTime())) {
        setCapturedAt(toLocalInputValue(recognizedAt));
      }
      setEvidence(result.evidence);
      setRecognizeInfo(result);
      setNotice(
        `识别完成：取到 ${6 - result.missing_fields.length}/6 个指标。请人工核对下方表单后再保存。`,
      );
    } catch (err) {
      setRecognizeError(toApiError(err));
    } finally {
      setRecognizeBusy(false);
    }
  };

  /* ------------------------------------------------------------ 提交落库 */
  const onSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setActionError(null);
    setNotice("");
    setFormError("");

    if (!noteId) {
      setFormError("请先选择要回填数据的笔记");
      return;
    }
    const iso = localInputToIso(capturedAt);
    if (!iso) {
      setFormError("请填写有效的采集时刻");
      return;
    }

    const payload: MetricSnapshotCreate = {
      note_id: noteId,
      captured_at: iso,
      data_source: mode === "screenshot" ? "screenshot" : "manual",
      evidence: mode === "screenshot" ? evidence : null,
    };
    for (const item of METRIC_INPUTS) {
      const parsed = parseCount(form[item.key]);
      if (parsed.error) {
        setFormError(`${item.label}：${parsed.error}`);
        return;
      }
      payload[item.key] = parsed.value;
    }

    setSaving(true);
    try {
      await apiClient.post("/api/v1/metrics", payload);
      setNotice("已保存一条数据快照");
      setForm({ ...EMPTY_FORM });
      setEvidence(null);
      setRecognizeInfo(null);
      setCapturedAt(toLocalInputValue(new Date()));
      reloadTimeline();
    } catch (err) {
      setActionError(toApiError(err));
    } finally {
      setSaving(false);
    }
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

  const missingLabels = (recognizeInfo?.missing_fields ?? []).map(
    (key) => FIELD_LABELS[key as MetricKey] ?? key,
  );
  const uncertainLabels = (recognizeInfo?.uncertain_fields ?? []).map(
    (key) => FIELD_LABELS[key as MetricKey] ?? key,
  );

  const rows = timeline?.items ?? [];

  return (
    <>
      {/* ---------------- 顶部：笔记选择器 ---------------- */}
      <Card>
        <CardContent className="flex flex-wrap items-end gap-4 pt-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="mt-note">选择笔记</Label>
            <Select
              items={noteItems}
              value={noteId || NOTE_NONE}
              onValueChange={(value) => {
                const next = String(value);
                chooseNote(next === NOTE_NONE ? "" : next);
              }}
            >
              <SelectTrigger id="mt-note" className="w-96">
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
          <div className="flex flex-col gap-1 text-sm text-muted-foreground">
            {notesLoading ? (
              <span>加载笔记列表…</span>
            ) : notesError ? (
              <span className="text-red-600">笔记列表加载失败：{notesError.message}</span>
            ) : noteId ? (
              <span>
                当前笔记：{currentNote ? currentNote.title : "（不在已发布列表）"}；已有快照{" "}
                {timeline?.items.length ?? 0} 条
              </span>
            ) : (
              <span className="text-amber-700">
                请先选择一篇「已发布」的笔记；选择器只列出已发布作品
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* ---------------- 中部：新增快照 ---------------- */}
      <Card>
        <CardHeader>
          <CardTitle>新增数据快照</CardTitle>
          <p className="text-sm text-muted-foreground">
            两种方式最终都保存为同一条快照：手工录入，或上传截图让视觉模型识别**候选值**后再人工核对。
            识别结果绝不自动落库。
          </p>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <Tabs
            value={mode}
            onValueChange={(value) => {
              setMode(String(value));
              setFormError("");
            }}
          >
            <TabsList>
              <TabsTrigger value="manual">手工录入</TabsTrigger>
              <TabsTrigger value="screenshot">截图识别</TabsTrigger>
            </TabsList>

            <TabsContent value="manual" className="pt-2">
              <p className="text-sm text-muted-foreground">
                直接从创作中心的数据面板抄录六个指标；取不到的留空（留空即未获取，不会臆造）。
              </p>
            </TabsContent>

            <TabsContent value="screenshot" className="flex flex-col gap-3 pt-2">
              <div className="flex flex-wrap items-center gap-3">
                <input
                  id="mt-file"
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  className="hidden"
                  onChange={(event) => void onPickFile(event)}
                />
                <Button
                  type="button"
                  variant="outline"
                  disabled={recognizeBusy}
                  onClick={() => document.getElementById("mt-file")?.click()}
                >
                  {recognizeBusy ? "识别中……（最长约 2 分钟）" : "选择截图并识别"}
                </Button>
                <label className="flex items-center gap-1.5 text-sm text-muted-foreground">
                  <input
                    type="checkbox"
                    className="size-4 accent-current"
                    checked={saveScreenshot}
                    onChange={(event) => setSaveScreenshot(event.target.checked)}
                  />
                  识别成功后把截图留档到服务端（需后端 SAVE_SCREENSHOT=true；默认不保存）
                </label>
              </div>
              <p className="text-xs text-muted-foreground">
                支持 PNG / JPEG / WebP（默认上限 4 MB）；截图仅本机流转，不向任何小红书域名发起请求。
              </p>
              {recognizeError ? <ErrorText error={recognizeError} /> : null}
              {recognizeInfo ? (
                <div className="rounded-lg border border-amber-600/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-800">
                  <p className="font-medium">识别结果已回填到下方表单，请对照截图人工核对后再保存：</p>
                  <ul className="mt-1 list-inside list-disc text-xs">
                    <li>
                      取到 {6 - recognizeInfo.missing_fields.length}/6 个指标；JSON 解析尝试{" "}
                      {recognizeInfo.parse_attempts} 次
                    </li>
                    {missingLabels.length > 0 ? (
                      <li>未能识别：{missingLabels.join("、")}（将按空值保存，可手工补填）</li>
                    ) : null}
                    {uncertainLabels.length > 0 ? (
                      <li>不确信：{uncertainLabels.join("、")}（请重点核对）</li>
                    ) : null}
                    {recognizeInfo.note_title ? <li>关联笔记：{recognizeInfo.note_title}</li> : null}
                    {recognizeInfo.screenshot_file ? (
                      <li>截图已留档：{recognizeInfo.screenshot_file}</li>
                    ) : null}
                    {recognizeInfo.warnings.map((text) => (
                      <li key={text}>{text}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </TabsContent>
          </Tabs>

          <form className="flex flex-col gap-4" onSubmit={(event) => void onSubmit(event)}>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <Field
                label="采集时刻"
                htmlFor="mt-captured"
                hint="同笔记同一时刻只允许一条快照"
              >
                <Input
                  id="mt-captured"
                  type="datetime-local"
                  value={capturedAt}
                  onChange={(event) => setCapturedAt(event.target.value)}
                />
              </Field>
              {METRIC_INPUTS.map((item) => (
                <Field key={item.key} label={item.label} htmlFor={`mt-${item.key}`} hint={item.hint}>
                  <Input
                    id={`mt-${item.key}`}
                    type="number"
                    min={0}
                    step={1}
                    inputMode="numeric"
                    placeholder="留空 = 未获取"
                    value={form[item.key]}
                    onChange={(event) =>
                      setForm((prev) => ({ ...prev, [item.key]: event.target.value }))
                    }
                  />
                </Field>
              ))}
            </div>

            {formError ? <p className="text-sm text-red-600">{formError}</p> : null}
            <Notice text={notice} />
            {actionError ? (
              <div className="flex flex-col gap-1">
                <ErrorText error={actionError} />
                {actionError.status === 409 ? (
                  <p className="text-xs text-amber-700">
                    提示：把上面的「采集时刻」改成与已有快照不同的时间（同笔记同一时刻只允许一条），
                    再点保存。
                  </p>
                ) : null}
              </div>
            ) : null}

            <div className="flex items-center gap-3">
              <Button type="submit" disabled={saving || !noteId}>
                {saving ? "保存中……" : "保存快照"}
              </Button>
              <span className="text-xs text-muted-foreground">
                来源将记为：{mode === "screenshot" ? "截图识别" : "手工录入"}
              </span>
            </div>
          </form>
        </CardContent>
      </Card>

      {/* ---------------- 底部：数据时间线 ---------------- */}
      <Card>
        <CardHeader>
          <CardTitle>数据时间线</CardTitle>
          <p className="text-sm text-muted-foreground">
            按采集时刻倒序；趋势图属第 17 步，本页只做时间线表格。
          </p>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>采集时刻</TableHead>
                <TableHead>来源</TableHead>
                <TableHead className="text-right">浏览量</TableHead>
                <TableHead className="text-right">点赞</TableHead>
                <TableHead className="text-right">收藏</TableHead>
                <TableHead className="text-right">评论</TableHead>
                <TableHead className="text-right">分享</TableHead>
                <TableHead className="text-right">新增粉丝</TableHead>
                <TableHead>截图留档</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {!noteId ? (
                <TableRow>
                  <TableCell colSpan={9} className="py-6 text-center text-muted-foreground">
                    请先在上方选择一篇笔记
                  </TableCell>
                </TableRow>
              ) : timelineLoading ? (
                <TableRow>
                  <TableCell colSpan={9} className="py-6 text-center text-muted-foreground">
                    加载中……
                  </TableCell>
                </TableRow>
              ) : timelineError ? (
                <TableRow>
                  <TableCell colSpan={9} className="py-6 text-center">
                    <ErrorText error={timelineError} />
                  </TableCell>
                </TableRow>
              ) : rows.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={9} className="py-6 text-center text-muted-foreground">
                    暂无快照（发布后用上方表单录入第一条数据）
                  </TableCell>
                </TableRow>
              ) : (
                rows.map((snapshot) => {
                  const sourceMeta = SOURCE_META[snapshot.data_source] ?? SOURCE_META.manual;
                  return (
                    <TableRow key={snapshot.id}>
                      <TableCell className="text-muted-foreground">
                        {formatDateTime(snapshot.captured_at)}
                      </TableCell>
                      <TableCell>
                        <Badge className={sourceMeta.cls}>{sourceMeta.text}</Badge>
                      </TableCell>
                      <TableCell className="text-right">{formatNumber(snapshot.views)}</TableCell>
                      <TableCell className="text-right">{formatNumber(snapshot.likes)}</TableCell>
                      <TableCell className="text-right">{formatNumber(snapshot.collects)}</TableCell>
                      <TableCell className="text-right">{formatNumber(snapshot.comments)}</TableCell>
                      <TableCell className="text-right">{formatNumber(snapshot.shares)}</TableCell>
                      <TableCell className="text-right">
                        {formatNumber(snapshot.new_followers)}
                      </TableCell>
                      <TableCell className="max-w-44 truncate text-xs text-muted-foreground">
                        {snapshot.screenshot_path ?? "—"}
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

export default function MetricsPage() {
  return (
    <main className="mx-auto flex w-full max-w-7xl flex-1 flex-col gap-6 px-6 py-10">
      <header className="flex flex-col gap-2">
        <h1 className="text-2xl font-medium tracking-tight">数据回填</h1>
        <p className="text-sm text-muted-foreground">
          第 15 步：把已发布笔记的运营数据（浏览 / 点赞 / 收藏 / 评论 / 分享 / 涨粉）沉淀为快照 ——
          手工录入或上传创作中心截图由视觉模型识别候选值，
          <strong>人工核对后</strong>才落库。不登录小红书、不抓取后台、不自动发布；截图仅本机处理。
        </p>
      </header>

      <Suspense fallback={<p className="text-sm text-muted-foreground">加载中……</p>}>
        <MetricsInner />
      </Suspense>
    </main>
  );
}
