/**
 * 前端健康检查代理（Route Handler，运行在服务端）。
 *
 * 浏览器请求本站 /api/health -> 本处理器在服务端请求后端 ${API_BASE_URL}/health/ready，
 * 并把后端的 JSON 与状态码原样透传。
 *
 * 后端不可达时返回结构化降级结果（不抛错、不白屏）：
 *   {"status":"degraded","checks":{"backend":"unreachable"}}
 */

import { NextResponse } from "next/server";

// 服务端变量（不加 NEXT_PUBLIC_ 前缀，不会暴露到浏览器）
const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";

export async function GET() {
  try {
    const res = await fetch(`${API_BASE_URL}/health/ready`, {
      cache: "no-store",
      // 后端在中间件不可达时最长约 10 秒返回，这里留 15 秒余量
      signal: AbortSignal.timeout(15000),
    });

    const body = await res.json().catch(() => null);

    // 透传后端 JSON 与状态码
    return NextResponse.json(
      body ?? {
        status: "degraded",
        checks: { backend: "invalid-response" },
      },
      { status: res.status },
    );
  } catch (err) {
    // 后端不可达 / 超时：返回结构化降级结果，HTTP 503
    return NextResponse.json(
      {
        status: "degraded",
        checks: { backend: "unreachable" },
        detail: err instanceof Error ? err.message : String(err),
      },
      { status: 503 },
    );
  }
}
