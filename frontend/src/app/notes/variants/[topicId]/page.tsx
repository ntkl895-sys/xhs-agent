/**
 * 「多版本对比」路由壳（**Server Component**）。
 *
 * ⚠️ Next 16（cacheComponents）下的组合约定（与 `/notes/[id]` 一致）：
 *
 * 1. `export const instant = false` —— **blocking route**，允许本动态段在请求时求值
 *    运行时 URL 数据；route segment config 只能由 Server Component 导出，
 *    因此本壳与交互实现 `variants-client.tsx` 拆成两个文件；
 * 2. 客户端组件的 `useParams()` 在服务端渲染时会请求「浏览器渲染」bailout，
 *    必须包在 `<Suspense>` 边界内（否则 build 失败）。
 *
 * 本壳不新增任何服务端数据获取：数据一律由客户端经同源 /api/v1/** 拉取。
 */

import { Suspense } from "react";

import VariantsClient from "./variants-client";

export const instant = false;

export default function TopicVariantsPage() {
  return (
    <Suspense
      fallback={
        <main className="mx-auto flex w-full max-w-7xl flex-1 flex-col gap-6 px-6 py-10">
          <p className="text-sm text-muted-foreground">加载中……</p>
        </main>
      }
    >
      <VariantsClient />
    </Suspense>
  );
}
