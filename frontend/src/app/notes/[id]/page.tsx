/**
 * 「创作台」路由壳（**Server Component**）。
 *
 * ⚠️ Next 16（cacheComponents）下的组合约定（经三轮 build 实验确定）：
 *
 * 1. `export const instant = false` —— **blocking route**，允许本动态段在请求时求值
 *    运行时 URL 数据（全局 SiteHeader 的 `usePathname()` 在预渲染静态壳时不可得，
 *    不声明会直接 build 失败）；route segment config **只能由 Server Component 导出**，
 *    因此本壳与交互实现 `note-studio-client.tsx` 拆成两个文件；
 * 2. 客户端组件的 `useParams()` 在服务端渲染时会请求「浏览器渲染」bailout，
 *    必须包在 `<Suspense>` 边界内（否则 build 失败）。
 *
 * 本壳不新增任何服务端数据获取：数据一律由客户端经同源 /api/v1/** 拉取。
 */

import { Suspense } from "react";

import NoteStudioClient from "./note-studio-client";

export const instant = false;

export default function NoteStudioPage() {
  return (
    <Suspense
      fallback={
        <main className="mx-auto flex w-full max-w-7xl flex-1 flex-col gap-6 px-6 py-10">
          <p className="text-sm text-muted-foreground">加载中……</p>
        </main>
      }
    >
      <NoteStudioClient />
    </Suspense>
  );
}
