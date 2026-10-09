import { forwardHeaders, passThrough, SSE_HEADERS, upstreamUnavailable, BACKEND_URL } from "../_proxy";

export const dynamic = "force-dynamic";

// POST /api-stream/chat -> backend POST /api/chat, streamed back without buffering.
export async function POST(req: Request) {
  const body = await req.text();
  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND_URL}/api/chat`, {
      method: "POST",
      headers: forwardHeaders(req, {
        "content-type": "application/json",
        accept: "text/event-stream",
      }),
      body,
      signal: req.signal,
      cache: "no-store",
    });
  } catch (err) {
    return upstreamUnavailable("/api/chat", err);
  }

  const type = upstream.headers.get("content-type") ?? "";
  if (!upstream.ok || !upstream.body || !type.includes("text/event-stream")) {
    return passThrough(upstream);
  }
  return new Response(upstream.body, { status: 200, headers: SSE_HEADERS });
}
