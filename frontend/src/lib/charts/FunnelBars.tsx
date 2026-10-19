/**
 * 漏斗横条（第 17 步 · 运营看板）——**npm 零新增依赖**（纯 CSS）。
 *
 * 约定（任务书 6.1 / 6.2）：
 *
 * - 只接收**已聚合好的**合计与六率（``totals`` / ``rates``）——不发请求、
 *   不做聚合算术；条形宽度 = 该指标合计 / views 合计（纯渲染归一）；
 * - 六个指标逐行显示「合计数值 + 对应率」；``views`` 行为基准（显示「基准」）；
 * - ``views`` 合计 <= 0 时六率显示「—」（后端已给 null，前端绝不臆造）；
 * - 底部显示整体互动率（``engagement_rate``）。
 */

import { formatNumber } from "@/components/shared";

export interface FunnelBarsProps {
  /** 六指标合计（缺失按 0；与后端 latest_totals 一致） */
  totals: Record<string, number>;
  /** 六率（views 合计<=0 时全 null） */
  rates: Record<string, number | null>;
}

interface FunnelRow {
  key: string;
  label: string;
  /** 对应率键；views 行为 null（基准） */
  rateKey: string | null;
  barClass: string;
}

const FUNNEL_ROWS: FunnelRow[] = [
  { key: "views", label: "浏览量", rateKey: null, barClass: "bg-sky-500" },
  { key: "likes", label: "点赞", rateKey: "like_rate", barClass: "bg-rose-500" },
  { key: "collects", label: "收藏", rateKey: "collect_rate", barClass: "bg-amber-500" },
  { key: "comments", label: "评论", rateKey: "comment_rate", barClass: "bg-emerald-500" },
  { key: "shares", label: "分享", rateKey: "share_rate", barClass: "bg-violet-500" },
  {
    key: "new_followers",
    label: "新增粉丝",
    rateKey: "follow_rate",
    barClass: "bg-cyan-500",
  },
];

/** 比率（0~1 小数）-> 百分比展示；null -> 「—」 */
function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(2)}%`;
}

export function FunnelBars({ totals, rates }: FunnelBarsProps) {
  const views = totals.views ?? 0;
  const canRate = views > 0;

  return (
    <div className="flex flex-col gap-2">
      {FUNNEL_ROWS.map((row) => {
        const value = totals[row.key] ?? 0;
        const percent = canRate
          ? Math.min(100, Math.max(row.key === "views" ? 100 : 0, (value / views) * 100))
          : 0;
        const rateText = row.rateKey === null ? "基准" : formatPercent(rates[row.rateKey]);
        return (
          <div
            key={row.key}
            className="grid grid-cols-[minmax(0,5.5rem)_1fr_5rem_4.5rem] items-center gap-2 text-sm"
          >
            <div className="text-muted-foreground">{row.label}</div>
            <div className="h-5 overflow-hidden rounded bg-muted">
              {canRate && value > 0 && (
                <div
                  className={`h-full rounded ${row.barClass}`}
                  style={{ width: `${Math.max(percent, row.key === "views" ? 100 : 1.5)}%` }}
                />
              )}
            </div>
            <div className="text-right tabular-nums">{formatNumber(value)}</div>
            <div className="text-right tabular-nums text-muted-foreground">
              {rateText}
            </div>
          </div>
        );
      })}
      <div className="mt-1 border-t border-border pt-2 text-xs text-muted-foreground">
        整体互动率（engagement_rate，含点赞 / 收藏 / 评论 / 分享）：{" "}
        <span className="tabular-nums">{formatPercent(rates.engagement_rate)}</span>
        {!canRate && <span className="ml-2">（浏览量合计为 0，各率不可计算）</span>}
      </div>
    </div>
  );
}

export default FunnelBars;
