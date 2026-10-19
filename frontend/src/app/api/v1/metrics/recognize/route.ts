/**
 * 截图识别代理（第 15 步）：multipart **原样转发**。
 *
 * - POST `/api/v1/metrics/recognize`（字段：file 必填；note_id / save_screenshot 可选）
 * - 服务端从入参构造 FormData 直传后端 —— boundary 由 fetch 自动生成，**不手动设头**；
 * - 视觉模型为同步长耗时接口：超时放大到 120s（与选题生成同档）；
 * - 识别结果只是**候选**（不写库），人工核对后才通过 `/api/v1/metrics` 落库。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function POST(request: NextRequest) {
  const formData = await request.formData();
  return proxyToBackend("/api/v1/metrics/recognize", {
    method: "POST",
    formData,
    timeoutMs: 120000,
  });
}
