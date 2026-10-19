/**
 * 笔记集合代理：GET（分页列表）。
 *
 * 第 12 步只做了后端 `/api/v1/notes`；本文件是第 13 步「封面工作室」顶部
 * 笔记选择器的必要薄转发（浏览器只请求本站 /api/v1/**）。
 * 仅转发 GET —— 笔记的写操作属后续步骤。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function GET(request: NextRequest) {
  return proxyToBackend("/api/v1/notes", {
    method: "GET",
    search: request.nextUrl.search,
  });
}
