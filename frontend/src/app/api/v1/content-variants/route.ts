/**
 * 多版本批次代理：GET（某选题的版本批次列表）。
 *
 * 纯只读转发（浏览器只请求本站 /api/v1/**）：查询串（topic_id）由 `proxyToBackend`
 * 原样透传；不写 `export const dynamic`（Route Handler 默认即动态）；
 * 生成（POST）与采用（PATCH）见同目录下的 `generate/` 与 `[noteId]/adopt/`。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function GET(request: NextRequest) {
  return proxyToBackend("/api/v1/content-variants", {
    method: "GET",
    search: request.nextUrl.search,
  });
}
