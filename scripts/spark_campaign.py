"""Flusso condiviso per le campagne Spark tramite le API locali."""

import argparse
import base64
import fcntl
import json
import os
import re
import sys
from getpass import getpass
from http.client import HTTPException
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

ORIGIN = "http://127.0.0.1:3000"
DATASET = "mimic-iv-hosp"
CASES_PER_PATH = 15
CASE_LABELS = {
    f"caso {path} {number}"
    for path in ("hosp", "hosp e ed")
    for number in range(1, CASES_PER_PATH + 1)
}
TERMINAL = {"completed", "failed", "interrupted"}
POLL_SECONDS = 3
BETWEEN_ATTEMPTS_SECONDS = 5
INACTIVITY_TIMEOUT_SECONDS = 600
RETRYABLE_HTTP = {500, 502, 503, 504}
CONDITIONS = {
    "base": (False, False),
    "reasoning": (True, False),
    "reasoning-tools": (True, True),
}
LEGACY_CONDITIONS = ("base", "reasoning-tools")
REASONING_CONDITIONS = ("reasoning", "reasoning-tools")
TOOL_EXTRACTION_VERSION = "mimic-record-v1"


class CampaignError(RuntimeError):
    pass


class ApiError(CampaignError):
    def __init__(self, status, operation):
        super().__init__(f"API locale, operazione {operation}, HTTP {status}.")
        self.status = status


class ApiTransportError(CampaignError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def local_api(token):
    auth = base64.b64encode(f"mimic:{token}".encode()).decode("ascii")
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(route, body=None):
        operation = route.split("?", 1)[0].rsplit("/", 1)[-1]
        headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
        data = None
        if body is not None:
            headers.update({"Origin": ORIGIN, "Content-Type": "application/json"})
            data = json.dumps(body).encode("utf-8")
        message = Request(f"{ORIGIN}/api/{route}", data, headers)
        try:
            with opener.open(message, timeout=35) as response:
                return json.load(response)
        except HTTPError as exc:
            exc.close()
            raise ApiError(exc.code, operation) from None
        except (OSError, HTTPException):
            raise ApiTransportError(f"API locale irraggiungibile durante {operation}.") from None
        except ValueError:
            raise CampaignError(f"Risposta API non valida durante {operation}.") from None

    return request


def select_cases(contexts):
    if not isinstance(contexts, list) or any(not isinstance(case, dict) for case in contexts):
        raise CampaignError("Elenco dei casi non valido.")
    selected = []
    versions = set()
    labels = set()
    for case in contexts:
        if case.get("dataset") != DATASET:
            continue
        version = case.get("dataset_version")
        if not isinstance(version, str) or not version:
            raise CampaignError("Versione del campione MIMIC mancante.")
        versions.add(version)
        label = " ".join((case.get("display_name") or "").split()).casefold()
        labels.add(label)
        selected.append(case.get("id"))
    if (
        labels != CASE_LABELS
        or len(selected) != len(CASE_LABELS)
        or any(not isinstance(case_id, str) or not case_id for case_id in selected)
        or len(set(selected)) != len(CASE_LABELS)
    ):
        raise CampaignError("Attesi 15 casi Hosp e 15 casi Hosp e ED, tutti distinti.")
    if len(versions) != 1:
        raise CampaignError("I casi MIMIC devono appartenere alla stessa versione del campione.")
    return versions.pop(), sorted(selected)


def save_checkpoint(path, state):
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(state, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def retry_idempotent(request, route, body=None, *, retry_conflict=False):
    statuses = RETRYABLE_HTTP | ({409} if retry_conflict else set())
    for attempt in range(6):
        try:
            return request(route, body)
        except (ApiError, ApiTransportError) as exc:
            if attempt == 5 or (isinstance(exc, ApiError) and exc.status not in statuses):
                raise
            print(f"{exc} Riprovo ({attempt + 2}/6).", flush=True)
            sleep(POLL_SECONDS)


def start_run(request, session_id, enabled, *, model, tools_enabled=None, tool_version=None):
    options = {
        "thinking": enabled,
        "tools_enabled": enabled if tools_enabled is None else tools_enabled,
        "expected_model": model,
        "inactivity_timeout_seconds": INACTIVITY_TIMEOUT_SECONDS,
    }
    if tool_version:
        options["expected_tool_extraction_version"] = tool_version
    try:
        return retry_idempotent(
            request, f"preparazioni/{session_id}/genera", options, retry_conflict=True
        )
    except ApiError as exc:
        if exc.status == 412:
            raise CampaignError(
                f"Verifica LLM_MODEL={model} e ricostruisci agent-api "
                "con la versione dei tool attesa."
            ) from None
        raise


def failure_label(run):
    code = run.get("failure_code")
    if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
        return code
    return "codice assente"


def report_checkpoint(checkpoint, *, model):
    if not checkpoint.is_file():
        raise CampaignError("Checkpoint assente. Indica quello della campagna con --state.")
    state = json.loads(checkpoint.read_text(encoding="utf-8"))
    if state.get("model") != model:
        raise CampaignError("Il checkpoint appartiene a un altro modello.")
    runs = state["runs"]
    case_count = len(state["cases"])
    retry_only = "retry_registry" in state
    conditions = state.get("conditions", LEGACY_CONDITIONS)
    total = case_count if retry_only else len(conditions) * case_count
    completed = failed = 0
    for number in range(1, total + 1):
        run = runs.get(str(number), {})
        status = run.get("status")
        if status == "completed":
            completed += 1
        elif status in {"failed", "interrupted"}:
            failed += 1
            condition = "reasoning+tool" if retry_only else conditions[(number - 1) // case_count]
            print(f"[{number}/{total}] {condition} — {status} ({failure_label(run)})")
    pending = total - completed - failed
    print(f"{completed}/{total} completati, {failed} non riusciti, {pending} da terminare.")


def execute_campaign(
    request,
    checkpoint,
    *,
    model,
    retry_registry=None,
    retry_prompt_version=None,
    conditions=LEGACY_CONDITIONS,
    tool_version=None,
):
    if (
        not conditions
        or any(c not in CONDITIONS for c in conditions)
        or len(set(conditions)) != len(conditions)
    ):
        raise CampaignError("Condizioni della campagna non valide.")
    if retry_registry is not None and tuple(conditions) != LEGACY_CONDITIONS:
        raise CampaignError("Il retry selettivo non è previsto nella nuova suite.")
    version, cases = select_cases(retry_idempotent(request, "contesti"))
    plan = {"model": model, "dataset_version": version}
    if tuple(conditions) != LEGACY_CONDITIONS:
        plan["conditions"] = list(conditions)
    if tool_version:
        plan["tool_extraction_version"] = tool_version
    if retry_registry is not None:
        plan["retry_registry"] = str(retry_registry)
        plan["retry_prompt_version"] = retry_prompt_version
    else:
        plan["cases"] = cases
    if checkpoint.exists():
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        if (
            any(state.get(key) != value for key, value in plan.items())
            or state.get("retry_registry") != plan.get("retry_registry")
            or tuple(state.get("conditions", LEGACY_CONDITIONS)) != tuple(conditions)
            or state.get("tool_extraction_version") != tool_version
            or any(case not in cases for case in state["cases"])
        ):
            raise CampaignError("Il checkpoint appartiene a un altro modello o campione.")
    else:
        if retry_registry is not None:
            failed = set()
            for path in retry_registry.glob("*.json"):
                record = json.loads(path.read_text(encoding="utf-8"))
                config = record.get("model_config") or {}
                run = record.get("test_spark") or {}
                if (
                    config.get("model") == model
                    and run.get("dataset_version") == version
                    and config.get("thinking") is True
                    and config.get("tools_enabled") is True
                    and record.get("prompt_version") == retry_prompt_version
                    and record.get("draft_technical_validation") == "failed"
                ):
                    failed.add(run.get("context_id"))
            cases = [case for case in cases if case in failed]
            if not cases:
                raise CampaignError(
                    "Nessuna bozza reasoning+tool non validata per questo campione."
                )
        state = {**plan, "cases": cases, "runs": {}}

    runs = state["runs"]
    cases = state["cases"]
    case_count = len(cases)
    total = case_count if retry_registry is not None else len(conditions) * case_count
    for mode_index, condition in enumerate(
        ("reasoning-tools",) if retry_registry is not None else conditions
    ):
        thinking, tools_enabled = CONDITIONS[condition]
        for index, case_id in enumerate(cases, 1):
            number = index + mode_index * case_count
            key = str(number)
            run = runs.get(key)
            if run is not None and run.get("status") in TERMINAL:
                continue
            if run is None:
                try:
                    prepared = request(
                        "preparazioni", {"context_id": case_id, "expected_dataset_version": version}
                    )
                except ApiError as exc:
                    if exc.status == 412:
                        raise CampaignError("La versione del campione è cambiata.") from None
                    raise
                run = runs[key] = {"session_id": str(UUID(prepared["id"])), "status": "prepared"}
                save_checkpoint(checkpoint, state)
            session_id = str(UUID(run["session_id"]))
            print(f"[{number}/{total}] {condition} — avvio o ripresa", flush=True)
            job = start_run(
                request,
                session_id,
                thinking,
                model=model,
                tools_enabled=tools_enabled,
                tool_version=tool_version,
            )
            while job["status"] in {"running", "cancelling"}:
                sleep(POLL_SECONDS)
                job = retry_idempotent(request, f"preparazioni/{session_id}/stato?summary=true")
            status = job["status"]
            if status not in TERMINAL:
                raise CampaignError("Stato della generazione inatteso.")
            run.update(
                status=status, run_id=job.get("run_id"), failure_code=job.get("failure_code")
            )
            save_checkpoint(checkpoint, state)
            suffix = "" if status == "completed" else f" ({failure_label(run)})"
            print(f"[{number}/{total}] {condition} — {status}{suffix}", flush=True)
            if number < total:
                sleep(BETWEEN_ATTEMPTS_SECONDS)

    completed = sum(run["status"] == "completed" for run in runs.values())
    print(f"Campagna terminata. {completed}/{total} completati, {total - completed} non riusciti.")
    return 0 if completed == total else 1


def main(model=None, checkpoint_name=None):
    repo = Path(__file__).resolve().parents[1]
    private = Path(os.environ.get("TESI_PRIVATE_DIR") or repo.parent / "codice-tesi-private")
    parser = argparse.ArgumentParser(description="Esegue la campagna Spark usando le API locali.")
    if model is None:
        parser.add_argument("--model", required=True, help="Modello configurato in agent-api.")
    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        required=checkpoint_name is None,
        help="Checkpoint privato da riutilizzare per riprendere la campagna.",
    )
    parser.add_argument("--report", action="store_true", help="Mostra gli esiti già registrati.")
    parser.add_argument(
        "--suite",
        choices=("legacy", "reasoning", "base"),
        default="legacy",
        help="reasoning: 30+30 con/senza tool corretti; base: soltanto 30 prove Base v15.",
    )
    parser.add_argument(
        "--retry-registry",
        type=Path,
        help="Riprova solo le bozze reasoning+tool con validazione tecnica fallita nel registro.",
    )
    parser.add_argument(
        "--retry-prompt-version", help="Versione del prompt dei tentativi da riprovare."
    )
    args = parser.parse_args()
    if not args.report and bool(args.retry_registry) != bool(args.retry_prompt_version):
        parser.error("--retry-registry e --retry-prompt-version vanno usati insieme.")
    model = model if model is not None else args.model
    if args.suite != "legacy" and args.retry_registry:
        parser.error("La nuova suite esegue tutti i 30 casi, senza retry selettivi.")
    if args.state is None:
        name = (
            checkpoint_name.replace("-60.json", "-reasoning-tools-v1.json")
            if args.suite == "reasoning"
            else checkpoint_name.replace("-60.json", "-base-v15.json")
            if args.suite == "base"
            else checkpoint_name
        )
        args.state = private / name
    checkpoint = args.state.expanduser().resolve()
    if checkpoint.is_relative_to(repo):
        parser.error("Il checkpoint deve essere esterno al repository.")

    try:
        if args.report:
            report_checkpoint(checkpoint, model=model)
            return 0
        token = os.environ.get("APP_MIMIC_TOKEN")
        if not token:
            if not sys.stdin.isatty():
                raise CampaignError("Imposta APP_MIMIC_TOKEN nell'ambiente o usa un terminale.")
            token = getpass("Password dell'interfaccia MIMIC: ")
        if not token:
            raise CampaignError("Password mancante.")
        checkpoint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with checkpoint.with_suffix(checkpoint.suffix + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CampaignError("Campagna già in esecuzione con questo checkpoint.") from None
            return execute_campaign(
                local_api(token),
                checkpoint,
                model=model,
                retry_registry=args.retry_registry.expanduser().resolve()
                if args.retry_registry
                else None,
                retry_prompt_version=args.retry_prompt_version,
                conditions=(
                    REASONING_CONDITIONS
                    if args.suite == "reasoning"
                    else ("base",)
                    if args.suite == "base"
                    else LEGACY_CONDITIONS
                ),
                tool_version=TOOL_EXTRACTION_VERSION if args.suite != "legacy" else None,
            )
    except KeyboardInterrupt:
        print("Script interrotto. Rilancialo per riprendere dal checkpoint.", file=sys.stderr)
        return 130
    except CampaignError as exc:
        print(exc, file=sys.stderr)
        return 2
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        print("Checkpoint o risposta della campagna non utilizzabile.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
