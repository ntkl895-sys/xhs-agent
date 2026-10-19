/**
 * 最近选题运行代理：GET（透传 limit）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function GET(request: NextRequest) {
  return proxyToBackend("/api/v1/topics/runs", {
    method: "GET",
    search: request.nextUrl.search,
  });
}
