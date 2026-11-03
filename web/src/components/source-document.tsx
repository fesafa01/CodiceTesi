"use client";

import { useMemo, useState } from "react";
import {
	FiArrowUpRight,
	FiCalendar,
	FiCode,
	FiDownload,
	FiFileText,
	FiSearch,
	FiX,
} from "react-icons/fi";
import { type Evidence, type Source } from "@/lib/types";
import {
	fieldLabel,
	parseSource,
	sourceTitle,
	type JsonValue,
	type ReadField,
} from "@/lib/source-reader";

type Props = {
	source: Source;
	evidences: Evidence[];
	documentURL?: string;
	onClose?: () => void;
};

function Value({ value, depth = 0 }: { value: JsonValue; depth?: number }) {
	if (value === null) return <span className="reader-null">null</span>;
	if (typeof value !== "object")
		return (
			<span className="reader-value">
				{typeof value === "boolean"
					? value
						? "true"
						: "false"
					: String(value) || '""'}
			</span>
		);
	if (depth > 8)
		return <pre className="reader-code">{JSON.stringify(value, null, 2)}</pre>;
	if (Array.isArray(value)) {
		if (!value.length) return <span className="reader-null">Elenco vuoto</span>;
		return (
			<ol className={`reader-array ${value.length === 1 ? "single" : ""}`}>
				{value.map((item, i) => (
					<li key={i}>
						<Value value={item} depth={depth + 1} />
					</li>
				))}
			</ol>
		);
	}
	const entries = Object.entries(value);
	if (!entries.length)
		return <span className="reader-null">Oggetto vuoto</span>;
	return (
		<dl className="reader-fields nested">
			{entries.map(([key, item]) => (
				<div key={key}>
					<dt title={key}>{fieldLabel(key)}</dt>
					<dd>
						<Value value={item} depth={depth + 1} />
					</dd>
				</div>
			))}
		</dl>
	);
}
function Fields({ fields }: { fields: ReadField[] }) {
	return (
		<dl className="reader-fields">
			{fields.map((item, i) => (
				<div key={i}>
					<dt title={item.label}>{fieldLabel(item.label)}</dt>
					<dd>
						<Value value={item.value} />
					</dd>
				</div>
			))}
		</dl>
	);
}
export function SourceDocument({
	source,
	evidences,
	documentURL,
	onClose,
}: Props) {
	const [view, setView] = useState<"read" | "original" | "json">("read");
	const [query, setQuery] = useState("");
	const blocks = useMemo(() => parseSource(source.text), [source.text]);
	const visible = blocks.filter((block) =>
		block.raw.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
	);
	const payload = { source, evidences };
	function download() {
		const url = URL.createObjectURL(
			new Blob([JSON.stringify(payload, null, 2)], {
				type: "application/json;charset=utf-8",
			}),
		);
		const link = document.createElement("a");
		link.href = url;
		link.download = `${source.id}.json`;
		link.click();
		URL.revokeObjectURL(url);
	}
	return (
		<section className="source-document" aria-label="Documento della fonte">
			<header className="reader-header">
				<div className="reader-heading">
					<span className="reader-icon">
						<FiFileText />
					</span>
					<div>
						<p className="eyebrow">DOCUMENTO ORIGINALE</p>
						<h2 id="source-document-title">{sourceTitle(source)}</h2>
						<p className="reader-id">{source.id}</p>
					</div>
				</div>
				<div className="reader-actions">
					{documentURL && (
						<a
							href={documentURL}
							target="_blank"
							rel="noreferrer"
							className="icon-button"
							aria-label="Apri documento in una nuova scheda"
							title="Apri in una nuova scheda"
						>
							<FiArrowUpRight />
						</a>
					)}
					<button
						className="icon-button"
						onClick={download}
						aria-label="Scarica fonte JSON"
						title="Scarica JSON"
					>
						<FiDownload />
					</button>
					{onClose && (
						<button
							className="icon-button"
							onClick={onClose}
							aria-label="Chiudi fonte"
						>
							<FiX />
						</button>
					)}
				</div>
			</header>
			<div className="reader-toolbar">
				<div
					className="reader-views"
					role="group"
					aria-label="Vista del documento"
				>
					<button
						aria-pressed={view === "read"}
						onClick={() => setView("read")}
					>
						<FiFileText />
						Lettura
					</button>
					<button
						aria-pressed={view === "original"}
						onClick={() => setView("original")}
					>
						Testo originale
					</button>
					<button
						aria-pressed={view === "json"}
						onClick={() => setView("json")}
					>
						<FiCode />
						JSON
					</button>
				</div>
				{view === "read" && (
					<div className="reader-search">
						<FiSearch />
						<input
							aria-label="Cerca nel documento"
							placeholder="Cerca nel documento…"
							value={query}
							onChange={(e) => setQuery(e.target.value)}
						/>
						{query && (
							<button
								onClick={() => setQuery("")}
								aria-label="Cancella ricerca"
							>
								<FiX />
							</button>
						)}
					</div>
				)}
			</div>
			<div className="source-reader-body">
				{view === "read" && (
					<>
						<p className="reader-caption">
							Contenuti della fonte impaginati per la consultazione. Date,
							valori e diciture sono quelli originali.
						</p>
						{query && (
							<p className="reader-search-result" role="status">
								{visible.length} blocchi corrispondenti su {blocks.length}
							</p>
						)}
						<div className="reader-blocks">
							{visible.map((block, i) =>
								block.type === "record" ? (
									<article className="reader-record" key={i}>
										<div className="reader-record-heading">
											<span className="reader-category">{block.category}</span>
											{block.date && (
												<span className="reader-date">
													<FiCalendar />
													<time dateTime={block.date}>
														{block.date.replace("T", " · ")}
													</time>
												</span>
											)}
										</div>
										<h3>{block.title}</h3>
										{!!block.fields.length && <Fields fields={block.fields} />}
									</article>
								) : block.type === "fields" ? (
									<article className="reader-record" key={i}>
										<Fields fields={block.fields} />
									</article>
								) : (
									<div key={i} className="reader-prose">
										{block.raw.split(/\n\s*\n/).map((paragraph, index) => (
											<p key={index}>{paragraph}</p>
										))}
									</div>
								),
							)}
						</div>
						{!visible.length && (
							<div className="empty">
								Nessun contenuto corrisponde alla ricerca.
								<button className="text-button" onClick={() => setQuery("")}>
									Mostra tutto il documento
								</button>
							</div>
						)}
					</>
				)}
				{view === "original" && (
					<pre className="reader-original">{source.text}</pre>
				)}
				{view === "json" && (
					<pre className="reader-code">{JSON.stringify(payload, null, 2)}</pre>
				)}
				<details className="reader-metadata">
					<summary>Provenienza e dettagli tecnici</summary>
					<dl className="reader-fields">
						<div>
							<dt>Tipo di fonte</dt>
							<dd>{source.kind}</dd>
						</div>
						<div>
							<dt>Degenza</dt>
							<dd>{source.context_id}</dd>
						</div>
						<div>
							<dt>Origine</dt>
							<dd>{source.origin}</dd>
						</div>
						<div>
							<dt>Versione</dt>
							<dd className="mono">{source.version}</dd>
						</div>
						<div>
							<dt>SHA-256</dt>
							<dd className="mono">{source.sha256}</dd>
						</div>
					</dl>
				</details>
			</div>
		</section>
	);
}
