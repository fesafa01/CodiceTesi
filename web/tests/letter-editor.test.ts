import { expect, test } from "bun:test";
import { letterText, sameEdits, sectionText } from "../src/lib/letter-editor";
import type { Job, Section } from "../src/lib/types";

const job: Job = {
    id: "session", context_id: "case", status: "running",
    preview: { response_revision: 0, sections: [{ section_id: "a", text: "In arrivo 👁" }] },
};

test("stream updates preserve manual text and intentionally empty sections", () => {
    expect(sectionText("a", {}, job)).toBe("In arrivo 👁");
    expect(sectionText("a", { a: "Modifica" }, job)).toBe("Modifica");
    expect(sectionText("a", { a: "" }, job)).toBe("");
    const updated = { ...job, preview: { response_revision: 1, sections: [{ section_id: "a", text: "Risposta nuova" }] } };
    expect(sectionText("a", { a: "Modifica" }, updated)).toBe("Modifica");
    expect(sectionText("a", {}, updated)).toBe("Risposta nuova");
});

test("validated result replaces preview while manual text has priority", () => {
    const completed = { ...job, status: "completed" as const,
        result: { draft: { sections: [{ section_id: "a", text: "Testo finale", sources: [] }] } } as Job["result"] };
    expect(sectionText("a", {}, completed)).toBe("Testo finale");
    expect(sectionText("a", { a: "Correzione" }, completed)).toBe("Correzione");
});

test("download uses current edits and labels unfinished generation", () => {
    const sections = [{ id: "a", title: "Prima" }, { id: "b", title: "Seconda" }] as Section[];
    const text = letterText(sections, { a: "Correzione", b: "Testo manuale" }, job);
    expect(text).toContain("GENERAZIONE NON COMPLETATA");
    expect(text).toContain("1. Prima\nCorrezione");
    expect(text).toContain("2. Seconda\nTesto manuale");
    expect(text).not.toContain("In arrivo");
});

test("download labels a recovered draft even after the job completes", () => {
    const recovered = { ...job, status: "completed" as const,
        result: { draft: { sections: [{ section_id: "a", text: "Testo recuperato", sources: [] }] },
            validation: { technical: "failed", clinical_review: "not_performed" } } as Job["result"] };
    const text = letterText([{ id: "a", title: "Prima" }] as Section[], {}, recovered);
    expect(text).toContain("CONTROLLI TECNICI NON SUPERATI");
    expect(text).toContain("Testo recuperato");
});

test("saved snapshot stays dirty if editing continues during a save", () => {
    expect(sameEdits({ a: "uno", b: "due" }, { b: "due", a: "uno" })).toBe(true);
    expect(sameEdits({ a: "nuovo" }, { a: "salvato" })).toBe(false);
    expect(sameEdits({ a: "" }, {})).toBe(false);
});
