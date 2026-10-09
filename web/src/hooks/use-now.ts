"use client";

import { useSyncExternalStore } from "react";

interface Clock {
  subscribe: (onChange: () => void) => () => void;
  getSnapshot: () => number;
}

// One shared interval per cadence, so dozens of countdowns cost one timer.
const clocks = new Map<number, Clock>();

function createClock(ms: number): Clock {
  const listeners = new Set<() => void>();
  let timer: ReturnType<typeof setInterval> | null = null;
  let now = Date.now();
  return {
    subscribe(onChange) {
      listeners.add(onChange);
      if (!timer) {
        now = Date.now();
        timer = setInterval(() => {
          now = Date.now();
          for (const l of listeners) l();
        }, ms);
      }
      return () => {
        listeners.delete(onChange);
        if (listeners.size === 0 && timer) {
          clearInterval(timer);
          timer = null;
        }
      };
    },
    getSnapshot: () => now,
  };
}

function clockFor(ms: number): Clock {
  let c = clocks.get(ms);
  if (!c) {
    c = createClock(ms);
    clocks.set(ms, c);
  }
  return c;
}

const serverSnapshot = () => 0;

/** Current time in ms, re-rendering every `intervalMs`. Returns 0 during SSR and hydration. */
export function useNow(intervalMs = 1000): number {
  const clock = clockFor(intervalMs);
  return useSyncExternalStore(clock.subscribe, clock.getSnapshot, serverSnapshot);
}
