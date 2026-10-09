"use client";

import { useState } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { errorMessage, isApiError } from "@/lib/errors";
import { useChatStore } from "@/lib/store";

/**
 * POST /api/auth/logout, then a full navigation to the landing page so every cache, store
 * and the live event stream start clean for whoever signs in next.
 */
export function useSignOut() {
  const [pending, setPending] = useState(false);
  const signOut = async () => {
    if (pending) return;
    setPending(true);
    try {
      await api.logout();
    } catch (e) {
      // A 401 means the session is already gone, which is what signing out wants anyway.
      if (!(isApiError(e) && e.status === 401)) {
        toast.error("Sign out did not complete", { description: errorMessage(e) });
        setPending(false);
        return;
      }
    }
    useChatStore.getState().reset();
    window.location.replace("/");
  };
  return { signOut, pending };
}
