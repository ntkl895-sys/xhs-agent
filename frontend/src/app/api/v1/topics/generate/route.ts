/**
 * 选题生成代理：POST。
 *
 * 生成走真实 LLM（每个热点一次模型调用 + 可能的有限回路），耗时可达数十秒，
 * 故 timeoutMs 放大到 120000；未配置 LLM 时后端返回 400（原样透传，前端友好提示）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function POST(request: NextRequest) {
  const body = await request.text();
  return proxyToBackend("/api/v1/topics/generate", {
    method: "POST",
    body,
    timeoutMs: 120000,
  });
}
