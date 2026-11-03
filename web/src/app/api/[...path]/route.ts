import { NextRequest } from "next/server";

export const dynamic = "force-dynamic";
const headers = {
	"Cache-Control": "no-store",
	"X-Content-Type-Options": "nosniff",
};

export async function GET(
	request: NextRequest,
	context: { params: Promise<{ path: string[] }> },
) {
	return proxy(request, (await context.params).path);
}
export async function POST(
	request: NextRequest,
	context: { params: Promise<{ path: string[] }> },
) {
	// Le operazioni partono dalla sola interfaccia sullo stesso origin.
	const origin = request.headers.get("origin");
	const host = request.headers.get("host");
	let sameOrigin = false;
	try {
		sameOrigin =
			!!origin &&
			!!host &&
			new URL(origin).host === host &&
			new URL(origin).protocol === request.nextUrl.protocol;
	} catch {
		/* origine assente o malformata */
	}
	if (!sameOrigin) {
		return Response.json(
			{ detail: "Origine della richiesta non valida." },
			{ status: 403, headers },
		);
	}
	return proxy(request, (await context.params).path);
}

async function proxy(request: NextRequest, path: string[]) {
	const route = path.join("/");
	const backend = process.env.APP_BACKEND_URL ?? "http://127.0.0.1:8001";
	const agent = process.env.AGENT_API_URL ?? "http://127.0.0.1:8002";
	const isList = request.method === "GET" && route === "contesti";
	const isPreparation =
		/^preparazioni(?:\/[0-9a-f-]{36}(?:\/(?:genera|stato|fonti\/[^/]+))?)?$/.test(
			route,
		);
	const allowed =
		isList ||
		(isPreparation &&
			((request.method === "GET" && path.length > 1 && path[2] !== "genera") ||
				(request.method === "POST" &&
					(path.length === 1 || (path.length === 3 && path[2] === "genera")))));
	if (!allowed)
		return Response.json(
			{ detail: "Risorsa non disponibile." },
			{ status: 404, headers },
		);
	const target = isList
		? `${backend}/api/v1/compilazione/contesti`
		: `${agent}/api/v1/${path.map(encodeURIComponent).join("/")}`;
	try {
		const response = await fetch(target, {
			method: request.method,
			cache: "no-store",
			signal: AbortSignal.timeout(30_000),
			headers: {
				Authorization: `Bearer ${process.env.APP_BACKEND_TOKEN ?? "demo-revisore"}`,
				"Content-Type": "application/json",
			},
			body: request.method === "POST" ? await request.text() : undefined,
		});
		return new Response(await response.text(), {
			status: response.status,
			headers: {
				...headers,
				"Content-Type": "application/json; charset=utf-8",
			},
		});
	} catch {
		return Response.json(
			{ detail: "Servizio non raggiungibile. Verificare backend e agente." },
			{ status: 502, headers },
		);
	}
}
