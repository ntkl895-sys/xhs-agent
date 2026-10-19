/**
 * 纯 CSS 横向条形（第 17 步 · 运营看板）——**npm 零新增依赖**。
 *
 * 约定（任务书 6.1）：
 *
 * - 只接收**已聚合好的** ``items``（``[{label, value, href?}]``）——不发请求；
 * - 按最大值归一宽度（归一化只是渲染比例，不是「聚合算术」）；
 * - ``value === null`` 的条目排最后、条形留空、数值显示「—」（不臆造）；
 * - ``href`` 存在时标题可点击（如跳笔记详情）。
 */

import Link from "next/link";

import { formatNumber } from "@/components/shared";

export interface HBarItem {
  label: string;
  value: number | null;
  /** 可选跳转（列表标题链接） */
  href?: string;
}

export interface HBarsProps {
  items: HBarItem[];
  /** 空态文案 */
  emptyText?: string;
}

export function HBars({ items, emptyText = "暂无数据" }: HBarsProps) {
  if (!items.length) {
    return (
      <div className="flex h-24 items-center justify-center rounded-md border border-dashed border-border text-sm text-muted-foreground">
        {emptyText}
      </div>
    );
  }

  const max = Math.max(
    1,
    ...items.map((item) => (item.value === null ? 0 : item.value)),
  );

  return (
    <div className="flex flex-col gap-2">
      {items.map((item, index) => {
        const percent =
          item.value === null ? 0 : Math.max(2, (item.value / max) * 100);
        return (
          <div
            key={`${item.label}-${index}`}
            className="grid grid-cols-[minmax(0,8.5rem)_1fr_4.5rem] items-center gap-2 text-sm"
          >
            <div className="truncate" title={item.label}>
              {item.href ? (
                <Link
                  href={item.href}
                  className="underline-offset-2 hover:underline"
                >
                  {item.label}
                </Link>
              ) : (
                item.label
              )}
            </div>
            <div className="h-4 overflow-hidden rounded bg-muted">
              {item.value !== null && (
                <div
                  className="h-full rounded bg-sky-500"
                  style={{ width: `${percent}%` }}
                />
              )}
            </div>
            <div className="text-right tabular-nums text-muted-foreground">
              {formatNumber(item.value)}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default HBars;
