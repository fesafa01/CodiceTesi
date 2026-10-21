-- Contesti e fonti documentali. init preserva le tabelle e i dati già presenti.
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
