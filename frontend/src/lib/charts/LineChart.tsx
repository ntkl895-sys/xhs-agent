"use client";

/**
 * 纯 SVG 折线图（第 17 步 · 运营看板）——**npm 零新增依赖**（禁图表库）。
 *
 * 约定（任务书 6.1）：
 *
 * - 只接收**已聚合好的** ``points``（``[{bucket, value}]``）——组件内**不发请求**；
 * - ``value === null`` 处**线段断开**（不补点、不插值——空桶代表没数据）；
 * - 每个点 ``<title>`` 显示 ``bucket：value``（原生 hover 提示）；
 * - 容器宽度用 ``ResizeObserver`` 自适应，高度固定；
 * - 无数据时渲染空态（不画假数据）。
 *
 * 颜色走 Tailwind 语义色（``text-sky-600`` + ``currentColor``），不引色板依赖。
 */

import { useEffect, useRef, useState } from "react";

export interface LineChartPoint {
  bucket: string;
  value: number | null;
}

export interface LineChartProps {
  points: LineChartPoint[];
  /** 固定高度（px） */
  height?: number;
  /** 无障碍标签（如「浏览量趋势」） */
  ariaLabel?: string;
  /** hover 提示里的系列名（如「浏览量」） */
  seriesLabel?: string;
}

const PAD_LEFT = 46;
const PAD_RIGHT = 14;
const PAD_TOP = 12;
const PAD_BOTTOM = 26;
const Y_TICKS = 4;

/** 把坐标轴最大值取整到「好看」的刻度（1/2/5 × 10^n 向上取）。 */
function niceCeil(value: number): number {
  if (value <= 0) return 1;
  const exponent = Math.floor(Math.log10(value));
  const base = Math.pow(10, exponent);
  for (const factor of [1, 2, 5, 10]) {
    if (value <= factor * base) return factor * base;
  }
  return 10 * base;
}

/** 刻度文本（大数压缩：≥1 万显示「x.x万」）。 */
function tickText(value: number): string {
  if (value >= 10000) {
    const w = value / 10000;
    return `${Number.isInteger(w) ? w : w.toFixed(1)}万`;
  }
  return String(value);
}

export function LineChart({
  points,
  height = 240,
  ariaLabel = "趋势图",
  seriesLabel = "数值",
}: LineChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const next = Math.round(entry.contentRect.width);
      setWidth((prev) => (prev === next ? prev : next));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const values = points
    .map((p) => p.value)
    .filter((v): v is number => v !== null && Number.isFinite(v));
  const hasData = points.length > 0 && values.length > 0;

  const innerWidth = Math.max(0, width - PAD_LEFT - PAD_RIGHT);
  const innerHeight = Math.max(0, height - PAD_TOP - PAD_BOTTOM);
  const maxValue = hasData ? niceCeil(Math.max(...values)) : 1;
  const count = points.length;

  const xAt = (index: number): number => {
    if (count <= 1) return PAD_LEFT + innerWidth / 2;
    return PAD_LEFT + (innerWidth * index) / (count - 1);
  };
  const yAt = (value: number): number =>
    PAD_TOP + innerHeight * (1 - value / maxValue);

  // 连续非空段（null 断开）：每段各自成 polyline
  const segments: { index: number; value: number }[][] = [];
  let current: { index: number; value: number }[] = [];
  points.forEach((point, index) => {
    if (point.value === null || !Number.isFinite(point.value)) {
      if (current.length) segments.push(current);
      current = [];
    } else {
      current.push({ index, value: point.value });
    }
  });
  if (current.length) segments.push(current);

  // x 轴标签抽稀：最多约 7 个（首尾必显示）
  const labelStep = Math.max(1, Math.ceil(count / 7));

  return (
    <div ref={containerRef} style={{ height }} className="w-full">
      {!hasData || width <= 0 ? (
        <div className="flex h-full items-center justify-center rounded-md border border-dashed border-border text-sm text-muted-foreground">
          暂无数据
        </div>
      ) : (
        <svg
          width={width}
          height={height}
          role="img"
          aria-label={ariaLabel}
          className="text-sky-600"
        >
          {/* y 轴网格与刻度 */}
          <g className="text-gray-500">
            {Array.from({ length: Y_TICKS + 1 }, (_, tick) => {
              const value = (maxValue * tick) / Y_TICKS;
              const y = yAt(value);
              return (
                <g key={tick}>
                  <line
                    x1={PAD_LEFT}
                    y1={y}
                    x2={PAD_LEFT + innerWidth}
                    y2={y}
                    stroke="currentColor"
                    strokeOpacity={tick === 0 ? 0.35 : 0.15}
                  />
                  <text
                    x={PAD_LEFT - 6}
                    y={y + 3}
                    textAnchor="end"
                    fontSize={11}
                    fill="currentColor"
                  >
                    {tickText(Math.round(value))}
                  </text>
                </g>
              );
            })}
          </g>

          {/* 折线（null 处断开） */}
          {segments.map((segment, segmentIndex) =>
            segment.length === 1 ? null : (
              <polyline
                key={`seg-${segmentIndex}`}
                points={segment
                  .map((item) => `${xAt(item.index)},${yAt(item.value)}`)
                  .join(" ")}
                fill="none"
                stroke="currentColor"
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
            ),
          )}

          {/* 数据点（含单点段） + 原生 tooltip */}
          {segments.flat().map((item) => (
            <circle
              key={`dot-${item.index}`}
              cx={xAt(item.index)}
              cy={yAt(item.value)}
              r={3}
              fill="currentColor"
            >
              <title>{`${points[item.index]?.bucket ?? item.index} ${seriesLabel}：${item.value}`}</title>
            </circle>
          ))}

          {/* x 轴标签（抽稀显示） */}
          <g className="text-gray-500">
            {points.map((point, index) => {
              const isEdge = index === 0 || index === count - 1;
              if (!isEdge && index % labelStep !== 0) return null;
              return (
                <text
                  key={`x-${index}`}
                  x={xAt(index)}
                  y={height - 8}
                  textAnchor="middle"
                  fontSize={11}
                  fill="currentColor"
                >
                  {point.bucket.length > 10 ? point.bucket.slice(5) : point.bucket}
                </text>
              );
            })}
          </g>
        </svg>
      )}
    </div>
  );
}

export default LineChart;
