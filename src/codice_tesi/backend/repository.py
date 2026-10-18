"""Query SQL cliniche condivise fra API e futuri strumenti diretti dell'agente."""

import base64
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from psycopg import AsyncConnection

from codice_tesi.backend.database import Database
from codice_tesi.backend.dataset_types import aware
from codice_tesi.backend.models import Episode, Measurement, Patient, Source, SourceVersion, Unit
from codice_tesi.backend.schemas import ChangeRead, SourceRead

Scope = Literal["episodio", "paziente"]
Channel = Literal["sio", "cdr"]


class ClinicalError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


@dataclass(frozen=True)
class Snapshot:
    scenario: str | None
    run_id: str | None
    revision: int
    time: datetime
    dataset_version: str | None
    unavailable_channels: tuple[str, ...]


async def snapshot(connection: AsyncConnection) -> Snapshot:
    cursor = await connection.execute("""
        SELECT st.scenario_id AS scenario, st.run_id, st.revision,
               st.\"current_time\" AS time, sc.dataset_version, sc.unavailable_channels
        FROM simulation_state st JOIN scenarios sc ON sc.id = st.scenario_id
        WHERE st.id = 1
    """)
    row = await cursor.fetchone()
    if row is None:
        cursor = await connection.execute("SELECT transaction_timestamp() AS time")
        now = (await cursor.fetchone())["time"]
        return Snapshot(None, None, 0, now, None, ())
    row["unavailable_channels"] = tuple(row["unavailable_channels"])
    return Snapshot(**row)


def encode_cursor(position: list, signature: str) -> str:
    raw = json.dumps({"posizione": position, "contesto": signature}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None, signature: str) -> list | None:
    if cursor is None:
        return None
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        data = json.loads(raw)
        if set(data) != {"posizione", "contesto"} or not isinstance(data["posizione"], list):
            raise ValueError
        if data["contesto"] != signature:
            raise ClinicalError(
                409,
                "CURSORE_OBSOLETO",
                "Ripetere la ricerca: contesto, filtri o revisione sono cambiati.",
            )
        return data["posizione"]
    except ClinicalError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ClinicalError(
            400, "CURSORE_NON_VALIDO", "Cursore di paginazione non valido."
        ) from None


class ClinicalRepository:
    def __init__(self, connection: AsyncConnection, view: Snapshot):
        self.connection, self.view = connection, view
        self._channels: dict[str, list[str]] = {}

    def signature(self, resource: str, **filters) -> str:
        value = json.dumps(
            {
                "risorsa": resource,
                "filtri": filters,
                "revisione": self.view.revision,
                "esecuzione": self.view.run_id,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(value.encode()).hexdigest()

    async def _one(self, query, params=None):
        cursor = await self.connection.execute(query, params)
        return await cursor.fetchone()

    async def _all(self, query, params=None):
        cursor = await self.connection.execute(query, params)
        return await cursor.fetchall()

    async def admission(self, id_: str) -> Episode:
        row = await self._one(
            "SELECT * FROM episodes WHERE id = %s AND kind = 'ricovero' AND started_at <= %s",
            (id_, self.view.time),
        )
        if row is None:
            raise ClinicalError(404, "RICOVERO_NON_TROVATO", "Ricovero non trovato.")
        return Episode.model_validate(row)

    async def patient(self, id_: str) -> Patient:
        row = await self._one("SELECT * FROM patients WHERE id = %s", (id_,))
        return Patient.model_validate(row)

    async def episode(self, admission: Episode, id_: str) -> Episode:
        row = await self._one(
            "SELECT * FROM episodes WHERE id = %s AND patient_id = %s AND started_at <= %s",
            (id_, admission.patient_id, self.view.time),
        )
        if row is None:
            raise ClinicalError(404, "RISORSA_NON_TROVATA", "Risorsa non disponibile nel contesto.")
        return Episode.model_validate(row)

    async def _page(self, query, params, model, *, limit, cursor, signature):
        position = decode_cursor(cursor, signature)
        if position is not None:
            if len(position) != 1 or not isinstance(position[0], str):
                raise ClinicalError(400, "CURSORE_NON_VALIDO", "Cursore non valido.")
            query += " AND id > %(position)s"
            params["position"] = position[0]
        params["limit"] = limit + 1
        rows = await self._all(query + " ORDER BY id LIMIT %(limit)s", params)
        next_cursor = (
            encode_cursor([rows[limit - 1]["id"]], signature) if len(rows) > limit else None
        )
        return [model.model_validate(row) for row in rows[:limit]], next_cursor

    async def units(self, *, limit, cursor):
        return await self._page(
            "SELECT * FROM clinical_units WHERE kind = 'clinica'",
            {},
            Unit,
            limit=limit,
            cursor=cursor,
            signature=self.signature("reparti"),
        )

    async def admissions(self, *, unit, status, limit, cursor):
        query = "SELECT * FROM episodes WHERE kind = 'ricovero' AND started_at <= %(time)s"
        params = {"time": self.view.time}
        if unit:
            query += " AND unit_id = %(unit)s"
            params["unit"] = unit
        if status:
            query += " AND status = %(status)s"
            params["status"] = status
        return await self._page(
            query,
            params,
            Episode,
            limit=limit,
            cursor=cursor,
            signature=self.signature("ricoveri", reparto=unit, stato=status),
        )

    async def context_page(self, admission, resource_type, *, scope, channel, limit, cursor):
        self._check_channel(channel)
        signature = self.signature(
            f"fhir-{resource_type}", ricovero=admission.id, ambito=scope, canale=channel
        )
        if resource_type == "Patient":
            query, params, model = (
                "SELECT * FROM patients WHERE id = %(patient)s",
                {"patient": admission.patient_id},
                Patient,
            )
        else:
            query = (
                "SELECT * FROM episodes WHERE patient_id = %(patient)s AND started_at <= %(time)s"
            )
            params, model = {"patient": admission.patient_id, "time": self.view.time}, Episode
            if scope == "episodio":
                query += " AND id = %(episode)s"
                params["episode"] = admission.id
        return await self._page(
            query, params, model, limit=limit, cursor=cursor, signature=signature
        )

    async def coverage(self, admission):
        return await self._all(
            "SELECT path AS modulo, status AS stato FROM module_coverage "
            "WHERE episode_id = %s ORDER BY path",
            (admission.id,),
        )

    async def template(self, id_):
        row = await self._one("SELECT * FROM letter_templates WHERE id = %s", (id_,))
        if row is None:
            raise ClinicalError(404, "MODELLO_NON_TROVATO", "Modello non disponibile.")
        return row

    def _check_channel(self, channel: str | None):
        if channel in self.view.unavailable_channels:
            raise ClinicalError(
                503,
                "CANALE_NON_DISPONIBILE",
                "Il canale richiesto è temporaneamente non disponibile.",
            )

    async def _prefetch_channels(self, ids):
        missing = set(ids) - self._channels.keys()
        if missing:
            self._channels.update({id_: [] for id_ in missing})
            rows = await self._all(
                "SELECT source_id, channel FROM source_channels WHERE source_id = ANY(%s)",
                (list(missing),),
            )
            for row in rows:
                self._channels[row["source_id"]].append(row["channel"])

    async def channels(self, id_):
        await self._prefetch_channels([id_])
        return sorted(self._channels[id_])

    @staticmethod
    def _columns(*, with_text=False, full=False):
        # Proiezioni statiche: gli inventari non caricano i byte PDF.
        columns = """s.*, s.id AS source_id, v.scenario_id, v.version, v.title,
            v.clinical_at, v.recorded_at, v.available_at, v.status,
            v.sha256, v.pdf_size, v.extraction_status, v.extraction_method"""
        if with_text or full:
            columns += ", v.text"
        if full:
            columns += ", v.sections, v.pdf"
        return columns

    def _visible(self, admission, scope, *, at=None, channel=None, include_retired=False):
        self._check_channel(channel)
        params = {
            "patient": admission.patient_id,
            "scenario": self.view.scenario,
            "time": at or self.view.time,
            "channels": [channel]
            if channel
            else [c for c in ("sio", "cdr") if c not in self.view.unavailable_channels],
        }
        where = """s.patient_id = %(patient)s AND v.scenario_id = %(scenario)s
            AND v.available_at <= %(time)s
            AND NOT EXISTS (
                SELECT 1 FROM source_versions newer
                WHERE newer.source_id = v.source_id AND newer.scenario_id = v.scenario_id
                  AND newer.available_at <= %(time)s AND newer.version > v.version
            )
            AND EXISTS (SELECT 1 FROM source_channels ch
                        WHERE ch.source_id = s.id AND ch.channel = ANY(%(channels)s))"""
        if scope == "episodio":
            where += " AND s.episode_id = %(episode)s"
            params["episode"] = admission.id
        if not include_retired:
            where += " AND v.status <> 'ritirato'"
        return where, params

    @staticmethod
    def _pair(row):
        return Source.model_validate(row), SourceVersion.model_validate(row)

    async def describe(self, source, version, current_status=None) -> SourceRead:
        return SourceRead(
            id=source.id,
            paziente_id=source.patient_id,
            episodio_id=source.episode_id,
            categoria=source.category,
            tipo=source.kind,
            titolo=version.title,
            sistema_origine=source.origin,
            autore_id=source.author_id,
            canali=await self.channels(source.id),
            data_clinica=version.clinical_at,
            registrato_il=version.recorded_at,
            disponibile_il=version.available_at,
            versione=version.version,
            stato=version.status,
            stato_corrente=current_status or version.status,
        )

    async def source(self, admission, id_, version=None, channel=None):
        # Prima l'identità nel contesto; soltanto dopo canale e contenuto.
        row = await self._one(
            "SELECT * FROM clinical_sources WHERE id = %s AND patient_id = %s",
            (id_, admission.patient_id),
        )
        if row is None:
            raise ClinicalError(
                404, "FONTE_NON_TROVATA", "Fonte non trovata nel contesto del paziente."
            )
        self._check_channel(channel)
        params = {"id": id_, "scenario": self.view.scenario, "time": self.view.time}
        query = (
            "SELECT "
            + self._columns(full=True)
            + " FROM clinical_sources s JOIN source_versions v ON v.source_id = s.id "
            "WHERE s.id = %(id)s AND v.scenario_id = %(scenario)s AND v.available_at <= %(time)s"
        )
        current = await self._one(query + " ORDER BY v.version DESC LIMIT 1", params)
        if current is None:
            raise ClinicalError(
                404, "FONTE_NON_TROVATA", "Fonte non trovata nel contesto del paziente."
            )
        channels = await self.channels(id_)
        if channel and channel not in channels:
            raise ClinicalError(
                404, "FONTE_NON_TROVATA", "Fonte non disponibile nel canale richiesto."
            )
        if not set(channels) - set(self.view.unavailable_channels):
            raise ClinicalError(
                503,
                "CANALE_NON_DISPONIBILE",
                "Il canale della fonte è temporaneamente non disponibile.",
            )
        selected = current
        if version is not None:
            selected = await self._one(
                query + " AND v.version = %(version)s", {**params, "version": version}
            )
        if selected is None or (version is None and current["status"] == "ritirato"):
            raise ClinicalError(404, "VERSIONE_NON_DISPONIBILE", "Versione non disponibile.")
        source, selected_version = self._pair(selected)
        return source, selected_version, current["status"]

    async def list_sources(
        self,
        admission,
        *,
        category,
        scope,
        channel,
        kind,
        q,
        after_date,
        before_date,
        limit,
        cursor,
        diagnostic_only=False,
        with_text=False,
        raw=False,
    ):
        if after_date and before_date and after_date > before_date:
            raise ClinicalError(
                422, "INTERVALLO_NON_VALIDO", "La data iniziale supera quella finale."
            )
        signature = self.signature(
            "fonti",
            ricovero=admission.id,
            categoria=category,
            ambito=scope,
            canale=channel,
            tipo=kind,
            q=q,
            dal=after_date,
            al=before_date,
            diagnostica=diagnostic_only,
        )
        position = decode_cursor(cursor, signature)
        where, params = self._visible(admission, scope, channel=channel)
        if diagnostic_only:
            where += " AND s.kind = ANY(%(diagnostic)s)"
            params["diagnostic"] = [
                "referto_laboratorio",
                "referto_ambulatoriale",
                "referto_istologico",
            ]
        for value, column, parameter, operator in [
            (category, "s.category", "category", "="),
            (kind, "s.kind", "kind", "="),
            (after_date, "v.clinical_at", "after", ">="),
            (before_date, "v.clinical_at", "before", "<="),
        ]:
            if value is not None:
                # Colonne e operatori provengono solo dall'elenco costante soprastante.
                where += f" AND {column} {operator} %({parameter})s"
                params[parameter] = value
        if q:
            where += " AND strpos(v.search_text, %(q)s) > 0"
            params["q"] = q.casefold()
        if position is not None:
            try:
                if len(position) != 2 or not isinstance(position[1], str) or len(position[1]) > 100:
                    raise ValueError
                params.update(position_time=aware(position[0]), position_id=position[1])
            except (ValueError, TypeError):
                raise ClinicalError(
                    400, "CURSORE_NON_VALIDO", "Posizione del cursore non valida."
                ) from None
            where += " AND (v.clinical_at, s.id) > (%(position_time)s, %(position_id)s)"
        params["limit"] = limit + 1
        rows = await self._all(
            "SELECT "
            + self._columns(with_text=with_text)
            + " FROM clinical_sources s JOIN source_versions v ON v.source_id = s.id WHERE "
            + where
            + " ORDER BY v.clinical_at, s.id LIMIT %(limit)s",
            params,
        )
        await self._prefetch_channels([r["source_id"] for r in rows[:limit]])
        next_cursor = (
            encode_cursor(
                [rows[limit - 1]["clinical_at"].isoformat(), rows[limit - 1]["source_id"]],
                signature,
            )
            if len(rows) > limit
            else None
        )
        pairs = [self._pair(row) for row in rows[:limit]]
        return (pairs if raw else [await self.describe(s, v) for s, v in pairs]), next_cursor

    async def measurements(self, source_id, version):
        rows = await self._all(
            "SELECT * FROM measurements WHERE scenario_id = %s AND source_id = %s "
            "AND version = %s ORDER BY ordinal",
            (self.view.scenario, source_id, version),
        )
        return [Measurement.model_validate(row) for row in rows]

    async def results(self, admission, *, kind, limit, cursor, scope="episodio", channel=None):
        signature = self.signature(
            "risultati", ricovero=admission.id, tipo=kind, ambito=scope, canale=channel
        )
        position = decode_cursor(cursor, signature)
        where, params = self._visible(admission, scope, channel=channel)
        if kind:
            where += " AND m.kind = %(kind)s"
            params["kind"] = kind
        if position is not None:
            try:
                if (
                    len(position) != 3
                    or not isinstance(position[1], str)
                    or type(position[2]) is not int
                ):
                    raise ValueError
                params.update(
                    position_time=aware(position[0]),
                    position_id=position[1],
                    position_ordinal=position[2],
                )
            except (ValueError, TypeError):
                raise ClinicalError(
                    400, "CURSORE_NON_VALIDO", "Posizione del cursore non valida."
                ) from None
            where += (
                " AND (v.clinical_at, s.id, m.ordinal) > "
                "(%(position_time)s, %(position_id)s, %(position_ordinal)s)"
            )
        params["limit"] = limit + 1
        rows = await self._all(
            "SELECT "
            + self._columns()
            + ", "
            + ", ".join(f"m.{name} AS result_{name}" for name in Measurement.model_fields)
            + " FROM clinical_sources s JOIN source_versions v ON v.source_id = s.id "
            "JOIN measurements m ON (m.scenario_id, m.source_id, m.version) = "
            "(v.scenario_id, v.source_id, v.version) WHERE "
            + where
            + " ORDER BY v.clinical_at, s.id, m.ordinal LIMIT %(limit)s",
            params,
        )
        await self._prefetch_channels([r["source_id"] for r in rows[:limit]])
        next_cursor = (
            encode_cursor(
                [
                    rows[limit - 1]["clinical_at"].isoformat(),
                    rows[limit - 1]["source_id"],
                    rows[limit - 1]["result_ordinal"],
                ],
                signature,
            )
            if len(rows) > limit
            else None
        )
        return [
            (
                *self._pair(row),
                Measurement.model_validate(
                    {name: row[f"result_{name}"] for name in Measurement.model_fields}
                ),
            )
            for row in rows[:limit]
        ], next_cursor

    async def changes(self, admission, revision):
        previous = await self._one("SELECT * FROM state_revisions WHERE revision = %s", (revision,))
        if previous is None or revision > self.view.revision:
            raise ClinicalError(400, "REVISIONE_NON_VALIDA", "Revisione non disponibile.")
        if previous["run_id"] != self.view.run_id or previous["scenario_id"] != self.view.scenario:
            raise ClinicalError(
                409,
                "ESECUZIONE_DIVERSA",
                "La revisione appartiene a un'altra esecuzione: ripetere l'inventario.",
            )

        async def inventory(at):
            where, params = self._visible(admission, "paziente", at=at)
            rows = await self._all(
                "SELECT s.id, v.version FROM clinical_sources s "
                "JOIN source_versions v ON v.source_id = s.id WHERE " + where,
                params,
            )
            return {r["id"]: r["version"] for r in rows}

        before, now = await inventory(previous["current_time"]), await inventory(self.view.time)
        return [
            ChangeRead(
                fonte_id=id_,
                modifica="aggiunta"
                if id_ not in before
                else "ritirata"
                if id_ not in now
                else "corretta",
                versione_precedente=before.get(id_),
                versione_corrente=now.get(id_),
            )
            for id_ in sorted(before.keys() | now.keys())
            if before.get(id_) != now.get(id_)
        ]


@asynccontextmanager
async def open_repository(database: Database) -> AsyncIterator[ClinicalRepository]:
    """Accesso diretto riusabile: una transazione di lettura, senza passare da HTTP."""
    async with database.connection(read_only=True) as connection:
        yield ClinicalRepository(connection, await snapshot(connection))
