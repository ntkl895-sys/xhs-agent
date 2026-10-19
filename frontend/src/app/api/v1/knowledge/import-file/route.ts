/**
 * 文档导入代理 · 文件上传（第 19 步）：multipart **原样转发**。
 *
 * - POST `/api/v1/knowledge/import-file`（字段：file 必填；chunk_type / ref_id /
 *   chunk_size / chunk_overlap 可选）；
 * - 服务端从入参构造 FormData 直传后端 —— boundary 由 fetch 自动生成，**不手动设头**；
 * - 仅支持 UTF-8 的 .txt / .md（≤1MB），后端校验后与文本导入同流程；
 * - 切分与向量化属长耗时接口：超时放大到 120s。
 */

import type { NextRequest } from "next/server";

import { proxyToBackend } from "@/lib/api-server";

export async function POST(request: NextRequest) {
  const formData = await request.formData();
  return proxyToBackend("/api/v1/knowledge/import-file", {
    method: "POST",
    formData,
    timeoutMs: 120000,
  });
}
