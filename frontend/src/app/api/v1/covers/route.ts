/**
 * 封面候选集合代理：GET（透传 note_id 查询串）/ POST（新建候选）。
 *
 * 薄转发：不写业务逻辑，状态码与响应体由 `proxyToBackend` 原样透传。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function GET(request: NextRequest) {
  return proxyToBackend("/api/v1/covers", {
    method: "GET",
    search: request.nextUrl.search,
  });
}

export async function POST(request: NextRequest) {
  const body = await request.text();
  return proxyToBackend("/api/v1/covers", { method: "POST", body });
}
