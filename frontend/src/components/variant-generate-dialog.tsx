"use client";

/**
 * 多版本生成 Dialog（第 23 步 · V2 增强（二），选题库页使用）。
 *
 * 为**同一个已通过选题**一次生成 N 个角度明显不同的版本：
 *
 * - 可选 N（1~5，默认 3）与可选角度（留空 = 自动从角度池选取；勾选时须恰好 N 个）；
 * - 提交 `POST /api/v1/content-variants/generate`（同步，单次调用产出 N 版，耗时可达
 *   1~2 分钟）；提交期间禁用全部操作并提示「生成中…请勿关闭」；
 * - 成功后展示汇总（请求 / 落库 / 拦截 / 不合格）与被拦截原因，并可跳转「版本对比」；
 * - 失败按 `ApiError` 友好展示（LLM 未就绪 400 / 选题不存在 404 / 全失败 failed）。
 *
 * 注意：**每次打开都由父级用 `key` 重新挂载**（避免重置 effect）；
 * 本组件不发起任何后台请求，全部交互都由用户显式触发。
 */

import { useCallback, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ErrorText, Notice, toApiError } from "@/components/shared";
import { ApiError, apiClient } from "@/lib/api-client";
import type { VariantsGenerateRequest, VariantsGenerateResult } from "@/lib/types";

/** 内置中文角度池（与后端 app/agent/content_variants/prompts.py 的 ANGLE_POOL 保持同步） */
const ANGLE_POOL = ["实用干货", "情绪共鸣", "反差观点", "亲身经历", "清单盘点", "避坑提醒"];

const N_OPTIONS = [1, 2, 3, 4, 5];

export interface VariantGenerateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 目标选题（必须为 approved 状态） */
  topicId: string;
  topicTitle: string;
}

type Phase = "form" | "result";

export function VariantGenerateDialog({
  open,
  onOpenChange,
  topicId,
  topicTitle,
}: VariantGenerateDialogProps) {
  const router = useRouter();
  const [n, setN] = useState(3);
  const [selectedAngles, setSelectedAngles] = useState<string[]>([]);
  const [fieldError, setFieldError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<ApiError | null>(null);
  const [phase, setPhase] = useState<Phase>("form");
  const [result, setResult] = useState<VariantsGenerateResult | null>(null);

  const toggleAngle = useCallback(
    (angle: string) => {
      setFieldError("");
      setSelectedAngles((prev) => {
        if (prev.includes(angle)) return prev.filter((item) => item !== angle);
        if (prev.length >= n) {
          setFieldError(`已勾选的角度数量不能超过 N（${n}）`);
          return prev;
        }
        return [...prev, angle];
      });
    },
    [n],
  );

  const changeN = useCallback((value: number) => {
    setFieldError("");
    setN(value);
    // N 调小时裁剪多余勾选（保证「0 或恰好 N 个」的口径）
    setSelectedAngles((prev) => prev.slice(0, value));
  }, []);

  const nItems = useMemo(
    () => Object.fromEntries(N_OPTIONS.map((value) => [String(value), `${value} 版`])),
    [],
  );

  const submit = useCallback(async () => {
    if (selectedAngles.length !== 0 && selectedAngles.length !== n) {
      setFieldError(`已勾选 ${selectedAngles.length} 个角度，需为 0（自动）或恰好 ${n} 个`);
      return;
    }
    setFieldError("");
    setSubmitting(true);
    setSubmitError(null);
    try {
      const body: VariantsGenerateRequest = {
        topic_id: topicId,
        n,
        angles: selectedAngles.length > 0 ? selectedAngles : null,
      };
      const res = await apiClient.post<VariantsGenerateResult>(
        "/api/v1/content-variants/generate",
        body,
      );
      setResult(res);
      setPhase("result");
    } catch (err) {
      setSubmitError(toApiError(err));
    } finally {
      setSubmitting(false);
    }
  }, [n, selectedAngles, topicId]);

  const goCompare = () => {
    onOpenChange(false);
    router.push(`/notes/variants/${topicId}`);
  };

  const blockedItems = (result?.items ?? []).filter((item) => item.status === "blocked");
  const rejectedItems = (result?.items ?? []).filter((item) => item.status === "rejected");

  return (
    <Dialog open={open} onOpenChange={(next) => (!next && !submitting ? onOpenChange(false) : undefined)}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{phase === "form" ? "生成多版本" : "生成结果"}</DialogTitle>
          <DialogDescription>
            {phase === "form"
              ? `为选题「${topicTitle.slice(0, 24)}」一次生成 N 个角度明显不同、可横向对比的版本。真实生成依赖后端已配置模型 key。`
              : "逐版已完成违禁词自检：命中风险的版本被拦截（不落库），其余版本已入库待对比。"}
          </DialogDescription>
        </DialogHeader>

        {phase === "form" ? (
          <div className="flex flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="vc-n">版本数 N</Label>
              <Select
                items={nItems}
                value={String(n)}
                onValueChange={(value) => changeN(Number(value))}
                disabled={submitting}
              >
                <SelectTrigger id="vc-n" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {N_OPTIONS.map((value) => (
                    <SelectItem key={value} value={String(value)}>
                      {value} 版{value === 3 ? "（默认）" : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="flex flex-col gap-2">
              <Label>角度（可选；留空 = 自动从角度池选取）</Label>
              <p className="text-xs text-muted-foreground">
                勾选时需恰好为 N（{n}）个；不勾选则由模型从内置角度池自选且互不重复。
              </p>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                {ANGLE_POOL.map((angle) => (
                  <label
                    key={angle}
                    className="flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm"
                  >
                    <input
                      type="checkbox"
                      className="size-4 rounded border-input accent-primary"
                      checked={selectedAngles.includes(angle)}
                      disabled={submitting}
                      onChange={() => toggleAngle(angle)}
                    />
                    {angle}
                  </label>
                ))}
              </div>
              {selectedAngles.length > 0 ? (
                <p className="text-xs text-muted-foreground">
                  已选 {selectedAngles.length} / {n} 个
                </p>
              ) : null}
            </div>

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
                      ? `生成完成：${result.persisted ?? 0} 个版本已入库`
                      : result.status === "partial"
                        ? "部分完成：已有版本入库，个别版本被拦截或不合格"
                        : `生成失败：${result.error_message ?? "全部版本不合格"}`
                  }
                />
                <ul className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
                  <li className="rounded-lg border border-border px-3 py-2">
                    请求 <span className="font-medium">{result.requested}</span>
                  </li>
                  <li className="rounded-lg border border-border px-3 py-2">
                    落库 <span className="font-medium">{result.persisted ?? 0}</span>
                  </li>
                  <li className="rounded-lg border border-border px-3 py-2">
                    拦截 <span className="font-medium">{result.blocked ?? 0}</span>
                  </li>
                  <li className="rounded-lg border border-border px-3 py-2">
                    不合格 <span className="font-medium">{result.rejected ?? 0}</span>
                  </li>
                </ul>
                {blockedItems.length > 0 ? (
                  <div className="flex flex-col gap-1 text-sm">
                    <span className="font-medium">被拦截版本（命中风险词，未入库）</span>
                    <ul className="list-inside list-disc text-muted-foreground">
                      {blockedItems.map((item, index) => (
                        <li key={`${item.angle}-${index}`}>
                          {item.angle || "（未命名角度）"}：{item.reason ?? "命中风险词"}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
                {rejectedItems.length > 0 ? (
                  <div className="flex flex-col gap-1 text-sm">
                    <span className="font-medium">不合格版本（未入库）</span>
                    <ul className="list-inside list-disc text-muted-foreground">
                      {rejectedItems.map((item, index) => (
                        <li key={`${item.angle}-${index}`}>
                          {item.angle || `第 ${index + 1} 版`}：{item.reason ?? "缺少标题或正文"}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
                {(result.persisted ?? 0) > 0 ? (
                  <div className="flex flex-wrap gap-1.5">
                    {(result.items ?? [])
                      .filter((item) => item.status === "qualified")
                      .map((item, index) => (
                        <Badge key={`${item.angle}-${index}`} variant="outline">
                          {item.angle || "未命名角度"} · {(item.title ?? "").slice(0, 14)}
                        </Badge>
                      ))}
                  </div>
                ) : null}
              </>
            ) : null}
            <ErrorText error={submitError} />
          </div>
        )}

        <DialogFooter>
          {phase === "form" ? (
            <>
              <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
                取消
              </Button>
              <Button onClick={() => void submit()} disabled={submitting}>
                {submitting ? "生成中，约 1~2 分钟，请勿关闭" : `生成 ${n} 个版本`}
              </Button>
            </>
          ) : (
            <>
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                关闭
              </Button>
              <Button onClick={goCompare} disabled={(result?.persisted ?? 0) === 0}>
                查看版本对比
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
