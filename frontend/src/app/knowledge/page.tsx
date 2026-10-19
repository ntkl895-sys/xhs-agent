"use client";

/**
 * 「知识库」页面（第 18 步 · RAG 爆款规律反哺；第 19 步 · 人工维护扩展）。
 *
 * 全栈链路：本页（浏览器）-> 本站 /api/v1/**（Route Handler 服务端代理）-> FastAPI。
 *
 * 四个 Tabs：
 * ① 知识块：chunk_type / status 筛选 + 表格（标题、类型、样本数、置信度、状态、时间）；
 *    行操作「查看」（Dialog 展示全文与来源证据）、「编辑」（改标题 / 正文，
 *    正文变化触发后端重向量化）与「归档 / 恢复」（PATCH）；
 *    顶部「新建知识块」Dialog（六类型 + 可选关联人设）；选类型 = 违禁词时
 *    额外显示违禁词管理条（「同步内置词表(seed)」+「新增违禁词」），且行内展示类别；
 * ② 提炼规律：显示当前已发布笔记数与样本门槛；按钮「提炼爆款规律」（BFF 超时 120s，
 *    首次会下载本地向量模型约 1.3GB）；结果展示新增 / 复用 / 跳过条数；
 * ③ 检索测试：输入 query + 类型 + k，展示 top-k（title / content / similarity），
 *    用于人工验收 RAG 召回质量；
 * ④ 文档导入（第 19 步）：文本粘贴或 .txt / .md 上传（FormData），可选目标类型 /
 *    关联人设 / 切分参数；结果如实展示切分 / 写入 / 跳过 / 失败计数。
 *
 * 人工维护组件集中在同目录 ./knowledge-admin.tsx，本文件负责页面壳与列表编排。
 *
 * 红线：规律只来自**本账号本地数据**（自己的已发布笔记 + 数据回填 + 单篇诊断），
 * 不爬竞品、不引入行业均值等外部数字；知识块只增 / 归档、无物理删除；
 * 接口不返回 embedding 向量与 ``llm_raw``。
 *
 * ⚠️ Next 16：动态 URL hook 必须包在 `<Suspense>` 中 —— 本页当前不读 URL 参数，
 * 仍按既有页面范式保留 Suspense 壳（与 dashboard / diagnostics 一致）。
 */

import { Suspense, useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
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
  Pagination,
  formatDateTime,
  toApiError,
} from "@/components/shared";
import {
  EditSnippetDialog,
  ForbiddenAdminBar,
  ImportTab,
  ManualCreateDialog,
} from "./knowledge-admin";
import { ApiError, apiClient, toQuery } from "@/lib/api-client";
import type {
  ChunkTypeName,
  DashboardSummary,
  ExtractResult,
  KnowledgeSearchResult,
  KnowledgeSnippet,
  SnippetList,
  SnippetStatusName,
  SnippetWriteResult,
} from "@/lib/types";

/* ------------------------------------------------------------------ 常量 */

const TAB_SNIPPETS = "snippets";
const TAB_EXTRACT = "extract";
const TAB_SEARCH = "search";
const TAB_IMPORT = "import";

const PAGE_SIZE = 20;

/** chunk_type 筛选（本步主要 hit_pattern / review_insight） */
const CHUNK_FILTER_OPTIONS: { value: string; label: string }[] = [
  { value: "all", label: "全部类型" },
  { value: "hit_pattern", label: "爆款规律" },
  { value: "review_insight", label: "复盘心得" },
  { value: "topic_template", label: "选题模板" },
  { value: "forbidden_word", label: "违禁词" },
  { value: "persona_knowledge", label: "人设知识" },
  { value: "general", label: "通用" },
];

const STATUS_FILTER_OPTIONS: { value: string; label: string }[] = [
  { value: "all", label: "全部状态" },
  { value: "active", label: "启用中" },
  { value: "archived", label: "已归档" },
];

/** 类型中文名（与后端 ChunkType 对齐） */
const CHUNK_LABELS: Record<string, string> = {
  hit_pattern: "爆款规律",
  review_insight: "复盘心得",
  topic_template: "选题模板",
  forbidden_word: "违禁词",
  persona_knowledge: "人设知识",
  general: "通用",
};

/** 检索测试的类型选项（本步知识库只有两类被灌入） */
const SEARCH_TYPE_OPTIONS: { value: string; label: string }[] = [
  { value: "all", label: "全部类型" },
  { value: "hit_pattern", label: "爆款规律" },
  { value: "review_insight", label: "复盘心得" },
];

const SEARCH_K_OPTIONS = ["3", "5", "10", "20"];

/* ------------------------------------------------------------------ 工具 */

/** 复制到剪贴板（Clipboard API + 隐藏 textarea 双兜底；与诊断 / 周报页同款） */
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

/** 从 meta 里安全读取并渲染一个标量（缺 → 「—」） */
function metaValue(meta: Record<string, unknown> | null, key: string): string {
  if (!meta) return "—";
  const value = meta[key];
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** 置信度显示（0~1 → 0.800；缺 → 「—」） */
function confidenceText(meta: Record<string, unknown> | null): string {
  if (!meta) return "—";
  const value = meta["confidence"];
  if (typeof value === "number") return value.toFixed(3);
  return "—";
}

/* ------------------------------------------------------------------ 详情 Dialog */

function SnippetDetail({
  snippet,
  onToggleStatus,
  busy,
}: {
  snippet: KnowledgeSnippet | null;
  onToggleStatus: (snippet: KnowledgeSnippet) => void;
  busy: boolean;
}) {
  const [copied, setCopied] = useState(false);
  if (!snippet) return null;
  const meta = snippet.meta;
  const evidence =
    meta && typeof meta["evidence"] === "object" && meta["evidence"] !== null
      ? (meta["evidence"] as Record<string, unknown>)
      : null;
  const sourceIds = Array.isArray(meta?.["source_note_ids"])
    ? (meta?.["source_note_ids"] as unknown[]).map(String)
    : [];

  return (
    <div className="space-y-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="secondary">{CHUNK_LABELS[snippet.chunk_type] ?? snippet.chunk_type}</Badge>
        <Badge variant={snippet.status === "active" ? "default" : "outline"}>
          {snippet.status === "active" ? "启用中" : "已归档"}
        </Badge>
        <span className="text-muted-foreground">{formatDateTime(snippet.created_at)}</span>
      </div>

      <div className="rounded-md border bg-muted/40 p-3">
        <p className="whitespace-pre-wrap leading-relaxed">{snippet.content}</p>
      </div>

      <div className="space-y-1.5">
        <div>
          <span className="text-muted-foreground">适用范围：</span>
          {metaValue(meta, "scenario")}
        </div>
        <div>
          <span className="text-muted-foreground">样本量：</span>
          {metaValue(meta, "sample_size")} 篇
        </div>
        <div>
          <span className="text-muted-foreground">置信度：</span>
          {confidenceText(meta)}
        </div>
        <div>
          <span className="text-muted-foreground">指标证据：</span>
          {evidence ? String(evidence["summary"] ?? "—") : "—"}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-muted-foreground">来源笔记：</span>
          {sourceIds.length ? (
            sourceIds.map((id) => (
              <a
                key={id}
                href={`/notes/${id}`}
                className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs underline-offset-2 hover:underline"
                title="跳转创作台"
              >
                {id.slice(0, 8)}…
              </a>
            ))
          ) : (
            <span>—</span>
          )}
        </div>
        <div>
          <span className="text-muted-foreground">提示词版本：</span>
          {metaValue(meta, "prompt_version")}
        </div>
        <div className="break-all">
          <span className="text-muted-foreground">内容指纹：</span>
          <span className="font-mono text-xs">{metaValue(meta, "content_hash")}</span>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={async () => {
            const ok = await copyText(snippet.content);
            setCopied(ok);
          }}
        >
          {copied ? "已复制" : "复制规律文本"}
        </Button>
        <Button
          variant={snippet.status === "active" ? "outline" : "default"}
          size="sm"
          disabled={busy}
          onClick={() => onToggleStatus(snippet)}
        >
          {busy ? "处理中…" : snippet.status === "active" ? "归档" : "恢复"}
        </Button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ 列表 Tab */

function SnippetsTab({
  list,
  loading,
  error,
  chunkFilter,
  statusFilter,
  page,
  onChunkFilter,
  onStatusFilter,
  onPage,
  onRefresh,
  onOpenDetail,
  onEdit,
  onCreate,
  onToggleStatus,
  patchBusyId,
  actionNotice,
  actionError,
}: {
  list: SnippetList | null;
  loading: boolean;
  error: ApiError | null;
  chunkFilter: string;
  statusFilter: string;
  page: number;
  onChunkFilter: (value: string) => void;
  onStatusFilter: (value: string) => void;
  onPage: (next: number) => void;
  onRefresh: () => void;
  onOpenDetail: (snippet: KnowledgeSnippet) => void;
  onEdit: (snippet: KnowledgeSnippet) => void;
  /** 打开「新建知识块」Dialog（预设类型：违禁词管理条传 forbidden_word） */
  onCreate: (preset: ChunkTypeName) => void;
  onToggleStatus: (snippet: KnowledgeSnippet) => void;
  patchBusyId: string | null;
  actionNotice: string;
  actionError: ApiError | null;
}) {
  const isForbiddenView = chunkFilter === "forbidden_word";
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <div className="w-44">
          <Label className="mb-1 block text-xs text-muted-foreground">类型</Label>
          <Select
            items={Object.fromEntries(CHUNK_FILTER_OPTIONS.map((item) => [item.value, item.label]))}
            value={chunkFilter}
            onValueChange={(value) => onChunkFilter(String(value))}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {CHUNK_FILTER_OPTIONS.map((item) => (
                <SelectItem key={item.value} value={item.value}>
                  {item.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="w-36">
          <Label className="mb-1 block text-xs text-muted-foreground">状态</Label>
          <Select
            items={Object.fromEntries(STATUS_FILTER_OPTIONS.map((item) => [item.value, item.label]))}
            value={statusFilter}
            onValueChange={(value) => onStatusFilter(String(value))}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {STATUS_FILTER_OPTIONS.map((item) => (
                <SelectItem key={item.value} value={item.value}>
                  {item.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <Button variant="outline" size="sm" onClick={onRefresh} disabled={loading}>
          刷新
        </Button>
        <Button size="sm" onClick={() => onCreate(isForbiddenView ? "forbidden_word" : "general")}>
          新建知识块
        </Button>
      </div>

      {isForbiddenView ? (
        <ForbiddenAdminBar onRefresh={onRefresh} onAddWord={() => onCreate("forbidden_word")} />
      ) : null}

      {actionNotice ? <Notice text={actionNotice} /> : null}
      <ErrorText error={actionError} />
      <ErrorText error={error} />

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>标题 / 内容</TableHead>
            <TableHead className="w-24">类型</TableHead>
            <TableHead className="w-20">样本数</TableHead>
            <TableHead className="w-20">置信度</TableHead>
            <TableHead className="w-24">状态</TableHead>
            <TableHead className="w-40">创建时间</TableHead>
            <TableHead className="w-40">操作</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {(list?.items ?? []).map((item) => (
            <TableRow key={item.id}>
              <TableCell className="max-w-md">
                <div className="truncate font-medium">{item.title ?? "（无标题）"}</div>
                <div className="truncate text-xs text-muted-foreground">{item.content}</div>
              </TableCell>
              <TableCell>
                <Badge variant="secondary">{CHUNK_LABELS[item.chunk_type] ?? item.chunk_type}</Badge>
                {item.chunk_type === "forbidden_word" ? (
                  <div className="mt-1 text-xs text-muted-foreground">
                    类别：{metaValue(item.meta, "category")}
                  </div>
                ) : null}
              </TableCell>
              <TableCell>{metaValue(item.meta, "sample_size")}</TableCell>
              <TableCell>{confidenceText(item.meta)}</TableCell>
              <TableCell>
                <Badge variant={item.status === "active" ? "default" : "outline"}>
                  {item.status === "active" ? "启用中" : "已归档"}
                </Badge>
              </TableCell>
              <TableCell className="text-xs text-muted-foreground">
                {formatDateTime(item.created_at)}
              </TableCell>
              <TableCell>
                <div className="flex gap-2">
                  <Button variant="outline" size="sm" onClick={() => onOpenDetail(item)}>
                    查看
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => onEdit(item)}>
                    编辑
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={patchBusyId === item.id}
                    onClick={() => onToggleStatus(item)}
                  >
                    {patchBusyId === item.id ? "处理中…" : item.status === "active" ? "归档" : "恢复"}
                  </Button>
                </div>
              </TableCell>
            </TableRow>
          ))}
          {!loading && (list?.items ?? []).length === 0 ? (
            <TableRow>
              <TableCell colSpan={7} className="py-8 text-center text-muted-foreground">
                暂无知识块：去「提炼规律」Tab 从已发布笔记中提炼爆款规律。
              </TableCell>
            </TableRow>
          ) : null}
          {loading && (list?.items ?? []).length === 0 ? (
            <TableRow>
              <TableCell colSpan={7} className="py-8 text-center text-muted-foreground">
                加载中…
              </TableCell>
            </TableRow>
          ) : null}
        </TableBody>
      </Table>

      <Pagination
        page={page}
        pageSize={PAGE_SIZE}
        total={list?.total ?? 0}
        loading={loading}
        onPage={onPage}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ 提炼 Tab */

function ExtractTab({
  publishedCount,
  publishedError,
  busy,
  error,
  notice,
  result,
  onRun,
}: {
  publishedCount: number | null;
  publishedError: ApiError | null;
  busy: boolean;
  error: ApiError | null;
  notice: string;
  result: ExtractResult | null;
  onRun: () => void;
}) {
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">提炼爆款规律（第六个业务图）</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <p className="text-muted-foreground">
            对比本账号「表现较好 / 较差」的已发布笔记，归纳可复用的规律并向量化写入知识库，
            供选题与创作反哺使用。规律只来自本账号本地数据（已发布笔记 + 数据回填 +
            单篇诊断），不联网抓取竞品、不引入行业均值等外部数字。
          </p>
          <div className="flex flex-wrap gap-6">
            <div>
              <span className="text-muted-foreground">当前已发布笔记：</span>
              <span className="font-medium">
                {publishedCount === null ? "—" : `${publishedCount} 篇`}
              </span>
            </div>
            <div>
              <span className="text-muted-foreground">样本门槛：</span>
              <span className="font-medium">至少 3 篇（后端 INSIGHTS_MIN_PUBLISHED，以提炼结果为准）</span>
            </div>
          </div>
          {publishedError ? <ErrorText error={publishedError} /> : null}
          <div className="flex flex-wrap items-center gap-3">
            <Button onClick={onRun} disabled={busy}>
              {busy ? "提炼中…（最长约 2 分钟）" : "提炼爆款规律"}
            </Button>
            <span className="text-xs text-muted-foreground">
              首次运行会加载 / 下载本地向量模型（约 1.3GB）；样本不足时不调用模型、不写入。
            </span>
          </div>
          {notice ? <Notice text={notice} /> : null}
          <ErrorText error={error} />
        </CardContent>
      </Card>

      {result ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              提炼结果{" "}
              <Badge variant={result.insufficient_sample ? "outline" : "default"}>
                {result.status}
              </Badge>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <div>
                <div className="text-muted-foreground">新增写入</div>
                <div className="text-xl font-semibold">{result.persisted}</div>
              </div>
              <div>
                <div className="text-muted-foreground">复用（已存在）</div>
                <div className="text-xl font-semibold">{result.reused}</div>
              </div>
              <div>
                <div className="text-muted-foreground">跳过（批内重复）</div>
                <div className="text-xl font-semibold">{result.skipped}</div>
              </div>
              <div>
                <div className="text-muted-foreground">剔除（不合格）</div>
                <div className="text-xl font-semibold">{result.rejected}</div>
              </div>
            </div>
            <div className="text-muted-foreground">
              样本 {result.sample_size ?? "—"} / 门槛 {result.min_required ?? "—"}；JSON 解析尝试{" "}
              {result.parse_attempts ?? "—"}；提示词版本 {result.prompt_version ?? "—"}
            </div>
            {result.data_gaps.length ? (
              <div>
                <div className="mb-1 font-medium">数据缺口（如实声明）</div>
                <ul className="list-disc space-y-1 pl-5 text-muted-foreground">
                  {result.data_gaps.map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {result.warnings.length ? (
              <div>
                <div className="mb-1 font-medium">宽容修正记录</div>
                <ul className="list-disc space-y-1 pl-5 text-muted-foreground">
                  {result.warnings.map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ 检索 Tab */

function SearchTab({
  query,
  searchType,
  searchK,
  busy,
  error,
  notice,
  result,
  onQuery,
  onType,
  onK,
  onRun,
}: {
  query: string;
  searchType: string;
  searchK: string;
  busy: boolean;
  error: ApiError | null;
  notice: string;
  result: KnowledgeSearchResult | null;
  onQuery: (value: string) => void;
  onType: (value: string) => void;
  onK: (value: string) => void;
  onRun: () => void;
}) {
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">检索测试（pgvector 余弦 top-k）</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground">
            输入一段文本，查看知识库的召回结果（相似度降序），用于人工验收 RAG 质量。
          </p>
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-72 flex-1">
              <Label className="mb-1 block text-xs text-muted-foreground">查询文本</Label>
              <Input
                value={query}
                onChange={(event) => onQuery(event.target.value)}
                placeholder="例如：通勤穿搭 早八 省时"
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !busy) onRun();
                }}
              />
            </div>
            <div className="w-36">
              <Label className="mb-1 block text-xs text-muted-foreground">类型</Label>
              <Select
                items={Object.fromEntries(SEARCH_TYPE_OPTIONS.map((item) => [item.value, item.label]))}
                value={searchType}
                onValueChange={(value) => onType(String(value))}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SEARCH_TYPE_OPTIONS.map((item) => (
                    <SelectItem key={item.value} value={item.value}>
                      {item.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="w-24">
              <Label className="mb-1 block text-xs text-muted-foreground">k</Label>
              <Select value={searchK} onValueChange={(value) => onK(String(value))}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SEARCH_K_OPTIONS.map((value) => (
                    <SelectItem key={value} value={value}>
                      {value}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Button onClick={onRun} disabled={busy}>
              {busy ? "检索中…" : "检索"}
            </Button>
          </div>
          {notice ? <Notice text={notice} /> : null}
          <ErrorText error={error} />
        </CardContent>
      </Card>

      {result ? (
        <div className="space-y-2">
          {result.items.map((item) => (
            <Card key={item.id}>
              <CardContent className="space-y-1.5 py-4 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="secondary">
                    {CHUNK_LABELS[item.chunk_type] ?? item.chunk_type}
                  </Badge>
                  <span className="text-xs text-muted-foreground">
                    相似度 {item.similarity.toFixed(4)}
                  </span>
                </div>
                <div className="font-medium">{item.title ?? "（无标题）"}</div>
                <p className="whitespace-pre-wrap text-muted-foreground">{item.content}</p>
              </CardContent>
            </Card>
          ))}
          {result.items.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              无命中：知识库中还没有相似内容（可先提炼规律，或换个说法再试）。
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ 主组件 */

function KnowledgeInner() {
  const [tab, setTab] = useState<string>(TAB_SNIPPETS);

  /* 知识块列表 */
  const [chunkFilter, setChunkFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [page, setPage] = useState(1);
  const [list, setList] = useState<SnippetList | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState<ApiError | null>(null);
  const [listToken, setListToken] = useState(0);

  /* 详情 Dialog 与归档 */
  const [detail, setDetail] = useState<KnowledgeSnippet | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [patchBusyId, setPatchBusyId] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState("");
  const [actionError, setActionError] = useState<ApiError | null>(null);

  /* 新建 / 编辑（第 19 步）：Dialog 每次打开由 key 重新挂载（避免 set-state-in-effect） */
  const [createOpen, setCreateOpen] = useState(false);
  const [createSeq, setCreateSeq] = useState(0);
  const [createPreset, setCreatePreset] = useState<ChunkTypeName>("general");
  const [editTarget, setEditTarget] = useState<KnowledgeSnippet | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [editSeq, setEditSeq] = useState(0);

  /* 提炼 */
  const [publishedCount, setPublishedCount] = useState<number | null>(null);
  const [publishedError, setPublishedError] = useState<ApiError | null>(null);
  const [extractBusy, setExtractBusy] = useState(false);
  const [extractError, setExtractError] = useState<ApiError | null>(null);
  const [extractNotice, setExtractNotice] = useState("");
  const [extractResult, setExtractResult] = useState<ExtractResult | null>(null);

  /* 检索测试 */
  const [query, setQuery] = useState("");
  const [searchType, setSearchType] = useState("all");
  const [searchK, setSearchK] = useState("5");
  const [searchBusy, setSearchBusy] = useState(false);
  const [searchError, setSearchError] = useState<ApiError | null>(null);
  const [searchNotice, setSearchNotice] = useState("");
  const [searchResult, setSearchResult] = useState<KnowledgeSearchResult | null>(null);

  /* ------------------------------------------------------------ 数据加载 */

  // 知识块列表（筛选 / 页码 / 手动刷新变化时重拉）
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      setListLoading(true);
      try {
        const data = await apiClient.get<SnippetList>(
          `/api/v1/knowledge${toQuery({
            chunk_type: chunkFilter === "all" ? null : chunkFilter,
            status: statusFilter === "all" ? null : statusFilter,
            limit: PAGE_SIZE,
            offset: (page - 1) * PAGE_SIZE,
          })}`,
        );
        if (!cancelled) {
          setList(data);
          setListError(null);
        }
      } catch (err) {
        if (!cancelled) setListError(toApiError(err));
      } finally {
        if (!cancelled) setListLoading(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [chunkFilter, statusFilter, page, listToken]);

  // 已发布笔记数（复用看板 summary 的 counts；提炼 Tab 展示样本情况）
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<DashboardSummary>(
          `/api/v1/dashboard/summary${toQuery({ range_days: 7 })}`,
        );
        if (!cancelled) {
          setPublishedCount(Number(data.counts.notes_published ?? 0));
          setPublishedError(null);
        }
      } catch (err) {
        if (!cancelled) setPublishedError(toApiError(err));
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, []);

  const reloadList = useCallback(() => {
    setListToken((token) => token + 1);
  }, []);

  /* ------------------------------------------------------------ 归档 / 恢复 */
  const onToggleStatus = useCallback(
    async (snippet: KnowledgeSnippet) => {
      const next: SnippetStatusName = snippet.status === "active" ? "archived" : "active";
      setPatchBusyId(snippet.id);
      setActionError(null);
      setActionNotice("");
      try {
        const updated = await apiClient.patch<KnowledgeSnippet>(
          `/api/v1/knowledge/${snippet.id}`,
          { status: next },
        );
        setActionNotice(
          next === "archived" ? "已归档（不再参与检索）" : "已恢复（重新参与检索）",
        );
        // 若详情 Dialog 正打开该条，同步刷新其状态
        setDetail((current) => (current && current.id === updated.id ? updated : current));
        reloadList();
      } catch (err) {
        setActionError(toApiError(err));
      } finally {
        setPatchBusyId(null);
      }
    },
    [reloadList],
  );

  /* ------------------------------------------------------------ 新建 / 编辑 */
  const openCreate = useCallback((preset: ChunkTypeName) => {
    setCreatePreset(preset);
    setCreateSeq((seq) => seq + 1);
    setCreateOpen(true);
  }, []);

  const openEdit = useCallback((snippet: KnowledgeSnippet) => {
    setEditTarget(snippet);
    setEditSeq((seq) => seq + 1);
    setEditOpen(true);
  }, []);

  const handleCreated = useCallback(
    (result: SnippetWriteResult) => {
      setActionError(null);
      const label = CHUNK_LABELS[result.chunk_type] ?? result.chunk_type;
      const brief =
        result.content.length > 24 ? `${result.content.slice(0, 24)}…` : result.content;
      const warn = result.warnings.length ? ` 提示：${result.warnings.join("；")}` : "";
      setActionNotice(`已创建知识块：${label}「${brief}」（启用中）。${warn}`);
      reloadList();
      setPage(1);
    },
    [reloadList],
  );

  const handleSaved = useCallback(
    (result: SnippetWriteResult) => {
      setActionError(null);
      const warn = result.warnings.length ? ` 提示：${result.warnings.join("；")}` : "";
      setActionNotice(`已保存修改。${warn}`);
      // 详情 / 编辑源对象同步为最新（编辑源供下次打开时初始化）
      setDetail((current) => (current && current.id === result.id ? result : current));
      setEditTarget((current) => (current && current.id === result.id ? result : current));
      reloadList();
    },
    [reloadList],
  );

  /* ------------------------------------------------------------ 提炼 */
  const onRunExtract = useCallback(async () => {
    setExtractError(null);
    setExtractNotice("");
    setExtractBusy(true);
    try {
      const result = await apiClient.post<ExtractResult>("/api/v1/knowledge/extract", {});
      setExtractResult(result);
      if (result.insufficient_sample) {
        setExtractNotice(
          `样本不足：当前 ${result.sample_size ?? 0} 篇已发布笔记，` +
            `至少 ${result.min_required ?? 3} 篇后再提炼（本次未调用模型、未写入知识库）。`,
        );
      } else {
        setExtractNotice(
          `提炼完成：新增 ${result.persisted} 条 / 复用 ${result.reused} 条 / ` +
            `跳过 ${result.skipped} 条${result.rejected ? ` / 剔除 ${result.rejected} 条` : ""}。`,
        );
      }
      reloadList();
      setPage(1);
    } catch (err) {
      const apiErr = toApiError(err);
      setExtractError(apiErr);
      if (apiErr.status === 400) {
        setExtractNotice(
          "提示：400 通常表示向量化未就绪（依赖未安装 / 模型未下载）或 LLM 未配置" +
            "（开关 / key / 模型名占位），请检查后端配置。",
        );
      }
    } finally {
      setExtractBusy(false);
    }
  }, [reloadList]);

  /* ------------------------------------------------------------ 检索 */
  const onRunSearch = useCallback(async () => {
    const text = query.trim();
    setSearchNotice("");
    setSearchError(null);
    if (!text) {
      setSearchNotice("请输入查询文本");
      return;
    }
    setSearchBusy(true);
    try {
      const result = await apiClient.post<KnowledgeSearchResult>("/api/v1/knowledge/search", {
        query: text,
        chunk_types: searchType === "all" ? null : [searchType as ChunkTypeName],
        k: Number(searchK),
      });
      setSearchResult(result);
    } catch (err) {
      const apiErr = toApiError(err);
      setSearchError(apiErr);
      if (apiErr.status === 400) {
        setSearchNotice(
          "提示：400 表示向量化未就绪（依赖未安装 / 模型未下载），请先安装依赖并下载模型。",
        );
      }
    } finally {
      setSearchBusy(false);
    }
  }, [query, searchType, searchK]);

  /* ------------------------------------------------------------ 渲染 */
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">知识库</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          从本账号已发布笔记中提炼爆款规律（向量化存储），并反哺选题与创作。
          规律仅为本账号个人经验，非行业标准；知识块只增 / 归档，不提供删除。
        </p>
      </div>

      <Tabs value={tab} onValueChange={(value) => setTab(String(value))}>
        <TabsList>
          <TabsTrigger value={TAB_SNIPPETS}>知识块</TabsTrigger>
          <TabsTrigger value={TAB_EXTRACT}>提炼规律</TabsTrigger>
          <TabsTrigger value={TAB_SEARCH}>检索测试</TabsTrigger>
          <TabsTrigger value={TAB_IMPORT}>文档导入</TabsTrigger>
        </TabsList>

        <TabsContent value={TAB_SNIPPETS} className="pt-4">
          <SnippetsTab
            list={list}
            loading={listLoading}
            error={listError}
            chunkFilter={chunkFilter}
            statusFilter={statusFilter}
            page={page}
            onChunkFilter={(value) => {
              setChunkFilter(value);
              setPage(1);
            }}
            onStatusFilter={(value) => {
              setStatusFilter(value);
              setPage(1);
            }}
            onPage={setPage}
            onRefresh={reloadList}
            onOpenDetail={(snippet) => {
              setDetail(snippet);
              setDetailOpen(true);
            }}
            onEdit={openEdit}
            onCreate={openCreate}
            onToggleStatus={onToggleStatus}
            patchBusyId={patchBusyId}
            actionNotice={actionNotice}
            actionError={actionError}
          />
        </TabsContent>

        <TabsContent value={TAB_EXTRACT} className="pt-4">
          <ExtractTab
            publishedCount={publishedCount}
            publishedError={publishedError}
            busy={extractBusy}
            error={extractError}
            notice={extractNotice}
            result={extractResult}
            onRun={() => void onRunExtract()}
          />
        </TabsContent>

        <TabsContent value={TAB_SEARCH} className="pt-4">
          <SearchTab
            query={query}
            searchType={searchType}
            searchK={searchK}
            busy={searchBusy}
            error={searchError}
            notice={searchNotice}
            result={searchResult}
            onQuery={setQuery}
            onType={setSearchType}
            onK={setSearchK}
            onRun={() => void onRunSearch()}
          />
        </TabsContent>

        <TabsContent value={TAB_IMPORT} className="pt-4">
          <ImportTab onImported={reloadList} />
        </TabsContent>
      </Tabs>

      {/* ---------------- 详情 Dialog ---------------- */}
      <Dialog open={detailOpen} onOpenChange={setDetailOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>知识块详情</DialogTitle>
          </DialogHeader>
          <SnippetDetail
            snippet={detail}
            onToggleStatus={(snippet) => void onToggleStatus(snippet)}
            busy={patchBusyId === detail?.id}
          />
        </DialogContent>
      </Dialog>

      {/* ---------------- 新建 / 编辑 Dialog（第 19 步） ---------------- */}
      <ManualCreateDialog
        key={`create-${createSeq}`}
        open={createOpen}
        onOpenChange={setCreateOpen}
        presetChunkType={createPreset}
        onCreated={handleCreated}
      />
      {editTarget ? (
        <EditSnippetDialog
          key={`edit-${editSeq}`}
          snippet={editTarget}
          open={editOpen}
          onOpenChange={setEditOpen}
          onSaved={handleSaved}
        />
      ) : null}
    </div>
  );
}

export default function KnowledgePage() {
  return (
    <Suspense fallback={<p className="text-sm text-muted-foreground">加载中…</p>}>
      <KnowledgeInner />
    </Suspense>
  );
}
