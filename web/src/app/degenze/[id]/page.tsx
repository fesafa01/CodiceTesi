"use client";
import { use, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ReferenceLetter } from "@/components/reference-letter";
import { SourceDocument } from "@/components/source-document";
import { sourceTitle } from "@/lib/source-reader";
import { letterText, sameEdits, sectionText } from "@/lib/letter-editor";
import {
	FiArrowLeft,
	FiArrowUpRight,
	FiCheck,
	FiChevronRight,
	FiDownload,
	FiFileText,
	FiLoader,
	FiSave,
} from "react-icons/fi";
import {
	api,
	type Job,
	type Preparation,
	type Source,
	type DraftSection,
    type Editor,
} from "@/lib/types";

const requirementLabels = {
	required: "Obbligatorio",
	optional: "Facoltativo",
	unconfirmed: "Da confermare",
	conditional: "Condizionale",
};
const phaseLabels: Record<string, string> = {
    avvio: "Avvio della generazione", preparazione: "Preparazione delle fonti",
    attesa_risposta: "Attesa della risposta", ragionamento: "Elaborazione delle fonti",
    redazione: "Stesura della bozza", lettura_fonti: "Verifiche sui dati documentati",
    correzione_tool: "Verifica dei parametri del tool",
    redazione_finale: "Preparazione della stesura finale con le fonti disponibili",
    controllo_risultato: "Controllo del risultato", completata: "Bozza pronta",
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
    const [thinking, setThinking] = useState(true);
    const [toolsEnabled, setToolsEnabled] = useState(false);
    const [elapsed, setElapsed] = useState(0);
    const [edits, setEdits] = useState<Record<string, string>>({});
    const [savedEditor, setSavedEditor] = useState<Editor>({ revision: 0, sections: {} });
    const [saving, setSaving] = useState(false);
    const [streamConnected, setStreamConnected] = useState(false);
    const [connectionError, setConnectionError] = useState("");
    const dirty = !sameEdits(edits, savedEditor.sections);
	const [source, setSource] = useState<Source>();
    const [focusEvidenceId, setFocusEvidenceId] = useState<string>();
	const dialog = useRef<HTMLDialogElement>(null);
	useEffect(() => {
		let disposed = false;
		api<Preparation>(`preparazioni/${id}`)
			.then((data) => {
				if (disposed) return;
				setPreparation(data);
				setJob(data);
                const editor = data.editor ?? { revision: 0, sections: {} };
                setSavedEditor(editor);
                setEdits(editor.sections);
                if (data.options) {
                    setThinking(data.options.thinking ?? true);
                    setToolsEnabled(data.options.tools_enabled);
                }
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
		if (!job || !["running", "cancelling", "completed"].includes(job.status) || job.result)
			return;
		let disposed = false;
		let streamAvailable = false;
		let timer: ReturnType<typeof setTimeout>;
        const stream = new EventSource(`/api/preparazioni/${id}/stream`);
        stream.onopen = () => {
            if (disposed) return;
            streamAvailable = true;
            setStreamConnected(true);
            setConnectionError("");
        };
        stream.onmessage = (event) => {
            if (disposed) return;
            try {
                const data: Job = JSON.parse(event.data);
                setJob(data);
                setConnectionError("");
                if (!["running", "cancelling"].includes(data.status)) stream.close();
            } catch { streamAvailable = false; }
        };
        stream.onerror = () => {
            streamAvailable = false;
            if (!disposed) setStreamConnected(false);
        };
		async function poll() {
			if (streamAvailable) {
                if (!disposed) timer = setTimeout(poll, 2500);
                return;
            }
			try {
				const data = await api<Job>(`preparazioni/${id}/stato`);
				if (disposed) return;
				setJob(data);
				setConnectionError("");
				if (!["running", "cancelling"].includes(data.status)) return;
			} catch (e) {
				if (!disposed)
					setConnectionError(
						`Aggiornamento interrotto: ${(e as Error).message} Nuovo tentativo in corso…`,
					);
			}
			if (!disposed) timer = setTimeout(poll, 2500);
		}
		void poll();
		return () => {
			disposed = true;
			clearTimeout(timer);
            stream.close();
            setStreamConnected(false);
		};
	}, [id, job?.status, !!job?.result]);
    useEffect(() => {
        if (!dirty) return;
        const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
        window.addEventListener("beforeunload", warn);
        return () => window.removeEventListener("beforeunload", warn);
    }, [dirty]);
    useEffect(() => {
        if (!job?.started_at || !["running", "cancelling"].includes(job.status)) return;
        const started = Date.parse(job.started_at);
        const update = () => setElapsed(Math.max(0, Math.floor((Date.now() - started) / 1000)));
        update();
        const timer = setInterval(update, 1000);
        return () => clearInterval(timer);
    }, [job?.started_at, job?.status]);
	useEffect(() => {
		if (source) dialog.current?.showModal();
	}, [source]);
	async function generate() {
		setStarting(true);
		setError("");
		try {
            if (dirty) await persistEdits();
			setJob(await api<Job>(`preparazioni/${id}/genera`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ thinking, tools_enabled: toolsEnabled }),
            }));
		} catch (e) {
			setError((e as Error).message);
		} finally {
			setStarting(false);
		}
	}
    async function persistEdits() {
        setSaving(true);
        try {
            const editor = await api<Editor>(`preparazioni/${id}/modifiche`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ revision: savedEditor.revision, sections: edits }),
            });
            setSavedEditor(editor);
        } finally { setSaving(false); }
    }
    async function saveEdits() {
        setError("");
        try { await persistEdits(); }
        catch (e) { setError((e as Error).message); }
    }
    async function cancel() {
        setError("");
        try {
            setJob(await api<Job>(`preparazioni/${id}/interrompi`, { method: "POST" }));
        } catch (e) { setError((e as Error).message); }
    }
	function download() {
		if (!job || !preparation) return;
		const text = letterText(sections, edits, job);
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
	const visibleIds = job.visible_section_ids ?? preparation.visible_section_ids;
    const sections = packet.template.sections.filter((section) => !visibleIds || visibleIds.includes(section.id));
    const definition = sections.find((s) => s.id === active) ?? sections[0];
    const selected = definition.id;
	const draft = job.result?.draft.sections.find((s) => s.section_id === selected);
	const busy = starting || ["running", "cancelling"].includes(job.status);
    const inventory = job.result?.facts ?? preparation.facts;
    const reviewIssues = (preparation.facts ?? inventory)?.issues ?? [];
    const recoveredDraft = job.result?.validation.technical === "failed";
    const rawDraftText = job.result?.raw_draft_text;
    const issues = [
        ...(inventory?.issues ?? []).filter((issue) => !issue.section_id),
        ...reviewIssues.filter((issue) => issue.section_id === selected),
    ];
	const terminalError = job.status === "failed" || job.status === "interrupted";
	const sourceURL = (sourceId: string) =>
		`/degenze/${id}/fonti/${encodeURIComponent(sourceId)}`;
	const showSource = (sourceId: string) => {
        setFocusEvidenceId(undefined);
		setSource(catalog.sources.find((s) => s.id === sourceId));
    };
    const showEvidence = (evidenceId: string) => {
        const evidence = catalog.evidences.find((e) => e.id === evidenceId);
        if (evidence) { showSource(evidence.source_id); setFocusEvidenceId(evidenceId); }
    };
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
							<button onClick={() => showEvidence(c.evidence_ids[0])}>
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
						{packet.context.display_name ?? packet.context.id}{" "}
						<span className="separator">/</span> Template Campus{" "}
						{packet.template.version}
					</p>
				</div>
				<div className="actions">
                    {job.status === "running" && (
                        <button onClick={cancel} className="button secondary">Interrompi</button>
                    )}
						<button onClick={download} className="button secondary"
                            disabled={!rawDraftText && !sections.some((s) => sectionText(s.id, edits, job).trim())}>
							<FiDownload /> Scarica bozza
						</button>
					<button
						className="button primary"
						onClick={generate}
						disabled={busy || saving || job.status !== "prepared"}
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
            <fieldset className="generation-options" disabled={starting || job.status !== "prepared"}>
                <legend>Opzioni di generazione</legend>
                <label>
                    <input type="checkbox" checked={thinking}
                        onChange={(e) => setThinking(e.target.checked)} />
                    Reasoning
                </label>
                <label>
                    <input type="checkbox" checked={toolsEnabled}
                        onChange={(e) => setToolsEnabled(e.target.checked)} />
                    Verifiche documentali
                </label>
                <small>Nessun limite di durata complessiva. Puoi interrompere la generazione durante l’attesa.</small>
            </fieldset>
            <div className="editor-summary" role="status">
                <span>{dirty ? "Modifiche da salvare" : savedEditor.revision > 0 ? "Modifiche salvate" : "Puoi scrivere in ogni sezione"}</span>
                <span>I testi inseriti a mano restano conservati durante la generazione.</span>
                <button onClick={saveEdits} className="button secondary" disabled={saving || !dirty}>
                    {saving ? <FiLoader className="spin" /> : <FiSave />}
                    {saving ? "Salvataggio…" : "Salva modifiche"}
                </button>
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
							? (recoveredDraft ? "Bozza recuperata" : "Bozza da revisionare")
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
            {connectionError && <div className="error" role="alert">{connectionError}</div>}
            {recoveredDraft && (
                <div className="notice" role="status">
                    Bozza salvata. Controlli tecnici non superati. Verifica testo e fonti prima dell’uso.
                </div>
            )}
            {rawDraftText && (
                <details className="panel" open={!job.result?.draft.sections.some((section) => section.text.trim())}>
                    <summary>Risposta originale del modello</summary>
                    <p className="draft-text">{rawDraftText}</p>
                </details>
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
					<FiLoader className="spin" />
                    {job.status === "cancelling" ? "Interruzione in corso" : (phaseLabels[job.phase ?? ""] ?? "Generazione in corso")}
                    {" · "}{Math.floor(elapsed / 60)} min {elapsed % 60} s.
                    {" "}Puoi consultare le fonti durante l’attesa.
                    {" "}{streamConnected ? "Testo aggiornato in tempo reale." : "Aggiornamento periodico del testo."}
				</div>
			)}
            {(busy || (!job.result && !!job.preview?.sections.length)) && (
                <p className="stream-note">Il testo in arrivo è provvisorio: i controlli su struttura e riferimenti vengono eseguiti al termine.</p>
            )}
			<div className="workspace">
				<section className="panel letter-panel">
					<div className="panel-heading">
						<div>
							<h2>
							<FiFileText /> Bozza della lettera
							</h2>
							<p>
								{sections.length} sezioni · Seleziona una
								sezione per consultarne le fonti · Testo modificabile
							</p>
						</div>
						<span className="paper-label">
							{job.result ? "BOZZA" : "TEMPLATE"}
						</span>
					</div>
					<div className="letter-sections">
						{sections.map((s, i) => {
							const generated = job.result?.draft.sections.find(
								(d) => d.section_id === s.id,
							);
                            const incoming = job.preview?.sections.find((d) => d.section_id === s.id);
                            const manual = Object.hasOwn(edits, s.id);
                            const needsReview = generated?.text === "" && (
                                s.id === "terapia_dimissione" || reviewIssues.some((issue) => issue.section_id === s.id)
                            );
                            const writing = busy && !!incoming && job.preview?.sections[job.preview.sections.length - 1]?.section_id === s.id;
							return (
								<article
									key={s.id}
									className={`letter-section ${selected === s.id ? "is-active" : ""} ${writing ? "is-streaming" : ""}`}
								>
									<button
										className="section-heading"
										onClick={() => setActive(s.id)}
										aria-pressed={selected === s.id}
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
                                        <div className="section-editor-meta">
                                            <span className={writing ? "stream-badge" : "muted"}>
                                                {manual ? "Testo modificato a mano" : needsReview ? "Da verificare" : generated ? "Testo generato" : writing ? "In scrittura…" : incoming ? "Testo ricevuto · provvisorio" : "Da compilare"}
                                            </span>
                                            {manual && <button className="text-button" onClick={() => setEdits((current) => {
                                                const next = { ...current }; delete next[s.id]; return next;
                                            })}>{generated || incoming ? "Usa testo AI" : "Rimuovi testo manuale"}</button>}
                                        </div>
                                        {!generated && !incoming && <div className="section-field-labels">
                                            {s.fields.map((f) => <span key={f.id}>{f.label}{f.context_key ? `: ${packet.context[f.context_key] ?? "Non disponibile"}` : ""}</span>)}
                                        </div>}
                                        <textarea
                                            className="section-editor"
                                            aria-label={`Testo — ${s.title}`}
                                            value={sectionText(s.id, edits, job)}
                                            rows={Math.min(16, Math.max(4, sectionText(s.id, edits, job).split("\n").length + 1))}
                                            maxLength={100000}
                                            placeholder="Scrivi qui oppure avvia la generazione per ricevere il testo."
                                            onFocus={() => setActive(s.id)}
                                            onChange={(e) => setEdits((current) => ({ ...current, [s.id]: e.target.value }))}
                                        />
                                        {manual && (generated || incoming) && <details className="generated-comparison">
                                            <summary>{busy ? "Testo AI in arrivo" : "Testo AI originale"}</summary>
                                            <p className="draft-text">{generated?.text ?? incoming?.text}</p>
                                        </details>}
                                        {generated && <button
													className="text-button citation-count"
													onClick={() => setActive(s.id)}
												>
													{generated.sources.length} fonti del testo AI{" "}
													<FiChevronRight />
												</button>}
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
                        {issues.length > 0 && (
                            <div className="documentary-issues">
                                <h3>Avvisi per il medico</h3>
                                {issues.map((issue, index) => (
                                    <p key={index}>{issue.message}{" "}
                                        {issue.evidence_ids.map((eid) => {
                                            const ev = catalog.evidences.find((e) => e.id === eid);
                                            return ev ? <button key={eid} className="text-button"
                                                onClick={() => showEvidence(eid)}>Apri passaggio {eid}</button> : null;
                                        })}
                                    </p>
                                ))}
                            </div>
                        )}
                        {inventory && inventory.facts.length > 0 && (
                            <details className="all-sources">
                                <summary>Dati strutturati riconosciuti ({inventory.facts.length})</summary>
                                <p className="muted">Estratti dai record strutturati; le note libere restano nelle fonti originali.</p>
                                {inventory.facts.map((fact) => (
                                    <article className="citation" key={fact.id}>
                                        <strong>{fact.label}</strong>
                                        <p>{fact.event_time} · {fact.status ?? "Stato non specificato"}</p>
                                        {"value" in fact.attributes && <p>{String(fact.attributes.value)} {String(fact.attributes.unit ?? "Unità non documentata")}</p>}
                                        <button className="text-button" onClick={() => showSource(fact.source_id)}>Apri fonte</button>
                                    </article>
                                ))}
                            </details>
                        )}
						{job.result ? (
							<>
								<p className="evidence-intro">
									Fonti citate dal modello per questa sezione.
								</p>
								{citations(draft)}
                                {Object.hasOwn(edits, selected) && <p className="trace-note">Le citazioni si riferiscono al testo AI originale; le modifiche manuali richiedono una nuova verifica delle fonti.</p>}
								<p className="trace-note">
									{recoveredDraft
                                        ? "Bozza recuperata. Verifica il testo e i riferimenti disponibili."
                                        : "Riferimenti controllati tecnicamente. Pertinenza e correttezza clinica da revisionare."}
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
			{packet.context.dataset === "mimic-iv-hosp" && (
				<ReferenceLetter key={id} context={packet.context} />
			)}
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
						key={source.id + (focusEvidenceId ?? "")}
						source={source}
                        focusEvidenceId={focusEvidenceId}
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
