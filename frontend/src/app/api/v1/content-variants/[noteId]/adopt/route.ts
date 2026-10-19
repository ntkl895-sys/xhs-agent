/**
 * 采用版本代理：PATCH（单事务：该版置采用、同批次其余置未采用）。
 *
 * 薄转发（浏览器只请求本站 /api/v1/**）：状态码与响应体由 `proxyToBackend`
 * 原样透传；**不提供** PUT / DELETE（与后端一致，未采用版本只软标记、永不删除）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

type Context = { params: Promise<{ noteId: string }> };

export async function PATCH(_request: NextRequest, context: Context) {
  const { noteId } = await context.params;
  return proxyToBackend(`/api/v1/content-variants/${encodeURIComponent(noteId)}/adopt`, {
    method: "PATCH",
    body: "{}",
  });
}
