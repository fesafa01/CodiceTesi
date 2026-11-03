import { expect, test } from "bun:test";
import { parseSource, splitFields } from "../src/lib/source-reader";

test("FHIR identifiers and separators inside JSON strings remain in the value", () => {
	const line =
		'2026-08-16T04:02:05+00:00 | MedicationRequest | Terapia | note=[{"text":"Testo | originale","reference":"urn:x|123"}] | stato=active';
	expect(splitFields(line)).toHaveLength(5);
	const record = parseSource(line)[0];
	expect(record.type).toBe("record");
	if (record.type !== "record") return;
	expect(record.fields).toEqual([
		{
			label: "note",
			value: [{ text: "Testo | originale", reference: "urn:x|123" }],
		},
		{ label: "stato", value: "active" },
	]);
	expect(record.raw).toBe(line);
});

test("patient fields retain all nested data, dates and exact primitive values", () => {
	const text =
		'name: [{"family":"Bini","given":["Federico"]}]\nbirthDate: "1942-02-03"\ngender: "male"\n\nTesto originale.\nCon una negazione: non riferita.';
	const blocks = parseSource(text);
	expect(blocks[0].type).toBe("fields");
	if (blocks[0].type !== "fields") return;
	expect(blocks[0].fields[0].value).toEqual([
		{ family: "Bini", given: ["Federico"] },
	]);
	expect(blocks[0].fields[1].value).toBe("1942-02-03");
	expect(blocks.map((b) => b.raw).join("\n")).toBe(text);
});

test("invalid embedded JSON is preserved as readable original text", () => {
	const record = parseSource(
		'2026-08-16T04:02:05+00:00 | CarePlan | Piano | activity={"detail":',
	)[0];
	expect(record.type).toBe("record");
	if (record.type !== "record") return;
	expect(record.fields[0].value).toBe('{"detail":');
});

test("escaped quotes do not split a JSON string into unrelated fields", () => {
	const value = { text: 'Dice "a | b"' };
	const text = `nota | text=${JSON.stringify(value)} | stato=final`;
	expect(splitFields(text)).toHaveLength(3);
	const block = parseSource(text)[0];
	if (block.type !== "record") throw new Error("Evento non riconosciuto");
	expect(block.fields.find((field) => field.label === "text")?.value).toEqual(
		value,
	);
});

test("an equals sign in a FHIR reference is part of the nested JSON value", () => {
	const text =
		'serviceProvider: {"reference":"Organization?identifier=https://example.org|123","display":"Clinica"}\nparticipant: [{"individual":{"reference":"Practitioner?identifier=http://hl7.org/fhir/sid/us-npi|999","display":"Medico"}}]';
	const block = parseSource(text)[0];
	expect(block.type).toBe("fields");
	if (block.type !== "fields") return;
	expect(block.fields).toEqual([
		{
			label: "serviceProvider",
			value: {
				reference: "Organization?identifier=https://example.org|123",
				display: "Clinica",
			},
		},
		{
			label: "participant",
			value: [
				{
					individual: {
						reference:
							"Practitioner?identifier=http://hl7.org/fhir/sid/us-npi|999",
						display: "Medico",
					},
				},
			],
		},
	]);
});
