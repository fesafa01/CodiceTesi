import type { Source } from "./types";

export type JsonValue =
	| null
	| boolean
	| number
	| string
	| JsonValue[]
	| { [key: string]: JsonValue };
export type ReadField = { label: string; value: JsonValue };
export type ReadBlock =
	| {
			type: "record";
			raw: string;
			date?: string;
			category: string;
			title: string;
			fields: ReadField[];
	  }
	| { type: "fields"; raw: string; fields: ReadField[] }
	| { type: "prose"; raw: string };

const labels: Record<string, string> = {
	name: "Nome",
	use: "Uso",
	family: "Cognome",
	given: "Nomi",
	prefix: "Prefisso",
	gender: "Sesso",
	birthDate: "Data di nascita",
	period: "Periodo",
	start: "Inizio",
	end: "Fine",
	type: "Tipo",
	coding: "Codifica",
	code: "Codice",
	system: "Sistema",
	display: "Descrizione",
	text: "Testo",
	reasonCode: "Motivo",
	serviceProvider: "Struttura",
	participant: "Partecipanti",
	individual: "Professionista",
	reference: "Riferimento",
	diagnosis: "Diagnosi",
	condition: "Condizione",
	rank: "Ordine",
	status: "Stato",
	activity: "Attività",
	detail: "Dettaglio",
	location: "Luogo",
	reasonReference: "Riferimenti al motivo",
	dosageInstruction: "Istruzioni di somministrazione",
	sequence: "Sequenza",
	timing: "Schema temporale",
	repeat: "Ripetizione",
	frequency: "Frequenza",
	periodUnit: "Unità del periodo",
	asNeededBoolean: "Al bisogno",
	doseAndRate: "Dose e velocità",
	doseQuantity: "Quantità della dose",
	value: "Valore",
	unit: "Unità",
	note: "Nota",
	occurrencePeriod: "Periodo previsto",
	basedOn: "Basato su",
	actor: "Soggetto",
	description: "Descrizione",
};
export const fieldLabel = (key: string) =>
	labels[key] ?? key.replace(/_/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2");
export function sourceTitle(source: Source) {
	if (source.kind === "contesto") return "Anagrafica e ricovero";
	if (source.kind === "dati_ricovero") return "Eventi della degenza";
	if (source.kind === "contesto_verificato") return "Contesto verificato";
	if (/^nota-\d+$/.test(source.kind))
		return `Nota clinica ${source.kind.slice(5)}`;
	return fieldLabel(source.kind);
}

// I separatori nelle stringhe e negli oggetti JSON appartengono al valore.
export function splitFields(text: string): string[] {
	const parts: string[] = [];
	let start = 0,
		depth = 0,
		quoted = false,
		escaped = false;
	for (let i = 0; i < text.length; i++) {
		const c = text[i];
		if (quoted) {
			if (escaped) escaped = false;
			else if (c === "\\") escaped = true;
			else if (c === '"') quoted = false;
		} else if (c === '"') quoted = true;
		else if (c === "{" || c === "[") depth++;
		else if (c === "}" || c === "]") depth--;
		else if (depth === 0 && text.slice(i, i + 3) === " | ") {
			parts.push(text.slice(start, i));
			start = i + 3;
			i += 2;
		}
	}
	parts.push(text.slice(start));
	return parts;
}
export function parseValue(text: string): JsonValue {
	try {
		return JSON.parse(text) as JsonValue;
	} catch {
		return text;
	}
}
function field(text: string): ReadField | undefined {
	// Le URL e le ore non sono coppie chiave/valore.
	const match = text.match(/^([^:=\[\]{}"\n]+?)(?:=|:\s+)(.*)$/);
	if (!match || match[1].includes("://")) return undefined;
	return { label: match[1].trim(), value: parseValue(match[2].trim()) };
}
export function parseSource(text: string): ReadBlock[] {
	const blocks: ReadBlock[] = [];
	for (const line of text.split("\n")) {
		const parts = splitFields(line);
		if (parts.length > 1) {
			const dated = /^\d{4}-\d{2}-\d{2}T/.test(parts[0]);
			const rest = parts.slice(dated ? 2 : 1);
			const titleParts: string[] = [];
			const fields: ReadField[] = [];
			for (const part of rest) {
				const parsed = field(part);
				if (parsed) fields.push(parsed);
				else titleParts.push(part);
			}
			blocks.push({
				type: "record",
				raw: line,
				date: dated ? parts[0] : undefined,
				category: dated ? parts[1] : "Documento",
				title: titleParts.join(" · ") || (dated ? parts[1] : parts[0]),
				fields: dated
					? fields
					: [{ label: "Riferimento", value: parts[0] }, ...fields],
			});
		} else {
			const parsed = field(line);
			const last = blocks.at(-1);
			if (parsed) {
				if (last?.type === "fields") {
					last.raw += "\n" + line;
					last.fields.push(parsed);
				} else blocks.push({ type: "fields", raw: line, fields: [parsed] });
			} else if (last?.type === "prose") last.raw += "\n" + line;
			else blocks.push({ type: "prose", raw: line });
		}
	}
	return blocks.filter((b) => b.raw.trim());
}
