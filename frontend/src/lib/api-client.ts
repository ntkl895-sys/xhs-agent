/**
 * 浏览器侧 API 客户端：只请求**本站** /api/v1/**。
 *
 * - 不感知后端地址（后端地址是服务端的事）；
 * - 把非 2xx 统一抛成 `ApiError`，页面据此展示 `message` 与字段级 `details`。
 */

import type { ApiErrorBody } from "./types";

/** 带状态码 / 错误码 / 字段明细的客户端错误。 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details?: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

/** 把 422 的 details 渲染成一行行可读文本。 */
export function describeDetails(details: unknown): string[] {
  if (!Array.isArray(details)) return [];
  return details
    .map((item) => {
      if (typeof item !== "object" || item === null) return "";
      const record = item as { loc?: unknown[]; msg?: unknown };
      const loc = Array.isArray(record.loc) ? record.loc.join(".") : "";
      const msg = typeof record.msg === "string" ? record.msg : "";
      return [loc, msg].filter(Boolean).join("：");
    })
    .filter(Boolean);
}

async function request<T>(path: string, init: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { cache: "no-store", ...init });
  } catch (err) {
    throw new ApiError(
      0,
      "network_error",
      `无法连接本站接口：${err instanceof Error ? err.message : String(err)}`,
    );
  }

  const text = await res.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!res.ok) {
    const body = (payload ?? {}) as Partial<ApiErrorBody>;
    throw new ApiError(
      res.status,
      body.code ?? "unknown",
      body.message ?? `请求失败（HTTP ${res.status}）`,
      body.details,
    );
  }
  return payload as T;
}

/** 把对象拼成查询串（跳过 null / undefined / 空字符串）。 */
export function toQuery(params: Record<string, string | number | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const apiClient = {
  get<T>(path: string): Promise<T> {
    return request<T>(path, { method: "GET" });
  },
  post<T>(path: string, body: unknown): Promise<T> {
    return request<T>(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },
  patch<T>(path: string, body: unknown): Promise<T> {
    return request<T>(path, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },
  /**
   * multipart 上传（第 15 步截图识别）：**不手动设置 Content-Type** ——
   * boundary 必须由 fetch 依据 FormData 自动生成，手动设头会导致后端解析失败。
   */
  postForm<T>(path: string, formData: FormData): Promise<T> {
    return request<T>(path, { method: "POST", body: formData });
  },
};
