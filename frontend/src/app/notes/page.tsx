"use client";

/**
 * 「创作台 · 笔记列表」页面（第 14 步）。
 *
 * 全栈链路：本页（浏览器）-> 本站 /api/v1/**（Route Handler 服务端代理）-> FastAPI。
 *
 * - 状态 Tabs（全部 / 草稿 / 待发布 / 已发布）+ 标题搜索 q + 分页（GET /api/v1/notes）；
 * - 行内「打开创作台」进入 `/notes/{id}`（人工编辑 + 复制搬运 + 手动发布标记）；
 * - 已发布条目直接展示作品链接（`noopener`），本页**不**提供任何发布动作。
 *
 * 筛选为页内状态（第 11 步约定：不入 URL），因此不使用 `useSearchParams`，无需 Suspense。
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  ErrorText,
  INITIAL_LOAD,
  Pagination,
  formatDateTime,
  toApiError,
} from "@/components/shared";
import type { LoadState } from "@/components/shared";
import { apiClient, toQuery } from "@/lib/api-client";
import type {
  ComplianceStatus,
  NoteListItem,
  NoteStatus,
  Page,
} from "@/lib/types";

const PAGE_SIZE = 10;

/** 状态过滤 Tabs（"all" 不过滤；不含 archived —— 归档属后续步骤） */
const STATUS_TABS: { value: string; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "draft", label: "草稿" },
  { value: "ready", label: "待发布" },
  { value: "published", label: "已发布" },
];

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

interface NoteQuery {
  page: number;
  status: string;
  q: string;
}

const INITIAL_QUERY: NoteQuery = { page: 1, status: "all", q: "" };

export default function NotesPage() {
  const [query, setQuery] = useState<NoteQuery>(INITIAL_QUERY);
  const [qInput, setQInput] = useState("");
  const [state, setState] = useState<LoadState<NoteListItem>>(INITIAL_LOAD);

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<Page<NoteListItem>>(
          `/api/v1/notes${toQuery({
            page: query.page,
            page_size: PAGE_SIZE,
            status: query.status === "all" ? null : query.status,
            q: query.q,
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
  }, [query]);

  const applyQuery = useCallback((patch: Partial<NoteQuery>) => {
    setState((prev) => ({ ...prev, loading: true }));
    setQuery((prev) => ({ ...prev, ...patch }));
  }, []);

  const rows = state.data?.items ?? [];

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-1 flex-col gap-6 px-6 py-10">
      <header className="flex flex-col gap-2">
        <h1 className="text-2xl font-medium tracking-tight">创作台 · 笔记列表</h1>
        <p className="text-sm text-muted-foreground">
          第 14 步：AI 草稿的人工编辑台。进入创作台可编辑正文、复制搬运、人工发布后回填标记。
          本系统不自动发布、不保存账号密码；发布与否完全由人决定。
        </p>
      </header>

      <div className="flex flex-wrap items-end justify-between gap-3">
        <Tabs
          value={query.status}
          onValueChange={(value) => applyQuery({ status: String(value), page: 1 })}
        >
          <TabsList>
            {STATUS_TABS.map((tab) => (
              <TabsTrigger key={tab.value} value={tab.value}>
                {tab.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>

        <form
          className="flex items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            applyQuery({ q: qInput.trim(), page: 1 });
          }}
        >
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="nt-q">标题搜索</Label>
            <Input
              id="nt-q"
              value={qInput}
              placeholder="按标题模糊匹配"
              onChange={(event) => setQInput(event.target.value)}
            />
          </div>
          <button type="submit" className={buttonVariants({ variant: "outline" })}>
            查询
          </button>
        </form>
      </div>

      <Card>
        <CardContent className="pt-4">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>标题</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>合规</TableHead>
                <TableHead>标签</TableHead>
                <TableHead>人设</TableHead>
                <TableHead>更新时间</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {state.loading ? (
                <TableRow>
                  <TableCell colSpan={7} className="py-6 text-center text-muted-foreground">
                    加载中……
                  </TableCell>
                </TableRow>
              ) : state.error ? (
                <TableRow>
                  <TableCell colSpan={7} className="py-6 text-center">
                    <ErrorText error={state.error} />
                  </TableCell>
                </TableRow>
              ) : rows.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={7} className="py-6 text-center text-muted-foreground">
                    暂无笔记（草稿由选题库的「生成笔记」产出；可切换状态或清除搜索）
                  </TableCell>
                </TableRow>
              ) : (
                rows.map((note) => {
                  const tags = note.tags ?? [];
                  const shown = tags.slice(0, 3);
                  const rest = tags.length - shown.length;
                  const statusMeta = STATUS_META[note.status] ?? STATUS_META.draft;
                  const complianceMeta =
                    COMPLIANCE_META[note.compliance_status] ?? COMPLIANCE_META.pending;
                  return (
                    <TableRow key={note.id}>
                      <TableCell className="max-w-64 min-w-52 whitespace-normal font-medium">
                        <Link
                          href={`/notes/${note.id}`}
                          className="underline-offset-4 hover:underline"
                        >
                          {note.title}
                        </Link>
                      </TableCell>
                      <TableCell>
                        <Badge className={statusMeta.cls}>{statusMeta.text}</Badge>
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-1.5">
                          <Badge className={complianceMeta.cls}>{complianceMeta.text}</Badge>
                          {note.compliance_status === "warning" &&
                          typeof note.forbidden_hits === "number" ? (
                            <span className="text-xs text-muted-foreground">
                              {note.forbidden_hits} 处命中
                            </span>
                          ) : null}
                        </div>
                      </TableCell>
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
                      <TableCell className="text-muted-foreground">
                        {note.persona_name ?? "—"}
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        {formatDateTime(note.updated_at)}
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex items-center justify-end gap-1.5">
                          {note.status === "published" ? (
                            <>
                              {note.publish_url ? (
                                <a
                                  href={note.publish_url}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  className="text-sm text-primary underline-offset-4 hover:underline"
                                >
                                  作品链接
                                </a>
                              ) : null}
                              <Link
                                href={`/metrics?note_id=${note.id}`}
                                className={buttonVariants({ variant: "outline", size: "sm" })}
                              >
                                回填数据
                              </Link>
                              <Link
                                href={`/diagnostics?note_id=${note.id}`}
                                className={buttonVariants({ variant: "outline", size: "sm" })}
                              >
                                诊断
                              </Link>
                            </>
                          ) : null}
                          <Link
                            href={`/notes/${note.id}`}
                            className={buttonVariants({ variant: "outline", size: "sm" })}
                          >
                            打开创作台
                          </Link>
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
    </main>
  );
}
