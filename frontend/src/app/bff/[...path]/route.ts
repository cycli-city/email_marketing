import { authEnv } from "@/lib/auth/server-env";
import { getAccessToken } from "@/lib/auth/session";
import { handleBffRequest } from "@/lib/bff/handler";

// Never cached, never prerendered: every call depends on the caller's session.
export const dynamic = "force-dynamic";

type Context = { params: Promise<{ path: string[] }> };

async function handle(request: Request, { params }: Context): Promise<Response> {
  let env;
  try {
    env = authEnv();
  } catch {
    // Fail closed. The problem (a missing/insecure setting) is logged by the proxy; nothing sensitive is echoed.
    return Response.json(
      {
        error: {
          code: "not_configured",
          message: "The dashboard is not configured",
          details: {},
          request_id: null,
        },
      },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
  const { path } = await params;
  return handleBffRequest(request, path, {
    appOrigin: env.appOrigin,
    backendUrl: env.backendUrl,
    getAccessToken,
  });
}

export { handle as DELETE, handle as GET, handle as HEAD, handle as PATCH, handle as POST, handle as PUT };
