"use client";
import { use, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { SourceDocument } from "@/components/source-document";
import { sourceTitle } from "@/lib/source-reader";
import {
	FiArrowLeft,
	FiArrowUpRight,
	FiCheck,
	FiChevronRight,
	FiDownload,
	FiFileText,
	FiLoader,
} from "react-icons/fi";
import {
	api,
	type Job,
	type Preparation,
	type Source,
	type DraftSection,
} from "@/lib/types";

const requirementLabels = {
	required: "Obbligatorio",
	optional: "Facoltativo",
	unconfirmed: "Da confermare",
	conditional: "Condizionale",
};
export default function LetterPage({
	params,
}: {
	params: Promise<{ id: string }>;
}) {
	const { id } = use(params);
	const [preparation, setPreparation] = useState<Preparation>();
	const [job, setJob] = useState<Job>();
	const [error, setError] = useState("");
	const [active, setActive] = useState("");
	const [starting, setStarting] = useState(false);
	const [source, setSource] = useState<Source>();
	const dialog = useRef<HTMLDialogElement>(null);
	useEffect(() => {
		let disposed = false;
		api<Preparation>(`preparazioni/${id}`)
			.then((data) => {
				if (disposed) return;
				setPreparation(data);
				setJob(data);
				setActive(data.packet.template.sections[0].id);
			})
			.catch((e) => {
				if (!disposed) setError(e.message);
			});
		return () => {
			disposed = true;
		};
	}, [id]);
	useEffect(() => {
		if (!job || !["running", "completed"].includes(job.status) || job.result)
			return;
		let disposed = false;
		let timer: ReturnType<typeof setTimeout>;
		async function poll() {
			try {
				const data = await api<Job>(`preparazioni/${id}/stato`);
				if (disposed) return;
				setJob(data);
				setError("");
				if (data.status !== "running") return;
			} catch (e) {
				if (!disposed)
					setError(
						`Aggiornamento interrotto: ${(e as Error).message} Nuovo tentativo in corso…`,
					);
			}
			if (!disposed) timer = setTimeout(poll, 2500);
		}
		void poll();
		return () => {
			disposed = true;
			clearTimeout(timer);
		};
	}, [id, job?.status, !!job?.result]);
	useEffect(() => {
		if (source) dialog.current?.showModal();
	}, [source]);
	async function generate() {
		setStarting(true);
		setError("");
		try {
			setJob(await api<Job>(`preparazioni/${id}/genera`, { method: "POST" }));
		} catch (e) {
			setError((e as Error).message);
		} finally {
			setStarting(false);
		}
	}
	function download() {
		if (!job?.result || !preparation) return;
		const text =
			"BOZZA DI LETTERA DI DIMISSIONE — DA REVISIONARE\n\n" +
			preparation.packet.template.sections
				.map(
					(s, i) =>
						`${i + 1}. ${s.title}\n${job.result!.draft.sections.find((d) => d.section_id === s.id)?.text ?? ""}\n`,
				)
				.join("\n");
		const url = URL.createObjectURL(
			new Blob([text], { type: "text/plain;charset=utf-8" }),
		);
		const anchor = document.createElement("a");
		anchor.href = url;
		anchor.download = `bozza-${job.context_id}.txt`;
		anchor.click();
		URL.revokeObjectURL(url);
	}
	if (!preparation || !job)
		return (
			<div className="empty">
				{error ? (
					<div role="alert">
						{error}
						<p>
							<Link href="/">Torna alle degenze</Link>
						</p>
					</div>
				) : (
					"Recupero di template e fonti…"
				)}
			</div>
		);
	const { packet } = preparation;
	const catalog = job.result?.catalog ?? preparation.catalog;
	const definition = packet.template.sections.find((s) => s.id === active)!;
	const draft = job.result?.draft.sections.find((s) => s.section_id === active);
	const busy = starting || job.status === "running";
	const terminalError = job.status === "failed" || job.status === "interrupted";
	const sourceURL = (sourceId: string) =>
		`/degenze/${id}/fonti/${encodeURIComponent(sourceId)}`;
	const showSource = (sourceId: string) =>
		setSource(catalog.sources.find((s) => s.id === sourceId));
	function citations(section: DraftSection | undefined) {
		if (!section?.sources.length)
			return (
				<p className="muted">Nessuna citazione proposta per questa sezione.</p>
			);
		return section.sources.map((c) => {
			const item = catalog.sources.find((s) => s.id === c.source_id)!;
			return (
				<article className="citation" key={c.source_id}>
					<div className="citation-title">
						<FiFileText />
							<button onClick={() => showSource(item.id)}>
							{sourceTitle(item)}
						</button>
					</div>
					<small className="mono">{item.id}</small>
					{c.evidence_ids.map((eid) => {
						const ev = catalog.evidences.find((e) => e.id === eid)!;
						return (
							<blockquote key={eid}>
								{Array.from(item.text).slice(ev.start, ev.end).join("")}
							</blockquote>
						);
					})}
					<a
						className="source-link"
						href={sourceURL(item.id)}
						target="_blank"
						rel="noreferrer"
					>
						Apri documento <FiArrowUpRight />
					</a>
				</article>
			);
		});
	}
	return (
		<>
			<nav className="steps" aria-label="Avanzamento">
				<Link href="/" className="step done">
					<b>
						<FiCheck />
					</b>{" "}
					Seleziona degenza
				</Link>
				<span className="step-line" />
				<span className="step active">
					<b>2</b> Prepara lettera
				</span>
			</nav>
			<div className="letter-heading">
				<div>
					<Link className="back-link" href="/">
						<FiArrowLeft /> Cambia degenza
					</Link>
					<h1>Lettera di dimissione</h1>
					<p>
						{packet.context.id.replace("synthea-caso-", "Caso ")}{" "}
						<span className="separator">/</span> Template Campus{" "}
						{packet.template.version}
					</p>
				</div>
				<div className="actions">
					{job.result && (
						<button onClick={download} className="button secondary">
							<FiDownload /> Scarica bozza
						</button>
					)}
					<button
						className="button primary"
						onClick={generate}
						disabled={busy || job.status !== "prepared"}
					>
						{busy ? <FiLoader className="spin" /> : <FiFileText />}
						{busy
							? "Generazione in corso…"
							: job.status === "completed"
								? "Bozza generata"
								: terminalError
									? "Generazione terminata"
									: "Genera"}
					</button>
				</div>
			</div>
			<div className="context-bar">
				<span>
					<strong>Paziente</strong>
					<span className="mono">
						{packet.context.patient_id ?? "Non disponibile"}
					</span>
				</span>
				<span>
					<strong>Episodio</strong>
					<span className="mono">
						{packet.context.episode_id ?? "Non disponibile"}
					</span>
				</span>
				<span>
					<strong>Fonti recuperate</strong>
					{packet.sources.length} documenti
				</span>
				<span>
					<strong>Stato</strong>
					<span className="status-pill">
						{job.result
							? "Bozza da revisionare"
							: busy
								? "In elaborazione"
								: terminalError
									? "Non completata"
									: "Pronta per generare"}
					</span>
				</span>
			</div>
			{error && (
				<div className="error" role="alert">
					{error}
				</div>
			)}
			{terminalError && (
				<div className="error" role="alert">
					{job.detail}{" "}
					<Link href="/">
						Seleziona nuovamente la degenza per un nuovo tentativo.
					</Link>
				</div>
			)}
			{busy && (
				<div className="notice" role="status">
					<FiLoader className="spin" /> Il modello sta preparando la bozza e le
					citazioni. Puoi consultare le fonti durante l’attesa.
				</div>
			)}
			<div className="workspace">
				<section className="panel letter-panel">
					<div className="panel-heading">
						<div>
							<h2>
								<FiFileText /> Template della lettera
							</h2>
							<p>
								{packet.template.sections.length} sezioni · Seleziona una
								sezione per consultarne le fonti
							</p>
						</div>
						<span className="paper-label">
							{job.result ? "BOZZA" : "TEMPLATE"}
						</span>
					</div>
					<div className="letter-sections">
						{packet.template.sections.map((s, i) => {
							const generated = job.result?.draft.sections.find(
								(d) => d.section_id === s.id,
							);
							return (
								<article
									key={s.id}
									className={`letter-section ${active === s.id ? "is-active" : ""}`}
								>
									<button
										className="section-heading"
										onClick={() => setActive(s.id)}
										aria-pressed={active === s.id}
									>
										<span className="section-number">
											{String(i + 1).padStart(2, "0")}
										</span>
										<span>{s.title}</span>
										<span className="requirement">
											{requirementLabels[s.requirement.status]}
										</span>
										<FiChevronRight />
									</button>
									<div className="section-body">
										{generated ? (
											<>
												<p className="draft-text">
													{generated.text ||
														"Campo lasciato vuoto secondo il template."}
												</p>
												<button
													className="text-button citation-count"
													onClick={() => setActive(s.id)}
												>
													{generated.sources.length} fonti citate{" "}
													<FiChevronRight />
												</button>
											</>
										) : (
											<div className="field-placeholders">
												{s.fields.map((f) => (
													<div key={f.id}>
														<span>{f.label}</span>
														<span>
															{f.context_key
																? (packet.context[f.context_key] ??
																	"Non disponibile")
																: "Da compilare con la generazione"}
														</span>
													</div>
												))}
											</div>
										)}
									</div>
								</article>
							);
						})}
					</div>
				</section>
				<aside className="panel evidence-panel">
					<div className="panel-heading">
						<div>
							<p className="eyebrow">FONTI E TRACCIABILITÀ</p>
							<h2>{definition.title}</h2>
						</div>
					</div>
					<div className="evidence-body">
						{job.result ? (
							<>
								<p className="evidence-intro">
									Fonti citate dal modello per questa sezione.
								</p>
								{citations(draft)}
								<p className="trace-note">
									Riferimenti controllati tecnicamente. Pertinenza e correttezza
									clinica da revisionare.
								</p>
							</>
						) : (
							<div className="before-generation">
								<span className="summary-icon">
									<FiFileText />
								</span>
								<h3>Le citazioni appariranno qui</h3>
								<p>
									Dopo la generazione troverai le fonti associate a questa
									sezione.
								</p>
							</div>
						)}
						<details className="all-sources" open={!job.result}>
							<summary>
								Tutte le fonti della degenza{" "}
								<span className="count">{catalog.sources.length}</span>
							</summary>
							<p className="muted">
								Documenti disponibili al modello, incluso il contesto
								verificato.
							</p>
							<div>
								{catalog.sources.map((s) => (
									<div className="source-row" key={s.id}>
										<button onClick={() => showSource(s.id)}>
											<FiFileText />
											<span>
												{sourceTitle(s)}
												<small>{s.id}</small>
											</span>
										</button>
										<a
											href={sourceURL(s.id)}
											target="_blank"
											rel="noreferrer"
											aria-label={`Apri documento ${s.id}`}
										>
											<FiArrowUpRight />
										</a>
									</div>
								))}
							</div>
						</details>
					</div>
				</aside>
			</div>
			<dialog
				ref={dialog}
				className="source-dialog"
				aria-labelledby="source-document-title"
				onClose={() => setSource(undefined)}
				onClick={(e) => {
					if (e.target === e.currentTarget) dialog.current?.close();
				}}
			>
				{source && (
					<SourceDocument
						key={source.id}
						source={source}
						evidences={catalog.evidences.filter(
							(e) => e.source_id === source.id,
						)}
						documentURL={sourceURL(source.id)}
						onClose={() => dialog.current?.close()}
					/>
				)}
			</dialog>
		</>
	);
}
