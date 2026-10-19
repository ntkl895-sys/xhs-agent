"use client";

/**
 * 选题生成 Dialog（第 11 步，两页共用）。
 *
 * - 雷达页：传入 `hotspotIds=[选中热点id]`（固定，不可改），只跑「单热点生成」；
 * - 选题库页：不传 `hotspotIds`，走后端「自动取热点」`limit_hotspots`。
 *
 * 提交 `POST /api/v1/topics/generate`（同步，耗时可达数十秒）：
 * 提交期间禁用全部操作并提示「生成中…请勿关闭」；成功后展示汇总与选题标题列表，
 * 并可跳转选题库；失败按 `ApiError` 友好展示（LLM 未就绪 400 / 人设缺失 404 / 部分失败 partial）。
 *
 * 注意：**每次打开都由父级用 `key` 重新挂载**，故这里无需「打开时重置」的 effect
 * （避免 `react-hooks/set-state-in-effect`）。persona 列表在打开后由异步 IIFE 拉取，
 * 首次 `await` 之前不做 `setState`。
 */

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ErrorText, Field, Notice, toApiError } from "@/components/shared";
import { ApiError, apiClient, toQuery } from "@/lib/api-client";
import type { Page, Persona, TopicGenerateRequest, TopicGenerateResult } from "@/lib/types";

export interface TopicGenerateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 固定热点 id（雷达页传单元素数组）；不传 = 走自动取热点 */
  hotspotIds?: string[];
  /** 固定热点的展示文案（可选） */
  hotspotLabel?: string;
  /** 生成成功后回调（用于刷新列表） */
  onGenerated?: () => void;
}

type Phase = "form" | "result";

export function TopicGenerateDialog({
  open,
  onOpenChange,
  hotspotIds,
  hotspotLabel,
  onGenerated,
}: TopicGenerateDialogProps) {
  const router = useRouter();
  const fixed = Boolean(hotspotIds && hotspotIds.length > 0);

  const [personas, setPersonas] = useState<Persona[]>([]);
  const [loadingPersonas, setLoadingPersonas] = useState(true);
  const [loadError, setLoadError] = useState<ApiError | null>(null);

  const [personaId, setPersonaId] = useState("");
  const [perHotspot, setPerHotspot] = useState(3);
  const [limitHotspots, setLimitHotspots] = useState(3);
  const [force, setForce] = useState(false);
  const [fieldError, setFieldError] = useState("");

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<ApiError | null>(null);
  const [phase, setPhase] = useState<Phase>("form");
  const [result, setResult] = useState<TopicGenerateResult | null>(null);

  // 打开时加载 active 人设（首次 await 之前不 setState）
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const run = async () => {
      try {
        const data = await apiClient.get<Page<Persona>>(
          `/api/v1/personas${toQuery({ status: "active", page: 1, page_size: 100 })}`,
        );
        if (!cancelled) {
          setPersonas(data.items);
          setLoadError(null);
          setLoadingPersonas(false);
        }
      } catch (err) {
        if (!cancelled) {
          setLoadError(toApiError(err));
          setLoadingPersonas(false);
        }
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [open]);

  const submit = useCallback(async () => {
    if (!personaId) {
      setFieldError("请选择人设");
      return;
    }
    if (perHotspot < 1 || perHotspot > 20) {
      setFieldError("每热点条数需在 1~20 之间");
      return;
    }
    if (!fixed && (limitHotspots < 1 || limitHotspots > 10)) {
      setFieldError("自动取热点数需在 1~10 之间");
      return;
    }
    setFieldError("");
    setSubmitting(true);
    setSubmitError(null);
    try {
      const body: TopicGenerateRequest = {
        persona_id: personaId,
        per_hotspot: perHotspot,
        limit_hotspots: limitHotspots,
        force,
      };
      if (fixed && hotspotIds) body.hotspot_ids = hotspotIds;
      const res = await apiClient.post<TopicGenerateResult>("/api/v1/topics/generate", body);
      setResult(res);
      setPhase("result");
      onGenerated?.();
    } catch (err) {
      setSubmitError(toApiError(err));
    } finally {
      setSubmitting(false);
    }
  }, [personaId, perHotspot, limitHotspots, force, fixed, hotspotIds, onGenerated]);

  const goTopics = () => {
    onOpenChange(false);
    router.push("/topics?status=all");
  };

  return (
    <Dialog open={open} onOpenChange={(next) => (!next && !submitting ? onOpenChange(false) : undefined)}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{phase === "form" ? "生成选题" : "生成结果"}</DialogTitle>
          <DialogDescription>
            {fixed
              ? `为热点「${hotspotLabel ?? hotspotIds?.[0] ?? ""}」生成选题。`
              : "按当天优先 + 热度自动挑选热点，为选中人设批量生成选题。"}
            真实生成依赖后端已配置 DeepSeek key。
          </DialogDescription>
        </DialogHeader>

        {phase === "form" ? (
          <div className="flex flex-col gap-3">
            <Field
              label="人设 *"
              htmlFor="gen-persona"
              hint={loadingPersonas ? "正在加载人设……" : undefined}
            >
              <Select
                items={{
                  __none__: "暂无可用人设",
                  ...Object.fromEntries(personas.map((p) => [p.id, p.name])),
                }}
                value={personaId}
                onValueChange={(value) => setPersonaId(String(value))}
                disabled={loadingPersonas || submitting}
              >
                <SelectTrigger id="gen-persona" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {personas.length === 0 ? (
                    <SelectItem value="__none__" disabled>
                      暂无可用人设
                    </SelectItem>
                  ) : (
                    personas.map((persona) => (
                      <SelectItem key={persona.id} value={persona.id}>
                        {persona.name}
                        {persona.account_name ? `（${persona.account_name}）` : ""}
                      </SelectItem>
                    ))
                  )}
                </SelectContent>
              </Select>
            </Field>

            <Field label="每热点条数" htmlFor="gen-per" hint="1~20，默认 3">
              <Input
                id="gen-per"
                type="number"
                min={1}
                max={20}
                value={perHotspot}
                disabled={submitting}
                onChange={(event) => setPerHotspot(Number(event.target.value))}
              />
            </Field>

            {!fixed ? (
              <Field label="自动取热点数" htmlFor="gen-limit" hint="1~10，默认 3">
                <Input
                  id="gen-limit"
                  type="number"
                  min={1}
                  max={10}
                  value={limitHotspots}
                  disabled={submitting}
                  onChange={(event) => setLimitHotspots(Number(event.target.value))}
                />
              </Field>
            ) : null}

            <div className="flex items-center gap-2">
              <input
                id="gen-force"
                type="checkbox"
                className="size-4 rounded border-input accent-primary"
                checked={force}
                disabled={submitting}
                onChange={(event) => setForce(event.target.checked)}
              />
              <label htmlFor="gen-force" className="text-sm">
                强制重生成（跳过已覆盖热点）
              </label>
            </div>

            {loadError ? <ErrorText error={loadError} /> : null}
            {fieldError ? <p className="text-xs text-red-600">{fieldError}</p> : null}
            <ErrorText error={submitError} />
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {result ? (
              <>
                <Notice
                  text={
                    result.status === "success"
                      ? "生成完成"
                      : result.status === "partial"
                        ? "部分完成（个别热点失败，其余已生成）"
                        : `生成结束：${result.status}`
                  }
                />
                <ul className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-3">
                  <li className="rounded-lg border border-border px-3 py-2">
                    请求热点 <span className="font-medium">{result.requested}</span>
                  </li>
                  <li className="rounded-lg border border-border px-3 py-2">
                    成功 <span className="font-medium">{result.succeeded}</span>
                  </li>
                  <li className="rounded-lg border border-border px-3 py-2">
                    失败 <span className="font-medium">{result.failed}</span>
                  </li>
                  <li className="rounded-lg border border-border px-3 py-2">
                    跳过 <span className="font-medium">{result.skipped}</span>
                  </li>
                  <li className="rounded-lg border border-border px-3 py-2">
                    落库 <span className="font-medium">{result.persisted_count}</span>
                  </li>
                </ul>
                {result.topics.length > 0 ? (
                  <div className="flex flex-col gap-1">
                    <span className="text-sm font-medium">本次生成选题</span>
                    <ul className="list-inside list-disc text-sm text-muted-foreground">
                      {result.topics.map((topic) => (
                        <li key={topic.id}>{topic.title}</li>
                      ))}
                    </ul>
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    本次没有新增选题（可能都已覆盖，或热点不适用）。
                  </p>
                )}
              </>
            ) : null}
            {submitError ? <ErrorText error={submitError} /> : null}
          </div>
        )}

        <DialogFooter>
          {phase === "form" ? (
            <>
              <Button
                variant="outline"
                onClick={() => onOpenChange(false)}
                disabled={submitting}
              >
                取消
              </Button>
              {/* 人设为必选项：未选中时禁用提交（提交入口双保险，见 submit 内的校验） */}
              <Button
                onClick={() => void submit()}
                disabled={submitting || loadingPersonas || !personaId}
              >
                {submitting ? "生成中，约 30~90 秒，请勿关闭" : "开始生成"}
              </Button>
            </>
          ) : (
            <>
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                关闭
              </Button>
              <Button onClick={goTopics}>去选题库查看</Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
