/**
 * 手动触发采集代理：POST。
 *
 * 采集是**同步长耗时**接口（多源抓取 + 落库，可能数十秒），故 timeoutMs 放大到 60000；
 * 后端若已有采集在跑会返回 409 `fetch_in_progress`，这里原样透传。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function POST(request: NextRequest) {
  const body = await request.text();
  return proxyToBackend("/api/v1/hotspots/refresh", {
    method: "POST",
    body,
    timeoutMs: 60000,
  });
}
