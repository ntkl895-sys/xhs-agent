/**
 * 知识块列表代理（第 18 步）：GET。
 *
 * - GET `/api/v1/knowledge?chunk_type=&status=&limit=&offset=` 分页列表；
 * - 薄转发：状态码与响应体原样透传（含 422）；
 * - **不提供** POST / PUT / PATCH / DELETE（提炼走 /knowledge/extract，
 *   归档走 /knowledge/[id] 的 PATCH）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function GET(request: NextRequest) {
  return proxyToBackend("/api/v1/knowledge", {
    method: "GET",
    search: request.nextUrl.search,
  });
}
