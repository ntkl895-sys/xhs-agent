/**
 * 各源状态代理：GET。
 *
 * 返回普通数组（非分页资源），语义直白；无查询参数。
 */

import { proxyToBackend } from "@/lib/api-server";

export async function GET() {
  return proxyToBackend("/api/v1/hotspots/sources", { method: "GET" });
}
