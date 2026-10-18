"""Mapping esplicito SQL → FHIR R4B; non importa né converte file clinici arbitrari.

fhir.resources controlla i modelli, non certifica profili, terminologie o correttezza clinica.
"""

import base64
from datetime import UTC
from typing import Literal

from fhir.resources.R4B.bundle import Bundle
from fhir.resources.R4B.capabilitystatement import CapabilityStatement
from fhir.resources.R4B.diagnosticreport import DiagnosticReport
from fhir.resources.R4B.documentreference import DocumentReference
from fhir.resources.R4B.encounter import Encounter
from fhir.resources.R4B.observation import Observation
from fhir.resources.R4B.operationoutcome import OperationOutcome
from fhir.resources.R4B.patient import Patient

from codice_tesi.backend.models import Episode, Measurement, Source, SourceVersion
from codice_tesi.backend.models import Patient as PatientRow
from codice_tesi.backend.repository import ClinicalError, ClinicalRepository

FHIR_VERSION = "4.3.0"
FHIR_MEDIA_TYPE = "application/fhir+json"
SYSTEM = "urn:codice-tesi:clinical"
ResourceType = Literal[
    "Patient", "Encounter", "Observation", "DiagnosticReport", "DocumentReference"
]
RESOURCE_MODELS = {
    "Patient": Patient,
    "Encounter": Encounter,
    "Observation": Observation,
    "DiagnosticReport": DiagnosticReport,
    "DocumentReference": DocumentReference,
}
DIAGNOSTIC_TYPES = {"referto_laboratorio", "referto_ambulatoriale", "referto_istologico"}


def timestamp(value):
    return value.astimezone(UTC).isoformat()


def reference(type_, id_):
    return {"reference": f"{type_}/{id_}"}


def identifier(value, kind):
    return {"system": f"{SYSTEM}/{kind}", "value": value}


def metadata(version=None, origin=None):
    data = {
        "tag": [
            {
                "system": f"{SYSTEM}/tags",
                "code": "synthetic",
                "display": "Dato interamente sintetico",
            }
        ]
    }
    if version:
        data.update(versionId=str(version.version), lastUpdated=timestamp(version.recorded_at))
    if origin:
        data["source"] = f"urn:codice-tesi:system:{origin}"
    return data


def patient(row: PatientRow):
    return Patient.model_validate(
        {
            "resourceType": "Patient",
            "id": row.id,
            "meta": metadata(),
            "identifier": [identifier(row.id, "patients")],
            "name": [{"family": row.family_name, "given": [row.given_name]}],
            "birthDate": row.birth_date.isoformat(),
        }
    )


def encounter(row: Episode):
    period = {"start": timestamp(row.started_at)}
    if row.discharged_at:
        period["end"] = timestamp(row.discharged_at)
    return Encounter.model_validate(
        {
            "resourceType": "Encounter",
            "id": row.id,
            "meta": metadata(),
            "identifier": [identifier(row.nosological_id or row.id, "episodes")],
            "status": "in-progress" if row.status == "in_degenza" else "finished",
            "class": {
                "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                "code": "IMP" if row.kind == "ricovero" else "AMB",
            },
            "subject": reference("Patient", row.patient_id),
            "period": period,
        }
    )


def observation(source: Source, version: SourceVersion, result: Measurement):
    code = {"text": result.name}
    if result.code:
        code["coding"] = [{"system": result.code_system, "code": result.code}]
    quantity = {"value": result.value, "unit": result.unit}
    if result.unit_code:
        quantity.update(system="http://unitsofmeasure.org", code=result.unit_code)
    data = {
        "resourceType": "Observation",
        "id": f"RES-{source.id}-{result.ordinal}",
        "meta": metadata(version, source.origin),
        "status": "final",
        "code": code,
        "category": [
            {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                        "code": "laboratory" if result.kind == "laboratorio" else "vital-signs",
                    }
                ]
            }
        ],
        "subject": reference("Patient", source.patient_id),
        "encounter": reference("Encounter", source.episode_id),
        "effectiveDateTime": timestamp(result.clinical_at),
        "issued": timestamp(version.recorded_at),
        "valueQuantity": quantity,
    }
    # Il collegamento al referto è diagnostico; nessuna inferenza di diagnosi o intervalli.
    if source.kind in DIAGNOSTIC_TYPES:
        data["derivedFrom"] = [reference("DocumentReference", source.id)]
    return Observation.model_validate(data)


def attachment(source, version, public_origin, ricovero_id):
    data = {"title": version.title, "creation": timestamp(version.recorded_at)}
    if source.category == "documento":
        data.update(
            contentType="application/pdf",
            size=version.pdf_size,
            url=f"{public_origin}/api/v1/ricoveri/{ricovero_id}/documenti/{source.id}/contenuto?versione={version.version}",
        )
        # Attachment.hash in R4B è SHA-1: non inserirvi il nostro SHA-256.
    else:
        data.update(
            contentType="text/plain; charset=utf-8",
            data=base64.b64encode(version.text.encode()).decode(),
        )
    return data


def document_reference(source, version, public_origin, ricovero_id):
    data = {
        "resourceType": "DocumentReference",
        "id": source.id,
        "meta": metadata(version, source.origin),
        "status": "current",
        "docStatus": "final" if version.status == "finale" else "preliminary",
        "type": {"text": source.kind},
        "description": version.title,
        "subject": reference("Patient", source.patient_id),
        "date": timestamp(version.recorded_at),
        "context": {"encounter": [reference("Encounter", source.episode_id)]},
        "content": [{"attachment": attachment(source, version, public_origin, ricovero_id)}],
    }
    if source.author_id:
        data["author"] = [{"identifier": identifier(source.author_id, "authors")}]
    return DocumentReference.model_validate(data)


async def diagnostic_report(repo, source, version, public_origin, ricovero_id):
    results = await repo.measurements(source.id, version.version)
    data = {
        "resourceType": "DiagnosticReport",
        "id": source.id,
        "meta": metadata(version, source.origin),
        "status": "final",
        "code": {"text": version.title},
        "subject": reference("Patient", source.patient_id),
        "encounter": reference("Encounter", source.episode_id),
        "effectiveDateTime": timestamp(version.clinical_at),
        "issued": timestamp(version.recorded_at),
        "presentedForm": [attachment(source, version, public_origin, ricovero_id)],
    }
    if results:
        data["result"] = [reference("Observation", f"RES-{source.id}-{r.ordinal}") for r in results]
    return DiagnosticReport.model_validate(data)


async def resource(
    repo: ClinicalRepository, admission: Episode, type_: ResourceType, id_: str, public_origin: str
):
    if type_ == "Patient":
        if id_ != admission.patient_id:
            raise ClinicalError(404, "RISORSA_NON_TROVATA", "Risorsa non disponibile nel contesto.")
        return patient(await repo.patient(id_))
    if type_ == "Encounter":
        row = await repo.episode(admission, id_)
        return encounter(row)
    if type_ == "Observation":
        try:
            if not id_.startswith("RES-"):
                raise ValueError
            source_id, ordinal = id_[4:].rsplit("-", 1)
            ordinal = int(ordinal)
        except ValueError:
            raise ClinicalError(
                404, "RISORSA_NON_TROVATA", "Risorsa non disponibile nel contesto."
            ) from None
        source, version, _ = await repo.source(admission, source_id)
        rows = await repo.measurements(source_id, version.version)
        row = next((r for r in rows if r.ordinal == ordinal), None)
        if row is None:
            raise ClinicalError(
                404, "RISORSA_NON_TROVATA", "Risultato non disponibile nel contesto."
            )
        return observation(source, version, row)
    source, version, _ = await repo.source(admission, id_)
    if type_ == "DocumentReference":
        return document_reference(source, version, public_origin, admission.id)
    if source.kind not in DIAGNOSTIC_TYPES:
        raise ClinicalError(404, "RISORSA_NON_TROVATA", "La fonte non è un referto diagnostico.")
    return await diagnostic_report(repo, source, version, public_origin, admission.id)


def bundle(resources, self_url, next_url, time, base_url):
    data = {
        "resourceType": "Bundle",
        "type": "searchset",
        "meta": metadata(),
        "timestamp": timestamp(time),
        "link": [{"relation": "self", "url": self_url}],
        "entry": [
            {
                "fullUrl": f"{base_url}/{r.get_resource_type()}/{r.id}",
                "resource": r.model_dump(exclude_none=True),
                "search": {"mode": "match"},
            }
            for r in resources
        ],
    }
    if next_url:
        data["link"].append({"relation": "next", "url": next_url})
    return Bundle.model_validate(data)


def capability(base_url):
    return CapabilityStatement.model_validate(
        {
            "resourceType": "CapabilityStatement",
            "status": "active",
            "date": "2026-10-17",
            "kind": "instance",
            "fhirVersion": FHIR_VERSION,
            "format": [FHIR_MEDIA_TYPE, "application/json", "text/json"],
            "implementation": {
                "description": "Adattatore sintetico di sola lettura vincolato al ricovero.",
                "url": base_url,
            },
            "software": {"name": "CodiceTesi backend clinico", "version": "0.2.0"},
            "description": (
                "Sottoinsieme read/search. Nessuna scrittura, autenticazione "
                "o conformità a profili nazionali dichiarata. Mapping esplicito da dati SQL; "
                "nessuna importazione HL7 v2, CDA, FHIR o estrazione di misure da PDF. "
                "Validazione con modelli Python, senza validatore HL7 o terminology server. "
                "_format seleziona JSON; XML e RDF non supportati. "
                "Base URL per ricovero e parametri cursor/ambito/canale specifici del backend."
            ),
            "rest": [
                {
                    "mode": "server",
                    "resource": [
                        {
                            "type": type_,
                            "interaction": [{"code": "read"}, {"code": "search-type"}],
                            "searchParam": [
                                {"name": "_count", "type": "number"},
                                {
                                    "name": "cursor",
                                    "type": "string",
                                    "documentation": "Cursore opaco del backend da link.next.",
                                },
                                {
                                    "name": "ambito",
                                    "type": "token",
                                    "documentation": "Convenzione locale: episodio o paziente.",
                                },
                                {
                                    "name": "canale",
                                    "type": "token",
                                    "documentation": "Convenzione locale: sio o cdr.",
                                },
                            ],
                        }
                        for type_ in RESOURCE_MODELS
                    ],
                }
            ],
        }
    )


def outcome(status, detail):
    return OperationOutcome.model_validate(
        {
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": {
                        400: "invalid",
                        404: "not-found",
                        406: "not-supported",
                        409: "conflict",
                        422: "invalid",
                        503: "transient",
                    }.get(status, "processing"),
                    "diagnostics": detail,
                }
            ],
        }
    )
