/**
 * 单篇笔记代理：GET（详情）/ PATCH（人工编辑 / 状态流转 / 标记发布）。
 *
 * 第 14 步新增的薄转发（浏览器只请求本站 /api/v1/**）：
 * 状态码与响应体由 `proxyToBackend` 原样透传；不写 `export const dynamic`
 * （Route Handler 默认即动态）；**不提供** PUT / DELETE（与后端一致）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

type Context = { params: Promise<{ id: string }> };

export async function GET(request: NextRequest, context: Context) {
  const { id } = await context.params;
  return proxyToBackend(`/api/v1/notes/${encodeURIComponent(id)}`, {
    method: "GET",
    search: request.nextUrl.search,
  });
}

export async function PATCH(request: NextRequest, context: Context) {
  const { id } = await context.params;
  const body = await request.text();
  return proxyToBackend(`/api/v1/notes/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body,
  });
}
