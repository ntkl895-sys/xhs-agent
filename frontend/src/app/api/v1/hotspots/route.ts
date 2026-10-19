/**
 * 热搜集合代理：GET（分页列表）。
 *
 * 浏览器只请求本站 /api/v1/hotspots（同源相对路径），由本 Route Handler 在**服务端**
 * 经 `proxyToBackend` 转发到 FastAPI；查询串原样透传，状态码与响应体原样回传。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function GET(request: NextRequest) {
  return proxyToBackend("/api/v1/hotspots", {
    method: "GET",
    search: request.nextUrl.search,
  });
}
