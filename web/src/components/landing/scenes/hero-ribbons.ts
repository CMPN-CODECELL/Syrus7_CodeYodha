import { rand, rgba, type SceneFactory } from "./util";

// Order-flow ribbons: many thin lines rise from the bottom of the hero, twist like a ribbon and
// pinch into a single point (the countersignature) before leaving towards the top-right edge as
// one narrow bundle (the executed order). Comets ride the lines; the pointer gently attracts them.

type V = [number, number];

const LINES = 64;
const SAMPLES = 46;
const COMETS = 16;

export const heroRibbons: SceneFactory = (ctx, { dark }) => {
  const lines = Array.from({ length: LINES }, (_, i) => ({
    u: i / (LINES - 1) - 0.5,
    phase: rand(i + 1) * Math.PI * 2,
    speed: 0.25 + rand(i + 50) * 0.35,
    alpha: 0.35 + rand(i + 90) * 0.65,
  }));
  const comets = Array.from({ length: COMETS }, (_, i) => ({
    line: Math.floor(rand(i * 3 + 7) * LINES),
    q: rand(i * 5 + 3) * 2,
    v: 0.09 + rand(i * 11 + 1) * 0.1,
  }));

  const C = dark
    ? { core: [150, 245, 214] as const, mid: [52, 205, 178] as const, far: [38, 120, 168] as const, comet: [236, 255, 250] as const, mode: "lighter" as const, gain: 1 }
    : { core: [6, 128, 108] as const, mid: [13, 148, 136] as const, far: [40, 110, 160] as const, comet: [4, 90, 80] as const, mode: "source-over" as const, gain: 1.25 };

  let w = 0;
  let h = 0;
  let narrow = false;
  // Spine geometry, recomputed on resize.
  let A: V = [0, 0];
  let A1: V = [0, 0];
  let A2: V = [0, 0];
  let P: V = [0, 0];
  let B1: V = [0, 0];
  let B2: V = [0, 0];
  let E: V = [0, 0];
  let W0 = 0;
  let W1 = 0;
  let grad: CanvasGradient | null = null;
  // Smoothed pointer.
  let mx = 0;
  let my = 0;
  let pull = 0;
  let flash = 0;
  let t = 0;

  function layout() {
    narrow = w < 768;
    P = narrow ? [w * 0.86, h * 0.9] : [w * 0.735, h * 0.44];
    A = narrow ? [w * 0.35, h + 40] : [w * 0.56, h + 40];
    E = [w + 90, narrow ? h * 0.62 : -h * 0.1];
    W0 = narrow ? w * 1.1 : w * 0.66;
    W1 = narrow ? w * 0.18 : w * 0.16;
    const dx = 1;
    const dy = narrow ? -0.9 : -0.62;
    const n = Math.hypot(dx, dy);
    const L = narrow ? w * 0.3 : w * 0.3;
    A1 = narrow ? [A[0], A[1] - h * 0.12] : [A[0] - w * 0.02, A[1] - h * 0.5];
    A2 = [P[0] - (dx / n) * L, P[1] - (dy / n) * L];
    B1 = [P[0] + (dx / n) * w * 0.12, P[1] + (dy / n) * w * 0.12];
    B2 = [E[0] - w * 0.12, E[1] + h * 0.06];
    const g = ctx.createRadialGradient(P[0], P[1], 0, P[0], P[1], Math.max(w, h) * 0.85);
    g.addColorStop(0, rgba(C.core, 1));
    g.addColorStop(0.18, rgba(C.mid, 0.95));
    g.addColorStop(0.55, rgba(C.mid, 0.55));
    g.addColorStop(1, rgba(C.far, 0.3));
    grad = g;
  }

  // Point on line `l` at path position q in [0, 2): [0, 1) rises into the signature, [1, 2) leaves it.
  const out: V = [0, 0];
  function point(l: (typeof lines)[number], q: number): V {
    const second = q >= 1;
    const s = second ? q - 1 : q;
    const p0 = second ? P : A;
    const p1 = second ? B1 : A1;
    const p2 = second ? B2 : A2;
    const p3 = second ? E : P;
    const m = 1 - s;
    const x = m * m * m * p0[0] + 3 * m * m * s * p1[0] + 3 * m * s * s * p2[0] + s * s * s * p3[0];
    const y = m * m * m * p0[1] + 3 * m * m * s * p1[1] + 3 * m * s * s * p2[1] + s * s * s * p3[1];
    let tx = 3 * m * m * (p1[0] - p0[0]) + 6 * m * s * (p2[0] - p1[0]) + 3 * s * s * (p3[0] - p2[0]);
    let ty = 3 * m * m * (p1[1] - p0[1]) + 6 * m * s * (p2[1] - p1[1]) + 3 * s * s * (p3[1] - p2[1]);
    const tl = Math.hypot(tx, ty) || 1;
    tx /= tl;
    ty /= tl;
    // Ribbon width shrinks to nothing at the signature and opens a little on the way out.
    const width = second ? W1 * Math.pow(s, 1.15) : W0 * Math.pow(1 - s, 1.3);
    const theta = (second ? 2.7 + 1.5 * s : 2.7 * s) + t * 0.22 + l.u * 1.1;
    const wobble = Math.sin(t * l.speed + l.phase + s * 5) * (second ? 3 * s : 7 * (1 - s));
    const off = l.u * width * Math.cos(theta) + wobble;
    let px = x - ty * off;
    let py = y + tx * off;
    if (pull > 0.001) {
      const ddx = mx - px;
      const ddy = my - py;
      const f = pull * 0.16 * Math.exp(-(ddx * ddx + ddy * ddy) / (2 * 150 * 150));
      px += ddx * f;
      py += ddy * f;
    }
    out[0] = px;
    out[1] = py;
    return out;
  }

  return {
    resize(size) {
      w = size.w;
      h = size.h;
      layout();
    },
    frame(time, dt, pointer) {
      t = time;
      const k = Math.min(1, dt * 4);
      if (pointer.inside) {
        if (pull < 0.01) {
          mx = pointer.x;
          my = pointer.y;
        }
        mx += (pointer.x - mx) * k;
        my += (pointer.y - my) * k;
      }
      pull += ((pointer.inside ? 1 : 0) - pull) * Math.min(1, dt * 2.5);

      ctx.clearRect(0, 0, w, h);
      ctx.globalCompositeOperation = C.mode;
      ctx.lineCap = "round";
      ctx.lineWidth = narrow ? 0.8 : 1;
      if (grad) ctx.strokeStyle = grad;
      const fade = (narrow ? 0.5 : 0.6) * C.gain;

      for (const l of lines) {
        ctx.globalAlpha = l.alpha * fade;
        ctx.beginPath();
        for (let i = 0; i <= SAMPLES * 2; i++) {
          const [x, y] = point(l, i / SAMPLES);
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.stroke();
      }
      ctx.globalAlpha = 1;

      // Comets: short bright streaks travelling along the lines.
      for (const c of comets) {
        const before = c.q;
        c.q += c.v * dt * (c.q > 0.85 && c.q < 1.15 ? 0.7 : 1);
        if (before < 1 && c.q >= 1) flash = 1;
        if (c.q >= 2) {
          c.q = 0;
          c.line = Math.floor(Math.random() * LINES);
        }
        const l = lines[c.line];
        const [hx, hy] = point(l, c.q);
        const head: V = [hx, hy];
        const [tx, ty] = point(l, Math.max(0, c.q - 0.05));
        const g = ctx.createLinearGradient(tx, ty, head[0], head[1]);
        g.addColorStop(0, rgba(C.comet, 0));
        g.addColorStop(1, rgba(C.comet, dark ? 0.9 : 0.75));
        ctx.strokeStyle = g;
        ctx.lineWidth = 1.6;
        ctx.beginPath();
        ctx.moveTo(tx, ty);
        for (let i = 1; i <= 6; i++) {
          const [x, y] = point(l, Math.max(0, c.q - 0.05 + (0.05 * i) / 6));
          ctx.lineTo(x, y);
        }
        ctx.stroke();
      }

      // The signature point: steady glow, a crisp core and a ring each time an order passes.
      flash = Math.max(0, flash - dt * 0.9);
      const pulse = 0.5 + 0.5 * Math.sin(t * 1.6);
      const glowR = 34 + pulse * 6;
      const glow = ctx.createRadialGradient(P[0], P[1], 0, P[0], P[1], glowR);
      glow.addColorStop(0, rgba(C.core, dark ? 0.55 : 0.35));
      glow.addColorStop(1, rgba(C.core, 0));
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(P[0], P[1], glowR, 0, Math.PI * 2);
      ctx.fill();
      if (flash > 0) {
        const r = 6 + (1 - flash) * 46;
        ctx.strokeStyle = rgba(C.core, flash * 0.7);
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.arc(P[0], P[1], r, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.globalCompositeOperation = "source-over";
      ctx.fillStyle = rgba(C.core, 1);
      ctx.beginPath();
      ctx.arc(P[0], P[1], 2.6, 0, Math.PI * 2);
      ctx.fill();
    },
  };
};

/** Where the signature point sits, as fractions of the hero box (for the HTML label beside it). */
export const HERO_SIGNATURE_POINT = { x: 0.735, y: 0.44 };
