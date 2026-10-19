/**
 * 周报详情代理（第 17 步）：GET 单条。
 *
 * 薄转发（浏览器只请求本站 /api/v1/**）：状态码与响应体由 `proxyToBackend`
 * 原样透传（含 404）；不写 `export const dynamic`（Route Handler 默认即动态）；
 * **不提供** PUT / PATCH / DELETE（周报只增不改）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

type Context = { params: Promise<{ run_id: string }> };

export async function GET(request: NextRequest, context: Context) {
  const { run_id } = await context.params;
  return proxyToBackend(`/api/v1/reports/weekly/${encodeURIComponent(run_id)}`, {
    method: "GET",
    search: request.nextUrl.search,
  });
}
