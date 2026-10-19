"use client";

/**
 * 「关键词搜索」面板（第 23 步）。
 *
 * 全栈链路：本组件（浏览器）-> 本站 /api/v1/search（Route Handler 服务端代理）
 * -> FastAPI -> 公开源（当前仅 GitHub 官方 API）。
 *
 * 红线与约定（与后端一致）：
 * - 只查询「公开、免登录、有官方接口」的源；不请求任何内容平台搜索域名；
 * - 结果外链**仅渲染 github.com 域**（其余域名降级为纯文本），
 *   一律 `target="_blank" rel="noopener noreferrer"`；
 * - 前端不持有任何令牌（令牌只在后端 .env；types 中没有 token 字段）；
 * - 查询为按钮 / 回车触发（不是挂载即请求），避免多余的上游调用。
 */

import { useCallback, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
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
import { ErrorText, Notice, formatDateTime, formatNumber, toApiError } from "@/components/shared";
import { ApiError, apiClient, toQuery } from "@/lib/api-client";
import type { SearchItem, SearchResponse } from "@/lib/types";

/** 只允许 github.com 域名的外链（与本步来源白名单一致） */
const GITHUB_PREFIX = "https://github.com/";

/** 单次查询返回条数（与后端默认口径一致；后端上限 30） */
const SEARCH_LIMIT = 10;

interface SearchState {
  loading: boolean;
  data: SearchResponse | null;
  error: ApiError | null;
}

const IDLE: SearchState = { loading: false, data: null, error: null };

/** 从 meta 中安全取字符串值（meta 是宽松字典） */
function metaText(item: SearchItem, key: string): string | null {
  const value = item.meta[key];
  return typeof value === "string" && value ? value : null;
}

export function KeywordSearchPanel() {
  const [qInput, setQInput] = useState("");
  const [source] = useState("github");
  const [sort, setSort] = useState("stars");
  const [state, setState] = useState<SearchState>(IDLE);
  const [notice, setNotice] = useState("");

  const doSearch = useCallback(async () => {
    const q = qInput.trim();
    if (!q) {
      setNotice("请输入关键词后再查询");
      return;
    }
    setState({ loading: true, data: null, error: null });
    setNotice("");
    try {
      const data = await apiClient.get<SearchResponse>(
        `/api/v1/search${toQuery({ q, source, sort, limit: SEARCH_LIMIT })}`,
      );
      setState({ loading: false, data, error: null });
    } catch (err) {
      setState({ loading: false, data: null, error: toApiError(err) });
    }
  }, [qInput, source, sort]);

  const copyLink = useCallback(async (item: SearchItem) => {
    try {
      await navigator.clipboard.writeText(item.url);
      setNotice(`已复制链接：${item.title}`);
    } catch {
      setNotice("复制失败：浏览器未授予剪贴板权限，可手动选择链接复制。");
    }
  }, []);

  const items = state.data?.items ?? [];
  const rateLimited = state.error?.code === "search_rate_limited";

  return (
    <Card>
      <CardHeader>
        <CardTitle>关键词搜索</CardTitle>
        <CardDescription>
          按关键词查询公开源（当前仅 GitHub 仓库；官方 API、只读公开数据）。
          结果默认带 30 分钟缓存；触发上游限速时会自动退避并给出提示。本工具不请求任何内容平台搜索域名。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {/* ---------------- 查询栏 ---------------- */}
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            void doSearch();
          }}
        >
          <div className="flex min-w-64 flex-1 flex-col gap-1.5">
            <Label htmlFor="kw-q">关键词</Label>
            <Input
              id="kw-q"
              value={qInput}
              placeholder="如：langgraph agent"
              onChange={(event) => setQInput(event.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="kw-source">来源</Label>
            <Select
              items={{ github: "GitHub" }}
              value={source}
              onValueChange={() => undefined}
            >
              <SelectTrigger id="kw-source" className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="github">GitHub</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="kw-sort">排序</Label>
            <Select
              items={{ stars: "Star 数", updated: "最近更新" }}
              value={sort}
              onValueChange={(value) => setSort(String(value))}
            >
              <SelectTrigger id="kw-sort" className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="stars">Star 数</SelectItem>
                <SelectItem value="updated">最近更新</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <Button type="submit" disabled={state.loading}>
            {state.loading ? "搜索中…" : "查询"}
          </Button>
        </form>

        <Notice text={notice} />

        {/* ---------------- 状态区 ---------------- */}
        {state.loading ? (
          <p className="text-sm text-muted-foreground">搜索中……（首次查询需请求 GitHub，缓存命中时会立即返回）</p>
        ) : state.error ? (
          <div className="flex flex-col gap-2">
            <ErrorText error={state.error} />
            {rateLimited ? (
              <p className="text-xs text-muted-foreground">
                提示：GitHub 匿名搜索限额（约 10 次/分钟）已用完，后端已按上游 Reset 时刻本地退避，稍后自动恢复；
                如需更高限额，可按 docs/search_radar.md 指引配置 GITHUB_TOKEN（只填在后端 .env）。
              </p>
            ) : null}
          </div>
        ) : state.data ? (
          state.data.count === 0 ? (
            <p className="text-sm text-muted-foreground">未找到相关仓库，换个关键词试试。</p>
          ) : (
            <div className="flex flex-col gap-2">
              <p className="text-xs text-muted-foreground">
                共 {state.data.count} 条{state.data.cached ? "（来自缓存）" : ""} · 来源 {state.data.source} ·
                排序 {state.data.sort === "updated" ? "最近更新" : "Star 数"}
              </p>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>仓库</TableHead>
                    <TableHead>描述</TableHead>
                    <TableHead className="w-24">Star</TableHead>
                    <TableHead className="w-24">语言</TableHead>
                    <TableHead className="w-40">最近更新</TableHead>
                    <TableHead className="w-24 text-right">操作</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {items.map((item) => {
                    const safeUrl = item.url.startsWith(GITHUB_PREFIX) ? item.url : null;
                    const language = metaText(item, "language");
                    const updatedAt = metaText(item, "updated_at");
                    return (
                      <TableRow key={item.url}>
                        <TableCell className="max-w-56 min-w-40 whitespace-normal font-medium">
                          {safeUrl ? (
                            <a
                              href={safeUrl}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-primary underline-offset-4 hover:underline"
                            >
                              {item.title}
                            </a>
                          ) : (
                            item.title
                          )}
                        </TableCell>
                        <TableCell className="max-w-80 min-w-56 whitespace-normal text-muted-foreground">
                          {item.snippet ?? "—"}
                        </TableCell>
                        <TableCell>{formatNumber(item.score)}</TableCell>
                        <TableCell>
                          {language ? <Badge variant="outline">{language}</Badge> : "—"}
                        </TableCell>
                        <TableCell className="text-muted-foreground">
                          {updatedAt ? formatDateTime(updatedAt) : "—"}
                        </TableCell>
                        <TableCell className="text-right">
                          <Button variant="outline" size="sm" onClick={() => void copyLink(item)}>
                            复制链接
                          </Button>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )
        ) : (
          <p className="text-sm text-muted-foreground">
            输入关键词（如 langgraph agent）后点击「查询」，结果将展示仓库名 / 描述 / Star / 语言 / 更新时间，
            可复制链接或在新标签打开。
          </p>
        )}
      </CardContent>
    </Card>
  );
}
