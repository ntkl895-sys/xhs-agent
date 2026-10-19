"use client";

/**
 * 「运营看板 + 周报」页面（第 17 步）。
 *
 * 全栈链路：本页（浏览器）-> 本站 /api/v1/**（Route Handler 服务端代理）-> FastAPI。
 *
 * - 顶部：「看板 / 周报」Tabs + 时间范围选择（默认 30 天）；
 * - 看板 Tab（**确定性只读聚合，零 LLM**）：
 *   ① KPI 卡行（账号 / 笔记分状态 / 在写选题 / 周期内发布）；
 *   ② 漏斗卡（六指标合计 + 六率；views 合计为 0 时显示「不可计算」）；
 *   ③ 趋势卡（指标切换 + 纯 SVG 折线；null 断线不插值）；
 *   ④ 排行卡（指标切换 + 横条；只列已发布笔记，点标题跳创作台）；
 * - 周报 Tab（**真 LangGraph 图，只读 + 建议**）：
 *   ① 周期选择（近 7 天默认 / 显式起止）+「生成周报」（BFF 超时 120s）；
 *   ② 结构化周报：概览 / 亮点 / 问题 / 数据依据 / 下周建议（逐条可复制）/
 *      数据缺口 / 置信度；
 *   ③ 周报历史（点「查看」拉详情回填）。
 *
 * 红线：看板与周报**只读**（不改笔记 / 选题 / 数据、不自动发布、不登录小红书）；
 * 本期无任何外部基准（跨篇基准属第 18 步 RAG）；接口不返回 ``llm_raw``；
 * 数字全部由后端程序计算，前端只做展示（图表组件不做二次聚合）。
 *
 * ⚠️ Next 16：动态 URL hook 必须包在 `<Suspense>` 中，故拆成 `DashboardInner`，
 * 外层 `<Suspense>` 包裹（本页当前未用 URL 参数，保留结构以便后续扩展）。
 */

import { Suspense, useCallback, useEffect, useState } from "react";

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
  formatDateTime,
  formatNumber,
  toApiError,
} from "@/components/shared";
import { ApiError, apiClient, toQuery } from "@/lib/api-client";
import { FunnelBars } from "@/lib/charts/FunnelBars";
import { HBars } from "@/lib/charts/HBars";
import { LineChart } from "@/lib/charts/LineChart";
import type {
  DashboardRanking,
  DashboardSummary,
  DashboardTrend,
  MetricName,
  WeeklyReport,
  WeeklyReportDetail,
  WeeklyReportGenerateRequest,
  WeeklyReportList,
  WeeklyReportSections,
} from "@/lib/types";

/* ------------------------------------------------------------------ 常量 */

const TAB_BOARD = "board";
const TAB_WEEKLY = "weekly";

/** 看板时间范围（近 N 个自然日） */
const RANGE_OPTIONS: { value: string; label: string }[] = [
  { value: "7", label: "近 7 天" },
  { value: "14", label: "近 14 天" },
  { value: "30", label: "近 30 天" },
  { value: "90", label: "近 90 天" },
];

/** 六个指标（顺序即展示顺序，与后端 METRIC_FIELDS 一致） */
const METRICS: { key: MetricName; label: string }[] = [
  { key: "views", label: "浏览量" },
  { key: "likes", label: "点赞" },
  { key: "collects", label: "收藏" },
  { key: "comments", label: "评论" },
  { key: "shares", label: "分享" },
  { key: "new_followers", label: "新增粉丝" },
];

/** KPI 卡（键与后端 counts 字典一致） */
const COUNT_ITEMS: { key: string; label: string }[] = [
  { key: "accounts", label: "账号总数" },
  { key: "notes_total", label: "笔记总数" },
  { key: "notes_published", label: "已发布" },
  { key: "notes_ready", label: "待发布" },
  { key: "notes_draft", label: "草稿" },
  { key: "topics_writing", label: "在写选题" },
  { key: "notes_published_in_range", label: "周期内发布" },
];

/** 周报常用周期（近 N 天） */
const WEEKLY_DAYS_OPTIONS: { value: string; label: string }[] = [
  { value: "7", label: "近 7 天（默认）" },
  { value: "14", label: "近 14 天" },
  { value: "30", label: "近 30 天" },
];

/* ------------------------------------------------------------------ 工具 */

/** 周期展示（ISO -> 「2026-10-06 ~ 2026-10-12」；缺 → 「—」） */
function periodText(start: string | null | undefined, end: string | null | undefined): string {
  const s = start ? start.slice(0, 10) : null;
  const e = end ? end.slice(0, 10) : null;
  if (!s && !e) return "—";
  return `${s ?? "—"} ~ ${e ?? "—"}`;
}

/** 复制到剪贴板（Clipboard API + 隐藏 textarea 双兜底；与诊断页同款） */
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

/* ------------------------------------------------------------------ 主组件 */

function DashboardInner() {
  /* ------------------------------------------------------------ 看板：范围 */
  const [rangeDays, setRangeDays] = useState("30");
  const [tab, setTab] = useState<string>(TAB_BOARD);

  /* ------------------------------------------------------------ 看板：汇总 */
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState<ApiError | null>(null);

  /* ------------------------------------------------------------ 看板：趋势 */
  const [trendMetric, setTrendMetric] = useState<MetricName>("views");
  const [trend, setTrend] = useState<DashboardTrend | null>(null);
  const [trendLoading, setTrendLoading] = useState(true);
  const [trendError, setTrendError] = useState<ApiError | null>(null);

  /* ------------------------------------------------------------ 看板：排行 */
  const [rankMetric, setRankMetric] = useState<MetricName>("views");
  const [ranking, setRanking] = useState<DashboardRanking | null>(null);
  const [rankingLoading, setRankingLoading] = useState(true);
  const [rankingError, setRankingError] = useState<ApiError | null>(null);

  /* ------------------------------------------------------------ 周报：生成 */
  const [genDays, setGenDays] = useState("7");
  const [periodStart, setPeriodStart] = useState("");
  const [periodEnd, setPeriodEnd] = useState("");
  const [weeklyGenerating, setWeeklyGenerating] = useState(false);
  const [weeklyError, setWeeklyError] = useState<ApiError | null>(null);
  const [weeklyFormError, setWeeklyFormError] = useState("");
  const [weeklyNotice, setWeeklyNotice] = useState("");
  const [activeReport, setActiveReport] = useState<
    WeeklyReport | WeeklyReportDetail | null
  >(null);
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null);

  /* ------------------------------------------------------------ 周报：历史 */
  const [history, setHistory] = useState<WeeklyReportList | null>(null);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState<ApiError | null>(null);
  const [historyToken, setHistoryToken] = useState(0);
  const [detailBusyId, setDetailBusyId] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<ApiError | null>(null);

  /* ------------------------------------------------------------ 数据加载 */

  // 汇总（rangeDays 变化时重拉）
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      setSummaryLoading(true);
      try {
        const data = await apiClient.get<DashboardSummary>(
          `/api/v1/dashboard/summary${toQuery({ range_days: Number(rangeDays) })}`,
        );
        if (!cancelled) {
          setSummary(data);
          setSummaryError(null);
        }
      } catch (err) {
        if (!cancelled) setSummaryError(toApiError(err));
      } finally {
        if (!cancelled) setSummaryLoading(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [rangeDays]);

  // 趋势（指标 / 范围变化时重拉）
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      setTrendLoading(true);
      try {
        const data = await apiClient.get<DashboardTrend>(
          `/api/v1/dashboard/trend${toQuery({
            metric: trendMetric,
            range_days: Number(rangeDays),
          })}`,
        );
        if (!cancelled) {
          setTrend(data);
          setTrendError(null);
        }
      } catch (err) {
        if (!cancelled) setTrendError(toApiError(err));
      } finally {
        if (!cancelled) setTrendLoading(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [trendMetric, rangeDays]);

  // 排行（指标变化时重拉）
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      setRankingLoading(true);
      try {
        const data = await apiClient.get<DashboardRanking>(
          `/api/v1/dashboard/ranking${toQuery({ metric: rankMetric, limit: 10 })}`,
        );
        if (!cancelled) {
          setRanking(data);
          setRankingError(null);
        }
      } catch (err) {
        if (!cancelled) setRankingError(toApiError(err));
      } finally {
        if (!cancelled) setRankingLoading(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [rankMetric]);

  // 周报历史（挂载时一次；token 变化时强制重拉）
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<WeeklyReportList>(
          `/api/v1/reports/weekly${toQuery({ limit: 20 })}`,
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
  }, [historyToken]);

  const reloadHistory = useCallback(() => {
    setHistoryLoading(true);
    setHistoryToken((token) => token + 1);
  }, []);

  /* ------------------------------------------------------------ 生成周报 */
  const onGenerateWeekly = async () => {
    setWeeklyError(null);
    setWeeklyNotice("");
    setWeeklyFormError("");
    setDetailError(null);

    const useExplicit = Boolean(periodStart || periodEnd);
    if (periodStart && periodEnd && periodStart > periodEnd) {
      setWeeklyFormError("周期起点晚于终点，请检查日期");
      return;
    }
    if (useExplicit && !periodStart && periodEnd) {
      // 只给终点：后端按默认天数向前补齐（允许，但提示用户更明确）
      setWeeklyNotice("只填了结束日期：起点将按默认周期（近 7 天）自动补齐");
    }

    const payload: WeeklyReportGenerateRequest = {};
    if (useExplicit) {
      payload.period_start = periodStart || null;
      payload.period_end = periodEnd || null;
    } else {
      payload.days = Number(genDays);
    }

    setWeeklyGenerating(true);
    try {
      const result = await apiClient.post<WeeklyReport>(
        "/api/v1/reports/weekly/generate",
        payload,
      );
      setActiveReport(result);
      setCopiedIndex(null);
      setWeeklyNotice(
        `周报已生成：周期 ${periodText(result.period_start, result.period_end)}` +
          (result.data_gaps.length ? `；含 ${result.data_gaps.length} 条数据缺口声明` : ""),
      );
      reloadHistory();
    } catch (err) {
      const apiErr = toApiError(err);
      setWeeklyError(apiErr);
      if (apiErr.status === 400) {
        setWeeklyNotice("提示：400 通常表示 LLM 未就绪（开关 / key / 模型名占位）或结构重试超限，请检查后端配置。");
      }
    } finally {
      setWeeklyGenerating(false);
    }
  };

  /* ------------------------------------------------------------ 周报详情 */
  const openReportDetail = async (runId: string) => {
    setDetailBusyId(runId);
    setDetailError(null);
    try {
      const data = await apiClient.get<WeeklyReportDetail>(
        `/api/v1/reports/weekly/${encodeURIComponent(runId)}`,
      );
      setActiveReport(data);
      setCopiedIndex(null);
      setWeeklyNotice("");
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
    setWeeklyNotice(ok ? "内容已复制到剪贴板" : "复制失败：请手动选中文本复制");
  };

  /* ------------------------------------------------------------ 派生数据 */
  const activeSections: WeeklyReportSections | null =
    activeReport && "report" in activeReport && activeReport.report
      ? activeReport.report
      : null;
  const activeRunId = activeReport ? activeReport.run_id : null;

  const trendPoints = trend?.points ?? [];

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-5 px-4 py-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold">运营看板 · 周报</h1>
        <p className="text-sm text-muted-foreground">
          看板为本地确定性聚合（零 LLM、只读）；周报基于周期事实做复盘建议（不修改任何数据、不自动发布）。
          数字全部由后端程序计算；本期无外部行业基准（跨篇基准见第 18 步）。
        </p>
      </div>

      <Tabs value={tab} onValueChange={(value) => setTab(String(value))}>
        <TabsList>
          <TabsTrigger value={TAB_BOARD}>看板</TabsTrigger>
          <TabsTrigger value={TAB_WEEKLY}>周报</TabsTrigger>
        </TabsList>

        {/* ============================================================ 看板 */}
        <TabsContent value={TAB_BOARD}>
          <div className="flex flex-col gap-5 pt-3">
            {/* 时间范围 */}
            <div className="flex flex-wrap items-end gap-3">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="db-range">时间范围</Label>
                <Select
                  items={Object.fromEntries(RANGE_OPTIONS.map((o) => [o.value, o.label]))}
                  value={rangeDays}
                  onValueChange={(value) => setRangeDays(String(value))}
                >
                  <SelectTrigger id="db-range" className="w-40">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {RANGE_OPTIONS.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              {summary ? (
                <p className="pb-2 text-xs text-muted-foreground">
                  数据生成于 {formatDateTime(summary.generated_at)}
                </p>
              ) : null}
            </div>

            {/* KPI 卡行 */}
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
              {COUNT_ITEMS.map((item) => (
                <Card key={item.key} className="py-3">
                  <CardContent className="flex flex-col gap-1 px-4">
                    <span className="text-xs text-muted-foreground">{item.label}</span>
                    <span className="text-xl font-semibold tabular-nums">
                      {summary ? formatNumber(summary.counts[item.key] ?? 0) : "—"}
                    </span>
                  </CardContent>
                </Card>
              ))}
            </div>
            {summaryLoading ? (
              <p className="text-sm text-muted-foreground">加载汇总…</p>
            ) : summaryError ? (
              <ErrorText error={summaryError} />
            ) : null}

            {/* 漏斗卡 */}
            <Card>
              <CardHeader>
                <CardTitle className="text-base">全账号最新漏斗（已发布笔记合计）</CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                {summary ? (
                  <>
                    <FunnelBars
                      totals={summary.funnel.latest_totals}
                      rates={summary.funnel.rates}
                    />
                    <p className="text-xs text-muted-foreground">
                      覆盖：{summary.funnel.covered_notes}/{summary.funnel.published_notes} 篇已发布笔记有指标数据；
                      缺失按 0 参与合计（各指标覆盖数见下）。
                    </p>
                    <div className="flex flex-wrap gap-2">
                      {METRICS.map((metric) => (
                        <Badge key={metric.key} variant="secondary">
                          {metric.label}：
                          {formatNumber(summary.funnel.metric_coverage[metric.key] ?? 0)} 篇有值
                        </Badge>
                      ))}
                    </div>
                  </>
                ) : summaryLoading ? (
                  <p className="text-sm text-muted-foreground">加载漏斗…</p>
                ) : null}
              </CardContent>
            </Card>

            {/* 趋势卡 */}
            <Card>
              <CardHeader className="flex-row items-center justify-between gap-3">
                <CardTitle className="text-base">指标趋势</CardTitle>
                <Select
                  items={Object.fromEntries(METRICS.map((m) => [m.key, m.label]))}
                  value={trendMetric}
                  onValueChange={(value) => setTrendMetric(String(value) as MetricName)}
                >
                  <SelectTrigger className="w-36" aria-label="趋势指标">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {METRICS.map((metric) => (
                      <SelectItem key={metric.key} value={metric.key}>
                        {metric.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </CardHeader>
              <CardContent className="flex flex-col gap-2">
                {trendLoading ? (
                  <p className="text-sm text-muted-foreground">加载趋势…</p>
                ) : trendError ? (
                  <ErrorText error={trendError} />
                ) : (
                  <>
                    <LineChart
                      points={trendPoints}
                      ariaLabel={`${trendMetric} 趋势`}
                      seriesLabel={
                        METRICS.find((m) => m.key === trendMetric)?.label ?? trendMetric
                      }
                    />
                    {trend ? (
                      <p className="text-xs text-muted-foreground">
                        分桶：{trend.bucket === "day" ? "按天" : `等间隔（每 ${trend.coverage.step_days} 天）`}；
                        覆盖：{trend.coverage.notes_with_data}/{trend.coverage.notes} 篇笔记有数据，
                        有数据桶 {trend.coverage.points_with_data}/{trend.coverage.points_total}；
                        空缺桶断线展示（不补 0）。
                      </p>
                    ) : null}
                  </>
                )}
              </CardContent>
            </Card>

            {/* 排行卡 */}
            <Card>
              <CardHeader className="flex-row items-center justify-between gap-3">
                <CardTitle className="text-base">笔记排行（已发布）</CardTitle>
                <Select
                  items={Object.fromEntries(METRICS.map((m) => [m.key, m.label]))}
                  value={rankMetric}
                  onValueChange={(value) => setRankMetric(String(value) as MetricName)}
                >
                  <SelectTrigger className="w-36" aria-label="排行指标">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {METRICS.map((metric) => (
                      <SelectItem key={metric.key} value={metric.key}>
                        {metric.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </CardHeader>
              <CardContent>
                {rankingLoading ? (
                  <p className="text-sm text-muted-foreground">加载排行…</p>
                ) : rankingError ? (
                  <ErrorText error={rankingError} />
                ) : (
                  <HBars
                    items={(ranking?.items ?? []).map((item) => ({
                      label: item.title ?? `（无标题 ${item.note_id.slice(0, 8)}）`,
                      value: item.value,
                      href: `/notes/${item.note_id}`,
                    }))}
                    emptyText="暂无已发布笔记"
                  />
                )}
              </CardContent>
            </Card>
          </div>
        </TabsContent>

        {/* ============================================================ 周报 */}
        <TabsContent value={TAB_WEEKLY}>
          <div className="flex flex-col gap-5 pt-3">
            {/* 生成卡 */}
            <Card>
              <CardHeader>
                <CardTitle className="text-base">生成周报</CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                <div className="flex flex-wrap items-end gap-3">
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor="wk-days">常用周期</Label>
                    <Select
                      items={Object.fromEntries(
                        WEEKLY_DAYS_OPTIONS.map((o) => [o.value, o.label]),
                      )}
                      value={genDays}
                      onValueChange={(value) => setGenDays(String(value))}
                    >
                      <SelectTrigger id="wk-days" className="w-44">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {WEEKLY_DAYS_OPTIONS.map((option) => (
                          <SelectItem key={option.value} value={option.value}>
                            {option.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor="wk-start">起始日期（可选）</Label>
                    <Input
                      id="wk-start"
                      type="date"
                      value={periodStart}
                      onChange={(event) => setPeriodStart(event.target.value)}
                      className="w-44"
                    />
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor="wk-end">结束日期（可选）</Label>
                    <Input
                      id="wk-end"
                      type="date"
                      value={periodEnd}
                      onChange={(event) => setPeriodEnd(event.target.value)}
                      className="w-44"
                    />
                  </div>
                  <Button
                    type="button"
                    disabled={weeklyGenerating}
                    onClick={() => void onGenerateWeekly()}
                  >
                    {weeklyGenerating ? "周报生成中……（最长约 2 分钟）" : "生成周报"}
                  </Button>
                </div>
                <p className="text-xs text-muted-foreground">
                  只填「起始 / 结束日期」时按显式周期；都不填时按「常用周期」。
                  生成会真实调用模型（消耗 token）；周期跨度上限 90 天。
                </p>
                {weeklyFormError ? (
                  <p className="text-sm text-red-600">{weeklyFormError}</p>
                ) : null}
                {weeklyNotice ? <Notice text={weeklyNotice} /> : null}
                {weeklyError ? <ErrorText error={weeklyError} /> : null}
              </CardContent>
            </Card>

            {/* 报告展示 */}
            {activeReport && activeSections ? (
              <Card>
                <CardHeader className="flex-row items-center justify-between gap-3">
                  <CardTitle className="text-base">周期复盘</CardTitle>
                  <div className="flex items-center gap-2">
                    <Badge variant="secondary">
                      {periodText(activeSections.period_start, activeSections.period_end)}
                    </Badge>
                    {"status" in activeReport && activeReport.status ? (
                      <Badge
                        variant={activeReport.status === "success" ? "default" : "secondary"}
                      >
                        {activeReport.status}
                      </Badge>
                    ) : null}
                  </div>
                </CardHeader>
                <CardContent className="flex flex-col gap-4">
                  {/* 概览 */}
                  <div className="flex flex-col gap-1">
                    <h3 className="text-sm font-medium">周期概览</h3>
                    <p className="whitespace-pre-wrap text-sm">{activeSections.summary || "—"}</p>
                  </div>

                  {/* 亮点 / 问题 / 数据依据 */}
                  <div className="grid gap-4 md:grid-cols-3">
                    <div className="flex flex-col gap-1">
                      <h3 className="text-sm font-medium">亮点</h3>
                      {activeSections.highlights.length ? (
                        <ul className="list-disc pl-5 text-sm">
                          {activeSections.highlights.map((item, index) => (
                            <li key={index}>{item}</li>
                          ))}
                        </ul>
                      ) : (
                        <p className="text-sm text-muted-foreground">—</p>
                      )}
                    </div>
                    <div className="flex flex-col gap-1">
                      <h3 className="text-sm font-medium">问题</h3>
                      {activeSections.issues.length ? (
                        <ul className="list-disc pl-5 text-sm">
                          {activeSections.issues.map((item, index) => (
                            <li key={index}>{item}</li>
                          ))}
                        </ul>
                      ) : (
                        <p className="text-sm text-muted-foreground">—</p>
                      )}
                    </div>
                    <div className="flex flex-col gap-1">
                      <h3 className="text-sm font-medium">数据依据</h3>
                      {activeSections.data_evidence.length ? (
                        <ul className="list-disc pl-5 text-sm">
                          {activeSections.data_evidence.map((item, index) => (
                            <li key={index}>{item}</li>
                          ))}
                        </ul>
                      ) : (
                        <p className="text-sm text-muted-foreground">—</p>
                      )}
                    </div>
                  </div>

                  {/* 下周建议（逐条可复制） */}
                  <div className="flex flex-col gap-2">
                    <h3 className="text-sm font-medium">下周建议（按重要性排序）</h3>
                    {activeSections.next_week_suggestions.length ? (
                      <ol className="flex flex-col gap-2">
                        {activeSections.next_week_suggestions.map((item, index) => (
                          <li
                            key={index}
                            className="flex items-start justify-between gap-3 rounded-md border border-border px-3 py-2"
                          >
                            <span className="text-sm">
                              <span className="mr-1 font-medium tabular-nums">
                                {index + 1}.
                              </span>
                              {item}
                            </span>
                            <Button
                              type="button"
                              variant="outline"
                              size="sm"
                              onClick={() => void onCopySuggestion(index, item)}
                            >
                              {copiedIndex === index ? "已复制" : "复制"}
                            </Button>
                          </li>
                        ))}
                      </ol>
                    ) : (
                      <p className="text-sm text-muted-foreground">—</p>
                    )}
                  </div>

                  {/* 数据缺口 */}
                  <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 dark:border-amber-800 dark:bg-amber-950/30">
                    <p className="text-sm font-medium">基准与数据缺口</p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      本期**无外部行业基准**（行业均值 / 爆款门槛等一律未使用；跨篇基准见第 18 步）。
                      以下为数据缺口如实声明：
                    </p>
                    {activeSections.data_gaps.length ? (
                      <ul className="mt-1 list-disc pl-5 text-sm">
                        {activeSections.data_gaps.map((item, index) => (
                          <li key={index}>{item}</li>
                        ))}
                      </ul>
                    ) : (
                      <p className="mt-1 text-sm text-muted-foreground">未发现数据缺口</p>
                    )}
                  </div>

                  {/* 元信息 */}
                  <p className="text-xs text-muted-foreground">
                    置信度：{activeSections.confidence === null ? "—" : activeSections.confidence}
                    {" · "}
                    JSON 解析尝试：{activeReport.parse_attempts ?? "—"}
                    {"llm_model" in activeReport && activeReport.llm_model
                      ? ` · 模型：${activeReport.llm_model}`
                      : ""}
                    {" · "}
                    查看编号：{activeRunId?.slice(0, 8) ?? "—"}
                  </p>
                </CardContent>
              </Card>
            ) : null}

            {detailError ? <ErrorText error={detailError} /> : null}

            {/* 周报历史 */}
            <Card>
              <CardHeader>
                <CardTitle className="text-base">周报历史</CardTitle>
              </CardHeader>
              <CardContent className="overflow-x-auto">
                {historyLoading ? (
                  <p className="text-sm text-muted-foreground">加载周报历史…</p>
                ) : historyError ? (
                  <ErrorText error={historyError} />
                ) : !history || history.items.length === 0 ? (
                  <p className="text-sm text-muted-foreground">暂无周报记录，先在上方生成一次。</p>
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>生成时间</TableHead>
                        <TableHead>周期</TableHead>
                        <TableHead>概览</TableHead>
                        <TableHead className="text-right">建议数</TableHead>
                        <TableHead className="text-right">缺口数</TableHead>
                        <TableHead className="text-right">操作</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {history.items.map((item) => (
                        <TableRow key={item.run_id}>
                          <TableCell className="whitespace-nowrap text-xs">
                            {item.created_at ? formatDateTime(item.created_at) : "—"}
                          </TableCell>
                          <TableCell className="whitespace-nowrap text-xs">
                            {periodText(item.period_start, item.period_end)}
                          </TableCell>
                          <TableCell className="max-w-72 truncate text-xs" title={item.summary}>
                            {item.summary || "—"}
                          </TableCell>
                          <TableCell className="text-right text-xs tabular-nums">
                            {item.next_week_suggestions_count}
                          </TableCell>
                          <TableCell className="text-right text-xs tabular-nums">
                            {item.data_gaps_count}
                          </TableCell>
                          <TableCell className="text-right">
                            <Button
                              type="button"
                              variant="outline"
                              size="sm"
                              disabled={detailBusyId === item.run_id}
                              onClick={() => void openReportDetail(item.run_id)}
                            >
                              {detailBusyId === item.run_id ? "加载中…" : "查看"}
                            </Button>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}

/* ------------------------------------------------------------------ 外壳 */

export default function DashboardPage() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto flex w-full max-w-6xl flex-col gap-3 px-4 py-6">
          <p className="text-sm text-muted-foreground">加载运营看板…</p>
        </div>
      }
    >
      <DashboardInner />
    </Suspense>
  );
}
