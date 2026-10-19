/**
 * 关键词搜索代理：GET（第 23 步）。
 *
 * 查询串原样透传（q / source / sort / limit / force_refresh）；搜索为**只读**接口。
 * timeoutMs 放大到 60000：上游 GitHub 首查（无缓存）加上后端重试/退避处理，
 * 保留与「热搜刷新」同级的长超时余量；缓存命中时毫秒级返回。
 * 前端不持有任何令牌（令牌只从后端 .env 读取）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function GET(request: NextRequest) {
  return proxyToBackend("/api/v1/search", {
    method: "GET",
    search: request.nextUrl.search,
    timeoutMs: 60000,
  });
}
