"""Lettura del dataset: note JSON, documenti PDF ed estrazioni verificate."""

import hashlib
import json
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from codice_tesi.backend.dataset_types import ImportedSource

Id = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def json_object(pairs):
    """Un campo ripetuto non deve sovrascrivere silenziosamente un dato clinico."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Chiavi JSON duplicate nel dataset.")
        result[key] = value
    return result


def invalid_json_constant(value):
    raise ValueError("Costanti numeriche non ammesse nel JSON del dataset.")


def parse_json(data):
    return json.loads(
        data.decode("utf-8"), object_pairs_hook=json_object, parse_constant=invalid_json_constant
    )


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Section(InputModel):
    id: str = Field(min_length=1, max_length=100)
    titolo: str = Field(min_length=1)
    testo: str = Field(min_length=1)
    pagina: int | None = Field(default=None, ge=1)


class Result(InputModel):
    kind: Literal["laboratorio", "parametro_vitale"]
    name: str = Field(min_length=1, max_length=150)
    value: Decimal = Field(allow_inf_nan=False, max_digits=20, decimal_places=8)
    unit: str = Field(min_length=1, max_length=60)
    clinical_at: AwareDatetime
    unit_code: str | None = Field(default=None, max_length=60)
    code: str | None = Field(default=None, max_length=100)
    code_system: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def coding_pair(self):
        if bool(self.code) != bool(self.code_system):
            raise ValueError("Codice e sistema di codifica devono essere presenti insieme.")
        return self


class SourceMetadata(InputModel):
    schema_version: Literal["2.0"]
    id: Id
    paziente_id: Id
    episodio_id: Id
    categoria: Literal["registrazione", "documento"]
    tipo: str = Field(min_length=1, max_length=60)
    titolo: str = Field(min_length=1, max_length=300)
    sistema_origine: str = Field(min_length=1, max_length=100)
    autore_id: Id | None
    canali: list[Literal["sio", "cdr"]] = Field(min_length=1)
    versione: int = Field(ge=1)
    stato: Literal["registrato", "finale", "ritirato"]
    data_clinica: AwareDatetime
    registrato_il: AwareDatetime
    disponibile_il: AwareDatetime
    sintetico: Literal[True]
    risultati: list[Result] = Field(default_factory=list)

    @model_validator(mode="after")
    def dates(self):
        if not self.data_clinica <= self.registrato_il <= self.disponibile_il:
            raise ValueError("Date della fonte incoerenti.")
        if len(set(self.canali)) != len(self.canali):
            raise ValueError("Canali duplicati.")
        if any(r.clinical_at > self.registrato_il for r in self.risultati):
            raise ValueError("Risultato successivo alla registrazione della fonte.")
        return self


class ClinicalNote(SourceMetadata):
    categoria: Literal["registrazione"]
    tipo: Literal[
        "valutazione_ingresso",
        "anamnesi",
        "diario_clinico",
        "nota_infermieristica",
        "valutazione_dimissione",
    ]
    sezioni: list[Section] = Field(min_length=1)


class Asset(InputModel):
    file: str
    sha256: Hash
    size: int = Field(gt=0, le=2_000_000)


class Original(Asset):
    media_type: Literal["application/pdf"]

    @model_validator(mode="after")
    def format(self):
        if Path(self.file).suffix.lower() != ".pdf":
            raise ValueError("Un originale application/pdf deve avere estensione .pdf.")
        return self


class Extract(Asset):
    media_type: Literal["application/json"]
    metodo: Literal["generazione_sintetica"]

    @model_validator(mode="after")
    def format(self):
        if Path(self.file).suffix.lower() != ".json":
            raise ValueError("Un'estrazione application/json deve avere estensione .json.")
        return self


class ClinicalDocument(SourceMetadata):
    categoria: Literal["documento"]
    originale: Original
    estrazione: Extract | None


class ExtractedText(InputModel):
    schema_version: Literal["2.0"]
    fonte_id: Id
    versione: int = Field(ge=1)
    originale_sha256: Hash
    sezioni: list[Section] = Field(min_length=1)


class Entry(InputModel):
    id: Id
    file: str


class ScenarioDefinition(InputModel):
    id: Id
    descrizione: str
    canali_non_disponibili: list[Literal["sio", "cdr"]] = Field(default_factory=list)
    fonti_aggiuntive: list[Entry] = Field(default_factory=list)


class Coverage(InputModel):
    episodio_id: Id
    modulo: str = Field(min_length=1, max_length=250)
    stato: str = Field(min_length=1, max_length=40)


class Manifest(InputModel):
    schema_version: Literal["2.0"]
    versione: str
    sintetico: Literal[True]
    snapshot: AwareDatetime
    scenario_iniziale: Id
    descrizione: str
    files: dict[str, str]


class Dataset:
    def __init__(self, directory: Path):
        self.directory = directory.resolve(strict=True)
        self.loaded: dict[str, bytes] = {}
        self.scenarios: dict[str, tuple[str, list[str], list[ImportedSource]]] = {}

    def read(self, relative: str) -> bytes:
        requested = Path(relative)
        if requested.is_absolute() or requested.suffix.lower() not in {".json", ".pdf"}:
            raise ValueError("Il dataset ammette soltanto file JSON/PDF relativi alla sua radice.")
        path = (self.directory / requested).resolve(strict=True)
        if not path.is_relative_to(self.directory) or not path.is_file():
            raise ValueError("Percorso esterno al dataset.")
        if path.stat().st_size > 2_000_000:
            raise ValueError("Fonte oltre il limite di importazione.")
        data = path.read_bytes()
        self.loaded[relative] = data
        return data

    def json(self, relative):
        if Path(relative).suffix.lower() != ".json":
            raise ValueError("I metadati del dataset devono avere estensione .json.")
        return parse_json(self.read(relative))

    def asset(self, asset: Asset) -> bytes:
        data = self.read(asset.file)
        if len(data) != asset.size or hashlib.sha256(data).hexdigest() != asset.sha256:
            raise ValueError("Integrità del documento o dell'estrazione non valida.")
        return data

    def source(self, entry: Entry) -> ImportedSource:
        raw = self.json(entry.file)
        if not isinstance(raw, dict):
            raise ValueError("La fonte deve essere un oggetto del formato locale del dataset.")
        if "resourceType" in raw:
            raise ValueError(
                "Il loader accetta il formato locale schema_version 2.0, non risorse FHIR. "
                "È necessario un adattatore di ingresso dedicato."
            )
        schema = ClinicalNote if raw.get("categoria") == "registrazione" else ClinicalDocument
        row = schema.model_validate(raw)
        if row.id != entry.id:
            raise ValueError("Catalogo e identità della fonte non coincidono.")
        pdf, sha, method = None, None, None
        if isinstance(row, ClinicalNote):
            sections = [s.model_dump(exclude_none=True) for s in row.sezioni]
            if any(s.pagina is not None for s in row.sezioni):
                raise ValueError("Una nota nativa non ha pagine di un documento PDF.")
            extraction_status = "non_applicabile"
        else:
            pdf = self.asset(row.originale)
            if not pdf.startswith(b"%PDF-") or b"%%EOF" not in pdf[-1024:]:
                raise ValueError("L'originale non è un documento PDF.")
            try:
                parsed = PdfReader(BytesIO(pdf), strict=True)
                if parsed.is_encrypted or not parsed.pages:
                    raise ValueError("Documento PDF cifrato o privo di pagine.")
                page_count = len(parsed.pages)
            except (PdfReadError, OSError) as exc:
                raise ValueError("Documento PDF non leggibile.") from exc
            sha = row.originale.sha256
            extraction_status = "non_disponibile"
            sections = []
            if row.estrazione:
                extracted = ExtractedText.model_validate(parse_json(self.asset(row.estrazione)))
                if extracted.fonte_id != row.id or extracted.versione != row.versione:
                    raise ValueError("Estrazione associata a un'altra fonte o versione.")
                if extracted.originale_sha256 != row.originale.sha256:
                    raise ValueError("Estrazione associata a un originale differente.")
                if any(s.pagina is None for s in extracted.sezioni):
                    raise ValueError("L'estrazione documentale deve indicare le pagine.")
                if any(s.pagina > page_count for s in extracted.sezioni):
                    raise ValueError("Pagina dell'estrazione assente nell'originale.")
                sections = [s.model_dump() for s in extracted.sezioni]
                extraction_status, method = "disponibile", row.estrazione.metodo
        if len({s["id"] for s in sections}) != len(sections):
            raise ValueError("Identificativi delle sezioni duplicati.")
        return ImportedSource(
            identity={
                "id": row.id,
                "patient_id": row.paziente_id,
                "episode_id": row.episodio_id,
                "kind": row.tipo,
                "category": row.categoria,
                "origin": row.sistema_origine,
                "author_id": row.autore_id,
                "channels": row.canali,
            },
            title=row.titolo,
            clinical_at=row.data_clinica,
            recorded_at=row.registrato_il,
            available_at=row.disponibile_il,
            status=row.stato,
            sections=sections,
            version=row.versione,
            measurements=[r.model_dump() for r in row.risultati],
            pdf=pdf,
            sha256=sha,
            extraction_status=extraction_status,
            extraction_method=method,
        )

    def load(self):
        manifest = Manifest.model_validate(self.json("manifest.json"))
        files = manifest.files
        required = {
            "reparti",
            "pazienti",
            "ricoveri",
            "prestazioni",
            "modello_lettera",
            "fonti",
            "scenari",
            "copertura",
        }
        if set(files) != required:
            raise ValueError("Manifest incompleto o non valido.")
        units, patients, stays, visits, template = [
            self.json(files[key])
            for key in ("reparti", "pazienti", "ricoveri", "prestazioni", "modello_lettera")
        ]
        episodes = {r["id"]: r["paziente_id"] for r in [*stays, *visits]}
        if len(episodes) != len(stays) + len(visits):
            raise ValueError("Identificativi degli episodi duplicati.")
        base = [self.source(Entry.model_validate(e)) for e in self.json(files["fonti"])]
        scenarios = [ScenarioDefinition.model_validate(s) for s in self.json(files["scenari"])]
        if len({s.id for s in scenarios}) != len(scenarios) or manifest.scenario_iniziale not in {
            s.id for s in scenarios
        }:
            raise ValueError("Scenari duplicati o scenario iniziale assente.")
        for scenario in scenarios:
            sources = [*base, *(self.source(e) for e in scenario.fonti_aggiuntive)]
            versions = set()
            for source in sources:
                if episodes.get(source.identity["episode_id"]) != source.identity["patient_id"]:
                    raise ValueError("Fonte associata a un paziente o episodio incoerente.")
                key = source.identity["id"], source.version
                if key in versions:
                    raise ValueError("Identità/versione duplicata nello scenario.")
                versions.add(key)
            self._check_versions(sources)
            self.scenarios[scenario.id] = (
                scenario.descrizione,
                scenario.canali_non_disponibili,
                sources,
            )
        coverage = [Coverage.model_validate(c) for c in self.json(files["copertura"])]
        if any(c.episodio_id not in episodes for c in coverage):
            raise ValueError("Copertura associata a un episodio assente.")
        if len({(c.episodio_id, c.modulo) for c in coverage}) != len(coverage):
            raise ValueError("Moduli di copertura duplicati.")
        digest = hashlib.sha256(b"clinical-dataset-v2")
        for name, data in sorted(self.loaded.items()):
            digest.update(name.encode())
            digest.update(data)
        return (
            manifest.model_dump(mode="json"),
            units,
            patients,
            stays,
            visits,
            template,
            base,
            [(c.episodio_id, c.modulo, c.stato) for c in coverage],
            digest.hexdigest(),
        )

    @staticmethod
    def _check_versions(sources):
        grouped = {}
        for s in sources:
            grouped.setdefault(s.identity["id"], []).append(s)
        for versions in grouped.values():
            ordered = sorted(versions, key=lambda s: s.version)
            if [s.version for s in ordered] != list(range(1, len(ordered) + 1)):
                raise ValueError("Sequenza delle versioni incompleta.")
            for old, new in zip(ordered, ordered[1:]):
                if new.available_at < old.available_at or new.recorded_at < old.recorded_at:
                    raise ValueError("Cronologia delle versioni incoerente.")
                if new.identity != old.identity:
                    raise ValueError("Identità modificata tra versioni.")
