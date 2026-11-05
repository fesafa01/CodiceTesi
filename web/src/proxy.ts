import { timingSafeEqual } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";

export function proxy(request: NextRequest) {
	const token = process.env.APP_MIMIC_TOKEN;
	if (!token) return NextResponse.next();

	const expected = Buffer.from("Basic " + Buffer.from(`mimic:${token}`).toString("base64"));
	const actual = Buffer.from(request.headers.get("authorization") ?? "");
	if (expected.length === actual.length && timingSafeEqual(expected, actual)) {
		return NextResponse.next();
	}
	return new NextResponse("Autenticazione locale MIMIC richiesta.", {
		status: 401,
		headers: {
			"WWW-Authenticate": 'Basic realm="MIMIC locale", charset="UTF-8"',
			"Cache-Control": "no-store",
		},
	});
}

export const config = { matcher: ["/((?!_next/static|_next/image).*)"] };
