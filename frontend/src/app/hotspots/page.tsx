"use client";

/**
 * 「热点雷达」页面（第 11 步）。
 *
 * 全栈链路：本页（浏览器）-> 本站 /api/v1/**（Route Handler 服务端代理）-> FastAPI。
 *
 * 三段：
 *  1. 源状态区：GET /hotspots/sources（配置清单 + 最近一次采集统计）+「手动刷新」POST /hotspots/refresh；
 *  2. 筛选 + 列表：GET /hotspots（source/status/q/hot_date/sort/order + 分页）；行内可标记状态（PATCH）与生成选题；
 *  3. 折叠区：GET /hotspots/runs（最近采集运行）。
 *
 * 数据获取约定：请求放在 effect 的异步 IIFE 中，**首次 await 之前不做 setState**，
 * 加载态由事件处理器显式打开（满足 react-hooks/set-state-in-effect）。
 * 热点外链只指向榜单原文（url / mobile_url），**不请求任何小红书域名**。
 */

import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
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
  ConfirmDialog,
  ErrorText,
  INITIAL_LOAD,
  Notice,
  Pagination,
  formatDateTime,
  formatNumber,
  toApiError,
} from "@/components/shared";
import type { LoadState } from "@/components/shared";
import { TopicGenerateDialog } from "@/components/topic-generate-dialog";
import { KeywordSearchPanel } from "./search-panel";
import { ApiError, apiClient, toQuery } from "@/lib/api-client";
import type {
  Hotspot,
  HotspotRefreshResult,
  HotspotRun,
  HotspotSource,
  HotspotStatus,
  Page,
} from "@/lib/types";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 10;

/** Tab 常量（第 23 步：热点雷达 + 关键词搜索） */
const TAB_RADAR = "radar";
const TAB_SEARCH = "search";

/** hot_date 三种模式：今天（不传）/ 不限（all）/ 自定义（用 date 值） */
const DATE_TODAY = "__today__";
const DATE_ALL = "all";
const DATE_CUSTOM = "__custom__";

interface HotspotQuery {
  page: number;
  source: string;
  status: string;
  q: string;
  hotDateMode: string;
  hotDate: string;
  sort: string;
  order: string;
}

const INITIAL_QUERY: HotspotQuery = {
  page: 1,
  source: "all",
  status: "all",
  q: "",
  hotDateMode: DATE_TODAY,
  hotDate: "",
  sort: "hot_score",
  order: "desc",
};

function HotspotStatusBadge({ status }: { status: HotspotStatus }) {
  const map: Record<HotspotStatus, { text: string; cls: string }> = {
    new: { text: "新", cls: "border-sky-600/30 bg-sky-500/15 text-sky-700" },
    adopted: { text: "已采用", cls: "border-emerald-600/30 bg-emerald-500/15 text-emerald-700" },
    ignored: { text: "已忽略", cls: "border-border bg-muted text-muted-foreground" },
  };
  const item = map[status] ?? map.new;
  return <Badge className={item.cls}>{item.text}</Badge>;
}

export default function HotspotsPage() {
  const [tab, setTab] = useState(TAB_RADAR);
  const [query, setQuery] = useState<HotspotQuery>(INITIAL_QUERY);
  const [reloadToken, setReloadToken] = useState(0);
  const [state, setState] = useState<LoadState<Hotspot>>(INITIAL_LOAD);
  const [qInput, setQInput] = useState("");

  const [sources, setSources] = useState<HotspotSource[]>([]);
  const [sourcesError, setSourcesError] = useState<ApiError | null>(null);

  const [runs, setRuns] = useState<HotspotRun[]>([]);
  const [runsError, setRunsError] = useState<ApiError | null>(null);
  const [runsToken, setRunsToken] = useState(0);

  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<ApiError | null>(null);
  const [refreshNotice, setRefreshNotice] = useState("");
  const [refreshResult, setRefreshResult] = useState<HotspotRefreshResult | null>(null);

  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const [confirmTarget, setConfirmTarget] = useState<{
    hotspot: Hotspot;
    next: HotspotStatus;
  } | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);

  const [genOpen, setGenOpen] = useState(false);
  const [genKey, setGenKey] = useState(0);
  const [genHotspotIds, setGenHotspotIds] = useState<string[]>([]);
  const [genLabel, setGenLabel] = useState("");

  // 源状态（挂载时拉一次）
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<HotspotSource[]>("/api/v1/hotspots/sources");
        if (!cancelled) {
          setSources(data);
          setSourcesError(null);
        }
      } catch (err) {
        if (!cancelled) setSourcesError(toApiError(err));
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, []);

  // 最近采集运行
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<HotspotRun[]>("/api/v1/hotspots/runs?limit=10");
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

  // 热点列表
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const hotDateParams =
          query.hotDateMode === DATE_TODAY
            ? {}
            : { hot_date: query.hotDateMode === DATE_ALL ? DATE_ALL : query.hotDate };
        const data = await apiClient.get<Page<Hotspot>>(
          `/api/v1/hotspots${toQuery({
            page: query.page,
            page_size: PAGE_SIZE,
            source: query.source === "all" ? null : query.source,
            status: query.status === "all" ? null : query.status,
            q: query.q,
            sort: query.sort,
            order: query.order,
            ...hotDateParams,
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
  }, [query, reloadToken]);

  const applyQuery = useCallback((patch: Partial<HotspotQuery>) => {
    setState((prev) => ({ ...prev, loading: true }));
    setQuery((prev) => ({ ...prev, ...patch }));
    setReloadToken((token) => token + 1);
  }, []);

  const reload = useCallback(() => {
    setState((prev) => ({ ...prev, loading: true }));
    setReloadToken((token) => token + 1);
    setRunsToken((token) => token + 1);
  }, []);

  const doRefresh = useCallback(async () => {
    setRefreshing(true);
    setRefreshError(null);
    setRefreshNotice("");
    try {
      const res = await apiClient.post<HotspotRefreshResult>("/api/v1/hotspots/refresh", {});
      setRefreshResult(res);
      setRefreshNotice("采集完成");
      reload();
    } catch (err) {
      const apiErr = toApiError(err);
      if (apiErr.status === 409 && apiErr.code === "fetch_in_progress") {
        setRefreshNotice(apiErr.message || "已有采集在进行，请稍候再试");
      } else {
        setRefreshError(apiErr);
      }
    } finally {
      setRefreshing(false);
    }
  }, [reload]);

  const changeStatus = useCallback(
    async (hotspot: Hotspot, next: HotspotStatus) => {
      setConfirmBusy(true);
      setActionError(null);
      try {
        await apiClient.patch<Hotspot>(`/api/v1/hotspots/${hotspot.id}`, { status: next });
        const labels: Record<HotspotStatus, string> = {
          new: "已重置为新",
          adopted: "已标记为已采用",
          ignored: "已标记为已忽略",
        };
        setNotice(`${labels[next]}：${hotspot.title.slice(0, 20)}`);
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

  const openGenerate = (hotspot: Hotspot) => {
    setGenHotspotIds([hotspot.id]);
    setGenLabel(hotspot.title);
    setGenKey((key) => key + 1);
    setGenOpen(true);
  };

  const rows = state.data?.items ?? [];

  return (
    <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col gap-6 px-6 py-10">
      <header className="flex flex-col gap-2">
        <h1 className="text-2xl font-medium tracking-tight">热点雷达</h1>
        <p className="text-sm text-muted-foreground">
          消费公开榜单热点（每日幂等采集）；第 23 步新增「关键词搜索」：按关键字查询公开源（GitHub）。可人工标记「采用 / 忽略」，并对单条热点一键生成选题。
          本工具不登录小红书、不请求任何小红书域名、不自动发布。
        </p>
      </header>

      <Tabs value={tab} onValueChange={(value) => setTab(String(value))}>
        <TabsList>
          <TabsTrigger value={TAB_RADAR}>热点雷达</TabsTrigger>
          <TabsTrigger value={TAB_SEARCH}>关键词搜索</TabsTrigger>
        </TabsList>

        <TabsContent value={TAB_RADAR} className="flex flex-col gap-6 pt-4">
        {/* ---------------- 源状态区 ---------------- */}
        <Card>
          <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-col gap-1">
                <CardTitle>数据源状态</CardTitle>
                <CardDescription>各源是否启用，以及最近一次采集的统计。</CardDescription>
              </div>
              <Button onClick={() => void doRefresh()} disabled={refreshing}>
                {refreshing ? "采集中…" : "手动刷新"}
              </Button>
            </div>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <p className="text-xs text-muted-foreground">采集中，可能需数秒~1 分钟；期间请勿重复点击。</p>

            {sourcesError ? <ErrorText error={sourcesError} /> : null}

            {sources.length > 0 ? (
              <div className="flex flex-wrap gap-2">
                {sources.map((source) => (
                  <div
                    key={source.source}
                    className="flex min-w-56 flex-col gap-1 rounded-lg border border-border px-3 py-2 text-xs"
                  >
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium">{source.name}</span>
                      <Badge
                        className={cn(
                          source.enabled
                            ? "border-emerald-600/30 bg-emerald-500/15 text-emerald-700"
                            : "border-border bg-muted text-muted-foreground",
                        )}
                      >
                        {source.enabled ? "启用" : "停用"}
                      </Badge>
                    </div>
                    <span className="text-muted-foreground">
                      最近采集：{source.last_fetched_at ? formatDateTime(source.last_fetched_at) : "—"}
                    </span>
                    <span className="text-muted-foreground">
                      状态 {source.last_status ?? "—"} · 新增 {formatNumber(source.last_new)} · 更新{" "}
                      {formatNumber(source.last_updated)} · 失败 {formatNumber(source.last_failed)}
                    </span>
                  </div>
                ))}
              </div>
            ) : null}

            {refreshNotice ? <Notice text={refreshNotice} /> : null}
            <ErrorText error={refreshError} />

            {refreshResult ? (
              <div className="flex flex-col gap-2">
                <p className="text-sm font-medium">
                  本次采集：源 {refreshResult.totals.sources} · 抓取 {refreshResult.totals.fetched} ·
                  新增 {refreshResult.totals.new} · 更新 {refreshResult.totals.updated} · 失败源{" "}
                  {refreshResult.totals.failed} · 拦截源 {refreshResult.totals.blocked}
                </p>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>源</TableHead>
                      <TableHead>状态</TableHead>
                      <TableHead>抓取</TableHead>
                      <TableHead>新增</TableHead>
                      <TableHead>更新</TableHead>
                      <TableHead>失败</TableHead>
                      <TableHead>拦截</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {refreshResult.per_source.map((item) => (
                      <TableRow key={item.source}>
                        <TableCell>{item.source_name}</TableCell>
                        <TableCell className="text-muted-foreground">{item.status}</TableCell>
                        <TableCell>{item.fetched}</TableCell>
                        <TableCell>{item.new}</TableCell>
                        <TableCell>{item.updated}</TableCell>
                        <TableCell>{item.failed}</TableCell>
                        <TableCell>{item.blocked}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            ) : null}
          </CardContent>
        </Card>

        {/* ---------------- 筛选栏 ---------------- */}
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            applyQuery({ q: qInput.trim(), page: 1 });
          }}
        >
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="hs-q">关键词</Label>
            <Input
              id="hs-q"
              value={qInput}
              placeholder="按标题模糊匹配"
              onChange={(event) => setQInput(event.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="hs-source">来源</Label>
            <Select
              items={{
                all: "全部来源",
                ...Object.fromEntries(sources.map((s) => [s.source, s.name])),
              }}
              value={query.source}
              onValueChange={(value) => applyQuery({ source: String(value), page: 1 })}
            >
              <SelectTrigger id="hs-source" className="w-36">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部来源</SelectItem>
                {sources.map((source) => (
                  <SelectItem key={source.source} value={source.source}>
                    {source.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="hs-status">状态</Label>
            <Select
              items={{ all: "全部", new: "新", adopted: "已采用", ignored: "已忽略" }}
              value={query.status}
              onValueChange={(value) => applyQuery({ status: String(value), page: 1 })}
            >
              <SelectTrigger id="hs-status" className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部</SelectItem>
                <SelectItem value="new">新</SelectItem>
                <SelectItem value="adopted">已采用</SelectItem>
                <SelectItem value="ignored">已忽略</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="hs-date-mode">归属日期</Label>
            <Select
              items={{ [DATE_TODAY]: "今天", [DATE_ALL]: "不限", [DATE_CUSTOM]: "自定义" }}
              value={query.hotDateMode}
              onValueChange={(value) => applyQuery({ hotDateMode: String(value), page: 1 })}
            >
              <SelectTrigger id="hs-date-mode" className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={DATE_TODAY}>今天</SelectItem>
                <SelectItem value={DATE_ALL}>不限</SelectItem>
                <SelectItem value={DATE_CUSTOM}>自定义</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {query.hotDateMode === DATE_CUSTOM ? (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="hs-date">日期</Label>
              <Input
                id="hs-date"
                type="date"
                className="w-40"
                value={query.hotDate}
                onChange={(event) => applyQuery({ hotDate: event.target.value, page: 1 })}
              />
            </div>
          ) : null}
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="hs-sort">排序</Label>
            <Select
              items={{ hot_score: "热度", fetched_at: "采集时间" }}
              value={query.sort}
              onValueChange={(value) => applyQuery({ sort: String(value), page: 1 })}
            >
              <SelectTrigger id="hs-sort" className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="hot_score">热度</SelectItem>
                <SelectItem value="fetched_at">采集时间</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="hs-order">方向</Label>
            <Select
              items={{ desc: "降序", asc: "升序" }}
              value={query.order}
              onValueChange={(value) => applyQuery({ order: String(value), page: 1 })}
            >
              <SelectTrigger id="hs-order" className="w-28">
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

        {/* ---------------- 列表 ---------------- */}
        <Card>
          <CardContent className="pt-4">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>标题</TableHead>
                  <TableHead>来源</TableHead>
                  <TableHead>热度</TableHead>
                  <TableHead>名次</TableHead>
                  <TableHead>归属日期</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>采集时间</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {state.loading ? (
                  <TableRow>
                    <TableCell colSpan={8} className="py-6 text-center text-muted-foreground">
                      加载中……
                    </TableCell>
                  </TableRow>
                ) : state.error ? (
                  <TableRow>
                    <TableCell colSpan={8} className="py-6 text-center">
                      <ErrorText error={state.error} />
                    </TableCell>
                  </TableRow>
                ) : rows.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={8} className="py-6 text-center text-muted-foreground">
                      暂无数据（可切换来源 / 日期，或点击「手动刷新」采集）
                    </TableCell>
                  </TableRow>
                ) : (
                  rows.map((hotspot) => {
                    const href = hotspot.url ?? hotspot.mobile_url ?? null;
                    return (
                      <TableRow key={hotspot.id}>
                        <TableCell className="max-w-72 min-w-56 whitespace-normal font-medium">
                          {href ? (
                            <a
                              href={href}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-primary underline-offset-4 hover:underline"
                            >
                              {hotspot.title}
                            </a>
                          ) : (
                            hotspot.title
                          )}
                        </TableCell>
                        <TableCell>
                          <Badge variant="outline">{hotspot.source}</Badge>
                        </TableCell>
                        <TableCell>{formatNumber(hotspot.hot_score)}</TableCell>
                        <TableCell className="text-muted-foreground">
                          {hotspot.rank_position ?? "—"}
                        </TableCell>
                        <TableCell className="text-muted-foreground">
                          {hotspot.hot_date ?? "—"}
                        </TableCell>
                        <TableCell>
                          <HotspotStatusBadge status={hotspot.status} />
                        </TableCell>
                        <TableCell className="text-muted-foreground">
                          {hotspot.fetched_at ? formatDateTime(hotspot.fetched_at) : "—"}
                        </TableCell>
                        <TableCell className="text-right">
                          <div className="flex justify-end gap-1.5">
                            <Button size="sm" onClick={() => openGenerate(hotspot)}>
                              生成选题
                            </Button>
                            {hotspot.status === "new" ? (
                              <>
                                <Button
                                  variant="secondary"
                                  size="sm"
                                  onClick={() => setConfirmTarget({ hotspot, next: "adopted" })}
                                >
                                  采用
                                </Button>
                                <Button
                                  variant="outline"
                                  size="sm"
                                  onClick={() => setConfirmTarget({ hotspot, next: "ignored" })}
                                >
                                  忽略
                                </Button>
                              </>
                            ) : (
                              <Button
                                variant="outline"
                                size="sm"
                                onClick={() => setConfirmTarget({ hotspot, next: "new" })}
                              >
                                重置为新
                              </Button>
                            )}
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

        {/* ---------------- 折叠区：最近采集运行 ---------------- */}
        <details className="rounded-xl border border-border">
          <summary className="cursor-pointer px-4 py-3 text-sm font-medium">
            最近采集运行（limit 10）
          </summary>
          <div className="border-t border-border p-4">
            {runsError ? <ErrorText error={runsError} /> : null}
            {runs.length === 0 && !runsError ? (
              <p className="text-sm text-muted-foreground">暂无运行记录。</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>触发</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead>耗时(ms)</TableHead>
                    <TableHead>新增/更新</TableHead>
                    <TableHead>开始时间</TableHead>
                    <TableHead>错误</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {runs.map((run) => (
                    <TableRow key={run.id}>
                      <TableCell>{run.trigger}</TableCell>
                      <TableCell>{run.status ?? "—"}</TableCell>
                      <TableCell className="text-muted-foreground">{run.latency_ms ?? "—"}</TableCell>
                      <TableCell className="text-muted-foreground">
                        {run.outputs
                          ? `${String(run.outputs.new ?? "—")} / ${String(run.outputs.updated ?? "—")}`
                          : "—"}
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        {formatDateTime(run.created_at)}
                      </TableCell>
                      <TableCell className="max-w-72 whitespace-normal text-red-600">
                        {run.error_message ?? "—"}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </div>
        </details>
        </TabsContent>

        <TabsContent value={TAB_SEARCH} className="pt-4">
          <KeywordSearchPanel />
        </TabsContent>
      </Tabs>

      <ConfirmDialog
        open={confirmTarget !== null}
        title="确认变更热点状态？"
        description={
          confirmTarget
            ? `将「${confirmTarget.hotspot.title.slice(0, 24)}」标记为${
                confirmTarget.next === "new"
                  ? "新"
                  : confirmTarget.next === "adopted"
                    ? "已采用"
                    : "已忽略"
              }。状态可随时人工改回，不影响已生成的选题。`
            : ""
        }
        confirmText="确认"
        busy={confirmBusy}
        onCancel={() => setConfirmTarget(null)}
        onConfirm={() =>
          confirmTarget && void changeStatus(confirmTarget.hotspot, confirmTarget.next)
        }
      />

      <TopicGenerateDialog
        key={genKey}
        open={genOpen}
        onOpenChange={setGenOpen}
        hotspotIds={genHotspotIds}
        hotspotLabel={genLabel}
        onGenerated={reload}
      />
    </main>
  );
}
