/**
 * 单个封面候选代理：PATCH（更新字段 / 互斥选中）。
 *
 * 本步不提供 PUT / DELETE（与后端一致，封面候选不物理删除）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

type Context = { params: Promise<{ id: string }> };

export async function PATCH(request: NextRequest, context: Context) {
  const { id } = await context.params;
  const body = await request.text();
  return proxyToBackend(`/api/v1/covers/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body,
  });
}
