export type Context = {
	id: string;
	patient_id: string | null;
	episode_id: string | null;
	scope: string;
	dataset: string;
	dataset_version: string;
	limitations: string[];
};
export type Source = {
	id: string;
	context_id: string;
	kind: string;
	text: string;
	origin: string;
	version: string;
	sha256: string;
};
export type Evidence = {
	id: string;
	source_id: string;
	start: number;
	end: number;
};
export type Catalog = { sources: Source[]; evidences: Evidence[] };
export type Section = {
	id: string;
	title: string;
	instruction: string;
	requirement: {
		status: "required" | "optional" | "unconfirmed" | "conditional";
	};
	fields: {
		id: string;
		label: string;
		context_key?: "patient_id" | "episode_id" | null;
	}[];
};
export type Packet = {
	context: Context;
	sources: Source[];
	template: { title: string; version: string; sections: Section[] };
};
export type DraftSection = {
	section_id: string;
	text: string;
	sources: { source_id: string; evidence_ids: string[] }[];
};
export type Result = {
	binding: { context_id: string; run_id: string };
	catalog: Catalog;
	draft: { sections: DraftSection[] };
	validation: { technical: string; clinical_review: string };
};
export type Job = {
	id: string;
	context_id: string;
	status: "prepared" | "running" | "completed" | "failed" | "interrupted";
	detail?: string;
	result?: Result;
};
export type Preparation = Job & { packet: Packet; catalog: Catalog };

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
	const response = await fetch(`/api/${path}`, { ...init, cache: "no-store" });
	const data = await response.json();
	if (!response.ok)
		throw new Error(
			typeof data.detail === "string"
				? data.detail
				: "Richiesta non completata.",
		);
	return data as T;
}
