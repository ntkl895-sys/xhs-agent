/**
 * 单篇诊断集合代理（第 16 步）：GET 诊断历史。
 *
 * - GET `/api/v1/diagnostics?note_id=…` 某笔记的诊断历史（薄转发，note_id 必填）
 *
 * 不做任何加工：状态码与响应体原样透传（含 400 / 404 / 422）；
 * **不提供** POST / PUT / PATCH / DELETE（生成走 /generate；诊断只增不改）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function GET(request: NextRequest) {
  return proxyToBackend("/api/v1/diagnostics", {
    method: "GET",
    search: request.nextUrl.search,
  });
}
