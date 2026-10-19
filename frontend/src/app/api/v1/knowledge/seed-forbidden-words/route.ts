/**
 * 违禁词词表同步代理（第 19 步）：POST → **幂等同步内置词表**。
 *
 * - POST `/api/v1/knowledge/seed-forbidden-words?dry_run=`；
 * - 幂等：与库内既有 active 违禁词 content_hash 相同则跳过（返回 reused）；
 * - `dry_run=true`：只预检将写入条数，**零写入**；
 * - 纯数据库批量写（不向量化、不调模型）：超时 60s（与热搜刷新同档）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function POST(request: NextRequest) {
  return proxyToBackend("/api/v1/knowledge/seed-forbidden-words", {
    method: "POST",
    search: request.nextUrl.search,
    timeoutMs: 60000,
  });
}
