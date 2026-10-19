/**
 * 选题集合代理：GET（分页列表，透传筛选 / 分页 / 排序查询串）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function GET(request: NextRequest) {
  return proxyToBackend("/api/v1/topics", {
    method: "GET",
    search: request.nextUrl.search,
  });
}
