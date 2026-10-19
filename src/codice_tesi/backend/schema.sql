-- Schema clinico senza dati iniziali. Eseguito da init, mai dal servizio di lettura.
-- IF NOT EXISTS preserva i dati: questo file non aggiorna tabelle già esistenti.
CREATE TABLE IF NOT EXISTS clinical_units (
    id varchar(80) PRIMARY KEY,
    name varchar(200) NOT NULL,
    kind varchar(30) NOT NULL
);
CREATE TABLE IF NOT EXISTS patients (
    id varchar(80) PRIMARY KEY,
    given_name varchar(100) NOT NULL,
    family_name varchar(100) NOT NULL,
    birth_date date NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
    id varchar(80) PRIMARY KEY,
    patient_id varchar(80) NOT NULL REFERENCES patients(id),
    kind varchar(30) NOT NULL,
    status varchar(30) NOT NULL,
    started_at timestamptz NOT NULL,
    nosological_id varchar(80),
    unit_id varchar(80) REFERENCES clinical_units(id),
    ward_id varchar(80) REFERENCES clinical_units(id),
    bed varchar(80),
    planned_discharge date,
    discharged_at timestamptz,
    description text,
    CONSTRAINT uq_episode_patient UNIQUE (id, patient_id)
);
CREATE INDEX IF NOT EXISTS ix_episodes_patient_id ON episodes(patient_id);
CREATE TABLE IF NOT EXISTS clinical_sources (
    id varchar(100) PRIMARY KEY,
    patient_id varchar(80) NOT NULL,
    episode_id varchar(80) NOT NULL,
    category varchar(30) NOT NULL CHECK (category IN ('registrazione', 'documento')),
    kind varchar(60) NOT NULL,
    origin varchar(100) NOT NULL,
    author_id varchar(80),
    CONSTRAINT fk_source_episode_patient FOREIGN KEY (episode_id, patient_id)
        REFERENCES episodes(id, patient_id)
);
CREATE INDEX IF NOT EXISTS ix_source_patient_episode ON clinical_sources(patient_id, episode_id);
CREATE TABLE IF NOT EXISTS source_channels (
    source_id varchar(100) NOT NULL REFERENCES clinical_sources(id),
    channel varchar(30) NOT NULL CHECK (channel IN ('sio', 'cdr')),
    PRIMARY KEY (source_id, channel)
);
CREATE TABLE IF NOT EXISTS scenarios (
    id varchar(40) PRIMARY KEY,
    description text NOT NULL,
    initial_time timestamptz NOT NULL,
    unavailable_channels jsonb NOT NULL,
    dataset_version varchar(40) NOT NULL,
    seed_digest varchar(64) NOT NULL
);
CREATE TABLE IF NOT EXISTS source_versions (
    scenario_id varchar(40) NOT NULL REFERENCES scenarios(id),
    source_id varchar(100) NOT NULL REFERENCES clinical_sources(id),
    version integer NOT NULL CHECK (version > 0),
    title varchar(300) NOT NULL,
    clinical_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL,
    available_at timestamptz NOT NULL,
    status varchar(40) NOT NULL,
    text text NOT NULL,
    search_text text NOT NULL,
    sections jsonb NOT NULL,
    pdf bytea,
    sha256 varchar(64),
    pdf_size integer,
    extraction_status varchar(30) NOT NULL DEFAULT 'non_applicabile',
    extraction_method varchar(100),
    PRIMARY KEY (scenario_id, source_id, version)
);
CREATE INDEX IF NOT EXISTS ix_version_visibility ON source_versions(scenario_id, available_at, source_id);
CREATE TABLE IF NOT EXISTS measurements (
    scenario_id varchar(40) NOT NULL,
    source_id varchar(100) NOT NULL,
    version integer NOT NULL,
    ordinal integer NOT NULL,
    kind varchar(40) NOT NULL,
    name varchar(150) NOT NULL,
    value numeric(20,8) NOT NULL,
    unit varchar(60) NOT NULL,
    clinical_at timestamptz NOT NULL,
    unit_code varchar(60),
    code varchar(100),
    code_system varchar(200),
    PRIMARY KEY (scenario_id, source_id, version, ordinal),
    CONSTRAINT fk_measurement_source_version FOREIGN KEY (scenario_id, source_id, version)
        REFERENCES source_versions(scenario_id, source_id, version)
);
CREATE TABLE IF NOT EXISTS module_coverage (
    episode_id varchar(80) NOT NULL REFERENCES episodes(id),
    path varchar(250) NOT NULL,
    status varchar(40) NOT NULL,
    PRIMARY KEY (episode_id, path)
);
-- Metadati storici dei dataset esterni. La compilazione corrente legge il
-- contratto versionato incluso nel pacchetto, non questa tabella.
CREATE TABLE IF NOT EXISTS letter_templates (
    id varchar(100) PRIMARY KEY,
    version integer NOT NULL,
    unit_id varchar(80) NOT NULL REFERENCES clinical_units(id),
    fields jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS simulation_state (
    id integer PRIMARY KEY CHECK (id = 1),
    scenario_id varchar(40) NOT NULL REFERENCES scenarios(id),
    run_id varchar(36) NOT NULL,
    "current_time" timestamptz NOT NULL,
    revision integer NOT NULL
);
CREATE TABLE IF NOT EXISTS state_revisions (
    revision integer PRIMARY KEY,
    scenario_id varchar(40) NOT NULL REFERENCES scenarios(id),
    run_id varchar(36) NOT NULL,
    "current_time" timestamptz NOT NULL
);

-- Contratto documentale neutro: gli ID clinici ignoti restano null nel payload.
-- Separato dalle tabelle storiche, che richiedono anagrafica e date complete.
CREATE TABLE IF NOT EXISTS composition_contexts (
    id text PRIMARY KEY,
    payload jsonb NOT NULL,
    CHECK (payload->>'id' = id)
);
CREATE TABLE IF NOT EXISTS composition_sources (
    id text PRIMARY KEY,
    context_id text NOT NULL REFERENCES composition_contexts(id),
    payload jsonb NOT NULL,
    CHECK (payload->>'id' = id AND payload->>'context_id' = context_id)
);
CREATE INDEX IF NOT EXISTS ix_composition_source_context ON composition_sources(context_id);
