/**
 * 账号集合代理：GET（列表）/ POST（新建）
 *
 * 浏览器 -> /api/v1/accounts -> 本处理器 -> 后端 /api/v1/accounts
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function GET(request: NextRequest) {
  return proxyToBackend("/api/v1/accounts", {
    method: "GET",
    search: request.nextUrl.search,
  });
}

export async function POST(request: NextRequest) {
  const body = await request.text();
  return proxyToBackend("/api/v1/accounts", { method: "POST", body });
}
