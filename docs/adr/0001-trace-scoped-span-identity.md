# 0001. Trace-scoped span identity

Status: accepted

## Context

Until now a span was identified by its `id` alone, and the ingest upsert was `ON CONFLICT(id) DO UPDATE SET` every column, including `trace_id`.
That was safe while only the SDK wrote spans, because it generates a fresh 32-hex id per span.
OpenTelemetry only guarantees that a span id (8 bytes, 16 hex characters) is unique within its trace.
Once spans arrive from other tracers, two traces can legitimately contain a span with the same id.
With the old key, the second one would silently overwrite the first and move it into the other trace.

## Decision

A span is identified by `(trace_id, id)`.
The `spans` table is rebuilt in migration 0002 with `PRIMARY KEY (trace_id, id)`, and the upsert uses `ON CONFLICT(trace_id, id)` and no longer writes `trace_id`.
The rebuild also adds `CHECK` constraints for `kind` and `status` so the closed sets are enforced in the schema as well as in the API.
The column keeps the name `id`, so the SDK payload, the API and the dashboard are unchanged.

## Rejected alternatives

- A composite string id such as `"<trace_id>:<span_id>"`: it keeps a single-column key, but every reader would have to build and parse it, and the API's `id` would stop matching the id the tracer reported.
- Keeping `id` as the key and rejecting a conflicting `trace_id`: it turns a spec-legal input into an error, and a legitimate collision would drop spans.
- A surrogate integer key plus a unique index on `(trace_id, id)`: it adds a column and an index for no query that needs it.

## Consequences

- The same span id in two traces is stored as two spans.
- A resend of a span updates it in place and can never move it to another trace.
- The primary key leads with `trace_id`, so it also serves the per-trace lookups, and the old `spans_trace_idx` is not recreated.
- SQLite cannot change a primary key in place, so the migration copies the table.
  It runs in one transaction after a backup of the database, and the copy time grows with the number of spans.
- Anything that refers to a span by `id` alone must also carry the trace id, for example a span id in a link or a score.
