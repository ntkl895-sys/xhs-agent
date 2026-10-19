/**
 * 周报历史代理（第 17 步）：GET。
 *
 * - GET `/api/v1/reports/weekly?limit=` 周报历史（created_at 倒序）；
 * - 薄转发：状态码与响应体原样透传；
 * - **不提供** PUT / PATCH / DELETE（周报只增不改，存 agent_runs）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function GET(request: NextRequest) {
  return proxyToBackend("/api/v1/reports/weekly", {
    method: "GET",
    search: request.nextUrl.search,
  });
}
