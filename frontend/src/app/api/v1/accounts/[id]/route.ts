/**
 * 单个账号代理：GET（详情）/ PATCH（更新，含归档 / 恢复）
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

type Context = { params: Promise<{ id: string }> };

export async function GET(_request: NextRequest, context: Context) {
  const { id } = await context.params;
  return proxyToBackend(`/api/v1/accounts/${encodeURIComponent(id)}`, { method: "GET" });
}

export async function PATCH(request: NextRequest, context: Context) {
  const { id } = await context.params;
  const body = await request.text();
  return proxyToBackend(`/api/v1/accounts/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body,
  });
}
