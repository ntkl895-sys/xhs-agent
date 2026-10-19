/**
 * 诊断生成代理（第 16 步）：POST **原样转发**（JSON text 透传）。
 *
 * - POST `/api/v1/diagnostics/generate`（body：note_id 必填；diagnosis_type 可选三值）
 * - 同步长耗时接口（真跑一次 LangGraph 诊断图）：超时放大到 120s（与生成 / 识别同档）；
 * - 诊断 = 只读 + 建议：不修改笔记 / 选题 / 数据，也不触发任何发布动作。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function POST(request: NextRequest) {
  const body = await request.text();
  return proxyToBackend("/api/v1/diagnostics/generate", {
    method: "POST",
    body,
    timeoutMs: 120000,
  });
}
