/**
 * 服务端统一代理封装（只在 Route Handler / Server 环境使用）。
 *
 * 范式（延续第 6 步）：
 *   浏览器只请求本站 /api/v1/**  ->  Route Handler 在**服务端**转请求后端。
 *
 * 因此后端地址用 `API_BASE_URL`（**不加 NEXT_PUBLIC_ 前缀**），
 * 永远不会出现在浏览器产物里。
 *
 * 第 15 步（数据回填）扩展：支持 multipart（FormData）**原样透传** ——
 * 截图识别的 `file` 字段只在服务端流转，浏览器产物里不含后端地址，也不含文件内容。
 */

import { NextResponse } from "next/server";

/** 后端基址（服务端变量）。 */
const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";

/** 代理默认超时（毫秒）。后端中间件不可达时约 10s 返回，这里留足余量。 */
const PROXY_TIMEOUT_MS = 15000;

export type ProxyMethod = "GET" | "POST" | "PATCH";

export interface ProxyInit {
  method: ProxyMethod;
  /** 原样透传的查询串（含前导 ?），如 "?page=1&q=abc" */
  search?: string;
  /** 原始请求体字符串（JSON），GET 可不传 */
  body?: string | null;
  /**
   * multipart **原样透传**（与 body 互斥；第 15 步截图识别）。
   *
   * 传 FormData 时**绝不**手动设置 Content-Type —— 边界（boundary）由
   * fetch 依据 FormData 自动生成；手动设置会破坏 multipart 解析。
   */
  formData?: FormData | null;
  /**
   * 可选超时（毫秒）。缺省用 {@link PROXY_TIMEOUT_MS}（15000）。
   * 仅少数同步长耗时接口需要放大：选题生成 / 截图识别 120000、热搜刷新 60000。
   */
  timeoutMs?: number;
}

/**
 * 把一次请求转发给后端，并把**状态码与响应体原样透传**。
 *
 * 后端不可达 / 超时：返回 503 + 统一错误体，页面据此展示友好提示。
 */
export async function proxyToBackend(path: string, init: ProxyInit): Promise<NextResponse> {
  const url = `${API_BASE_URL}${path}${init.search ?? ""}`;

  const hasForm = init.formData != null;
  // FormData 时交给 fetch 自动生成 multipart 头（含 boundary）
  const headers =
    !hasForm && init.body ? { "Content-Type": "application/json" } : undefined;

  try {
    const res = await fetch(url, {
      method: init.method,
      cache: "no-store",
      headers,
      body: hasForm ? init.formData : (init.body ?? undefined),
      signal: AbortSignal.timeout(init.timeoutMs ?? PROXY_TIMEOUT_MS),
    });

    const text = await res.text();
    if (text) {
      try {
        return NextResponse.json(JSON.parse(text), { status: res.status });
      } catch {
        // 后端返回了非 JSON（如代理 502 页面）：原样透传文本
        return new NextResponse(text, {
          status: res.status,
          headers: { "content-type": res.headers.get("content-type") ?? "text/plain" },
        });
      }
    }
    return new NextResponse(null, { status: res.status });
  } catch (err) {
    return NextResponse.json(
      {
        status: "error",
        code: "backend_unreachable",
        message: "后端不可达或超时，请确认 uvicorn / docker compose 已启动。",
        details: err instanceof Error ? err.message : String(err),
      },
      { status: 503 },
    );
  }
}
