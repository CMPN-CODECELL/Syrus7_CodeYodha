"use client";

import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";
import { useIsDark, useReducedMotion } from "./hooks";

export interface SceneSize {
  w: number;
  h: number;
}

export interface ScenePointer {
  /** Pointer position in canvas CSS pixels; meaningless while `inside` is false. */
  x: number;
  y: number;
  inside: boolean;
}

export interface Scene {
  resize(size: SceneSize): void;
  /** Draws one full frame. `t` is seconds of animated time, `dt` the step since the last frame. */
  frame(t: number, dt: number, pointer: ScenePointer): void;
}

export type SceneFactory = (ctx: CanvasRenderingContext2D, env: { dark: boolean }) => Scene;

/**
 * A decorative canvas that runs a scene. DPR-aware (capped at 2x), resized by ResizeObserver,
 * paused while off-screen or while the tab is hidden, and frozen on a calm static frame under
 * prefers-reduced-motion. `pointerTarget` lets a scene react to the pointer over a parent element
 * while the canvas itself stays pointer-events: none.
 */
export function SceneCanvas({
  factory,
  className,
  staticTime = 4,
  pointerTarget = false,
}: {
  factory: SceneFactory;
  className?: string;
  /** Animated time used for the reduced-motion still frame. */
  staticTime?: number;
  /** When true, pointer moves over the nearest [data-scene-host] ancestor (or the parent) are fed to the scene. */
  pointerTarget?: boolean;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  const dark = useIsDark();
  const reduced = useReducedMotion();

  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const scene = factory(ctx, { dark });
    const pointer: ScenePointer = { x: 0, y: 0, inside: false };
    let size: SceneSize = { w: 0, h: 0 };
    let raf = 0;
    let last = 0;
    let t = reduced ? staticTime : 0;
    let onScreen = false;

    const paint = (dt: number) => {
      if (size.w > 0 && size.h > 0) scene.frame(t, dt, pointer);
    };
    const loop = (now: number) => {
      const dt = last ? Math.min(0.05, (now - last) / 1000) : 1 / 60;
      last = now;
      t += dt;
      paint(dt);
      raf = requestAnimationFrame(loop);
    };
    const stop = () => {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
    };
    const sync = () => {
      // A hidden tab may come back with the mouse elsewhere: let the scene spring back until it moves.
      if (document.hidden) pointer.inside = false;
      const run = onScreen && !document.hidden && !reduced;
      if (run && !raf) {
        last = 0;
        raf = requestAnimationFrame(loop);
      } else if (!run) {
        stop();
      }
    };

    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (!r) return;
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      canvas.width = Math.max(1, Math.round(r.width * dpr));
      canvas.height = Math.max(1, Math.round(r.height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      size = { w: r.width, h: r.height };
      scene.resize(size);
      paint(0);
    });
    ro.observe(canvas);

    const io = new IntersectionObserver(
      ([entry]) => {
        onScreen = !!entry?.isIntersecting;
        sync();
      },
      { rootMargin: "80px" },
    );
    io.observe(canvas);
    document.addEventListener("visibilitychange", sync);

    const host = pointerTarget ? (canvas.closest<HTMLElement>("[data-scene-host]") ?? canvas.parentElement) : null;
    // Last known mouse position anywhere on the page (false while the pointer is touch or out of the window).
    let clientX = 0;
    let clientY = 0;
    let mouse = false;
    const place = () => {
      const rect = canvas.getBoundingClientRect();
      pointer.x = clientX - rect.left;
      pointer.y = clientY - rect.top;
    };
    const track = (e: PointerEvent) => {
      clientX = e.clientX;
      clientY = e.clientY;
      mouse = e.pointerType === "mouse";
    };
    const onOut = (e: PointerEvent) => {
      if (!e.relatedTarget) mouse = false;
    };
    const onMove = (e: PointerEvent) => {
      track(e);
      place();
      pointer.inside = mouse;
    };
    const onLeave = () => {
      pointer.inside = false;
    };
    // Scrolling under a resting mouse fires no pointermove: check what the mouse rests on now, so the
    // scene lets go when the host scrolls away and picks the pointer up again when it scrolls back.
    const onScroll = () => {
      if (!host || !mouse) return;
      const el = document.elementFromPoint(clientX, clientY);
      pointer.inside = !!el && host.contains(el);
      if (pointer.inside) place();
    };
    host?.addEventListener("pointermove", onMove, { passive: true });
    host?.addEventListener("pointerleave", onLeave);
    if (host) {
      window.addEventListener("pointermove", track, { passive: true });
      window.addEventListener("pointerout", onOut, { passive: true });
      window.addEventListener("scroll", onScroll, { passive: true });
    }

    return () => {
      stop();
      ro.disconnect();
      io.disconnect();
      document.removeEventListener("visibilitychange", sync);
      host?.removeEventListener("pointermove", onMove);
      host?.removeEventListener("pointerleave", onLeave);
      window.removeEventListener("pointermove", track);
      window.removeEventListener("pointerout", onOut);
      window.removeEventListener("scroll", onScroll);
    };
  }, [factory, dark, reduced, staticTime, pointerTarget]);

  return <canvas ref={ref} aria-hidden className={cn("pointer-events-none block size-full", className)} />;
}
