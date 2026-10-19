"use client";

/**
 * xhs-agent 环境自检首页（第 6 步骨架，不含任何业务功能）。
 *
 * 客户端请求本站 /api/health -> 由 Route Handler 代理到后端 /health/ready，
 * 渲染「后端 / PostgreSQL / pgvector 扩展 / Redis」四项状态与原始返回 JSON。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

type StatusLevel = "ok" | "fail" | "unknown";

type HealthPayload = {
  status?: string;
  checks?: Record<string, unknown>;
} | null;

type CheckKey = "backend" | "database" | "pgvector" | "redis";

const ITEMS: { key: CheckKey; label: string; hint: string }[] = [
  { key: "backend", label: "后端 FastAPI", hint: "GET /health/ready 是否可达" },
  { key: "database", label: "PostgreSQL", hint: "SELECT 1" },
  { key: "pgvector", label: "pgvector 扩展", hint: "pg_extension 中存在 vector" },
  { key: "redis", label: "Redis", hint: "PING" },
];

const LEVEL_STYLE: Record<
  StatusLevel,
  { badge: string; symbol: string; text: string }
> = {
  ok: {
    badge: "border-emerald-600/30 bg-emerald-500/15 text-emerald-700",
    symbol: "✓",
    text: "正常",
  },
  fail: {
    badge: "border-red-600/30 bg-red-500/15 text-red-700",
    symbol: "✗",
    text: "异常",
  },
  unknown: {
    badge: "border-border bg-muted text-muted-foreground",
    symbol: "–",
    text: "未测",
  },
};

function levelOf(payload: HealthPayload, key: CheckKey): StatusLevel {
  const checks = payload?.checks;
  if (!checks) return "unknown";
  if (key === "backend") {
    // 只有明确标记 unreachable 才算后端异常；否则代理已成功拿到后端响应
    return checks.backend === "unreachable" ? "fail" : "ok";
  }
  const value = checks[key];
  if (typeof value === "boolean") return value ? "ok" : "fail";
  return "unknown";
}

/** 一次健康检查的原始结果（不触碰 React 状态，便于在 effect 与事件处理器间复用） */
type HealthResult =
  | { ok: true; status: number; json: HealthPayload; raw: string }
  | { ok: false; message: string };

/** 请求本站 /api/health（由 Route Handler 在服务端代理到后端） */
async function fetchHealth(): Promise<HealthResult> {
  try {
    const res = await fetch("/api/health", { cache: "no-store" });
    const text = await res.text();
    let json: HealthPayload = null;
    try {
      json = JSON.parse(text) as HealthPayload;
    } catch {
      json = null;
    }
    return { ok: true, status: res.status, json, raw: JSON.stringify(json ?? text, null, 2) };
  } catch (err) {
    // 极少数情况：连本站 /api/health 都请求不到
    return { ok: false, message: err instanceof Error ? err.message : String(err) };
  }
}

export default function Home() {
  const [payload, setPayload] = useState<HealthPayload>(null);
  const [httpStatus, setHttpStatus] = useState<number | null>(null);
  const [rawText, setRawText] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(true);
  const [failure, setFailure] = useState<string>("");
  const [checkedAt, setCheckedAt] = useState<string>("");

  const applyResult = useCallback((result: HealthResult) => {
    if (result.ok) {
      setFailure("");
      setHttpStatus(result.status);
      setPayload(result.json);
      setRawText(result.raw);
    } else {
      setHttpStatus(null);
      setPayload(null);
      setRawText("");
      setFailure(result.message);
    }
    setCheckedAt(new Date().toLocaleString("zh-CN"));
    setLoading(false);
  }, []);

  // 注意：首次 await 之前不做 setState —— 满足 react-hooks/set-state-in-effect，
  // 同时避免 effect 内的同步 setState 触发级联渲染。
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      const result = await fetchHealth();
      if (!cancelled) applyResult(result);
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [applyResult]);

  const handleRecheck = useCallback(() => {
    setLoading(true);
    setFailure("");
    void fetchHealth().then(applyResult);
  }, [applyResult]);

  const levels = useMemo(
    () => ITEMS.map((item) => ({ ...item, level: levelOf(payload, item.key) })),
    [payload],
  );

  const allOk = levels.every((item) => item.level === "ok");
  const anyFail = levels.some((item) => item.level === "fail");
  const overallLevel: StatusLevel = allOk ? "ok" : anyFail ? "fail" : "unknown";
  const overallStyle = LEVEL_STYLE[overallLevel];

  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-6 px-6 py-12">
      <header className="flex flex-col gap-2">
        <h1 className="text-2xl font-medium tracking-tight">
          小红书运营 Agent · 环境自检
        </h1>
        <p className="text-sm text-muted-foreground">
          第 6 步骨架：浏览器 → Next.js(/api/health) → FastAPI(/health/ready) →
          PostgreSQL / pgvector / Redis。本页不含任何业务功能。
        </p>
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-muted/40 px-3 py-2">
          <span className="text-sm text-muted-foreground">业务入口：</span>
          <Link
            href="/accounts"
            className="text-sm font-medium text-primary underline-offset-4 hover:underline"
          >
            账号与人设管理 →
          </Link>
        </div>
      </header>

      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-col gap-1">
              <CardTitle>系统状态</CardTitle>
              <CardDescription>
                {loading
                  ? "正在检测……"
                  : checkedAt
                    ? `最后检测：${checkedAt}（HTTP ${httpStatus ?? "—"}）`
                    : "尚未检测"}
              </CardDescription>
            </div>
            <div className="flex items-center gap-2">
              <Badge className={overallStyle.badge}>
                <span aria-hidden>{overallStyle.symbol}</span>
                {loading ? "检测中" : overallStyle.text}
              </Badge>
              <Button
                variant="outline"
                size="sm"
                onClick={handleRecheck}
                disabled={loading}
              >
                {loading ? "检测中…" : "重新检测"}
              </Button>
            </div>
          </div>
        </CardHeader>

        <CardContent className="flex flex-col gap-3">
          <ul className="flex flex-col divide-y divide-border">
            {levels.map((item) => {
              const style = LEVEL_STYLE[item.level];
              return (
                <li
                  key={item.key}
                  className="flex flex-wrap items-center justify-between gap-2 py-3"
                >
                  <div className="flex flex-col">
                    <span className="text-sm font-medium">{item.label}</span>
                    <span className="text-xs text-muted-foreground">
                      {item.hint}
                    </span>
                  </div>
                  <Badge className={style.badge}>
                    <span aria-hidden>{style.symbol}</span>
                    {style.text}
                  </Badge>
                </li>
              );
            })}
          </ul>

          {failure ? (
            <p className="rounded-lg border border-red-600/30 bg-red-500/10 px-3 py-2 text-sm text-red-700">
              无法访问本站 /api/health：{failure}
            </p>
          ) : null}

          <div className="flex flex-col gap-2 border-t border-border pt-4">
            <span className="text-sm font-medium">原始返回 JSON</span>
            <pre className="max-h-72 overflow-auto rounded-lg border border-border bg-muted/50 p-3 text-xs leading-relaxed">
              {rawText || "（暂无数据）"}
            </pre>
          </div>
        </CardContent>
      </Card>

      <footer className="text-xs text-muted-foreground">
        提示：中间件未启动时，就绪探针会在约 10 秒内返回 503（degraded）并标红对应项，
        属预期行为，不是崩溃。
      </footer>
    </main>
  );
}
