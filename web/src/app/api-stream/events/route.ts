import { forwardHeaders, passThrough, SSE_HEADERS, upstreamUnavailable, BACKEND_URL } from "../_proxy";

export const dynamic = "force-dynamic";

// GET /api-stream/events -> backend GET /api/events (live ticks, outcomes, monitor, security).
export async function GET(req: Request) {
  let upstream: Response;
  try {
    upstream = await fetch(`${BACKEND_URL}/api/events`, {
      method: "GET",
      headers: forwardHeaders(req, { accept: "text/event-stream" }),
      signal: req.signal,
      cache: "no-store",
    });
  } catch (err) {
    return upstreamUnavailable("/api/events", err);
  }

  const type = upstream.headers.get("content-type") ?? "";
  if (!upstream.ok || !upstream.body || !type.includes("text/event-stream")) {
    return passThrough(upstream);
  }
  return new Response(upstream.body, { status: 200, headers: SSE_HEADERS });
}
