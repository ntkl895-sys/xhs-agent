/**
 * 文档导入代理 · 文本粘贴（第 19 步）：POST **原样转发**。
 *
 * - POST `/api/v1/knowledge/import-text` 长文本 → 切分 → 逐块向量化 → 批量入库；
 * - 切分与向量化属长耗时接口：超时放大到 120s（与提炼 / 识别同档）；
 * - 部分块向量化失败如实计数（failed），不影响其它块；未就绪 → 400（零写入）。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function POST(request: NextRequest) {
  const body = await request.text();
  return proxyToBackend("/api/v1/knowledge/import-text", {
    method: "POST",
    body,
    timeoutMs: 120000,
  });
}
