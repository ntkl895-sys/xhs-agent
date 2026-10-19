/**
 * 看板趋势代理（第 17 步）：GET。
 *
 * - GET `/api/v1/dashboard/trend?metric=&range_days=&note_id=&bucket=` 指标趋势点；
 * - 薄转发：状态码与响应体原样透传（含 404 / 422）；
 * - **不提供** POST / PUT / PATCH / DELETE（看板是确定性只读聚合）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function GET(request: NextRequest) {
  return proxyToBackend("/api/v1/dashboard/trend", {
    method: "GET",
    search: request.nextUrl.search,
  });
}
