/**
 * 单条诊断代理（第 16 步）：GET 详情。
 *
 * 第 16 步的薄转发（浏览器只请求本站 /api/v1/**）：
 * 状态码与响应体由 `proxyToBackend` 原样透传；不写 `export const dynamic`
 * （Route Handler 默认即动态）；**不提供** PUT / PATCH / DELETE（诊断只增不改）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

type Context = { params: Promise<{ id: string }> };

export async function GET(request: NextRequest, context: Context) {
  const { id } = await context.params;
  return proxyToBackend(`/api/v1/diagnostics/${encodeURIComponent(id)}`, {
    method: "GET",
    search: request.nextUrl.search,
  });
}
