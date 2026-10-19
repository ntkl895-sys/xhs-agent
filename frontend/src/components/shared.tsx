"use client";

/**
 * 业务无关的通用展示件（第 11 步从 `accounts/page.tsx` 原样搬出）。
 *
 * 搬移原则：**逐字保持**原有实现与样式，只把「与账号 / 人设业务无关」的部分
 * 提取成共享组件，供 `/accounts`、`/hotspots`、`/topics` 复用，避免三份重复。
 *
 * 本文件不含任何接口调用与业务字段，纯展示 / 交互。
 */

import { useState } from "react";

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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, describeDetails } from "@/lib/api-client";
import type { Page } from "@/lib/types";

/* ------------------------------------------------------------------ 加载态 */

/** 列表加载状态 */
export interface LoadState<T> {
  loading: boolean;
  data: Page<T> | null;
  error: ApiError | null;
}

export const INITIAL_LOAD: LoadState<never> = { loading: true, data: null, error: null };

/** 把任意异常归一成 ApiError（未知异常兜底为 code=unknown）。 */
export function toApiError(err: unknown): ApiError {
  return err instanceof ApiError ? err : new ApiError(0, "unknown", String(err));
}

/* ------------------------------------------------------------------ 格式化 */

/** ISO 时间 -> 本地可读文本 */
export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { hour12: false });
}

/** 热度等大整数 -> 千分位文本；null/undefined 显示为「—」。 */
export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString("en-US");
}

/** 0~1 评分 -> 两位小数；null/undefined 显示为「—」。 */
export function formatScore(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toFixed(2);
}

/* ------------------------------------------------------------------ 提示件 */

export function ErrorText({ error }: { error: ApiError | null }) {
  if (!error) return null;
  const detailLines = describeDetails(error.details);
  return (
    <div className="rounded-lg border border-red-600/30 bg-red-500/10 px-3 py-2 text-sm text-red-700">
      <p>
        {error.message}
        <span className="ml-2 text-xs opacity-80">({error.code})</span>
      </p>
      {detailLines.length > 0 ? (
        <ul className="mt-1 list-inside list-disc text-xs">
          {detailLines.map((line, index) => (
            <li key={index}>{line}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function Notice({ text }: { text: string }) {
  if (!text) return null;
  return (
    <p className="rounded-lg border border-emerald-600/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-700">
      {text}
    </p>
  );
}

/* ------------------------------------------------------------------ 表单件 */

export function Field({
  label,
  htmlFor,
  error,
  hint,
  children,
}: {
  label: string;
  htmlFor?: string;
  error?: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
      {hint && !error ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
      {error ? <p className="text-xs text-red-600">{error}</p> : null}
    </div>
  );
}

/** 可增删的标签输入（内容支柱 / 人设禁忌 / 选题标签 / 关键词） */
export function TagInput({
  value,
  onChange,
  placeholder,
  maxItems = 10,
  maxLength = 50,
}: {
  value: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  /** 最多可添加项数（默认 10） */
  maxItems?: number;
  /** 单项最大字数（默认 50） */
  maxLength?: number;
}) {
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");

  const add = () => {
    const item = draft.trim();
    if (!item) return;
    if (item.length > maxLength) {
      setError(`单项不超过 ${maxLength} 字`);
      return;
    }
    if (value.length >= maxItems) {
      setError(`最多 ${maxItems} 项`);
      return;
    }
    if (value.includes(item)) {
      setError("该项已存在");
      setDraft("");
      return;
    }
    setError("");
    onChange([...value, item]);
    setDraft("");
  };

  return (
    <div className="flex flex-col gap-2">
      <div className="flex gap-2">
        <Input
          value={draft}
          placeholder={placeholder}
          onChange={(event) => {
            setDraft(event.target.value);
            setError("");
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              add();
            }
          }}
        />
        <Button type="button" variant="outline" onClick={add}>
          添加
        </Button>
      </div>
      {error ? <p className="text-xs text-red-600">{error}</p> : null}
      {value.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {value.map((item) => (
            <Badge key={item} variant="secondary" className="gap-1">
              {item}
              <button
                type="button"
                aria-label={`移除 ${item}`}
                className="text-muted-foreground hover:text-foreground"
                onClick={() => onChange(value.filter((entry) => entry !== item))}
              >
                ×
              </button>
            </Badge>
          ))}
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">暂无</p>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ 分页 / 确认 */

export function Pagination({
  page,
  pageSize,
  total,
  loading,
  onPage,
}: {
  page: number;
  pageSize: number;
  total: number;
  loading: boolean;
  onPage: (next: number) => void;
}) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 pt-2 text-sm text-muted-foreground">
      <span>
        第 {page} / {pageCount} 页，共 {total} 条
      </span>
      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={loading || page <= 1}
          onClick={() => onPage(page - 1)}
        >
          上一页
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={loading || page >= pageCount}
          onClick={() => onPage(page + 1)}
        >
          下一页
        </Button>
      </div>
    </div>
  );
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmText,
  busy,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  description: string;
  confirmText: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={(next) => (!next ? onCancel() : undefined)}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel} disabled={busy}>
            取消
          </Button>
          <Button variant="destructive" onClick={onConfirm} disabled={busy}>
            {busy ? "处理中…" : confirmText}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
