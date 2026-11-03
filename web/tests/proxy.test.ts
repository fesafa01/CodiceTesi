import { afterEach, expect, test } from "bun:test";
import { NextRequest } from "next/server";
import { GET, POST } from "../src/app/api/[...path]/route";
const originalFetch = globalThis.fetch;
afterEach(() => {
	globalThis.fetch = originalFetch;
});
const params = (path: string[]) => ({ params: Promise.resolve({ path }) });

test("standalone server accepts public same-origin Host even with internal URL", async () => {
	let called = "";
	globalThis.fetch = (async (url: string) => {
		called = url;
		return Response.json({ id: "prepared" }, { status: 201 });
	}) as typeof fetch;
	const request = new NextRequest("http://0.0.0.0:3000/api/preparazioni", {
		method: "POST",
		headers: { host: "localhost:3000", origin: "http://localhost:3000" },
		body: "{}",
	});
	expect((await POST(request, params(["preparazioni"]))).status).toBe(201);
	expect(called).toEndWith("/api/v1/preparazioni");
});

test("cross-origin generation is rejected before contacting agent", async () => {
	let called = false;
	globalThis.fetch = (async () => {
		called = true;
		return Response.json({});
	}) as typeof fetch;
	const request = new NextRequest("http://localhost:3000/api/preparazioni", {
		method: "POST",
		headers: { host: "localhost:3000", origin: "https://example.com" },
		body: "{}",
	});
	expect((await POST(request, params(["preparazioni"]))).status).toBe(403);
	expect(called).toBe(false);
});

test("proxy does not expose arbitrary backend endpoints", async () => {
	expect(
		(
			await GET(
				new NextRequest("http://localhost:3000/api/admin"),
				params(["admin"]),
			)
		).status,
	).toBe(404);
});

test("stop is POST-only and forwards to the selected preparation", async () => {
    let called = "";
    globalThis.fetch = (async (url: string) => {
        called = url;
        return Response.json({status: "interrupted"});
    }) as typeof fetch;
    const id = "00000000-0000-0000-0000-000000000001";
    const path = ["preparazioni", id, "interrompi"];
    const url = `http://localhost:3000/api/preparazioni/${id}/interrompi`;
    expect((await GET(new NextRequest(url), params(path))).status).toBe(404);
    expect(called).toBe("");
    const request = new NextRequest(url, {method: "POST",
        headers: {host: "localhost:3000", origin: "http://localhost:3000"}});
    expect((await POST(request, params(path))).status).toBe(200);
    expect(called).toEndWith(`/api/v1/preparazioni/${id}/interrompi`);
});

test("stream forwards the first event without buffering the whole generation", async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({ start(c) {
        controller = c;
        c.enqueue(encoder.encode('data: {"status":"running"}\n\n'));
    } });
    const id = "00000000-0000-0000-0000-000000000001";
    const request = new NextRequest(`http://localhost:3000/api/preparazioni/${id}/stream`);
    globalThis.fetch = (async (_url: string, init: RequestInit) => {
        expect(init.signal).toBe(request.signal);
        return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
    }) as typeof fetch;
    const response = await GET(request, params(["preparazioni", id, "stream"]));
    expect(response.headers.get("content-type")).toStartWith("text/event-stream");
    const reader = response.body!.getReader();
    expect(new TextDecoder().decode((await reader.read()).value)).toContain('"running"');
    controller.enqueue(encoder.encode('data: {"status":"completed"}\n\n'));
    controller.close();
    expect(new TextDecoder().decode((await reader.read()).value)).toContain('"completed"');
    expect((await reader.read()).done).toBe(true);
});

test("manual edits are POST-only and preserve the revision and section text", async () => {
    const id = "00000000-0000-0000-0000-000000000001";
    const path = ["preparazioni", id, "modifiche"];
    const url = `http://localhost:3000/api/preparazioni/${id}/modifiche`;
    const body = JSON.stringify({ revision: 2, sections: { a: "Modifica 👁" } });
    globalThis.fetch = (async (_url: string, init: RequestInit) => {
        expect(init.body).toBe(body);
        return Response.json({ revision: 3 });
    }) as typeof fetch;
    expect((await GET(new NextRequest(url), params(path))).status).toBe(404);
    const request = new NextRequest(url, { method: "POST", body,
        headers: { host: "localhost:3000", origin: "http://localhost:3000" } });
    expect((await POST(request, params(path))).status).toBe(200);
    const postStream = new NextRequest(url.replace("modifiche", "stream"), { method: "POST",
        headers: { host: "localhost:3000", origin: "http://localhost:3000" } });
    expect((await POST(postStream, params(["preparazioni", id, "stream"]))).status).toBe(404);
});
