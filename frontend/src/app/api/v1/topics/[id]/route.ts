/**
 * 单个选题代理：GET（详情）/ PATCH（人工微调，含 draft→approved / discarded）。
 *
 * 详情接口**不返回 llm_raw**（它在 agent_runs），前端也就不可能展示它。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

type Context = { params: Promise<{ id: string }> };

export async function GET(_request: NextRequest, context: Context) {
  const { id } = await context.params;
  return proxyToBackend(`/api/v1/topics/${encodeURIComponent(id)}`, { method: "GET" });
}

export async function PATCH(request: NextRequest, context: Context) {
  const { id } = await context.params;
  const body = await request.text();
  return proxyToBackend(`/api/v1/topics/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body,
  });
}
