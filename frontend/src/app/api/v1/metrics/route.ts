/**
 * 数据回填集合代理（第 15 步）：GET 时间线 + POST 快照落库。
 *
 * - GET  `/api/v1/metrics?note_id=…`  某笔记的数据时间线（薄转发，note_id 必填）
 * - POST `/api/v1/metrics`            落库一条快照（JSON 薄转发；**唯一写入口**）
 *
 * 不做任何加工：状态码与响应体原样透传（含 400 / 404 / 409 / 422）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function GET(request: NextRequest) {
  return proxyToBackend("/api/v1/metrics", {
    method: "GET",
    search: request.nextUrl.search,
  });
}

export async function POST(request: NextRequest) {
  const body = await request.text();
  return proxyToBackend("/api/v1/metrics", {
    method: "POST",
    body,
  });
}
