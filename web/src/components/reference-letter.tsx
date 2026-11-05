"use client";

import { useEffect, useState } from "react";
import { api, type Context } from "@/lib/types";

type Letter = { note_id: string; text: string };

export function ReferenceLetter({ context }: { context: Context }) {
	const [open, setOpen] = useState(false);
	return (
		<details className="panel reference-letter" onToggle={(event) => setOpen(event.currentTarget.open)}>
			<summary>Lettera di dimissione originale</summary>
			{open && <ReferenceText context={context} />}
		</details>
	);
}

function ReferenceText({ context }: { context: Context }) {
	const [letter, setLetter] = useState<Letter>();
	const [error, setError] = useState("");
	const { id, dataset_version: version } = context;

	useEffect(() => {
		const controller = new AbortController();
		const query = new URLSearchParams({ versione: version });
		api<Letter>(`contesti/${encodeURIComponent(id)}/lettera-originale?${query}`, {
			signal: controller.signal,
		}).then(setLetter).catch((err: Error) => {
			if (!controller.signal.aborted) setError(err.message);
		});
		return () => controller.abort();
	}, [id, version]);

	return (
		<div className="reference-letter-content">
			<p className="muted">Riferimento MIMIC-IV-Note in inglese, disponibile per il confronto.</p>
			{error ? <p role="alert">{error}</p> : letter ? (
				<>
					<p className="reader-caption">Documento {letter.note_id}</p>
					<pre lang="en">{letter.text}</pre>
				</>
			) : <p role="status">Caricamento della lettera originale…</p>}
		</div>
	);
}
