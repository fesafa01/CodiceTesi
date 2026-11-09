import type { Job, Section } from "./types";

export function sectionText(id: string, edits: Record<string, string>, job: Job): string {
    if (Object.hasOwn(edits, id)) return edits[id];
    return job.result?.draft.sections.find((s) => s.section_id === id)?.text
        ?? job.preview?.sections.find((s) => s.section_id === id)?.text
        ?? "";
}

export function sameEdits(a: Record<string, string>, b: Record<string, string>): boolean {
    return Object.keys(a).length === Object.keys(b).length
        && Object.keys(a).every((id) => Object.hasOwn(b, id) && a[id] === b[id]);
}

export function letterText(sections: Section[], edits: Record<string, string>, job: Job): string {
    const partial = job.result?.validation.technical === "failed"
        ? " — CONTROLLI TECNICI NON SUPERATI"
        : job.status !== "completed" ? " — GENERAZIONE NON COMPLETATA" : "";
    const title = `BOZZA DI LETTERA DI DIMISSIONE — DA REVISIONARE${partial}\n\n`;
    const raw = job.result?.raw_draft_text;
    const text = sections.map((section) => sectionText(section.id, edits, job));
    const body = raw && !text.some((value) => value.trim()) ? ""
        : sections.map((s, i) => `${i + 1}. ${s.title}\n${text[i]}\n`).join("\n");
    const original = raw ? `${body ? "\n" : ""}RISPOSTA ORIGINALE DEL MODELLO\n\n${raw}` : "";
    return title + body + original;
}
