"use client";

import {
  AreaSeries,
  ColorType,
  CrosshairMode,
  LineStyle,
  TickMarkType,
  createChart,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useTheme } from "next-themes";
import { useEffect, useRef } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/states";
import { useHistory } from "@/lib/queries";
import { useLiveStore } from "@/lib/store";
import { cn } from "@/lib/utils";

/** Resolve a CSS color (including oklch) to rgba by painting one pixel. */
function cssColor(varName: string, alpha = 1): string {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(varName).trim() || "#888";
  const c = document.createElement("canvas");
  c.width = c.height = 1;
  const ctx = c.getContext("2d");
  if (!ctx) return raw;
  ctx.fillStyle = raw;
  ctx.fillRect(0, 0, 1, 1);
  const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
  return `rgba(${r}, ${g}, ${b}, ${((a / 255) * alpha).toFixed(3)})`;
}

const priceFmt = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

// Lightweight Charts treats timestamps as UTC; render every label in IST instead.
const IST = "Asia/Kolkata";
const istTime = new Intl.DateTimeFormat("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: IST });
const istTimeSec = new Intl.DateTimeFormat("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: IST });
const istDay = new Intl.DateTimeFormat("en-IN", { day: "2-digit", month: "short", timeZone: IST });
const istMonth = new Intl.DateTimeFormat("en-IN", { month: "short", year: "numeric", timeZone: IST });
const istFull = new Intl.DateTimeFormat("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: IST });

function toDate(t: Time): Date {
  if (typeof t === "number") return new Date(t * 1000);
  if (typeof t === "string") return new Date(t);
  return new Date(Date.UTC(t.year, t.month - 1, t.day));
}

function tickLabel(t: Time, type: TickMarkType): string {
  const d = toDate(t);
  switch (type) {
    case TickMarkType.Year:
      return String(d.getUTCFullYear());
    case TickMarkType.Month:
      return istMonth.format(d);
    case TickMarkType.DayOfMonth:
      return istDay.format(d);
    case TickMarkType.TimeWithSeconds:
      return istTimeSec.format(d);
    default:
      return istTime.format(d);
  }
}

export interface ChartLine {
  price: number; // rupees
  title: string;
  tone: "human" | "warning" | "danger" | "info";
}

/**
 * Lightweight Charts area chart: loads /api/market/history, then appends live SSE ticks.
 * The TradingView attribution logo stays enabled (required by the Apache-2.0 NOTICE).
 */
export function PriceChart({
  symbol,
  instrumentId,
  height = 280,
  lines = [],
  className,
}: {
  symbol: string;
  instrumentId: string | undefined;
  height?: number;
  lines?: ChartLine[];
  className?: string;
}) {
  const host = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Area"> | null>(null);
  const lastTime = useRef<number>(0);
  const priceLines = useRef<IPriceLine[]>([]);
  const history = useHistory(symbol);
  const { resolvedTheme } = useTheme();

  // Create the chart once per mount.
  useEffect(() => {
    if (!host.current) return;
    const chart = createChart(host.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        attributionLogo: true,
        fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
        fontSize: 11,
      },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.12, bottom: 0.08 } },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: true, tickMarkFormatter: tickLabel },
      crosshair: { mode: CrosshairMode.Magnet },
      localization: {
        locale: "en-IN",
        priceFormatter: (p: number) => priceFmt.format(p),
        timeFormatter: (t: Time) => `${istFull.format(toDate(t))} IST`,
      },
      handleScale: { axisPressedMouseMove: false },
    });
    const series = chart.addSeries(AreaSeries, {
      lineWidth: 2,
      priceLineVisible: true,
      lastValueVisible: true,
      priceFormat: { type: "price", precision: 2, minMove: 0.05 },
    });
    chartRef.current = chart;
    seriesRef.current = series;
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      priceLines.current = [];
    };
  }, []);

  // Theme colors.
  useEffect(() => {
    const chart = chartRef.current;
    const series = seriesRef.current;
    if (!chart || !series) return;
    chart.applyOptions({
      layout: { textColor: cssColor("--muted-foreground") },
      grid: {
        vertLines: { color: cssColor("--foreground", 0.04) },
        horzLines: { color: cssColor("--foreground", 0.05) },
      },
      crosshair: {
        vertLine: { color: cssColor("--foreground", 0.25), labelBackgroundColor: cssColor("--surface-3") },
        horzLine: { color: cssColor("--foreground", 0.25), labelBackgroundColor: cssColor("--surface-3") },
      },
    });
    series.applyOptions({
      lineColor: cssColor("--human"),
      topColor: cssColor("--human", 0.28),
      bottomColor: cssColor("--human", 0.0),
    });
  }, [resolvedTheme]);

  // History.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series || !history.data) return;
    const data = history.data.map((p) => ({ time: p.time as UTCTimestamp, value: p.value }));
    series.setData(data);
    lastTime.current = data.at(-1)?.time ?? 0;
    chartRef.current?.timeScale().fitContent();
  }, [history.data]);

  // Live ticks: subscribe outside React render to avoid re-rendering on every tick.
  useEffect(() => {
    if (!instrumentId) return;
    return useLiveStore.subscribe((s, prev) => {
      const t = s.ticks[instrumentId];
      if (!t || t === prev.ticks[instrumentId]) return;
      const series = seriesRef.current;
      if (!series || !lastTime.current) return;
      const ts = Math.floor(Date.parse(t.ts) / 1000);
      const time = Math.max(ts, lastTime.current) as UTCTimestamp;
      series.update({ time, value: t.ltp_paise / 100 });
      lastTime.current = time;
    });
  }, [instrumentId]);

  // Reference lines (approval band, mandate triggers).
  const linesKey = JSON.stringify(lines);
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    for (const pl of priceLines.current) series.removePriceLine(pl);
    priceLines.current = (JSON.parse(linesKey) as ChartLine[]).map((l) =>
      series.createPriceLine({
        price: l.price,
        title: l.title,
        color: cssColor(`--${l.tone}`),
        lineStyle: LineStyle.Dashed,
        lineWidth: 1,
        axisLabelVisible: true,
      }),
    );
  }, [linesKey, resolvedTheme]);

  return (
    <div className={cn("relative w-full", className)} style={{ height }}>
      <div ref={host} className="absolute inset-0" />
      {history.isPending && <Skeleton className="absolute inset-0 rounded-lg" />}
      {history.error && (
        <div className="absolute inset-0 grid place-items-center p-4">
          <ErrorState error={history.error} title={`Could not load ${symbol} history`} onRetry={() => void history.refetch()} compact />
        </div>
      )}
    </div>
  );
}
