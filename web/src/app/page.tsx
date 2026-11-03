"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
	FiArrowRight,
	FiSearch,
	FiUser,
	FiRefreshCw,
	FiCheck,
	FiFileText,
} from "react-icons/fi";
import { api, type Context, type Job } from "@/lib/types";

export default function CasesPage() {
	const router = useRouter();
	const [contexts, setContexts] = useState<Context[]>([]);
	const [selected, setSelected] = useState("");
	const [search, setSearch] = useState("");
	const [loading, setLoading] = useState(true);
	const [preparing, setPreparing] = useState(false);
	const [error, setError] = useState("");
	async function load() {
		setLoading(true);
		setError("");
		setSelected("");
		try {
			setContexts(await api<Context[]>("contesti"));
		} catch (e) {
			setError((e as Error).message);
		} finally {
			setLoading(false);
		}
	}
	useEffect(() => {
		void load();
	}, []);
	async function proceed() {
		setPreparing(true);
		setError("");
		try {
			const job = await api<Job>("preparazioni", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ context_id: selected }),
			});
			router.push(`/degenze/${job.id}`);
		} catch (e) {
			setError((e as Error).message);
			setPreparing(false);
		}
	}
	const filtered = contexts.filter((c) =>
		`${c.id} ${c.patient_id ?? ""} ${c.episode_id ?? ""}`
			.toLowerCase()
			.includes(search.toLowerCase()),
	);
	const current = contexts.find((c) => c.id === selected);
	return (
		<>
			<nav className="steps" aria-label="Avanzamento">
				<span className="step active">
					<b>1</b> Seleziona degenza
				</span>
				<span className="step-line" />
				<span className="step">
					<b>2</b> Prepara lettera
				</span>
			</nav>
			<div className="page-heading">
				<p className="eyebrow">LETTERA DI DIMISSIONE</p>
				<h1>Da quale degenza iniziamo?</h1>
				<p>
					Seleziona un caso per recuperare le informazioni e preparare la
					lettera.
				</p>
			</div>
			{error && (
				<div className="error" role="alert">
					{error}{" "}
					<button onClick={load} className="text-button">
						Riprova
					</button>
				</div>
			)}
			<div className="selection-layout">
				<section className="panel case-panel" aria-label="Degenze disponibili">
					<div className="panel-heading">
						<div>
							<h2>
								Degenze disponibili{" "}
								<span className="count">{contexts.length}</span>
							</h2>
							<p>Casi caricati dal database</p>
						</div>
						<button
							className="icon-button"
							aria-label="Aggiorna degenze"
							onClick={load}
							disabled={loading || preparing}
						>
							<FiRefreshCw className={loading ? "spin" : ""} />
						</button>
					</div>
					<div className="search">
						<FiSearch />
						<input
							aria-label="Cerca degenza"
							placeholder="Cerca per caso, paziente o episodio…"
							value={search}
							onChange={(e) => setSearch(e.target.value)}
						/>
					</div>
					<div
						className="case-list"
						role="radiogroup"
						aria-label="Seleziona una degenza"
						aria-busy={loading}
					>
						{loading ? (
							<div className="empty">Caricamento delle degenze…</div>
						) : (
							filtered.map((c) => (
								<label
									key={c.id}
									className={`case-row ${selected === c.id ? "selected" : ""}`}
								>
									<input
										type="radio"
										name="degenza"
										value={c.id}
										checked={selected === c.id}
										onChange={() => setSelected(c.id)}
										disabled={preparing}
									/>
									<span className="patient-icon">
										<FiUser />
									</span>
									<span className="case-info">
										<strong>{c.id.replace("synthea-caso-", "Caso ")}</strong>
										<span>Paziente {c.patient_id ?? "non identificato"}</span>
										<small>Episodio {c.episode_id ?? "non disponibile"}</small>
									</span>
									<span className="row-choice" aria-hidden="true">
										{selected === c.id && <FiCheck />}
									</span>
								</label>
							))
						)}
						{!loading && !filtered.length && (
							<div className="empty">
								{contexts.length
									? "Nessuna degenza corrisponde alla ricerca."
									: "Nessuna degenza disponibile. Verifica l’importazione del dataset e le assegnazioni."}
							</div>
						)}
					</div>
					<div className="list-footer">
						{filtered.length} degenze visualizzate
					</div>
				</section>
				<aside className="selection-summary panel">
					<span className="summary-icon">
						<FiFileText />
					</span>
					<p className="eyebrow">IL PROSSIMO PASSO</p>
					<h2>Le fonti, prima della bozza.</h2>
					<p>
						Recuperiamo i dati della degenza e apriamo il template della
						lettera. Sarai tu ad avviare la generazione.
					</p>
					<ol className="summary-list">
						<li>
							<span>01</span> Informazioni della degenza
						</li>
						<li>
							<span>02</span> Template con tutte le sezioni
						</li>
						<li>
							<span>03</span> Fonti originali consultabili
						</li>
					</ol>
					{current && (
						<div className="selected-summary">
							<small>DEGENZA SELEZIONATA</small>
							<strong>{current.id.replace("synthea-caso-", "Caso ")}</strong>
						</div>
					)}
					<button
						className="button primary full"
						disabled={!selected || preparing || loading}
						onClick={proceed}
					>
						{preparing ? "Recupero delle informazioni…" : "Continua"}
						<FiArrowRight />
					</button>
					<small className="muted">
						La generazione inizierà solo premendo “Genera”.
					</small>
				</aside>
			</div>
		</>
	);
}
