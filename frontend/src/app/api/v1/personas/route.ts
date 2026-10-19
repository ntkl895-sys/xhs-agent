/**
 * 人设集合代理：GET（列表）/ POST（新建）
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function GET(request: NextRequest) {
  return proxyToBackend("/api/v1/personas", {
    method: "GET",
    search: request.nextUrl.search,
  });
}

export async function POST(request: NextRequest) {
  const body = await request.text();
  return proxyToBackend("/api/v1/personas", { method: "POST", body });
}
