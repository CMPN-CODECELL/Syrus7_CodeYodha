// Shared helpers for the streaming proxies. Rewrites can buffer text/event-stream responses,
// so the two streaming endpoints are piped through route handlers instead.

export const BACKEND_URL = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

const FORWARDED_REQUEST_HEADERS = [
  "cookie",
  "x-csrf-token",
  "user-agent",
  "origin",
  "referer",
  "accept-language",
  "last-event-id",
];

export function forwardHeaders(req: Request, extra: Record<string, string> = {}): Headers {
  const h = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const v = req.headers.get(name);
    if (v) h.set(name, v);
  }
  for (const [k, v] of Object.entries(extra)) h.set(k, v);
  return h;
}

export const SSE_HEADERS: HeadersInit = {
  "content-type": "text/event-stream; charset=utf-8",
  "cache-control": "no-cache, no-transform",
  "x-accel-buffering": "no",
  connection: "keep-alive",
};

export function upstreamUnavailable(path: string, err: unknown): Response {
  const reason = err instanceof Error ? err.message : String(err);
  return Response.json(
    {
      code: "BACKEND_UNREACHABLE",
      message: `The Countersign API at ${BACKEND_URL}${path} is not reachable (${reason}). Start the backend on port 8000, or run the UI with NEXT_PUBLIC_MOCK=1.`,
    },
    { status: 502 },
  );
}

/** Pass a non-stream upstream response through unchanged (errors keep their JSON body). */
export async function passThrough(upstream: Response): Promise<Response> {
  const body = await upstream.text();
  return new Response(body, {
    status: upstream.status,
    headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" },
  });
}
