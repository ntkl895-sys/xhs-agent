"use client";

/**
 * 「选题库」页面（第 11 步）。
 *
 * 全栈链路：本页（浏览器）-> 本站 /api/v1/**（Route Handler 服务端代理）-> FastAPI。
 *
 * - 顶部「为热点批量生成选题」→ 共用生成 Dialog（不传 hotspot_ids，走后端自动取热点）；
 * - 筛选 + 列表：GET /topics（persona/account/status/q/generated_at/sort/order + 分页）；
 * - 行操作：草稿可「查看 / 编辑 / 通过 / 弃用」，非草稿只「查看」；
 * - 折叠区：GET /topics/runs（最近选题运行，含 token 与 json_parse_attempts）。
 *
 * 详情 / 编辑都走 GET /topics/{id}；**接口不返回 llm_raw**，页面也就不可能展示它。
 *
 * ⚠️ Next 16：`useSearchParams()` 必须包在 `<Suspense>` 中（否则 build 失败），
 * 故把用到它的部分拆成子组件 `TopicsInner`，外层用 `<Suspense>` 包裹。
 */

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
import { Textarea } from "@/components/ui/textarea";
import {
  ConfirmDialog,
  ErrorText,
  Field,
  INITIAL_LOAD,
  Notice,
  Pagination,
  TagInput,
  formatDateTime,
  formatNumber,
  formatScore,
  toApiError,
} from "@/components/shared";
import type { LoadState } from "@/components/shared";
import { TopicGenerateDialog } from "@/components/topic-generate-dialog";
import { VariantGenerateDialog } from "@/components/variant-generate-dialog";
import { ApiError, apiClient, toQuery } from "@/lib/api-client";
import type {
  Account,
  Page,
  Persona,
  TopicDifficulty,
  TopicListItem,
  TopicOut,
  TopicRun,
  TopicStatus,
  TopicUpdateRequest,
} from "@/lib/types";

const PAGE_SIZE = 10;

const DATE_ALL = "all";
const DATE_TODAY = "__today__";
const DATE_CUSTOM = "__custom__";

const VALID_STATUS: TopicStatus[] = [
  "draft",
  "approved",
  "writing",
  "published",
  "discarded",
];

/** 本机「今天」的 YYYY-MM-DD（用于 generated_at=今天 的筛选） */
function todayIso(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

interface TopicQuery {
  page: number;
  persona: string;
  account: string;
  status: string;
  q: string;
  dateMode: string;
  date: string;
  sort: string;
  order: string;
}

const INITIAL_QUERY: TopicQuery = {
  page: 1,
  persona: "all",
  account: "all",
  status: "draft",
  q: "",
  dateMode: DATE_ALL,
  date: "",
  sort: "score",
  order: "desc",
};

const STATUS_META: Record<TopicStatus, { text: string; cls: string }> = {
  draft: { text: "待审", cls: "border-sky-600/30 bg-sky-500/15 text-sky-700" },
  approved: { text: "已通过", cls: "border-emerald-600/30 bg-emerald-500/15 text-emerald-700" },
  writing: { text: "写作中", cls: "border-amber-600/30 bg-amber-500/15 text-amber-700" },
  published: { text: "已发布", cls: "border-teal-600/30 bg-teal-500/15 text-teal-700" },
  discarded: { text: "已弃", cls: "border-border bg-muted text-muted-foreground" },
};

const DIFFICULTY_META: Record<TopicDifficulty, { text: string; cls: string }> = {
  easy: { text: "易", cls: "border-emerald-600/30 bg-emerald-500/15 text-emerald-700" },
  medium: { text: "中", cls: "border-amber-600/30 bg-amber-500/15 text-amber-700" },
  hard: { text: "难", cls: "border-red-600/30 bg-red-500/15 text-red-700" },
};

interface EditFormState {
  title: string;
  summary: string;
  angle: string;
  pillar: string;
  content_outline: string[];
  tags: string[];
  target_keywords: string[];
  review_note: string;
}

const EMPTY_EDIT_FORM: EditFormState = {
  title: "",
  summary: "",
  angle: "",
  pillar: "",
  content_outline: [],
  tags: [],
  target_keywords: [],
  review_note: "",
};

function TopicsInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const urlHotspotId = searchParams.get("hotspot_id");
  const urlStatus = searchParams.get("status");
  const initialStatus =
    urlStatus && (urlStatus === "all" || VALID_STATUS.includes(urlStatus as TopicStatus))
      ? urlStatus
      : INITIAL_QUERY.status;

  const [query, setQuery] = useState<TopicQuery>({ ...INITIAL_QUERY, status: initialStatus });
  const [reloadToken, setReloadToken] = useState(0);
  const [state, setState] = useState<LoadState<TopicListItem>>(INITIAL_LOAD);
  const [qInput, setQInput] = useState("");

  const [personas, setPersonas] = useState<Persona[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);

  const [runs, setRuns] = useState<TopicRun[]>([]);
  const [runsError, setRunsError] = useState<ApiError | null>(null);
  const [runsToken, setRunsToken] = useState(0);

  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const [confirmTarget, setConfirmTarget] = useState<{
    topic: TopicListItem;
    next: TopicStatus;
  } | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);

  const [detailOpen, setDetailOpen] = useState(false);
  const [detailTopic, setDetailTopic] = useState<TopicOut | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<ApiError | null>(null);

  const [editOpen, setEditOpen] = useState(false);
  const [editTopic, setEditTopic] = useState<TopicOut | null>(null);
  const [editLoading, setEditLoading] = useState(false);
  const [editForm, setEditForm] = useState<EditFormState>(EMPTY_EDIT_FORM);
  const [editFieldErrors, setEditFieldErrors] = useState<Record<string, string>>({});
  const [editSaving, setEditSaving] = useState(false);
  const [editError, setEditError] = useState<ApiError | null>(null);

  const [genOpen, setGenOpen] = useState(false);
  const [genKey, setGenKey] = useState(0);

  // 第 23 步 · V2 增强（二）：同选题多版本生成（对 approved 选题开放）
  const [variantsOpen, setVariantsOpen] = useState(false);
  const [variantsTopic, setVariantsTopic] = useState<TopicListItem | null>(null);
  const [variantsKey, setVariantsKey] = useState(0);

  // 人设 / 账号下拉数据（挂载时各拉一次）
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const p = await apiClient.get<Page<Persona>>(
          `/api/v1/personas${toQuery({ status: "active", page: 1, page_size: 100 })}`,
        );
        if (!cancelled) setPersonas(p.items);
      } catch {
        if (!cancelled) setPersonas([]);
      }
      try {
        const a = await apiClient.get<Page<Account>>(
          `/api/v1/accounts${toQuery({ status: "all", page: 1, page_size: 100 })}`,
        );
        if (!cancelled) setAccounts(a.items);
      } catch {
        if (!cancelled) setAccounts([]);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, []);

  // 最近选题运行
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<TopicRun[]>("/api/v1/topics/runs?limit=10");
        if (!cancelled) {
          setRuns(data);
          setRunsError(null);
        }
      } catch (err) {
        if (!cancelled) setRunsError(toApiError(err));
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [runsToken]);

  // 选题列表
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const dateParams =
          query.dateMode === DATE_ALL
            ? {}
            : { generated_at: query.dateMode === DATE_TODAY ? todayIso() : query.date };
        const data = await apiClient.get<Page<TopicListItem>>(
          `/api/v1/topics${toQuery({
            page: query.page,
            page_size: PAGE_SIZE,
            persona_id: query.persona === "all" ? null : query.persona,
            account_id: query.account === "all" ? null : query.account,
            status: query.status === "all" ? null : query.status,
            hotspot_id: urlHotspotId,
            q: query.q,
            sort: query.sort,
            order: query.order,
            ...dateParams,
          })}`,
        );
        if (!cancelled) setState({ loading: false, data, error: null });
      } catch (err) {
        if (!cancelled) setState({ loading: false, data: null, error: toApiError(err) });
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [query, reloadToken, urlHotspotId]);

  const applyQuery = useCallback((patch: Partial<TopicQuery>) => {
    setState((prev) => ({ ...prev, loading: true }));
    setQuery((prev) => ({ ...prev, ...patch }));
    setReloadToken((token) => token + 1);
  }, []);

  const reload = useCallback(() => {
    setState((prev) => ({ ...prev, loading: true }));
    setReloadToken((token) => token + 1);
    setRunsToken((token) => token + 1);
  }, []);

  const openDetail = useCallback(async (topic: TopicListItem) => {
    setDetailOpen(true);
    setDetailTopic(null);
    setDetailError(null);
    setDetailLoading(true);
    try {
      const data = await apiClient.get<TopicOut>(`/api/v1/topics/${topic.id}`);
      setDetailTopic(data);
    } catch (err) {
      setDetailError(toApiError(err));
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const openEdit = useCallback(async (topic: TopicListItem) => {
    setEditOpen(true);
    setEditTopic(null);
    setEditError(null);
    setEditFieldErrors({});
    setEditLoading(true);
    try {
      const data = await apiClient.get<TopicOut>(`/api/v1/topics/${topic.id}`);
      setEditTopic(data);
      setEditForm({
        title: data.title,
        summary: data.summary ?? "",
        angle: data.angle ?? "",
        pillar: data.pillar ?? "",
        content_outline: data.content_outline ?? [],
        tags: data.tags ?? [],
        target_keywords: data.target_keywords ?? [],
        review_note: data.review_note ?? "",
      });
    } catch (err) {
      setEditError(toApiError(err));
    } finally {
      setEditLoading(false);
    }
  }, []);

  const saveEdit = async () => {
    if (!editTopic) return;
    const fieldErrors: Record<string, string> = {};
    const title = editForm.title.trim();
    if (!title) fieldErrors.title = "标题不能为空";
    else if (title.length > 200) fieldErrors.title = "标题不超过 200 字";
    setEditFieldErrors(fieldErrors);
    if (Object.keys(fieldErrors).length > 0) return;

    const payload: TopicUpdateRequest = {
      title,
      summary: editForm.summary.trim() || null,
      angle: editForm.angle.trim() || null,
      pillar: editForm.pillar.trim() || null,
      content_outline: editForm.content_outline,
      tags: editForm.tags,
      target_keywords: editForm.target_keywords,
      review_note: editForm.review_note.trim() || null,
    };

    setEditSaving(true);
    setEditError(null);
    try {
      await apiClient.patch<TopicOut>(`/api/v1/topics/${editTopic.id}`, payload);
      setNotice("已保存选题");
      setEditOpen(false);
      reload();
    } catch (err) {
      setEditError(toApiError(err));
    } finally {
      setEditSaving(false);
    }
  };

  const changeStatus = useCallback(
    async (topic: TopicListItem, next: TopicStatus) => {
      setConfirmBusy(true);
      setActionError(null);
      try {
        await apiClient.patch<TopicOut>(`/api/v1/topics/${topic.id}`, { status: next });
        setNotice(next === "approved" ? "已通过该选题" : "已弃用该选题");
        setConfirmTarget(null);
        reload();
      } catch (err) {
        setActionError(toApiError(err));
      } finally {
        setConfirmBusy(false);
      }
    },
    [reload],
  );

  const personaOptions = useMemo(
    () => personas.map((persona) => ({ id: persona.id, name: persona.name })),
    [personas],
  );
  const accountOptions = useMemo(
    () => accounts.map((account) => ({ id: account.id, name: account.name })),
    [accounts],
  );

  const rows = state.data?.items ?? [];

  return (
    <>
      <div className="flex flex-wrap items-center justify-end gap-3">
        <Button
          onClick={() => {
            setGenKey((key) => key + 1);
            setGenOpen(true);
          }}
        >
          为热点批量生成选题
        </Button>
      </div>

      {urlHotspotId ? (
        <p className="rounded-lg border border-sky-600/30 bg-sky-500/10 px-3 py-2 text-sm text-sky-700">
          正在查看某热点的选题（hotspot_id={urlHotspotId}）。
          <button
            type="button"
            className="ml-2 underline underline-offset-4"
            onClick={() => router.replace("/topics")}
          >
            清除该筛选
          </button>
        </p>
      ) : null}

      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          applyQuery({ q: qInput.trim(), page: 1 });
        }}
      >
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="tp-q">关键词</Label>
          <Input
            id="tp-q"
            value={qInput}
            placeholder="按标题模糊匹配"
            onChange={(event) => setQInput(event.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="tp-persona">人设</Label>
          <Select
            items={{ all: "全部", ...Object.fromEntries(personaOptions.map((o) => [o.id, o.name])) }}
            value={query.persona}
            onValueChange={(value) => applyQuery({ persona: String(value), page: 1 })}
          >
            <SelectTrigger id="tp-persona" className="w-44">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部</SelectItem>
              {personaOptions.map((option) => (
                <SelectItem key={option.id} value={option.id}>
                  {option.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="tp-account">账号</Label>
          <Select
            items={{ all: "全部", ...Object.fromEntries(accountOptions.map((o) => [o.id, o.name])) }}
            value={query.account}
            onValueChange={(value) => applyQuery({ account: String(value), page: 1 })}
          >
            <SelectTrigger id="tp-account" className="w-40">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部</SelectItem>
              {accountOptions.map((option) => (
                <SelectItem key={option.id} value={option.id}>
                  {option.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="tp-status">状态</Label>
          <Select
            items={{
              all: "全部",
              draft: "待审",
              approved: "已通过",
              writing: "写作中",
              published: "已发布",
              discarded: "已弃",
            }}
            value={query.status}
            onValueChange={(value) => applyQuery({ status: String(value), page: 1 })}
          >
            <SelectTrigger id="tp-status" className="w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部</SelectItem>
              <SelectItem value="draft">待审</SelectItem>
              <SelectItem value="approved">已通过</SelectItem>
              <SelectItem value="writing">写作中</SelectItem>
              <SelectItem value="published">已发布</SelectItem>
              <SelectItem value="discarded">已弃</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="tp-date-mode">生成日期</Label>
          <Select
            items={{ [DATE_ALL]: "不限", [DATE_TODAY]: "今天", [DATE_CUSTOM]: "自定义" }}
            value={query.dateMode}
            onValueChange={(value) => applyQuery({ dateMode: String(value), page: 1 })}
          >
            <SelectTrigger id="tp-date-mode" className="w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={DATE_ALL}>不限</SelectItem>
              <SelectItem value={DATE_TODAY}>今天</SelectItem>
              <SelectItem value={DATE_CUSTOM}>自定义</SelectItem>
            </SelectContent>
          </Select>
        </div>
        {query.dateMode === DATE_CUSTOM ? (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="tp-date">日期</Label>
            <Input
              id="tp-date"
              type="date"
              className="w-40"
              value={query.date}
              onChange={(event) => applyQuery({ date: event.target.value, page: 1 })}
            />
          </div>
        ) : null}
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="tp-sort">排序</Label>
          <Select
            items={{ score: "评分", generated_at: "生成时间" }}
            value={query.sort}
            onValueChange={(value) => applyQuery({ sort: String(value), page: 1 })}
          >
            <SelectTrigger id="tp-sort" className="w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="score">评分</SelectItem>
              <SelectItem value="generated_at">生成时间</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="tp-order">方向</Label>
          <Select
            items={{ desc: "降序", asc: "升序" }}
            value={query.order}
            onValueChange={(value) => applyQuery({ order: String(value), page: 1 })}
          >
            <SelectTrigger id="tp-order" className="w-28">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="desc">降序</SelectItem>
              <SelectItem value="asc">升序</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <Button type="submit" variant="outline">
          查询
        </Button>
      </form>

      <ErrorText error={actionError} />
      <Notice text={notice} />

      <Card>
        <CardContent className="pt-4">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>标题</TableHead>
                <TableHead>人设</TableHead>
                <TableHead>支柱</TableHead>
                <TableHead>标签</TableHead>
                <TableHead>难度</TableHead>
                <TableHead>热度</TableHead>
                <TableHead>评分</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>生成时间</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {state.loading ? (
                <TableRow>
                  <TableCell colSpan={10} className="py-6 text-center text-muted-foreground">
                    加载中……
                  </TableCell>
                </TableRow>
              ) : state.error ? (
                <TableRow>
                  <TableCell colSpan={10} className="py-6 text-center">
                    <ErrorText error={state.error} />
                  </TableCell>
                </TableRow>
              ) : rows.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={10} className="py-6 text-center text-muted-foreground">
                    暂无数据（可切换状态 / 人设，或点击「为热点批量生成选题」）
                  </TableCell>
                </TableRow>
              ) : (
                rows.map((topic) => {
                  const tags = topic.tags ?? [];
                  const shown = tags.slice(0, 3);
                  const rest = tags.length - shown.length;
                  const statusMeta = STATUS_META[topic.status] ?? STATUS_META.draft;
                  const diffMeta = topic.difficulty ? DIFFICULTY_META[topic.difficulty] : null;
                  return (
                    <TableRow key={topic.id}>
                      <TableCell className="max-w-64 min-w-52 whitespace-normal font-medium">
                        {topic.title}
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        {topic.persona_name ?? "公共库"}
                      </TableCell>
                      <TableCell className="text-muted-foreground">{topic.pillar ?? "—"}</TableCell>
                      <TableCell>
                        {shown.length > 0 ? (
                          <div className="flex flex-wrap items-center gap-1">
                            {shown.map((tag) => (
                              <Badge key={tag} variant="outline">
                                {tag}
                              </Badge>
                            ))}
                            {rest > 0 ? (
                              <span className="text-xs text-muted-foreground">+{rest}</span>
                            ) : null}
                          </div>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </TableCell>
                      <TableCell>
                        {diffMeta ? (
                          <Badge className={diffMeta.cls}>{diffMeta.text}</Badge>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </TableCell>
                      <TableCell>{formatNumber(topic.heat_score)}</TableCell>
                      <TableCell>{formatScore(topic.score)}</TableCell>
                      <TableCell>
                        <Badge className={statusMeta.cls}>{statusMeta.text}</Badge>
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        {topic.generated_at ? formatDateTime(topic.generated_at) : "—"}
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex justify-end gap-1.5">
                          <Button variant="outline" size="sm" onClick={() => void openDetail(topic)}>
                            查看
                          </Button>
                          {topic.status === "draft" ? (
                            <>
                              <Button
                                variant="outline"
                                size="sm"
                                onClick={() => void openEdit(topic)}
                              >
                                编辑
                              </Button>
                              <Button
                                size="sm"
                                onClick={() => setConfirmTarget({ topic, next: "approved" })}
                              >
                                通过
                              </Button>
                              <Button
                                variant="destructive"
                                size="sm"
                                onClick={() => setConfirmTarget({ topic, next: "discarded" })}
                              >
                                弃用
                              </Button>
                            </>
                          ) : null}
                          {topic.status === "approved" ? (
                            <Button
                              size="sm"
                              onClick={() => {
                                setVariantsTopic(topic);
                                setVariantsKey((key) => key + 1);
                                setVariantsOpen(true);
                              }}
                            >
                              多版本生成
                            </Button>
                          ) : null}
                          {topic.status === "writing" || topic.status === "published" ? (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => router.push(`/notes/variants/${topic.id}`)}
                            >
                              版本对比
                            </Button>
                          ) : null}
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })
              )}
            </TableBody>
          </Table>
          <Pagination
            page={query.page}
            pageSize={PAGE_SIZE}
            total={state.data?.total ?? 0}
            loading={state.loading}
            onPage={(next) => applyQuery({ page: next })}
          />
        </CardContent>
      </Card>

      {/* ---------------- 折叠区：最近选题运行 ---------------- */}
      <details className="rounded-xl border border-border">
        <summary className="cursor-pointer px-4 py-3 text-sm font-medium">
          最近选题运行（limit 10）
        </summary>
        <div className="border-t border-border p-4">
          {runsError ? <ErrorText error={runsError} /> : null}
          {runs.length === 0 && !runsError ? (
            <p className="text-sm text-muted-foreground">暂无运行记录。</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>状态</TableHead>
                  <TableHead>prompt / completion / total</TableHead>
                  <TableHead>JSON 尝试</TableHead>
                  <TableHead>耗时(ms)</TableHead>
                  <TableHead>生成/落库/拒绝/跳过</TableHead>
                  <TableHead>时间</TableHead>
                  <TableHead>错误</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map((run) => (
                  <TableRow key={run.run_id}>
                    <TableCell>{run.status ?? "—"}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {formatNumber(run.prompt_tokens)} / {formatNumber(run.completion_tokens)} /{" "}
                      {formatNumber(run.total_tokens)}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {run.json_parse_attempts ?? "—"}
                    </TableCell>
                    <TableCell className="text-muted-foreground">{run.latency_ms ?? "—"}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {formatNumber(run.generated)} / {formatNumber(run.persisted)} /{" "}
                      {formatNumber(run.rejected)} / {formatNumber(run.skipped)}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {formatDateTime(run.created_at)}
                    </TableCell>
                    <TableCell className="max-w-64 whitespace-normal text-red-600">
                      {run.error_message ?? "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </div>
      </details>

      {/* ---------------- 详情 Dialog ---------------- */}
      <Dialog open={detailOpen} onOpenChange={setDetailOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>选题详情</DialogTitle>
            <DialogDescription>来源热点溯源 + 生成元信息（不含模型原始输出）。</DialogDescription>
          </DialogHeader>

          {detailLoading ? (
            <p className="text-sm text-muted-foreground">加载中……</p>
          ) : detailError ? (
            <ErrorText error={detailError} />
          ) : detailTopic ? (
            <div className="flex flex-col gap-3 text-sm">
              <div className="flex flex-col gap-1">
                <span className="text-xs text-muted-foreground">标题</span>
                <span className="font-medium">{detailTopic.title}</span>
              </div>
              {detailTopic.summary ? (
                <div className="flex flex-col gap-1">
                  <span className="text-xs text-muted-foreground">摘要</span>
                  <span>{detailTopic.summary}</span>
                </div>
              ) : null}
              {detailTopic.angle ? (
                <div className="flex flex-col gap-1">
                  <span className="text-xs text-muted-foreground">切入角度</span>
                  <span>{detailTopic.angle}</span>
                </div>
              ) : null}
              <div className="flex flex-wrap gap-4">
                <span className="text-muted-foreground">
                  支柱：{detailTopic.pillar ?? "—"}
                </span>
                <span className="text-muted-foreground">热度：{formatNumber(detailTopic.heat_score)}</span>
                <span className="text-muted-foreground">
                  难度：
                  {detailTopic.difficulty ? DIFFICULTY_META[detailTopic.difficulty].text : "—"}
                </span>
                <span className="text-muted-foreground">评分：{formatScore(detailTopic.score)}</span>
                <span className="text-muted-foreground">
                  模型：{detailTopic.llm_model ?? "—"}
                </span>
              </div>

              {detailTopic.content_outline && detailTopic.content_outline.length > 0 ? (
                <div className="flex flex-col gap-1">
                  <span className="text-xs text-muted-foreground">内容大纲</span>
                  <ol className="list-inside list-decimal">
                    {detailTopic.content_outline.map((item, index) => (
                      <li key={index}>{item}</li>
                    ))}
                  </ol>
                </div>
              ) : null}

              {detailTopic.tags && detailTopic.tags.length > 0 ? (
                <div className="flex flex-col gap-1">
                  <span className="text-xs text-muted-foreground">话题标签</span>
                  <div className="flex flex-wrap gap-1">
                    {detailTopic.tags.map((tag) => (
                      <Badge key={tag} variant="outline">
                        #{tag}
                      </Badge>
                    ))}
                  </div>
                </div>
              ) : null}

              {detailTopic.target_keywords && detailTopic.target_keywords.length > 0 ? (
                <div className="flex flex-col gap-1">
                  <span className="text-xs text-muted-foreground">目标关键词</span>
                  <div className="flex flex-wrap gap-1">
                    {detailTopic.target_keywords.map((keyword) => (
                      <Badge key={keyword} variant="secondary">
                        {keyword}
                      </Badge>
                    ))}
                  </div>
                </div>
              ) : null}

              {detailTopic.source_trace && detailTopic.source_trace.length > 0 ? (
                <div className="flex flex-col gap-1">
                  <span className="text-xs text-muted-foreground">来源热点溯源</span>
                  <ul className="list-inside list-disc">
                    {detailTopic.source_trace.map((trace, index) => {
                      const href = trace.url ?? trace.mobile_url ?? null;
                      return (
                        <li key={index}>
                          <span className="text-muted-foreground">
                            [{trace.source ?? "—"}]{" "}
                          </span>
                          {href ? (
                            <a
                              href={href}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-primary underline-offset-4 hover:underline"
                            >
                              {trace.title ?? href}
                            </a>
                          ) : (
                            (trace.title ?? "—")
                          )}
                        </li>
                      );
                    })}
                  </ul>
                </div>
              ) : null}

              <div className="flex flex-wrap gap-4 border-t border-border pt-3 text-xs text-muted-foreground">
                <span>状态：{STATUS_META[detailTopic.status]?.text ?? detailTopic.status}</span>
                <span>
                  生成时间：
                  {detailTopic.generated_at ? formatDateTime(detailTopic.generated_at) : "—"}
                </span>
                <span>提示词版本：{String(detailTopic.meta?.prompt_version ?? "—")}</span>
                <span>JSON 尝试：{String(detailTopic.meta?.json_parse_attempts ?? "—")}</span>
              </div>
            </div>
          ) : null}

          <DialogFooter>
            <Button variant="outline" onClick={() => setDetailOpen(false)}>
              关闭
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ---------------- 编辑 Dialog ---------------- */}
      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>编辑选题</DialogTitle>
            <DialogDescription>仅调整文字与结构；状态流转请用列表的「通过 / 弃用」。</DialogDescription>
          </DialogHeader>

          {editLoading ? (
            <p className="text-sm text-muted-foreground">加载中……</p>
          ) : editTopic ? (
            <div className="flex flex-col gap-3">
              <Field label="标题 *" htmlFor="ed-title" error={editFieldErrors.title}>
                <Input
                  id="ed-title"
                  value={editForm.title}
                  onChange={(event) => setEditForm({ ...editForm, title: event.target.value })}
                />
              </Field>
              <Field label="摘要" htmlFor="ed-summary">
                <Textarea
                  id="ed-summary"
                  value={editForm.summary}
                  onChange={(event) => setEditForm({ ...editForm, summary: event.target.value })}
                />
              </Field>
              <Field label="切入角度" htmlFor="ed-angle">
                <Textarea
                  id="ed-angle"
                  value={editForm.angle}
                  onChange={(event) => setEditForm({ ...editForm, angle: event.target.value })}
                />
              </Field>
              <Field label="支柱" htmlFor="ed-pillar">
                <Input
                  id="ed-pillar"
                  value={editForm.pillar}
                  onChange={(event) => setEditForm({ ...editForm, pillar: event.target.value })}
                />
              </Field>
              <Field label="内容大纲" hint="3~6 条为宜，可增删排序">
                <TagInput
                  value={editForm.content_outline}
                  onChange={(next) => setEditForm({ ...editForm, content_outline: next })}
                  placeholder="输入后回车或点「添加」"
                  maxItems={8}
                />
              </Field>
              <Field label="话题标签" hint="3~8 个纯词，无需带 #">
                <TagInput
                  value={editForm.tags}
                  onChange={(next) => setEditForm({ ...editForm, tags: next })}
                  placeholder="输入后回车或点「添加」"
                  maxItems={8}
                  maxLength={30}
                />
              </Field>
              <Field label="目标关键词">
                <TagInput
                  value={editForm.target_keywords}
                  onChange={(next) => setEditForm({ ...editForm, target_keywords: next })}
                  placeholder="输入后回车或点「添加」"
                  maxItems={10}
                />
              </Field>
              <Field label="审核备注" htmlFor="ed-review">
                <Textarea
                  id="ed-review"
                  value={editForm.review_note}
                  onChange={(event) =>
                    setEditForm({ ...editForm, review_note: event.target.value })
                  }
                />
              </Field>

              <ErrorText error={editError} />
            </div>
          ) : (
            <ErrorText error={editError} />
          )}

          <DialogFooter>
            <Button variant="outline" onClick={() => setEditOpen(false)} disabled={editSaving}>
              取消
            </Button>
            <Button onClick={() => void saveEdit()} disabled={editSaving || editLoading}>
              {editSaving ? "保存中…" : "保存"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={confirmTarget !== null}
        title={confirmTarget?.next === "discarded" ? "确认弃用该选题？" : "确认通过该选题？"}
        description={
          confirmTarget
            ? `「${confirmTarget.topic.title.slice(0, 24)}」将被标记为${
                confirmTarget.next === "discarded" ? "已弃" : "已通过"
              }。仅草稿可流转，操作后不可在本步改回。`
            : ""
        }
        confirmText={confirmTarget?.next === "discarded" ? "确认弃用" : "确认通过"}
        busy={confirmBusy}
        onCancel={() => setConfirmTarget(null)}
        onConfirm={() =>
          confirmTarget && void changeStatus(confirmTarget.topic, confirmTarget.next)
        }
      />

      <TopicGenerateDialog
        key={genKey}
        open={genOpen}
        onOpenChange={setGenOpen}
        onGenerated={reload}
      />

      {variantsTopic ? (
        <VariantGenerateDialog
          key={variantsKey}
          open={variantsOpen}
          onOpenChange={setVariantsOpen}
          topicId={variantsTopic.id}
          topicTitle={variantsTopic.title}
        />
      ) : null}
    </>
  );
}

export default function TopicsPage() {
  return (
    <main className="mx-auto flex w-full max-w-7xl flex-1 flex-col gap-6 px-6 py-10">
      <header className="flex flex-col gap-2">
        <h1 className="text-2xl font-medium tracking-tight">选题库</h1>
        <p className="text-sm text-muted-foreground">
          第 11 步：AI 生成选题的人工审核台。草稿可「通过 / 弃用」，其余状态只读。浏览器只请求本站
          /api/v1/**，由服务端 Route Handler 转发到后端；不登录小红书、不自动发布。
        </p>
      </header>

      <Suspense fallback={<p className="text-sm text-muted-foreground">加载中……</p>}>
        <TopicsInner />
      </Suspense>
    </main>
  );
}
