"use client";

import { QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "next-themes";
import { useState, type ReactNode } from "react";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { isApiError } from "@/lib/errors";

function makeClient() {
  const client: QueryClient = new QueryClient({
    queryCache: new QueryCache({
      // Any request that finds the session gone re-checks /api/me, which sends the shell to sign-in.
      onError: (err, query) => {
        if (isApiError(err) && err.status === 401 && query.queryKey[0] !== "me") {
          void client.invalidateQueries({ queryKey: ["me"] });
        }
      },
    }),
    defaultOptions: {
      queries: {
        staleTime: 5_000,
        refetchOnWindowFocus: false,
        retry: (count, err) => {
          if (isApiError(err) && err.status >= 400 && err.status < 500) return false;
          return count < 2;
        },
      },
      mutations: { retry: false },
    },
  });
  return client;
}

export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(makeClient);
  return (
    <QueryClientProvider client={client}>
      <ThemeProvider attribute="class" defaultTheme="dark" enableSystem disableTransitionOnChange>
        <TooltipProvider delay={200}>
          {children}
          <Toaster position="bottom-right" closeButton visibleToasts={4} />
        </TooltipProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
