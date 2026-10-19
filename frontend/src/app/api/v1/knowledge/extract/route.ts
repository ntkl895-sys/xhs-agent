/**
 * 爆款规律提炼代理（第 18 步）：POST **原样转发**（无请求体）。
 *
 * - POST `/api/v1/knowledge/extract` 跑第六个业务图（真 LLM + 本地向量化）；
 * - 同步长耗时接口（本地模型可能首次加载 / 下载，约 1.3GB）：超时放大到 120s
 *   （与生成 / 识别 / 诊断 / 周报同档）；
 * - 样本不足不是错误：后端返回 201 + `insufficient_sample=true`（零写入），
 *   页面据此给出「至少发布 N 篇」的指引。
 */

import { proxyToBackend } from "@/lib/api-server";

export async function POST() {
  return proxyToBackend("/api/v1/knowledge/extract", {
    method: "POST",
    timeoutMs: 120000,
  });
}
