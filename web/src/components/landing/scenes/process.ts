import { clamp01, lerp, quad, rand, rgba, type SceneFactory } from "./util";

// One scene per "How it works" step. Each is a calm, looping line drawing in the step's color:
// 01 Ask (violet, the AI's color), 02 Verify (blue to mint), 03 Countersign (mint, the human's
// color), 04 Execute (violet field with a single mint path that is sent exactly once).

type RGB = readonly [number, number, number];

function palette(dark: boolean) {
  return dark
    ? {
        violet: [176, 160, 255] as RGB,
        violetDeep: [112, 96, 228] as RGB,
        blue: [96, 150, 240] as RGB,
        mint: [110, 236, 200] as RGB,
        white: [236, 250, 255] as RGB,
        dim: [120, 130, 170] as RGB,
        mode: "lighter" as GlobalCompositeOperation,
      }
    : {
        violet: [104, 76, 220] as RGB,
        violetDeep: [82, 60, 190] as RGB,
        blue: [36, 98, 200] as RGB,
        mint: [8, 140, 112] as RGB,
        white: [20, 60, 70] as RGB,
        dim: [120, 128, 150] as RGB,
        mode: "source-over" as GlobalCompositeOperation,
      };
}

function dot(ctx: CanvasRenderingContext2D, x: number, y: number, r: number) {
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.fill();
}

/** 01 Ask: a fan of possible readings radiating from your words, swaying as the AI drafts. */
export const askScene: SceneFactory = (ctx, { dark }) => {
  const C = palette(dark);
  const N = 120;
  const rays = Array.from({ length: N }, (_, i) => {
    const r = rand(i + 3);
    // Denser towards the vertical, like a real fan of interpretations.
    const a = Math.sign(r - 0.5) * Math.pow(Math.abs(r - 0.5) * 2, 1.25) * 1.42;
    return { a, len: 0.35 + rand(i + 40) * 0.62, ph: rand(i + 80) * Math.PI * 2, sp: 0.4 + rand(i + 120) * 0.5 };
  });
  let w = 0;
  let h = 0;
  return {
    resize(s) {
      w = s.w;
      h = s.h;
    },
    frame(t) {
      ctx.clearRect(0, 0, w, h);
      ctx.globalCompositeOperation = C.mode;
      const ox = w / 2;
      const oy = h + 6;
      const reach = Math.min(h * 0.98, w * 0.62);
      const pulse = ((t * 0.32) % 1.4) * reach;
      const g = ctx.createRadialGradient(ox, oy, 0, ox, oy, reach);
      g.addColorStop(0, rgba(C.white, 0.95));
      g.addColorStop(0.25, rgba(C.violet, 0.8));
      g.addColorStop(clamp01(pulse / reach - 0.06), rgba(C.violetDeep, 0.42));
      g.addColorStop(clamp01(pulse / reach), rgba(C.violet, 0.95));
      g.addColorStop(clamp01(pulse / reach + 0.06), rgba(C.violetDeep, 0.42));
      g.addColorStop(1, rgba(C.violetDeep, 0.3));
      ctx.strokeStyle = g;
      ctx.fillStyle = rgba(C.violet, dark ? 0.85 : 0.7);
      ctx.lineWidth = 1;
      for (const r of rays) {
        const a = r.a + Math.sin(t * r.sp * 0.5 + r.ph) * 0.025;
        const len = reach * r.len * (0.93 + 0.07 * Math.sin(t * r.sp + r.ph));
        const x = ox + Math.sin(a) * len;
        const y = oy - Math.cos(a) * len;
        ctx.globalAlpha = 0.55;
        ctx.beginPath();
        ctx.moveTo(ox, oy);
        ctx.lineTo(x, y);
        ctx.stroke();
        ctx.globalAlpha = 0.9;
        dot(ctx, x, y, 1.6);
      }
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = "source-over";
    },
  };
};

/** 02 Verify: a wave of stems; a scan sweeps across and every stem it has checked turns mint. */
export const verifyScene: SceneFactory = (ctx, { dark }) => {
  const C = palette(dark);
  let w = 0;
  let h = 0;
  return {
    resize(s) {
      w = s.w;
      h = s.h;
    },
    frame(t) {
      ctx.clearRect(0, 0, w, h);
      ctx.globalCompositeOperation = C.mode;
      const margin = w * 0.08;
      const step = w < 600 ? 7 : 9;
      const n = Math.floor((w - margin * 2) / step);
      const cycle = 7;
      const scan = margin + ((t % cycle) / (cycle - 1.2)) * (w - margin * 2);
      const base = h + 2;
      // Two shared vertical fades (checked and unchecked) instead of one gradient per stem.
      const fade = (col: RGB, a: number) => {
        const g = ctx.createLinearGradient(0, h * 0.1, 0, base);
        g.addColorStop(0, rgba(col, a));
        g.addColorStop(1, rgba(col, 0.03));
        return g;
      };
      const okStroke = fade(C.mint, 0.95);
      const pendingStroke = fade(C.blue, 0.55);
      ctx.lineWidth = 1;
      for (let i = 0; i <= n; i++) {
        const x = margin + i * step;
        const k = x / w;
        const amp =
          0.42 +
          0.2 * Math.sin(k * 7.5 + t * 0.55) +
          0.1 * Math.sin(k * 17 - t * 0.9) +
          0.06 * Math.sin(k * 31 + t * 1.4);
        const top = base - h * clamp01(amp) * 0.86;
        const checked = x <= scan;
        ctx.strokeStyle = checked ? okStroke : pendingStroke;
        ctx.beginPath();
        ctx.moveTo(x, base);
        ctx.lineTo(x, top);
        ctx.stroke();
        ctx.fillStyle = rgba(checked ? C.mint : C.violet, checked ? 1 : 0.7);
        dot(ctx, x, top, checked ? 1.8 : 1.4);
      }
      // The scan line itself.
      if (scan < w - margin) {
        const g = ctx.createLinearGradient(scan - 60, 0, scan, 0);
        g.addColorStop(0, rgba(C.mint, 0));
        g.addColorStop(1, rgba(C.mint, dark ? 0.16 : 0.12));
        ctx.fillStyle = g;
        ctx.fillRect(scan - 60, 0, 60, h);
        ctx.fillStyle = rgba(C.mint, 0.7);
        ctx.fillRect(scan, h * 0.04, 1, h * 0.96);
      }
      ctx.globalCompositeOperation = "source-over";
    },
  };
};

/** 03 Countersign: a burst of curves rising from one bright point, the signature. */
export const countersignScene: SceneFactory = (ctx, { dark }) => {
  const C = palette(dark);
  const N = 150;
  const rays = Array.from({ length: N }, (_, i) => {
    const r = rand(i + 7);
    const a = (r - 0.5) * 2.95;
    return { a, len: 0.45 + rand(i + 33) * 0.55, bend: 0.45 + rand(i + 66) * 0.25, ph: rand(i + 99) * 6.28 };
  });
  let w = 0;
  let h = 0;
  return {
    resize(s) {
      w = s.w;
      h = s.h;
    },
    frame(t) {
      ctx.clearRect(0, 0, w, h);
      ctx.globalCompositeOperation = C.mode;
      const ox = w / 2;
      const oy = h - 4;
      const reach = Math.min(h * 0.92, w * 0.5);
      const grow = 1 - Math.pow(1 - clamp01(t / 1.8), 3);
      const wave = (t * 0.45) % 1.3;
      ctx.lineWidth = 1;
      for (const r of rays) {
        const a = r.a + Math.sin(t * 0.4 + r.ph) * 0.012;
        const R = reach * r.len;
        const tx = ox + Math.sin(a) * R * 1.18;
        const ty = oy - Math.cos(a) * R;
        const cx = ox + Math.sin(a * r.bend) * R * 0.55;
        const cy = oy - Math.cos(a * r.bend) * R * 0.95;
        const end = grow * (0.96 + 0.04 * Math.sin(t + r.ph));
        const bright = Math.exp(-Math.pow((r.len - wave) * 5, 2));
        ctx.strokeStyle = rgba(C.mint, 0.28 + bright * 0.5);
        ctx.beginPath();
        ctx.moveTo(ox, oy);
        for (let i = 1; i <= 16; i++) {
          const k = (i / 16) * end;
          ctx.lineTo(quad(ox, cx, tx, k), quad(oy, cy, ty, k));
        }
        ctx.stroke();
        ctx.fillStyle = rgba(C.mint, 0.55 + bright * 0.45);
        dot(ctx, quad(ox, cx, tx, end), quad(oy, cy, ty, end), 1.5);
      }
      const glow = ctx.createRadialGradient(ox, oy, 0, ox, oy, 70);
      glow.addColorStop(0, rgba(C.white, dark ? 0.9 : 0.5));
      glow.addColorStop(0.3, rgba(C.mint, dark ? 0.45 : 0.25));
      glow.addColorStop(1, rgba(C.mint, 0));
      ctx.fillStyle = glow;
      dot(ctx, ox, oy, 70);
      ctx.globalCompositeOperation = "source-over";
    },
  };
};

/** 04 Execute: arcs from the executor to the market; exactly one path is lit and travelled once. */
export const executeScene: SceneFactory = (ctx, { dark }) => {
  const C = palette(dark);
  const N = 60;
  const arcs = Array.from({ length: N }, (_, i) => ({ k: i / (N - 1), lift: rand(i + 5) * 0.18, ph: rand(i + 17) * 6.28 }));
  const motes = Array.from({ length: 40 }, (_, i) => ({ arc: Math.floor(rand(i * 9 + 2) * N), q: rand(i * 4 + 1), v: 0.08 + rand(i * 6 + 3) * 0.1 }));
  let w = 0;
  let h = 0;
  const geom = (i: number, t: number) => {
    const a = arcs[i];
    const ox = w * 0.36;
    const oy = h * 0.2;
    const ang = lerp(Math.PI * 0.98, Math.PI * 0.02, a.k);
    const R = Math.min(w * 0.44, h);
    const cxC = w * 0.56;
    const cyC = h * 1.18;
    const tx = cxC + Math.cos(ang) * R;
    const ty = cyC - Math.sin(ang) * R * 1.02;
    const mx = lerp(ox, tx, 0.55) + 18 * Math.sin(t * 0.3 + a.ph);
    const my = Math.min(oy, ty) - h * (0.08 + a.lift);
    return { ox, oy, mx, my, tx, ty };
  };
  return {
    resize(s) {
      w = s.w;
      h = s.h;
    },
    frame(t, dt) {
      ctx.clearRect(0, 0, w, h);
      ctx.globalCompositeOperation = C.mode;
      const cycle = 5.5;
      const chosen = Math.floor(t / cycle) % 2 === 0 ? Math.floor(N * 0.68) : Math.floor(N * 0.42);
      const progress = clamp01(((t % cycle) - 0.4) / 2.6);
      ctx.lineWidth = 1;
      for (let i = 0; i < N; i++) {
        const g = geom(i, t);
        ctx.strokeStyle = rgba(i % 3 === 0 ? C.blue : C.violet, 0.22 + 0.18 * Math.sin(arcs[i].ph + t * 0.6) ** 2);
        ctx.beginPath();
        ctx.moveTo(g.ox, g.oy);
        ctx.quadraticCurveTo(g.mx, g.my, g.tx, g.ty);
        ctx.stroke();
        ctx.fillStyle = rgba(C.violet, 0.5);
        dot(ctx, g.tx, g.ty, 1.3);
      }
      ctx.fillStyle = rgba(C.white, dark ? 0.75 : 0.5);
      for (const m of motes) {
        m.q += m.v * dt;
        if (m.q > 1) m.q -= 1;
        const g = geom(m.arc, t);
        dot(ctx, quad(g.ox, g.mx, g.tx, m.q), quad(g.oy, g.my, g.ty, m.q), 1.1);
      }
      // The one countersigned order: drawn as it travels, then marked as filled.
      const g = geom(chosen, t);
      ctx.strokeStyle = rgba(C.mint, 0.95);
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(g.ox, g.oy);
      for (let i = 1; i <= 30; i++) {
        const k = (i / 30) * progress;
        ctx.lineTo(quad(g.ox, g.mx, g.tx, k), quad(g.oy, g.my, g.ty, k));
      }
      ctx.stroke();
      const hx = quad(g.ox, g.mx, g.tx, progress);
      const hy = quad(g.oy, g.my, g.ty, progress);
      ctx.fillStyle = rgba(C.mint, 1);
      dot(ctx, hx, hy, 3);
      if (progress >= 1) {
        const since = (t % cycle) - 3;
        const r = 4 + clamp01(since / 1.5) * 22;
        ctx.strokeStyle = rgba(C.mint, 1 - clamp01(since / 1.5));
        ctx.lineWidth = 1.4;
        ctx.beginPath();
        ctx.arc(g.tx, g.ty, r, 0, Math.PI * 2);
        ctx.stroke();
      }
      const glow = ctx.createRadialGradient(g.ox, g.oy, 0, g.ox, g.oy, 28);
      glow.addColorStop(0, rgba(C.white, dark ? 0.8 : 0.45));
      glow.addColorStop(1, rgba(C.violet, 0));
      ctx.fillStyle = glow;
      dot(ctx, g.ox, g.oy, 28);
      ctx.globalCompositeOperation = "source-over";
    },
  };
};
