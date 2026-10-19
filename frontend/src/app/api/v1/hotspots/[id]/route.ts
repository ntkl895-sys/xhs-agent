/**
 * 单个热点代理：仅 PATCH（人工标记状态 new / adopted / ignored）。
 *
 * 后端**没有**单热点详情接口（列表行字段已足够），故这里刻意**不导出 GET**，
 * 避免出现「前端有、后端无」的假接口。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

type Context = { params: Promise<{ id: string }> };

export async function PATCH(request: NextRequest, context: Context) {
  const { id } = await context.params;
  const body = await request.text();
  return proxyToBackend(`/api/v1/hotspots/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body,
  });
}
