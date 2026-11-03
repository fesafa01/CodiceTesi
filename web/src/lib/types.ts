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
export type FactInventory = {
    facts: { id: string; source_id: string; evidence_id: string; kind: string;
        label: string; event_time: string | null; status: string | null;
        attributes: Record<string, unknown> }[];
    issues: { code: string; message: string; fact_ids: string[]; evidence_ids: string[] }[];
};
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
    facts?: FactInventory;
};
export type Editor = {
    revision: number;
    sections: Record<string, string>;
    updated_at?: string;
};
export type Preview = {
    response_revision: number;
    sections: { section_id: string; text: string }[];
};
export type Job = {
	id: string;
	context_id: string;
	status: "prepared" | "running" | "cancelling" | "completed" | "failed" | "interrupted";
    run_id?: string;
    started_at?: string;
    options?: { thinking: boolean; tools_enabled: boolean; timeout_seconds: number | null };
    phase?: string;
    progress?: { durata_secondi?: number; caratteri_testo?: number; tool_calls?: number };
	detail?: string;
	result?: Result;
    preview?: Preview;
};
export type Preparation = Job & { packet: Packet; catalog: Catalog; facts?: FactInventory; editor?: Editor };

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
