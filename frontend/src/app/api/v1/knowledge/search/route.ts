/**
 * 知识检索测试代理（第 18 步）：POST **原样转发**（JSON text 透传）。
 *
 * - POST `/api/v1/knowledge/search`（body：query / chunk_types? / k?）→ top-k；
 * - 检索需向量化查询文本（本地模型可能首次加载）：超时放大到 120s；
 * - 向量化未就绪时后端返回 400（如实告知，不假装无命中）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function POST(request: NextRequest) {
  const body = await request.text();
  return proxyToBackend("/api/v1/knowledge/search", {
    method: "POST",
    body,
    timeoutMs: 120000,
  });
}
