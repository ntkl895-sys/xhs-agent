/**
 * 多版本生成代理：POST。
 *
 * 单次调用产出 N 个版本（含有限回路与逐版判定），耗时可达 1~2 分钟，
 * 故 timeoutMs 放大到 120000；未配置 LLM 时后端返回 400（原样透传，前端友好提示）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function POST(request: NextRequest) {
  const body = await request.text();
  return proxyToBackend("/api/v1/content-variants/generate", {
    method: "POST",
    body,
    timeoutMs: 120000,
  });
}
