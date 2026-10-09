"use client";

import { SceneCanvas } from "./scene-canvas";
import { globeScene } from "./scenes/globe";
import { heroRibbons } from "./scenes/hero-ribbons";

// Client wrappers so server sections can place canvases without passing functions across the boundary.

export function HeroCanvas({ className }: { className?: string }) {
  return <SceneCanvas factory={heroRibbons} className={className} staticTime={6} pointerTarget />;
}

export function GlobeCanvas({ className }: { className?: string }) {
  return <SceneCanvas factory={globeScene} className={className} staticTime={2} />;
}
