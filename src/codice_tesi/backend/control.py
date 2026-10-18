"""Comandi amministrativi del simulatore, separati dalle letture cliniche."""

from datetime import datetime
from uuid import uuid4

from psycopg import AsyncConnection

from codice_tesi.backend.repository import ClinicalError, snapshot


async def set_state(
    connection: AsyncConnection, *, scenario_id: str | None = None, at: datetime | None = None
):
    cursor = await connection.execute("SELECT * FROM simulation_state WHERE id = 1 FOR UPDATE")
    state = await cursor.fetchone()
    if state is None:
        raise ValueError("Importare un dataset prima di controllare gli scenari.")
    cursor = await connection.execute(
        "SELECT * FROM scenarios WHERE id = %s", (scenario_id or state["scenario_id"],)
    )
    scenario = await cursor.fetchone()
    if scenario is None:
        raise ValueError("Scenario non disponibile.")
    run_id = str(uuid4()) if scenario_id else state["run_id"]
    time = scenario["initial_time"] if scenario_id else at
    if time is None or time.tzinfo is None:
        raise ValueError("Specificare una data con fuso orario.")
    if scenario_id is None and time < state["current_time"]:
        raise ValueError(
            "L'orologio può solo avanzare; selezionare di nuovo lo scenario per ripristinarlo."
        )
    revision = state["revision"] + 1
    cursor = await connection.execute(
        """UPDATE simulation_state SET scenario_id = %s, run_id = %s, \"current_time\" = %s,
           revision = %s WHERE id = 1 AND revision = %s""",
        (scenario["id"], run_id, time, revision, state["revision"]),
    )
    if cursor.rowcount != 1:
        raise ClinicalError(
            409, "CONTROLLO_CONCORRENTE", "Lo stato è stato modificato da un altro controllo."
        )
    await connection.execute(
        'INSERT INTO state_revisions (revision, scenario_id, run_id, "current_time") '
        "VALUES (%s, %s, %s, %s)",
        (revision, scenario["id"], run_id, time),
    )
    return await snapshot(connection)
