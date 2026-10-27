# 0002. A hand-written OTLP decoder, checked against the reference

Status: accepted

## Context

spanlight is about to accept OpenTelemetry traces over OTLP/HTTP.
Real exporters send binary protobuf (`application/x-protobuf`), and OTLP/JSON is useful for `curl` and for exporters that cannot do protobuf.
Both must be decoded into spanlight's own span model.

The `server` extra is deliberately small: FastAPI and uvicorn.
The obvious way to read protobuf is the `protobuf` package with the generated `opentelemetry-proto` classes.
That adds a runtime dependency that is large, pins a protobuf major version that can collide with the pins of the application being traced (spanlight is installed next to it), and brings generated code that changes between releases.
OTLP/JSON also cannot use protobuf's own JSON parser, because OTLP/JSON differs from proto3 JSON: trace and span ids are hex strings, not base64.

## Decision

Decode OTLP traces with code in this repository and no new runtime dependency.

- `spanlight/server/otlp/wire.py` is a strict protobuf wire reader: varint, fixed32, fixed64 and length-delimited fields.
- `spanlight/server/otlp/decode.py` has one decoder per encoding, and both produce the same frozen dataclasses (`model.py`), so mapping and storage never see the encoding.
- `spanlight/server/otlp/encode.py` writes `ExportTraceServiceResponse` and `google.rpc.Status` in both encodings.
- The decoder follows proto3 parsing rules: unknown fields and known fields with the wrong wire type are skipped, a repeated singular message is merged, the last member of a oneof wins, and strings must be valid UTF-8.
  Framing errors reject the whole request with `OtlpDecodeError`, and nothing is sliced or allocated until a length is known to fit the buffer.
- It is stricter than the reference in four places, all deliberate: group wire types are rejected, AnyValue nesting is capped at 16, and attribute, event and link counts and string lengths are capped (the OpenTelemetry SDK defaults), with the excess counted in the `dropped_*_count` fields.
  Per-span problems (an id of the wrong length or all zeros, no start time) do not fail the request; the span carries a `reject` reason, because OTLP answers those with a partial success.

Correctness comes from tests, not from the dependency.
`opentelemetry-proto`, `opentelemetry-sdk` and the OTLP exporter are dev dependencies, and `hypothesis` drives the checks:

- messages generated for every field are serialized by the reference and must decode to what the reference parses;
- random bytes, every truncation of valid messages, and mutations of them must be accepted or rejected exactly as the reference does, and never raise anything but `OtlpDecodeError`;
- unknown fields of each wire type are injected into every message type;
- the JSON decoder is checked against `MessageToDict` of the same messages.

`HYPOTHESIS_PROFILE=deep` or `soak` runs the same properties with many more examples.

## Rejected alternatives

- Depend on `protobuf` and `opentelemetry-proto` at runtime: simpler to write, but it ties every installation to a protobuf version, adds generated code to the trust surface of a service that parses untrusted input, and still needs a separate JSON path.
- Accept JSON only: the standard exporters, including doc-pilot's, send protobuf.
- A gRPC server: it needs `grpcio` and a second port for no current client.
- Skip group fields as the reference does: it keeps a little more compatibility with exotic senders, and costs a recursive skipper to bound; OTLP has never used groups.

## Consequences

- The server installs without protobuf, and the decoder is about a thousand lines we own and must keep correct when the OTLP schema gains fields.
  New fields are skipped as unknown until we map them, and the differential tests fail loudly if a reference release changes how existing fields parse.
- Field numbers are taken from the real proto package (1.45) and pinned by the tests that compare against it.
- Resource `entity_refs` and the string-index fields are accepted and ignored, but validated like the reference, so a malformed one still rejects the request.
- Decoding a request builds its whole object graph, so memory is proportional to the request size.
  The request size cap in the HTTP layer is what bounds it; the limits here bound how much of it is kept.
