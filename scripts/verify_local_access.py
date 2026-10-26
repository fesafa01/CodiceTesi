"""Verifica in sola lettura le identità fittizie sui 30 casi Synthea locali.

Eseguire dalla cartella CodiceTesi con PYTHONPATH=src. Nessuna inferenza o
modifica ai dati. Gli esiti attesi sono indipendenti dalla policy importata dal
backend. Il report contiene solo codici, identificativi sintetici e conteggi.
"""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from codice_tesi.discharge.models import CompositionPacket


def check(base_url, path, token, expected_status, allowed_contexts, expected_context=None):
    request = Request(base_url.rstrip("/") + "/api/v1/compilazione" + path)
    if token is not None:
        request.add_header("Authorization", "Bearer " + token)
    observed = {"path": path, "expected_status": expected_status, "passed": False}
    try:
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            observed["status"] = response.code
            payload = json.loads(response.read())
            observed["no_store"] = response.headers.get("Cache-Control") == "no-store"
            observed["bearer_challenge"] = response.headers.get("WWW-Authenticate") == "Bearer"
        valid = observed["status"] == expected_status and observed["no_store"]
        if expected_status == 200:
            packet = CompositionPacket.model_validate(payload)
            observed["returned_context"] = packet.context.id
            observed["source_count"] = len(packet.sources)
            valid = valid and packet.context.id in allowed_contexts
            if expected_context is not None:
                valid = valid and packet.context.id == expected_context
        else:
            observed["contains_sources"] = "sources" in payload
            valid = valid and not observed["contains_sources"]
            if expected_status == 401:
                valid = valid and observed["bearer_challenge"]
        observed["passed"] = valid
    except Exception as error:
        observed["error_type"] = type(error).__name__
    return observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-url", default="http://127.0.0.1:8001")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Usare una nuova destinazione: il report non viene sovrascritto.")
    contexts = tuple(f"synthea-caso-{number:02}" for number in range(1, 31))
    profiles = (
        ("senza-credenziali", None, frozenset()),
        ("credenziale-invalida", "demo-inesistente", frozenset()),
        ("medico-a", "demo-medico-a", frozenset(contexts[::2])),
        ("medico-b", "demo-medico-b", frozenset(contexts[1::2])),
        ("revisore", "demo-revisore", frozenset(contexts)),
    )
    results = []
    for subject, token, allowed in profiles:
        for context in contexts:
            expected = 401 if not allowed else 200 if context in allowed else 403
            observed = check(
                args.backend_url,
                f"/contesti/{context}/pacchetto",
                token,
                expected,
                allowed,
                expected_context=context,
            )
            results.append({"profile": subject, **observed})
        for seed in (0, 1, 7, 42, 99):
            observed = check(
                args.backend_url,
                f"/avvio-test?seed={seed}",
                token,
                200 if allowed else 401,
                allowed,
            )
            results.append({"profile": subject, **observed})
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "scope": "local demo access; synthetic cases; read-only; no inference",
        "checks": len(results),
        "passed": sum(result["passed"] for result in results),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(f"Controlli superati: {report['passed']}/{report['checks']}")
    return 0 if report["passed"] == report["checks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
