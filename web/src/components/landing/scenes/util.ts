// Small math and color helpers shared by the canvas scenes. Kept out of the "use client"
// module so plain modules (and server components importing their constants) can use them.

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
