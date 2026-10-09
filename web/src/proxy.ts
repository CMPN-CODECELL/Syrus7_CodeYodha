import { NextResponse, type NextRequest } from "next/server";

// Optimistic guard for the signed-in app: no session cookie means straight to sign-in.
// The API still authorizes every request; an expired cookie is caught by the app shell.
// Mock mode keeps its session in browser storage, so the shell does the redirect there.
const MOCK = process.env.NEXT_PUBLIC_MOCK === "1";

export function proxy(request: NextRequest) {
  if (MOCK || request.cookies.has("cs_session")) return NextResponse.next();
  const { pathname, search } = request.nextUrl;
  const url = request.nextUrl.clone();
  url.pathname = "/signin";
  url.search = `?next=${encodeURIComponent(`${pathname}${search}`)}`;
  return NextResponse.redirect(url);
}

export const config = {
  matcher: [
    "/copilot/:path*",
    "/portfolio/:path*",
    "/market/:path*",
    "/outcomes/:path*",
    "/mandates/:path*",
    "/plans/:path*",
    "/attack-lab/:path*",
    "/security/:path*",
  ],
};
