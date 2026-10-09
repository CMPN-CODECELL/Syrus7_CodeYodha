"use client";

import { useSyncExternalStore } from "react";
import { passkeysSupported } from "@/lib/webauthn";

const noop = () => () => {};

/** Passkey support, assumed true during SSR and hydration so markup matches, then checked for real. */
export function usePasskeysSupported(): boolean {
  return useSyncExternalStore(noop, passkeysSupported, () => true);
}
