"use client";

/**
 * 多版本对比视图（Client Component，第 23 步 · V2 增强（二））。
 *
 * 全栈链路：本页（浏览器）-> 本站 /api/v1/**（Route Handler 服务端代理）-> FastAPI。
 *
 * - 数据：`GET /api/v1/content-variants?topic_id=`（批次倒序）+ `GET /api/v1/topics/{id}`（标题/状态）；
 * - 同屏并列 N 张版本卡片（角度徽标 / 标题 / 正文 / 标签 / 合规状态）；
 * - 「采用此版本」→ `PATCH …/{note_id}/adopt`（单事务互斥）→ 跳转创作台详情页继续人工编辑；
 *   未采用的版本只做**软标记**（置灰 + 「未采用」徽标），**不删除**、不自动发布；
 * - 只读展示用 `content_check` 概要；接口不返回 llm_raw，页面也就不可能展示它。
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ErrorText, formatDateTime, toApiError } from "@/components/shared";
import { ApiError, apiClient } from "@/lib/api-client";
import type {
  ComplianceStatus,
  TopicOut,
  VariantAdoptResult,
  VariantBatch,
  VariantsBatchesResult,
  VariantNote,
} from "@/lib/types";

const COMPLIANCE_META: Record<ComplianceStatus, { text: string; cls: string }> = {
  pending: { text: "待检", cls: "border-border bg-muted text-muted-foreground" },
  passed: { text: "合规", cls: "border-emerald-600/30 bg-emerald-500/15 text-emerald-700" },
  warning: { text: "有风险", cls: "border-red-600/30 bg-red-500/15 text-red-700" },
};

const STATUS_TEXT: Record<string, string> = {
  draft: "待审",
  approved: "已通过",
  writing: "写作中",
  published: "已发布",
  discarded: "已弃",
};

function shortGroupId(groupId: string): string {
  return groupId.slice(0, 8);
}

/** 批次视图状态（非分页载荷，与列表页的 `LoadState<T>`（Page<T>）区分开） */
interface BatchesState {
  loading: boolean;
  data: VariantsBatchesResult | null;
  error: ApiError | null;
}

const INITIAL_BATCHES: BatchesState = { loading: true, data: null, error: null };

export default function VariantsClient() {
  const router = useRouter();
  const params = useParams<{ topicId: string }>();
  const topicId = String(params?.topicId ?? "");

  const [state, setState] = useState<BatchesState>(INITIAL_BATCHES);
  const [topic, setTopic] = useState<TopicOut | null>(null);
  const [adoptingId, setAdoptingId] = useState<string | null>(null);
  const [adoptError, setAdoptError] = useState<ApiError | null>(null);

  useEffect(() => {
    if (!topicId) return;
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<VariantsBatchesResult>(
          `/api/v1/content-variants?topic_id=${encodeURIComponent(topicId)}`,
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
  }, [topicId]);

  useEffect(() => {
    if (!topicId) return;
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<TopicOut>(`/api/v1/topics/${encodeURIComponent(topicId)}`);
        if (!cancelled) setTopic(data);
      } catch {
        // 标题为增强展示：失败静默（批次主体不受影响）
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [topicId]);

  const adopt = useCallback(
    async (note: VariantNote) => {
      setAdoptingId(note.note_id);
      setAdoptError(null);
      try {
        await apiClient.patch<VariantAdoptResult>(
          `/api/v1/content-variants/${note.note_id}/adopt`,
          {},
        );
        // 采用成功 → 跳转创作台详情页继续人工编辑（同批次其余已在服务端置未采用）
        router.push(`/notes/${note.note_id}`);
      } catch (err) {
        setAdoptError(toApiError(err));
        setAdoptingId(null);
      }
    },
    [router],
  );

  const batches: VariantBatch[] = state.data?.batches ?? [];
  const totalVersions = batches.reduce((sum, batch) => sum + batch.variants.length, 0);

  return (
    <main className="mx-auto flex w-full max-w-7xl flex-1 flex-col gap-6 px-6 py-10">
      <header className="flex flex-col gap-2">
        <div className="flex items-center gap-3">
          <Link
            href="/topics"
            className="text-sm text-muted-foreground underline-offset-4 hover:underline"
          >
            ← 返回选题库
          </Link>
        </div>
        <h1 className="text-2xl font-medium tracking-tight">多版本对比</h1>
        <p className="text-sm text-muted-foreground">
          {topic ? (
            <>
              选题「{topic.title}」
              <Badge className="ml-2 border-border bg-muted text-muted-foreground">
                {STATUS_TEXT[topic.status] ?? topic.status}
              </Badge>
            </>
          ) : (
            "同选题多版本创作：一次生成 N 个角度明显不同的版本，横向对比后采用其一进入创作台。"
          )}
        </p>
        <p className="text-xs text-muted-foreground">
          采用某个版本后，其余版本仅作「未采用」软标记（不删除）；发布与否始终由人工决定，系统不自动发布。
        </p>
      </header>

      <ErrorText error={adoptError} />

      {state.loading ? (
        <p className="text-sm text-muted-foreground">加载中……</p>
      ) : state.error ? (
        <ErrorText error={state.error} />
      ) : batches.length === 0 ? (
        <Card>
          <CardContent className="py-10 text-center text-sm text-muted-foreground">
            该选题暂无多版本批次。请在「选题库」对已通过（approved）的选题点击「多版本生成」。
          </CardContent>
        </Card>
      ) : (
        <>
          <p className="text-xs text-muted-foreground">
            共 {batches.length} 个批次 · {totalVersions} 个版本
          </p>
          {batches.map((batch) => {
            const hasAdopted = batch.variants.some((item) => item.adopted);
            return (
              <section key={batch.group_id} className="flex flex-col gap-3">
                <div className="flex items-center gap-2 text-sm">
                  <span className="font-medium">批次 {shortGroupId(batch.group_id)}</span>
                  <span className="text-muted-foreground">{formatDateTime(batch.created_at)}</span>
                  <span className="text-muted-foreground">· {batch.variants.length} 版</span>
                </div>
                <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                  {batch.variants.map((variant) => {
                    const complianceMeta =
                      COMPLIANCE_META[variant.compliance_status] ?? COMPLIANCE_META.pending;
                    const cardClass = variant.adopted
                      ? "flex flex-col gap-3 rounded-xl border border-emerald-600/50 bg-emerald-500/5 p-4 ring-1 ring-emerald-500/40"
                      : hasAdopted
                        ? "flex flex-col gap-3 rounded-xl border border-border p-4 opacity-60"
                        : "flex flex-col gap-3 rounded-xl border border-border p-4";
                    return (
                      <div key={variant.note_id} className={cardClass}>
                        <div className="flex flex-wrap items-center gap-1.5">
                          <Badge variant="outline">角度：{variant.angle || "—"}</Badge>
                          <Badge className={complianceMeta.cls}>{complianceMeta.text}</Badge>
                          {variant.adopted ? (
                            <Badge className="border-emerald-600/30 bg-emerald-500/15 text-emerald-700">
                              已采用
                            </Badge>
                          ) : hasAdopted ? (
                            <Badge variant="outline">未采用</Badge>
                          ) : null}
                        </div>
                        <h3 className="text-base leading-snug font-medium">{variant.title}</h3>
                        <p className="max-h-44 overflow-y-auto text-sm whitespace-pre-wrap text-muted-foreground">
                          {variant.body}
                        </p>
                        {variant.tags.length > 0 ? (
                          <div className="flex flex-wrap items-center gap-1">
                            {variant.tags.map((tag) => (
                              <Badge key={tag} variant="outline">
                                {tag}
                              </Badge>
                            ))}
                          </div>
                        ) : null}
                        <div className="mt-auto flex items-center justify-between gap-2 pt-1">
                          <span className="text-xs text-muted-foreground">
                            {formatDateTime(variant.created_at)}
                          </span>
                          {variant.adopted ? (
                            <Button size="sm" onClick={() => router.push(`/notes/${variant.note_id}`)}>
                              进入创作台编辑
                            </Button>
                          ) : hasAdopted ? (
                            <Button size="sm" variant="outline" disabled>
                              未采用
                            </Button>
                          ) : (
                            <Button
                              size="sm"
                              disabled={adoptingId !== null}
                              onClick={() => void adopt(variant)}
                            >
                              {adoptingId === variant.note_id ? "采用中…" : "采用此版本"}
                            </Button>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </section>
            );
          })}
        </>
      )}
    </main>
  );
}
