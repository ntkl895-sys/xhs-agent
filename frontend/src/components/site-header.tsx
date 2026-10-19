"use client";

/**
 * 顶部站点导航（第 11 步；第 13 步加入「封面」，第 14 步加入「创作台」，
 * 第 15 步加入「数据回填」，第 16 步加入「单篇诊断」，第 17 步加入「运营看板」，
 * 第 18 步加入「知识库」）。
 *
 * 入口：环境自检 / 账号与人设 / 热点雷达 / 选题库 / 创作台 / 封面 / 数据回填 /
 * 单篇诊断 / 运营看板 / 知识库；当前页高亮。
 *
 * ⚠️ Next 16（cacheComponents）：`usePathname()` 是运行时 URL 数据；对**动态段路由**
 * （如 `/notes/[id]`）的预渲染会请求「浏览器渲染」bailout，必须包在 `<Suspense>` 内
 * （否则 `next build` 报 `blocking-prerender-client-hook`）。因此拆为
 * `HeaderShell`（纯展示、无 hook）+ `HeaderWithPathname`（读 pathname）+ Suspense：
 * fallback 是**不带高亮**的同款导航壳，视觉上无跳变；
 * 静态路由不受影响（pathname 在 build 时可知，直接渲染高亮版、不落 fallback）。
 */

import Link from "next/link";
import { Suspense } from "react";
import { usePathname } from "next/navigation";

const NAV: { href: string; label: string }[] = [
  { href: "/", label: "环境自检" },
  { href: "/accounts", label: "账号与人设" },
  { href: "/hotspots", label: "热点雷达" },
  { href: "/topics", label: "选题库" },
  { href: "/notes", label: "创作台" },
  { href: "/covers", label: "封面" },
  { href: "/metrics", label: "数据回填" },
  { href: "/diagnostics", label: "单篇诊断" },
  { href: "/dashboard", label: "运营看板" },
  { href: "/knowledge", label: "知识库" },
];

/** 计算当前高亮项（与既有实现等价："/" 完全匹配，其余前缀匹配） */
function activeHrefOf(pathname: string): string | null {
  for (const item of NAV) {
    if (item.href === "/" ? pathname === "/" : pathname.startsWith(item.href)) {
      return item.href;
    }
  }
  return null;
}

/** 纯展示壳：不依赖任何运行时 hook，可被预渲染，也用作 Suspense fallback */
function HeaderShell({ activeHref }: { activeHref: string | null }) {
  return (
    <header className="border-b border-border bg-background/80 backdrop-blur">
      <nav className="mx-auto flex w-full max-w-6xl flex-wrap items-center gap-x-1 gap-y-1 px-6 py-3">
        <span className="mr-3 text-sm font-medium">小红书运营 Agent</span>
        {NAV.map((item) => {
          const active = item.href === activeHref;
          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={active ? "page" : undefined}
              className={
                active
                  ? "rounded-md bg-muted px-2.5 py-1 text-sm font-medium text-foreground"
                  : "rounded-md px-2.5 py-1 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              }
            >
              {item.label}
            </Link>
          );
        })}
      </nav>
    </header>
  );
}

/** 读取运行时 pathname 并高亮（动态段路由下由外层 Suspense 承载 bailout） */
function HeaderWithPathname() {
  const pathname = usePathname();
  return <HeaderShell activeHref={activeHrefOf(pathname)} />;
}

export function SiteHeader() {
  return (
    <Suspense fallback={<HeaderShell activeHref={null} />}>
      <HeaderWithPathname />
    </Suspense>
  );
}
