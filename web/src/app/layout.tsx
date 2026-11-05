import type { Metadata } from "next";
import Link from "next/link";
import { FiFileText } from "react-icons/fi";
import "./globals.css";
export const metadata: Metadata = {
	title: "Dimissioni · Assistente documentale",
	description:
		"Preparazione della lettera di dimissione da fonti cliniche.",
};
export default function RootLayout({
	children,
}: Readonly<{ children: React.ReactNode }>) {
	return (
		<html lang="it">
			<body>
				<header className="topbar">
					<div className="shell topbar-inner">
						<Link href="/" className="brand">
							<span className="brand-icon">
								<FiFileText />
							</span>
							<span>
								Dimissioni
								<span className="brand-sub">Assistente documentale</span>
							</span>
						</Link>
						<span className="demo-badge">
							<span className="dot" />
							Ambiente di ricerca · Accesso locale
						</span>
					</div>
				</header>
				<main className="shell">{children}</main>
				<footer className="shell footer">
					Prototipo di supporto documentale{" "}
					<span>La bozza richiede la revisione del medico.</span>
				</footer>
			</body>
		</html>
	);
}
