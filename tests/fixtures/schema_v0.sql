-- The spanlight schema at e52b962 (before versioned migrations), frozen
-- so tests can build a legacy database. Do not edit.

CREATE TABLE IF NOT EXISTS traces (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  started_at REAL NOT NULL,
  ended_at   REAL,
  status     TEXT NOT NULL DEFAULT 'ok',
  session    TEXT,
  metadata   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS traces_started_idx ON traces(started_at DESC);

CREATE TABLE IF NOT EXISTS spans (
  id            TEXT PRIMARY KEY,
  trace_id      TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
  parent_id     TEXT,
  name          TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT 'other',
  started_at    REAL NOT NULL,
  ended_at      REAL,
  status        TEXT NOT NULL DEFAULT 'ok',
  error         TEXT,
  model         TEXT,
  input_tokens  INTEGER,
  output_tokens INTEGER,
  cost_usd      REAL,
  input         TEXT,
  output        TEXT,
  attributes    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS spans_trace_idx ON spans(trace_id);
CREATE INDEX IF NOT EXISTS spans_model_idx ON spans(model) WHERE model IS NOT NULL;
