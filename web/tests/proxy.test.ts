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
