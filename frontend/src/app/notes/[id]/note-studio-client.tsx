"use client";

/**
 * 「创作台」页面（第 14 步）：单篇笔记的人工编辑 + 复制搬运 + 手动发布闭环。
 *
 * 全栈链路：本页（浏览器）-> 本站 /api/v1/**（Route Handler 服务端代理）-> FastAPI。
 *
 * - 编辑区：title / body / tags「保存修改」走 PATCH（仅编辑字段，不触碰状态机）；
 *   展示违禁词自检命中（仅提示、不阻断）；正文 Textarea 原样换行；
 * - 预览与封面：轻量图文预览（`whitespace-pre-wrap` 原样换行，**不引入 markdown 库**）
 *   + 当前选中封面只读缩略图（实时渲染，跳转 `/covers?note_id=` 去设计 / 下载 PNG）；
 * - 发布操作区：复制标题 / 正文 / 标签（`navigator.clipboard`，失败兜底提示手动复制）、
 *   前往小红书发布页外链（仅导航，**绝不 fetch / 携带内容**）、状态机按钮
 *   （定稿 / 退回 / 标记已发布（可回填链接与 id）/ 撤销发布标记）。
 *
 * ⚠️ 本文件是 **Client Component**。`/notes/[id]` 无法穷举预生成参数，
 * blocking route（`export const instant = false`）由同目录的 `page.tsx`（Server 壳）声明
 * —— route segment config 只能由 Server Component 导出，以此允许 `useParams()`
 * 与全局 SiteHeader 的 `usePathname()` 在请求时求值（否则 build 报
 * `blocking-prerender-client-hook`）。
 *
 * ⚠️ 红线：本页不自动发布、不登录小红书、不保存账号密码 / Cookie；
 * 发布动作完全由人完成，系统只记录「我已在小红书发布」这一事实。
 * 下方 `XHS_PUBLISH_URL` 是**全前端唯一**被允许出现的小红书地址（导航外链）。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";

import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  ConfirmDialog,
  ErrorText,
  Field,
  Notice,
  TagInput,
  formatDateTime,
  toApiError,
} from "@/components/shared";
import { ApiError, apiClient, toQuery } from "@/lib/api-client";
import { COVER_HEIGHT, COVER_WIDTH } from "@/lib/cover/constants";
import { CoverCanvas } from "@/lib/cover/templates";
import type { CoverDraft } from "@/lib/cover/templates";
import type {
  ComplianceStatus,
  Cover,
  CoverList,
  NoteOut,
  NoteStatus,
  NoteUpdateRequest,
} from "@/lib/types";
import { cn } from "@/lib/utils";

// 复用第 13 步「封面工作室」的渲染样式（CoverCanvas 的 DOM 依赖 .cover 系列类）
import "../../covers/covers.css";

/** 唯一被允许出现的小红书地址：官方发布页，仅作导航外链（红线：绝不 fetch / 携带内容 / 后端使用） */
const XHS_PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish";

const STATUS_META: Record<NoteStatus, { text: string; cls: string }> = {
  draft: { text: "草稿", cls: "border-sky-600/30 bg-sky-500/15 text-sky-700" },
  ready: { text: "待发布", cls: "border-amber-600/30 bg-amber-500/15 text-amber-700" },
  published: { text: "已发布", cls: "border-emerald-600/30 bg-emerald-500/15 text-emerald-700" },
  archived: { text: "已归档", cls: "border-border bg-muted text-muted-foreground" },
};

const COMPLIANCE_META: Record<ComplianceStatus, { text: string; cls: string }> = {
  pending: { text: "待检", cls: "border-border bg-muted text-muted-foreground" },
  passed: { text: "合规", cls: "border-emerald-600/30 bg-emerald-500/15 text-emerald-700" },
  warning: { text: "有风险", cls: "border-red-600/30 bg-red-500/15 text-red-700" },
};

const FIELD_LABELS: Record<string, string> = { title: "标题", body: "正文", tags: "标签" };

/* ------------------------------------------------------------ 封面缩略图 */

const COVER_THUMB_WIDTH = 150;
const COVER_THUMB_HEIGHT = Math.round((COVER_THUMB_WIDTH * COVER_HEIGHT) / COVER_WIDTH);

/** 从封面候选还原渲染草稿（背景图不入库，恒为 null；与封面工作室同口径） */
function coverDraftOf(cover: Cover): CoverDraft {
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

/** 当前选中封面（只读缩略图）：按 config_json 实时小尺寸渲染（不存图片） */
function SelectedCoverThumb({ cover }: { cover: Cover }) {
  const draft = coverDraftOf(cover);
  const scale = COVER_THUMB_WIDTH / COVER_WIDTH;
  return (
    <div
      className="cover-thumb relative shrink-0"
      style={{ width: COVER_THUMB_WIDTH, height: COVER_THUMB_HEIGHT }}
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

/* ------------------------------------------------------------ 页面 */

interface EditForm {
  title: string;
  body: string;
  tags: string[];
}

function NoteStudioClient() {
  const params = useParams<{ id: string }>();
  const noteId = params?.id ?? "";

  /* ------------------------------------------------------ 详情与封面 */
  const [note, setNote] = useState<NoteOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<ApiError | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [covers, setCovers] = useState<CoverList | null>(null);

  /* ------------------------------------------------------ 编辑表单 */
  const [form, setForm] = useState<EditForm>({ title: "", body: "", tags: [] });
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [editError, setEditError] = useState<ApiError | null>(null);

  /* ------------------------------------------------------ 操作反馈 */
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  /* ------------------------------------------------------ 发布 Dialog */
  const [publishOpen, setPublishOpen] = useState(false);
  const [publishUrl, setPublishUrl] = useState("");
  const [publishNoteId, setPublishNoteId] = useState("");
  const [publishError, setPublishError] = useState<ApiError | null>(null);

  /* ------------------------------------------------------ 撤销确认 */
  const [revertOpen, setRevertOpen] = useState(false);

  // 详情加载（初次挂载 + 每次写操作成功后的重新拉取）
  useEffect(() => {
    if (!noteId) return;
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<NoteOut>(
          `/api/v1/notes/${encodeURIComponent(noteId)}`,
        );
        if (cancelled) return;
        setNote(data);
        setForm({
          title: data.title,
          body: data.body ?? "",
          tags: data.tags ?? [],
        });
        setLoadError(null);
      } catch (err) {
        if (!cancelled) setLoadError(toApiError(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [noteId, reloadToken]);

  // 当前选中封面（只读展示；失败不阻塞主流程）
  useEffect(() => {
    if (!noteId) return;
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<CoverList>(
          `/api/v1/covers${toQuery({ note_id: noteId })}`,
        );
        if (!cancelled) setCovers(data);
      } catch {
        if (!cancelled) setCovers(null);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [noteId, reloadToken]);

  const reload = useCallback(() => {
    setReloadToken((token) => token + 1);
  }, []);

  const selectedCover = useMemo(() => {
    if (!covers || !covers.selected_id) return null;
    return covers.items.find((item) => item.id === covers.selected_id) ?? null;
  }, [covers]);

  const tagsText = useMemo(
    () => (note?.tags ?? []).map((tag) => `#${tag}`).join(" "),
    [note],
  );

  /* ------------------------------------------------------ 保存修改 */
  const saveEdit = async () => {
    if (!note) return;
    const errors: Record<string, string> = {};
    const title = form.title.trim();
    if (!title) errors.title = "标题不能为空";
    else if (title.length > 100) errors.title = "标题不超过 100 字";
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) return;

    setSaving(true);
    setEditError(null);
    setNotice("");
    try {
      const payload: NoteUpdateRequest = {
        title,
        body: form.body.trim() ? form.body : null,
        tags: form.tags.length > 0 ? form.tags : null,
      };
      await apiClient.patch<NoteOut>(`/api/v1/notes/${encodeURIComponent(note.id)}`, payload);
      setNotice("已保存修改");
      reload();
    } catch (err) {
      setEditError(toApiError(err));
    } finally {
      setSaving(false);
    }
  };

  /* ------------------------------------------------------ 状态流转 */
  const changeStatus = useCallback(
    async (payload: NoteUpdateRequest, okText: string): Promise<boolean> => {
      if (!note) return false;
      setBusy(true);
      setActionError(null);
      setNotice("");
      try {
        await apiClient.patch<NoteOut>(
          `/api/v1/notes/${encodeURIComponent(note.id)}`,
          payload,
        );
        setNotice(okText);
        reload();
        return true;
      } catch (err) {
        setActionError(toApiError(err));
        return false;
      } finally {
        setBusy(false);
      }
    },
    [note, reload],
  );

  const confirmPublish = async () => {
    if (!note) return;
    setBusy(true);
    setPublishError(null);
    setActionError(null);
    setNotice("");
    try {
      await apiClient.patch<NoteOut>(`/api/v1/notes/${encodeURIComponent(note.id)}`, {
        status: "published",
        publish_url: publishUrl.trim() || null,
        xhs_note_id: publishNoteId.trim() || null,
      } satisfies NoteUpdateRequest);
      setPublishOpen(false);
      setPublishUrl("");
      setPublishNoteId("");
      setNotice("已标记为「已发布」；来源选题同步为已发布");
      reload();
    } catch (err) {
      setPublishError(toApiError(err));
    } finally {
      setBusy(false);
    }
  };

  const confirmRevert = async () => {
    const ok = await changeStatus({ status: "ready" }, "已撤销发布标记（发布字段已清空）");
    if (ok) setRevertOpen(false);
  };

  /* ------------------------------------------------------ 复制搬运 */
  const copyText = async (text: string, label: string) => {
    setActionError(null);
    try {
      await navigator.clipboard.writeText(text);
      setNotice(`已复制${label}（可到小红书发布页粘贴）`);
    } catch {
      setNotice(`复制${label}失败：浏览器可能未授权剪贴板，请手动选中文本复制`);
    }
  };

  const statusMeta = note ? STATUS_META[note.status] ?? STATUS_META.draft : null;
  const complianceMeta = note
    ? COMPLIANCE_META[note.compliance_status] ?? COMPLIANCE_META.pending
    : null;

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-1 flex-col gap-6 px-6 py-10">
      <header className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-medium tracking-tight">创作台</h1>
          {statusMeta ? <Badge className={statusMeta.cls}>{statusMeta.text}</Badge> : null}
          {complianceMeta ? (
            <Badge className={complianceMeta.cls}>违禁词：{complianceMeta.text}</Badge>
          ) : null}
        </div>
        <p className="text-sm text-muted-foreground">
          第 14 步：人工编辑 → 复制搬运 → 到小红书人工发布 → 回填「我已发布」。
          系统不自动发布、不登录小红书、不保存账号密码。
        </p>
        <div className="flex flex-wrap items-center gap-4">
          <Link href="/notes" className="text-sm text-primary underline-offset-4 hover:underline">
            ← 返回笔记列表
          </Link>
          {note && note.status === "published" ? (
            <>
              <Link
                href={`/metrics?note_id=${note.id}`}
                className="text-sm text-primary underline-offset-4 hover:underline"
              >
                数据回填 →
              </Link>
              <Link
                href={`/diagnostics?note_id=${note.id}`}
                className="text-sm text-primary underline-offset-4 hover:underline"
              >
                单篇诊断 →
              </Link>
            </>
          ) : null}
        </div>
      </header>

      <ErrorText error={actionError} />
      <Notice text={notice} />

      {loading ? (
        <p className="text-sm text-muted-foreground">加载中……</p>
      ) : loadError ? (
        <ErrorText error={loadError} />
      ) : note ? (
        <div className="grid gap-6 lg:grid-cols-2 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)_minmax(0,0.85fr)]">
          {/* ================= 左 / 上 · 编辑区 ================= */}
          <Card className="self-start">
            <CardHeader>
              <CardTitle>编辑</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <Field
                label="标题 *"
                htmlFor="ns-title"
                error={fieldErrors.title}
                hint={`${form.title.length} / 100 字`}
              >
                <Input
                  id="ns-title"
                  value={form.title}
                  maxLength={100}
                  onChange={(event) => setForm({ ...form, title: event.target.value })}
                />
              </Field>
              <Field label="正文" htmlFor="ns-body" hint="原样换行；右侧预览即所见">
                <Textarea
                  id="ns-body"
                  value={form.body}
                  rows={16}
                  className="min-h-[300px]"
                  onChange={(event) => setForm({ ...form, body: event.target.value })}
                />
              </Field>
              <Field label="标签" hint="纯词、不含 #；复制时自动加 #">
                <TagInput
                  value={form.tags}
                  onChange={(next) => setForm({ ...form, tags: next })}
                  placeholder="输入后回车或点「添加」"
                  maxItems={10}
                  maxLength={50}
                />
              </Field>

              {note.content_check && note.content_check.hits.length > 0 ? (
                <div className="rounded-lg border border-amber-600/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-700">
                  <p className="font-medium">
                    违禁词自检：命中 {note.content_check.hits.length} 处（仅提示，不阻断保存与发布）
                  </p>
                  <ul className="mt-1 list-inside list-disc text-xs">
                    {note.content_check.hits.map((hit, index) => (
                      <li key={`${hit.word}-${hit.field}-${index}`}>
                        「{hit.word}」（{hit.category} · {FIELD_LABELS[hit.field] ?? hit.field}）
                      </li>
                    ))}
                  </ul>
                </div>
              ) : note.content_check ? (
                <p className="rounded-lg border border-emerald-600/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-700">
                  违禁词自检：未命中
                </p>
              ) : null}

              <div className="flex flex-wrap items-center gap-3">
                <Button onClick={() => void saveEdit()} disabled={saving}>
                  {saving ? "保存中…" : "保存修改"}
                </Button>
                <span className="text-xs text-muted-foreground">
                  保存仅更新标题 / 正文 / 标签，不改变状态。
                </span>
              </div>
              <ErrorText error={editError} />
            </CardContent>
          </Card>

          {/* ================= 中 · 预览与封面 ================= */}
          <div className="flex min-w-0 flex-col gap-6">
            <Card>
              <CardHeader>
                <CardTitle>预览</CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col gap-2">
                <div className="flex flex-col gap-3 rounded-lg border border-border p-4">
                  <h2 className="text-base font-medium break-words whitespace-pre-wrap">
                    {form.title || "（未填写标题）"}
                  </h2>
                  {form.body.trim() ? (
                    <div className="text-sm leading-6 break-words whitespace-pre-wrap">
                      {form.body}
                    </div>
                  ) : (
                    <p className="text-sm text-muted-foreground">（暂无正文）</p>
                  )}
                  {form.tags.length > 0 ? (
                    <div className="flex flex-wrap gap-1.5">
                      {form.tags.map((tag) => (
                        <span key={tag} className="text-sm text-sky-700">
                          #{tag}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </div>
                <p className="text-xs text-muted-foreground">
                  轻量图文预览：正文原样换行（不做 markdown 渲染），实时跟随编辑内容。
                </p>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>封面</CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                {selectedCover ? (
                  <div className="flex items-start gap-4">
                    <SelectedCoverThumb cover={selectedCover} />
                    <div className="flex flex-col gap-1 text-xs text-muted-foreground">
                      <span>模板：{selectedCover.template_key}</span>
                      <span>配色：{selectedCover.color_scheme}</span>
                      <span>
                        规格：{selectedCover.width}×{selectedCover.height}
                      </span>
                    </div>
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    尚未选中封面 —— 去封面工作室设计一张吧。
                  </p>
                )}
                <div>
                  <Link
                    href={`/covers?note_id=${note.id}`}
                    className={buttonVariants({ variant: "outline", size: "sm" })}
                  >
                    去做 / 下载封面
                  </Link>
                </div>
                <p className="text-xs text-muted-foreground">
                  封面在「封面工作室」实时渲染并下载（1242×1656 PNG）；
                  本页只读展示当前选中候选，不导出图片。
                </p>
              </CardContent>
            </Card>
          </div>

          {/* ================= 右 / 下 · 发布操作区 ================= */}
          <Card className="self-start">
            <CardHeader>
              <CardTitle>发布</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <div className="rounded-lg border border-red-600/30 bg-red-500/10 px-3 py-2 text-xs text-red-700">
                本系统不自动发布、不登录小红书、不保存账号密码 / Cookie；是否发布、何时发布完全由你决定
                —— 请人工到小红书完成发布后，再回到本页标记。
              </div>

              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!note.title}
                  onClick={() => void copyText(note.title, "标题")}
                >
                  复制标题
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!note.body}
                  onClick={() => void copyText(note.body ?? "", "正文")}
                >
                  复制正文
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!tagsText}
                  onClick={() => void copyText(tagsText, "标签")}
                >
                  复制标签
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                复制的是已保存版本；修改后请先点「保存修改」。标签按「#词1 #词2」格式输出。
              </p>

              <a
                href={XHS_PUBLISH_URL}
                target="_blank"
                rel="noopener noreferrer"
                className={cn(buttonVariants({ size: "sm" }), "w-fit")}
              >
                前往小红书发布页 ↗
              </a>
              <p className="text-xs text-muted-foreground">
                在新页面人工粘贴标题 / 正文、上传封面导出图、点「发布」—— 本系统不会代发。
              </p>

              <div className="border-t border-border pt-3">
                {note.status === "draft" ? (
                  <div className="flex flex-col gap-2">
                    <Button
                      disabled={busy}
                      onClick={() => void changeStatus({ status: "ready" }, "已标记定稿（待发布）")}
                    >
                      标记定稿（待发布）
                    </Button>
                    <p className="text-xs text-muted-foreground">
                      定稿表示内容确认完毕、等待人工发布；仍可随时「退回修改」。
                    </p>
                  </div>
                ) : note.status === "ready" ? (
                  <div className="flex flex-col gap-2">
                    <Button disabled={busy} onClick={() => setPublishOpen(true)}>
                      我已在小红书发布
                    </Button>
                    <Button
                      variant="outline"
                      disabled={busy}
                      onClick={() => void changeStatus({ status: "draft" }, "已退回修改")}
                    >
                      退回修改
                    </Button>
                  </div>
                ) : note.status === "published" ? (
                  <div className="flex flex-col gap-2 text-sm">
                    <p className="text-muted-foreground">
                      发布时间：
                      {note.published_at ? formatDateTime(note.published_at) : "—"}
                    </p>
                    <p className="break-all text-muted-foreground">
                      作品链接：
                      {note.publish_url ? (
                        <a
                          href={note.publish_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-primary underline-offset-4 hover:underline"
                        >
                          {note.publish_url}
                        </a>
                      ) : (
                        "—"
                      )}
                    </p>
                    <p className="text-muted-foreground">笔记 id：{note.xhs_note_id ?? "—"}</p>
                    <Button
                      variant="destructive"
                      className="w-fit"
                      disabled={busy}
                      onClick={() => setRevertOpen(true)}
                    >
                      撤销发布标记
                    </Button>
                    <p className="text-xs text-muted-foreground">
                      撤销只影响本系统的记录（清空发布时间 / 链接 / id），不会改动小红书上的内容。
                    </p>
                  </div>
                ) : null}
              </div>
            </CardContent>
          </Card>
        </div>
      ) : null}

      {/* ---------------- 发布 Dialog（记录「我已发布」与回填） ---------------- */}
      <Dialog
        open={publishOpen}
        onOpenChange={(next) => {
          if (!next && !busy) setPublishOpen(false);
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>我已在小红书完成发布</DialogTitle>
            <DialogDescription>
              记录发布事实：状态将置为「已发布」，来源选题同步为已发布。两个输入都可留空，之后可撤销。
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-3">
            <Field label="作品链接（可留空）" htmlFor="pub-url">
              <Input
                id="pub-url"
                value={publishUrl}
                placeholder="https://..."
                onChange={(event) => setPublishUrl(event.target.value)}
              />
            </Field>
            <Field label="小红书笔记 id（可留空）" htmlFor="pub-id">
              <Input
                id="pub-id"
                value={publishNoteId}
                maxLength={64}
                placeholder="如 65f0…"
                onChange={(event) => setPublishNoteId(event.target.value)}
              />
            </Field>
            <ErrorText error={publishError} />
          </div>
          <DialogFooter>
            <Button variant="outline" disabled={busy} onClick={() => setPublishOpen(false)}>
              取消
            </Button>
            <Button disabled={busy} onClick={() => void confirmPublish()}>
              {busy ? "提交中…" : "确认已发布"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ---------------- 撤销发布（二次确认） ---------------- */}
      <ConfirmDialog
        open={revertOpen}
        title="撤销发布标记？"
        description="将清空发布时间 / 作品链接 / 笔记 id，状态回到「待发布」，来源选题退回「写作中」。此操作只影响本系统的记录。"
        confirmText="确认撤销"
        busy={busy}
        onConfirm={() => void confirmRevert()}
        onCancel={() => setRevertOpen(false)}
      />
    </main>
  );
}

export default NoteStudioClient;
