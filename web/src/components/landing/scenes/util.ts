// Small math and color helpers shared by the canvas scenes. Kept out of the "use client"
// module so plain modules (and server components importing their constants) can use them.

import type { ScenePointer } from "../scene-canvas";

export type { Scene, SceneFactory, ScenePointer, SceneSize } from "../scene-canvas";

/** Deterministic pseudo-random in [0, 1) from an integer seed, so scenes look the same on every load. */
export function rand(seed: number): number {
  const x = Math.sin(seed * 127.1 + 311.7) * 43758.5453123;
  return x - Math.floor(x);
}

export function lerp(a: number, b: number, k: number): number {
  return a + (b - a) * k;
}

export function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v;
}

/** Point on a cubic Bezier at parameter k. */
export function cubic(p0: number, p1: number, p2: number, p3: number, k: number): number {
  const m = 1 - k;
  return m * m * m * p0 + 3 * m * m * k * p1 + 3 * m * k * k * p2 + k * k * k * p3;
}

/** Point on a quadratic Bezier at parameter k. */
export function quad(p0: number, p1: number, p2: number, k: number): number {
  const m = 1 - k;
  return m * m * p0 + 2 * m * k * p1 + k * k * p2;
}

/** rgba() string from an [r, g, b] triple. */
export function rgba(c: readonly [number, number, number], a: number): string {
  return `rgba(${c[0]},${c[1]},${c[2]},${a < 0 ? 0 : a > 1 ? 1 : a})`;
}

/** An x, y pair that hot loops reuse instead of allocating. */
export type Pair = [number, number];

/**
 * Pointer comb shared by the scenes: points near the cursor are pushed straight away from it, so
 * lines drawn through displaced points part and fan open around the pointer, like combing hair.
 * The push maps distance r to r + m(r) with m(r) = strength * (1 - (r/R)^2)^2 * r / sqrt(r^2 + core^2):
 * zero at the cursor itself (no NaN, no jump), exactly zero beyond R (straight runs stay straight)
 * and strictly increasing while strength < 0.65 R, so the map never folds and lines never cross.
 * A pin keeps that only with a radius of at least 1.8 * strength: a tighter fade crushes the points
 * between the pin and the cursor into the pin, and lines fold over there.
 */
export interface Comb {
  /** True while any displacement is applied; scenes keep their plain drawing path otherwise. */
  readonly active: boolean;
  /** Smooths the cursor and eases the strength in or out. Call once per frame before drawing. */
  update(pointer: ScenePointer, dt: number): void;
  /** Keeps (x, y) in place: the push fades out within `r` px of it (r >= 1.8 * strength). `r` 0 removes the pin. */
  pin(x: number, y: number, r: number): void;
  /** Whether (x, y) is within reach of the push, plus `pad` px. */
  near(x: number, y: number, pad?: number): boolean;
  /** Whether anything inside the box (x0, y0)-(x1, y1) can be displaced. */
  nearBox(x0: number, y0: number, x1: number, y1: number): boolean;
  /** Eased 0 to 1 influence of the cursor at (x, y), with the same falloff and pin as the push. */
  weight(x: number, y: number): number;
  /** Displaced (x, y), written into one shared pair: read it before the next call. */
  apply(x: number, y: number): Pair;
}

// Seconds: cursor follow time constant, then the ease in and ease out of the whole effect.
const COMB_FOLLOW = 0.06;
const COMB_IN = 0.28;
const COMB_OUT = 0.34;

export function createComb({ radius = 160, strength = 60, core = 18 } = {}): Comb {
  const out: Pair = [0, 0];
  const r2 = radius * radius;
  const c2 = core * core;
  let cx = 0;
  let cy = 0;
  let ramp = 0;
  let amt = 0;
  let px = 0;
  let py = 0;
  let pr = 0;
  return {
    get active() {
      return amt > 0.0005;
    },
    update(pointer, dt) {
      if (pointer.inside && amt < 0.02) {
        // Starting from rest: appear under the cursor instead of gliding in from the last spot.
        cx = pointer.x;
        cy = pointer.y;
      } else {
        const k = 1 - Math.exp(-dt / COMB_FOLLOW);
        cx += (pointer.x - cx) * k;
        cy += (pointer.y - cy) * k;
      }
      ramp = clamp01(ramp + (pointer.inside ? dt / COMB_IN : -dt / COMB_OUT));
      amt = ramp * ramp * (3 - 2 * ramp);
    },
    pin(x, y, r) {
      px = x;
      py = y;
      pr = r;
    },
    near(x, y, pad = 0) {
      const dx = x - cx;
      const dy = y - cy;
      const r = radius + pad;
      return amt > 0.0005 && dx * dx + dy * dy < r * r;
    },
    nearBox(x0, y0, x1, y1) {
      return (
        amt > 0.0005 &&
        cx > Math.min(x0, x1) - radius &&
        cx < Math.max(x0, x1) + radius &&
        cy > Math.min(y0, y1) - radius &&
        cy < Math.max(y0, y1) + radius
      );
    },
    weight(x, y) {
      if (amt <= 0.0005) return 0;
      const dx = x - cx;
      const dy = y - cy;
      const d2 = dx * dx + dy * dy;
      if (d2 >= r2) return 0;
      const u = 1 - d2 / r2;
      return amt * u * u * pinned(x, y);
    },
    apply(x, y) {
      out[0] = x;
      out[1] = y;
      if (amt <= 0.0005) return out;
      const dx = x - cx;
      const dy = y - cy;
      const d2 = dx * dx + dy * dy;
      if (d2 >= r2) return out;
      const u = 1 - d2 / r2;
      const f = ((amt * strength * u * u) / Math.sqrt(d2 + c2)) * pinned(x, y);
      out[0] = x + dx * f;
      out[1] = y + dy * f;
      return out;
    },
  };

  // 1 away from the pin, easing to 0 at it.
  function pinned(x: number, y: number): number {
    if (pr <= 0) return 1;
    const ex = x - px;
    const ey = y - py;
    const e2 = ex * ex + ey * ey;
    if (e2 >= pr * pr) return 1;
    const v = Math.sqrt(e2) / pr;
    return v * v * (3 - 2 * v);
  }
}

/** Adds a straight line to the current path, bent by the comb where it passes near the cursor. */
export function combLine(ctx: CanvasRenderingContext2D, comb: Comb, x0: number, y0: number, x1: number, y1: number) {
  let p = comb.apply(x0, y0);
  ctx.moveTo(p[0], p[1]);
  if (comb.nearBox(x0, y0, x1, y1)) {
    // About one sample every 6 px; samples outside the reach come back unmoved.
    const n = Math.max(2, Math.ceil(Math.hypot(x1 - x0, y1 - y0) / 6));
    for (let i = 1; i < n; i++) {
      p = comb.apply(lerp(x0, x1, i / n), lerp(y0, y1, i / n));
      ctx.lineTo(p[0], p[1]);
    }
  }
  p = comb.apply(x1, y1);
  ctx.lineTo(p[0], p[1]);
}

/**
 * Adds a quadratic Bezier from k = 0 to `end` as a polyline of `steps` samples, the same path the
 * scenes drew before, with each step split `fine` times where the comb bends it.
 */
export function combQuad(
  ctx: CanvasRenderingContext2D,
  comb: Comb,
  x0: number,
  y0: number,
  x1: number,
  y1: number,
  x2: number,
  y2: number,
  steps: number,
  end = 1,
  fine = 4,
) {
  let p = comb.apply(x0, y0);
  ctx.moveTo(p[0], p[1]);
  const hot = comb.nearBox(Math.min(x0, x1, x2), Math.min(y0, y1, y2), Math.max(x0, x1, x2), Math.max(y0, y1, y2));
  let ax = x0;
  let ay = y0;
  for (let i = 1; i <= steps; i++) {
    const k = (i / steps) * end;
    const bx = quad(x0, x1, x2, k);
    const by = quad(y0, y1, y2, k);
    if (hot) {
      const pad = Math.abs(bx - ax) + Math.abs(by - ay);
      if (comb.near(ax, ay, pad) || comb.near(bx, by, pad)) {
        const k0 = ((i - 1) / steps) * end;
        for (let j = 1; j < fine; j++) {
          const kk = k0 + ((k - k0) * j) / fine;
          p = comb.apply(quad(x0, x1, x2, kk), quad(y0, y1, y2, kk));
          ctx.lineTo(p[0], p[1]);
        }
      }
    }
    p = comb.apply(bx, by);
    ctx.lineTo(p[0], p[1]);
    ax = bx;
    ay = by;
  }
}
