/**
 * 单个知识块代理（第 18 步）：GET 详情 + PATCH 归档/恢复。
 *
 * - GET  `/api/v1/knowledge/{id}` 详情；
 * - PATCH `/api/v1/knowledge/{id}` 仅 status 在 active / archived 间切换；
 * - **不提供** PUT / DELETE（知识块只增 / 归档，无物理删除，与后端一致）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

type Context = { params: Promise<{ id: string }> };

export async function GET(request: NextRequest, context: Context) {
  const { id } = await context.params;
  return proxyToBackend(`/api/v1/knowledge/${encodeURIComponent(id)}`, {
    method: "GET",
    search: request.nextUrl.search,
  });
}

export async function PATCH(request: NextRequest, context: Context) {
  const { id } = await context.params;
  const body = await request.text();
  return proxyToBackend(`/api/v1/knowledge/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body,
  });
}
