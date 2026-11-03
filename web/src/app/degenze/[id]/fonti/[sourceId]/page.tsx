"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import { FiArrowLeft } from "react-icons/fi";
import { SourceDocument } from "@/components/source-document";
import { api, type Source, type Evidence } from "@/lib/types";

export default function SourcePage({
	params,
}: {
	params: Promise<{ id: string; sourceId: string }>;
}) {
	const { id, sourceId } = use(params);
	const [data, setData] = useState<{ source: Source; evidences: Evidence[] }>();
	const [error, setError] = useState("");
	useEffect(() => {
		let disposed = false;
		setData(undefined);
		setError("");
		api<{ source: Source; evidences: Evidence[] }>(
			`preparazioni/${id}/fonti/${encodeURIComponent(sourceId)}`,
		)
			.then((result) => {
				if (!disposed) setData(result);
			})
			.catch((e) => {
				if (!disposed) setError(e.message);
			});
		return () => {
			disposed = true;
		};
	}, [id, sourceId]);
	return (
		<div className="source-page">
			<Link href={`/degenze/${id}`} className="back-link">
				<FiArrowLeft /> Torna alla lettera
			</Link>
			{error ? (
				<div className="error" role="alert">
					{error}
				</div>
			) : data ? (
				<div className="panel">
					<SourceDocument
						key={data.source.id}
						source={data.source}
						evidences={data.evidences}
					/>
				</div>
			) : (
				<div className="empty" role="status">
					Caricamento del documento…
				</div>
			)}
		</div>
	);
}
