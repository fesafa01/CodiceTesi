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
	const reference = process.env.REFERENCE_API_URL ?? "http://127.0.0.1:8003";
	const isList = request.method === "GET" && route === "contesti";
	const isReference = request.method === "GET" &&
		/^contesti\/mimic-[0-9]+\/lettera-originale$/.test(route);
	const isPreparation =
		/^preparazioni(?:\/[0-9a-f-]{36}(?:\/(?:genera|interrompi|modifiche|stream|stato|fonti\/[^/]+))?)?$/.test(
			route,
		);
	const allowed =
		isList || isReference ||
		(isPreparation &&
			((request.method === "GET" && path.length > 1 && !["genera", "interrompi", "modifiche"].includes(path[2])) ||
				(request.method === "POST" &&
					(path.length === 1 || (path.length === 3 && ["genera", "interrompi", "modifiche"].includes(path[2]))))));
	if (!allowed)
		return Response.json(
			{ detail: "Risorsa non disponibile." },
			{ status: 404, headers },
		);
	const statusSummary = path[2] === "stato" && request.nextUrl.searchParams.get("summary") === "true";
	const target = isList
		? `${backend}/api/v1/compilazione/contesti`
		: isReference
		? `${reference}/api/v1/revisione/${route}${request.nextUrl.search}`
		: `${agent}/api/v1/${path.map(encodeURIComponent).join("/")}${statusSummary ? "?summary=true" : ""}`;
	const isStream = request.method === "GET" && path[2] === "stream";
	const isGenerationStart = request.method === "POST" && path[2] === "genera";
	try {
		const options: RequestInit = {
			method: request.method,
			cache: "no-store",
			signal: isStream ? request.signal : AbortSignal.timeout(30_000),
			headers: {
				...(process.env.APP_MIMIC_TOKEN && { Authorization: `Bearer ${process.env.APP_MIMIC_TOKEN}` }),
				"Content-Type": "application/json",
			},
			body: request.method === "POST" ? await request.text() : undefined,
		};
		const response = await fetch(target, options).catch((error: unknown) => {
			// L'agente avvia al massimo un lavoro per lo stesso ID di preparazione.
			if (!isGenerationStart || options.signal?.aborted) throw error;
			return fetch(target, options);
		});
		if (isStream && response.ok) {
			return new Response(response.body, {
				status: response.status,
				headers: { ...headers, "Content-Type": "text/event-stream; charset=utf-8", "X-Accel-Buffering": "no" },
			});
		}
		return new Response(await response.text(), {
			status: response.status,
			headers: {
				...headers,
				"Content-Type": "application/json; charset=utf-8",
			},
		});
	} catch {
		return Response.json(
			{ detail: isReference ? "Il servizio delle lettere originali non è raggiungibile." : "Servizio non raggiungibile. Verificare backend e agente." },
			{ status: 502, headers },
		);
	}
}
