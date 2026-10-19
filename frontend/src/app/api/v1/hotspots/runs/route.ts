/**
 * 最近采集运行代理：GET（透传 limit 等查询串）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function GET(request: NextRequest) {
  return proxyToBackend("/api/v1/hotspots/runs", {
    method: "GET",
    search: request.nextUrl.search,
  });
}
