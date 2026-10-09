import { rgba, type SceneFactory } from "./util";

// A slowly turning dotted globe with a glowing rim and one orbiting signature. Land is not a real
// map: it is smooth 3D value noise sampled on a Fibonacci sphere, thresholded to about 38% cover.

const N = 2400;
const GOLDEN = Math.PI * (3 - Math.sqrt(5));

function hash3(x: number, y: number, z: number): number {
  const s = Math.sin(x * 127.1 + y * 311.7 + z * 74.7) * 43758.5453;
  return s - Math.floor(s);
}

function noise3(x: number, y: number, z: number): number {
  const xi = Math.floor(x);
  const yi = Math.floor(y);
  const zi = Math.floor(z);
  const fx = x - xi;
  const fy = y - yi;
  const fz = z - zi;
  const sx = fx * fx * (3 - 2 * fx);
  const sy = fy * fy * (3 - 2 * fy);
  const sz = fz * fz * (3 - 2 * fz);
  const l = (a: number, b: number, k: number) => a + (b - a) * k;
  const c = (dx: number, dy: number, dz: number) => hash3(xi + dx, yi + dy, zi + dz);
  return l(
    l(l(c(0, 0, 0), c(1, 0, 0), sx), l(c(0, 1, 0), c(1, 1, 0), sx), sy),
    l(l(c(0, 0, 1), c(1, 0, 1), sx), l(c(0, 1, 1), c(1, 1, 1), sx), sy),
    sz,
  );
}

export const globeScene: SceneFactory = (ctx, { dark }) => {
  const pts = new Float32Array(N * 4);
  const field = new Float32Array(N);
  for (let i = 0; i < N; i++) {
    const y = 1 - (i / (N - 1)) * 2;
    const r = Math.sqrt(1 - y * y);
    const phi = i * GOLDEN;
    const x = Math.cos(phi) * r;
    const z = Math.sin(phi) * r;
    pts[i * 4] = x;
    pts[i * 4 + 1] = y;
    pts[i * 4 + 2] = z;
    field[i] = 0.65 * noise3(x * 1.7 + 4, y * 1.7 + 1, z * 1.7 + 9) + 0.35 * noise3(x * 3.6, y * 3.6 + 5, z * 3.6) - Math.abs(y) * 0.12;
  }
  const threshold = Float32Array.from(field).sort()[Math.floor(N * 0.62)];
  for (let i = 0; i < N; i++) pts[i * 4 + 3] = field[i] > threshold ? 1 : 0;

  const C = dark
    ? { land: [120, 240, 205] as const, sea: [90, 150, 170] as const, rim: [70, 220, 180] as const, body: [12, 26, 28] as const, orbit: [150, 245, 214] as const }
    : { land: [8, 128, 104] as const, sea: [100, 130, 150] as const, rim: [20, 170, 140] as const, body: [236, 246, 244] as const, orbit: [8, 120, 98] as const };

  let w = 0;
  let h = 0;
  return {
    resize(s) {
      w = s.w;
      h = s.h;
    },
    frame(t) {
      ctx.clearRect(0, 0, w, h);
      const cx = w / 2;
      const cy = h / 2;
      const R = Math.min(w, h) * 0.36;
      const rot = t * 0.11;
      const tilt = 0.36;
      const cr = Math.cos(rot);
      const sr = Math.sin(rot);
      const ct = Math.cos(tilt);
      const st = Math.sin(tilt);

      // Outer glow and the sphere body.
      const halo = ctx.createRadialGradient(cx, cy, R * 0.82, cx, cy, R * 1.45);
      halo.addColorStop(0, rgba(C.rim, dark ? 0.32 : 0.22));
      halo.addColorStop(0.35, rgba(C.rim, dark ? 0.1 : 0.07));
      halo.addColorStop(1, rgba(C.rim, 0));
      ctx.fillStyle = halo;
      ctx.beginPath();
      ctx.arc(cx, cy, R * 1.45, 0, Math.PI * 2);
      ctx.fill();
      const body = ctx.createRadialGradient(cx - R * 0.3, cy - R * 0.35, R * 0.1, cx, cy, R);
      body.addColorStop(0, rgba(C.body, dark ? 0.9 : 0.95));
      body.addColorStop(1, rgba(C.body, 1));
      ctx.fillStyle = body;
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.fill();

      // Dots, front hemisphere only; brightness follows depth so the limb falls off softly.
      for (let pass = 0; pass < 2; pass++) {
        const land = pass === 1;
        ctx.fillStyle = rgba(land ? C.land : C.sea, 1);
        for (let i = 0; i < N; i++) {
          if ((pts[i * 4 + 3] === 1) !== land) continue;
          const x = pts[i * 4];
          const y = pts[i * 4 + 1];
          const z = pts[i * 4 + 2];
          const x1 = x * cr - z * sr;
          const z1 = x * sr + z * cr;
          const y2 = y * ct - z1 * st;
          const z2 = y * st + z1 * ct;
          if (z2 < 0.02) continue;
          const size = land ? 1.25 + z2 * 1.1 : 0.9 + z2 * 0.5;
          ctx.globalAlpha = land ? 0.3 + z2 * 0.7 : 0.1 + z2 * 0.22;
          ctx.fillRect(cx + x1 * R - size / 2, cy - y2 * R - size / 2, size, size);
        }
      }
      ctx.globalAlpha = 1;

      // Crisp rim.
      ctx.strokeStyle = rgba(C.rim, dark ? 0.55 : 0.45);
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.stroke();

      // Orbit with a travelling signature; hidden while it passes behind the globe.
      const tiltO = -0.32;
      const rx = R * 1.3;
      const ry = R * 0.34;
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(tiltO);
      ctx.strokeStyle = rgba(C.orbit, dark ? 0.22 : 0.3);
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 5]);
      ctx.beginPath();
      ctx.ellipse(0, 0, rx, ry, 0, 0, Math.PI * 2);
      ctx.stroke();
      ctx.setLineDash([]);
      const a = t * 0.5;
      const ox = Math.cos(a) * rx;
      const oy = Math.sin(a) * ry;
      const behind = Math.sin(a) < 0 && Math.hypot(ox, oy) < R;
      if (!behind) {
        const g = ctx.createRadialGradient(ox, oy, 0, ox, oy, 14);
        g.addColorStop(0, rgba(C.orbit, 0.9));
        g.addColorStop(1, rgba(C.orbit, 0));
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.arc(ox, oy, 14, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = rgba(C.orbit, 1);
        ctx.beginPath();
        ctx.arc(ox, oy, 2.4, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
    },
  };
};
