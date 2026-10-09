import { clamp01, createComb, rand, rgba, type Pair, type SceneFactory } from "./util";

// Order-flow ribbons: many thin lines rise from the bottom of the hero, twist like a ribbon and
// pinch into a single point (the countersignature) before leaving towards the top-right edge as
// one narrow bundle (the executed order). Comets ride the lines; the pointer combs them apart.

type V = [number, number];

const LINES = 64;
const SAMPLES = 46;
const COMETS = 16;
// Sub-steps per sample where the comb bends a line, and how far past its reach to look for them.
const FINE = 4;
const FINE_PAD = 40;
// Extra ribbon width (px, edge to edge) where the cursor is near the spine: the bundle fans open.
// It scales the ribbon's own cross section, so it widens in whatever order the twist shows and
// never pinches or crosses threads. Seen edge on the ribbon still spans about FAN_EDGE of its
// width, and FAN_FLOOR (px) caps the scale where the ribbon is thinner than that.
const FAN = 140;
const FAN_EDGE = 0.3;
const FAN_FLOOR = 40;
// The fan calms within this many px of the signature; on the way out, the fan and the push towards
// the label (below the bundle) calm within the second, so parted threads stay clear of the label.
const FAN_CALM = 180;
const LABEL_CALM = 300;
// Widest nav content box: on wider screens the head still leaves the hero under Get started.
const NAV_MAX = 1440;
// Spine samples per frame, FINE per drawn sample, plus one scratch slot for the comets.
const GRID = SAMPLES * 2 * FINE;
const SCRATCH = GRID + 1;

function smooth(v: number): number {
  const k = clamp01(v);
  return k * k * (3 - 2 * k);
}

export const heroRibbons: SceneFactory = (ctx, { dark }) => {
  const lines = Array.from({ length: LINES }, (_, i) => {
    const u = i / (LINES - 1) - 0.5;
    return {
      u,
      twist: u * 1.1,
      phase: rand(i + 1) * Math.PI * 2,
      speed: 0.25 + rand(i + 50) * 0.35,
      alpha: 0.35 + rand(i + 90) * 0.65,
    };
  });
  // Mean of u * sin(twist) over the threads: how far off the spine the bundle's centre sits when edge on.
  const centre = lines.reduce((a, l) => a + l.u * Math.sin(l.twist), 0) / LINES;
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
  // Threads part around the cursor; the signature point itself stays pinned under its label.
  const comb = createComb({ radius: 170, strength: 66, core: 16 });
  let flash = 0;
  let t = 0;
  // Spine samples shared by every line in a frame: position, normal, ribbon width, twist, wobble, fan.
  const sx = new Float64Array(GRID + 2);
  const sy = new Float64Array(GRID + 2);
  const nx = new Float64Array(GRID + 2);
  const ny = new Float64Array(GRID + 2);
  const sw = new Float64Array(GRID + 2);
  const st = new Float64Array(GRID + 2);
  const wa = new Float64Array(GRID + 2);
  const wp = new Float64Array(GRID + 2);
  const mid = new Float64Array(GRID + 2);
  const grow = new Float64Array(GRID + 2);
  const keep = new Float64Array(GRID + 2);

  function layout() {
    narrow = w < 768;
    // Right edge and width of the nav's content box (the full width up to NAV_MAX).
    const right = Math.min(w, (w + NAV_MAX) / 2);
    const span = Math.min(w, NAV_MAX);
    P = narrow ? [w * 0.86, h * 0.9] : [w * 0.735, h * 0.44];
    A = narrow ? [w * 0.35, h + 40] : [w * 0.56, h + 40];
    E = [right + 90, narrow ? h * 0.62 : -h * 0.1];
    W0 = narrow ? w * 1.1 : w * 0.66;
    W1 = narrow ? w * 0.18 : span * 0.16;
    const dx = 1;
    const dy = narrow ? -0.9 : -0.62;
    const n = Math.hypot(dx, dy);
    const L = narrow ? w * 0.3 : w * 0.3;
    A1 = narrow ? [A[0], A[1] - h * 0.12] : [A[0] - w * 0.02, A[1] - h * 0.5];
    A2 = [P[0] - (dx / n) * L, P[1] - (dy / n) * L];
    B1 = [P[0] + (dx / n) * span * 0.12, P[1] + (dy / n) * span * 0.12];
    B2 = [E[0] - span * 0.12, E[1] + h * 0.06];
    comb.pin(P[0], P[1], 120);
    const g = ctx.createRadialGradient(P[0], P[1], 0, P[0], P[1], Math.max(w, h) * 0.85);
    g.addColorStop(0, rgba(C.core, 1));
    g.addColorStop(0.18, rgba(C.mid, 0.95));
    g.addColorStop(0.55, rgba(C.mid, 0.55));
    g.addColorStop(1, rgba(C.far, 0.3));
    grad = g;
  }

  // Fills spine slot `j` for path position q in [0, 2): [0, 1) rises into the signature, [1, 2) leaves it.
  function spine(q: number, j: number) {
    const second = q >= 1;
    const s = second ? q - 1 : q;
    const p0 = second ? P : A;
    const p1 = second ? B1 : A1;
    const p2 = second ? B2 : A2;
    const p3 = second ? E : P;
    const m = 1 - s;
    const x = m * m * m * p0[0] + 3 * m * m * s * p1[0] + 3 * m * s * s * p2[0] + s * s * s * p3[0];
    const y = m * m * m * p0[1] + 3 * m * m * s * p1[1] + 3 * m * s * s * p2[1] + s * s * s * p3[1];
    const tx = 3 * m * m * (p1[0] - p0[0]) + 6 * m * s * (p2[0] - p1[0]) + 3 * s * s * (p3[0] - p2[0]);
    const ty = 3 * m * m * (p1[1] - p0[1]) + 6 * m * s * (p2[1] - p1[1]) + 3 * s * s * (p3[1] - p2[1]);
    const tl = Math.hypot(tx, ty) || 1;
    const theta = (second ? 2.7 + 1.5 * s : 2.7 * s) + t * 0.22;
    sx[j] = x;
    sy[j] = y;
    nx[j] = -ty / tl;
    ny[j] = tx / tl;
    // Ribbon width shrinks to nothing at the signature and opens a little on the way out.
    sw[j] = second ? W1 * Math.pow(s, 1.15) : W0 * Math.pow(1 - s, 1.3);
    st[j] = theta;
    mid[j] = -Math.sin(theta) * sw[j] * centre;
    wa[j] = second ? 3 * s : 7 * (1 - s);
    wp[j] = s * 5;
    // Near the cursor the threads also fan out, so the bundle spreads, not just bends.
    grow[j] = 0;
    keep[j] = 1;
    if (comb.active) {
      const d = Math.hypot(x - P[0], y - P[1]);
      // On the way out +normal points at the label: how much parting is let through that way.
      if (second && !narrow) keep[j] = smooth(d / LABEL_CALM);
      const k = comb.weight(x, y);
      if (k > 0) {
        const span = sw[j] * Math.max(FAN_EDGE, Math.abs(Math.cos(theta)));
        grow[j] = (k * smooth(d / FAN_CALM) * FAN) / Math.max(FAN_FLOOR, span);
      }
    }
  }

  // Point on line `l` at spine slot `j`, combed into `out`; `raw` keeps the position before the push.
  const raw: V = [0, 0];
  const out: Pair = [0, 0];
  function at(l: (typeof lines)[number], j: number): Pair {
    const twist = l.u * sw[j] * Math.cos(st[j] + l.twist);
    // The fan widens about the bundle's own centre line, so an edge-on ribbon opens both ways.
    let spread = (twist - mid[j]) * grow[j];
    if (spread > 0) spread *= keep[j];
    const off = twist + spread + Math.sin(t * l.speed + l.phase + wp[j]) * wa[j];
    raw[0] = sx[j] + nx[j] * off;
    raw[1] = sy[j] + ny[j] * off;
    const p = comb.apply(raw[0], raw[1]);
    out[0] = p[0];
    out[1] = p[1];
    const push = (p[0] - raw[0]) * nx[j] + (p[1] - raw[1]) * ny[j];
    if (push > 0 && keep[j] < 1) {
      out[0] -= nx[j] * push * (1 - keep[j]);
      out[1] -= ny[j] * push * (1 - keep[j]);
    }
    return out;
  }

  // Point on line `l` at any path position (the comets ride between the shared samples).
  function point(l: (typeof lines)[number], q: number): Pair {
    spine(q, SCRATCH);
    return at(l, SCRATCH);
  }

  return {
    resize(size) {
      w = size.w;
      h = size.h;
      layout();
    },
    frame(time, dt, pointer) {
      t = time;
      comb.update(pointer, dt);
      const hot = comb.active;
      // The sub-steps between drawn samples are only needed while the comb bends the lines.
      for (let j = 0; j <= GRID; j += hot ? 1 : FINE) spine(j / (SAMPLES * FINE), j);

      ctx.clearRect(0, 0, w, h);
      ctx.globalCompositeOperation = C.mode;
      ctx.lineCap = "round";
      ctx.lineWidth = narrow ? 0.8 : 1;
      if (grad) ctx.strokeStyle = grad;
      const fade = (narrow ? 0.5 : 0.6) * C.gain;

      for (const l of lines) {
        ctx.globalAlpha = l.alpha * fade;
        ctx.beginPath();
        let wasNear = false;
        for (let j = 0; j <= GRID; j += FINE) {
          const p = at(l, j);
          const x = p[0];
          const y = p[1];
          const isNear = hot && comb.near(raw[0], raw[1], FINE_PAD);
          if (j === 0) {
            ctx.moveTo(x, y);
          } else {
            // Finer steps where the comb bends the line, so the parting stays a smooth curve.
            if (wasNear || isNear) {
              for (let k = j - FINE + 1; k < j; k++) {
                const s = at(l, k);
                ctx.lineTo(s[0], s[1]);
              }
            }
            ctx.lineTo(x, y);
          }
          wasNear = isNear;
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
        const head = point(l, c.q);
        const hx = head[0];
        const hy = head[1];
        const tail = point(l, Math.max(0, c.q - 0.05));
        const tx = tail[0];
        const ty = tail[1];
        const g = ctx.createLinearGradient(tx, ty, hx, hy);
        g.addColorStop(0, rgba(C.comet, 0));
        g.addColorStop(1, rgba(C.comet, dark ? 0.9 : 0.75));
        ctx.strokeStyle = g;
        ctx.lineWidth = 1.6;
        ctx.beginPath();
        ctx.moveTo(tx, ty);
        for (let i = 1; i <= 6; i++) {
          const p = point(l, Math.max(0, c.q - 0.05 + (0.05 * i) / 6));
          ctx.lineTo(p[0], p[1]);
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
