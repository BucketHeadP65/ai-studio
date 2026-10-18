"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";
import { Toaster } from "sonner";

import { DraftProvider } from "@/api/draft-store";
import { useLiveInvalidation } from "@/app/live-invalidation";
import { FollowMeProvider } from "@/components/follow-me/provider";

// The dashboard's single TanStack Query provider, mounted once in the root
// layout so every station can `useQuery`/`useMutation` at runtime. `useState`
// holds the client across re-renders (a fresh `new QueryClient()` in the render
// body would discard the cache each render).
//
// Defaults are tuned for a pushed dashboard, not a polling one:
// `refetchOnWindowFocus: false` keeps a focus change from firing surprise
// network requests (and from racing component tests), `retry: 1` keeps a single
// transient failure from stalling on the default exponential backoff, and
// `refetchInterval: 60_000` is the safety net under the push channel, not the
// way anything is meant to stay current. `refetchOnReconnect` covers the case
// the streams themselves cannot: the machine was offline.
function LiveInvalidation({ children }: { children: ReactNode }) {
  useLiveInvalidation();
  return <>{children}</>;
}

export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            retry: 1,
            refetchOnWindowFocus: false,
            refetchInterval: 60_000,
            refetchOnReconnect: true,
          },
        },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <LiveInvalidation>
        <DraftProvider>
          <FollowMeProvider>{children}</FollowMeProvider>
        </DraftProvider>
      </LiveInvalidation>
      {/* The one toast outlet: any station calls sonner's toast() and lands here.
          Unstyled, painted with the app's own tokens so every theme carries it; the
          kind (error, success, warning) shows in the wash and ink, never in hue alone. */}
      <Toaster
        position="bottom-right"
        closeButton
        toastOptions={{
          unstyled: true,
          classNames: {
            toast:
              "flex w-80 items-center gap-2.5 rounded-lg border border-line bg-surface px-4 py-3 text-body text-label-1 shadow-lg",
            title: "font-medium",
            description: "text-caption text-label-3",
            closeButton: "text-label-3 hover:text-label-1",
            error: "border-danger-ink/35 bg-danger-soft text-danger-ink",
            success: "bg-ok-soft text-ok-ink",
            warning: "border-warn/35 bg-warn-soft text-warn-ink",
          },
        }}
      />
    </QueryClientProvider>
  );
}
