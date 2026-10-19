/**
 * 手动创建知识块代理（第 19 步）：POST **原样转发**。
 *
 * - POST `/api/v1/knowledge/manual` 人工录入一条知识块（六类型 + 可选人设关联）；
 * - `forbidden_word` 不向量化（离线可维护）；其它类型经后端向量化，
 *   未就绪 → 400（**零写入**），页面据此给出「向量化未就绪」提示；
 * - 向量化属长耗时接口：超时放大到 120s（与提炼 / 识别 / 诊断同档）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function POST(request: NextRequest) {
  const body = await request.text();
  return proxyToBackend("/api/v1/knowledge/manual", {
    method: "POST",
    body,
    timeoutMs: 120000,
  });
}
