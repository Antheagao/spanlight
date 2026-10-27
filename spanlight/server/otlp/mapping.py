"""Map decoded OTLP spans onto spanlight's span model.

Spans are mapped one at a time, so the result for a span never depends on the
rest of the request or on when the other spans of its trace arrive. What the
trace looks like is derived later, from the stored spans (db.ingest_otlp).

The LLM-specific columns (model, tokens, cost) are filled only for LLM spans.
Instrumentation often puts a rolled-up cost on a parent such as
`invoke_agent`; mapping it as an LLM span would count that cost twice.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from spanlight.sdk import estimate_cost

from .model import ExportRequest, Span
from .values import json_safe

OPERATION_NAME = "gen_ai.operation.name"
LLM_OPERATIONS = frozenset({"chat", "text_completion", "generate_content"})
OPERATION_KINDS = {"execute_tool": "tool", "retrieval": "retrieval"}

# Opt-in message content, stored in the span's input and output columns.
INPUT_MESSAGES = "gen_ai.input.messages"
OUTPUT_MESSAGES = "gen_ai.output.messages"

# OTLP SpanKind and Status.StatusCode numbers.
SPAN_KIND_NAMES = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER",
                   5: "CONSUMER"}
STATUS_CODE_ERROR = 2

# The most distinct reject reasons spelled out in a partial-success message.
MAX_REASONS_LISTED = 5

# The native API's limit on a span's name.
MAX_NAME_CHARS = 512


@dataclass(frozen=True)
class Context:
    """A resource or a scope, identified by the digest of its content. It is
    stored once per trace, however many spans share it."""

    digest: str
    data: dict


def _context(data: dict) -> Context:
    safe = json_safe(data)
    canonical = json.dumps(
        safe, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False
    )
    return Context(hashlib.sha256(canonical.encode()).hexdigest(), safe)


@dataclass
class MappedBatch:
    spans: list[dict] = field(default_factory=list)
    rejected: int = 0
    # Why spans were rejected, with counts, for the partial-success message.
    reasons: Counter = field(default_factory=Counter)

    @property
    def message(self) -> str:
        if not self.rejected:
            return ""
        listed = [f"{reason} ({count})" for reason, count in
                  self.reasons.most_common(MAX_REASONS_LISTED)]
        return f"rejected {self.rejected} span(s): " + "; ".join(listed)


def map_request(request: ExportRequest, cost_attributes: tuple[str, ...]) -> MappedBatch:
    batch = MappedBatch()
    for resource_spans in request.resource_spans:
        resource = resource_spans.resource
        resource_context = None
        for scope_spans in resource_spans.scope_spans:
            scope = scope_spans.scope
            scope_context = None
            for span in scope_spans.spans:
                if span.reject is not None:
                    batch.rejected += 1
                    batch.reasons[span.reject] += 1
                    continue
                # Built once per resource and scope, not per span: they can be big.
                if resource_context is None:
                    resource_context = _context(
                        {
                            "attributes": resource.attributes,
                            "dropped_attributes_count": resource.dropped_attributes_count,
                            "schema_url": resource_spans.schema_url,
                        }
                    )
                if scope_context is None:
                    scope_context = _context(
                        {
                            "name": scope.name,
                            "version": scope.version,
                            "attributes": scope.attributes,
                            "dropped_attributes_count": scope.dropped_attributes_count,
                            "schema_url": scope_spans.schema_url,
                        }
                    )
                batch.spans.append(map_span(span, resource_context, scope_context, cost_attributes))
    return batch


def _seconds(unix_nano: int) -> float:
    return unix_nano / 1_000_000_000


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _count(value: Any) -> int | None:
    """A non-negative whole number (bool is not one). Some instrumentation
    writes token counts as floats, such as 12.0."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _money(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    cost = float(value)
    return cost if math.isfinite(cost) and cost >= 0 else None


def _kind(attributes: dict) -> str:
    operation = attributes.get(OPERATION_NAME)
    if operation in LLM_OPERATIONS:
        return "llm"
    return OPERATION_KINDS.get(operation, "other") if isinstance(operation, str) else "other"


def _llm_fields(attributes: dict, cost_attributes: tuple[str, ...]) -> tuple[dict, str | None]:
    """The model, token and cost columns of an LLM span, and where the cost
    came from (`attribute:<name>`, `price_table`, or None)."""
    model = _text(attributes.get("gen_ai.response.model")) or _text(
        attributes.get("gen_ai.request.model")
    )
    input_tokens = _count(attributes.get("gen_ai.usage.input_tokens"))
    if input_tokens is None:
        input_tokens = _count(attributes.get("gen_ai.usage.prompt_tokens"))
    output_tokens = _count(attributes.get("gen_ai.usage.output_tokens"))
    if output_tokens is None:
        output_tokens = _count(attributes.get("gen_ai.usage.completion_tokens"))
    cost, source = None, None
    # The application knows cache and batch pricing; spanlight does not.
    for name in cost_attributes:
        cost = _money(attributes.get(name))
        if cost is not None:
            source = f"attribute:{name}"
            break
    if cost is None:
        cost = estimate_cost(model, input_tokens, output_tokens)
        source = "price_table" if cost is not None else None
    columns = {
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost,
    }
    return columns, source


def _error_text(span: Span) -> str | None:
    """The message of an ERROR span: its status message, else what its last
    `exception` event recorded."""
    if span.status.message:
        return span.status.message
    for event in reversed(span.events):
        if event.name == "exception":
            kind = _text(event.attributes.get("exception.type"))
            message = _text(event.attributes.get("exception.message"))
            if kind and message:
                return f"{kind}: {message}"
            return kind or message
    return None


def _payload(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(json_safe(value), allow_nan=False, ensure_ascii=False)


def map_span(
    span: Span,
    resource: Context,
    scope: Context,
    cost_attributes: tuple[str, ...],
) -> dict:
    """One accepted span as a row for `db.ingest_otlp`. Its resource and scope
    travel as `contexts`, which are not span columns."""
    attributes = dict(span.attributes)
    kind = _kind(attributes)
    columns = {"model": None, "input_tokens": None, "output_tokens": None, "cost_usd": None}
    cost_source = None
    if kind == "llm":
        columns, cost_source = _llm_fields(attributes, cost_attributes)
    input_text = _payload(attributes.pop(INPUT_MESSAGES, None))
    output_text = _payload(attributes.pop(OUTPUT_MESSAGES, None))

    started_at = _seconds(span.start_time_unix_nano)
    ended_at = None
    if span.end_time_unix_nano:
        # An end before the start is clock skew; a negative duration would
        # corrupt every latency percentile.
        ended_at = max(_seconds(span.end_time_unix_nano), started_at)
    failed = span.status.code == STATUS_CODE_ERROR

    otel: dict[str, Any] = {
        "span_kind": SPAN_KIND_NAMES.get(span.kind, span.kind),
        "resource": resource.digest,
        "scope": scope.digest,
        "trace_state": span.trace_state,
        "flags": span.flags,
        "dropped_attributes_count": span.dropped_attributes_count,
        "dropped_events_count": span.dropped_events_count,
        "dropped_links_count": span.dropped_links_count,
    }
    if cost_source is not None:
        otel["cost_source"] = cost_source
    return {
        "id": span.span_id.hex(),
        "trace_id": span.trace_id.hex(),
        "parent_id": span.parent_span_id.hex() if span.parent_span_id else None,
        "name": (span.name or "(unnamed)")[:MAX_NAME_CHARS],
        "kind": kind,
        "started_at": started_at,
        "ended_at": ended_at,
        "status": "error" if failed else "ok",
        "error": _error_text(span) if failed else None,
        "input": input_text,
        "output": output_text,
        "attributes": json_safe(attributes),
        "service": _text(resource.data["attributes"].get("service.name")),
        "contexts": {"resource": resource, "scope": scope},
        "events": json_safe(
            [
                {
                    "time": _seconds(event.time_unix_nano),
                    "name": event.name,
                    "attributes": event.attributes,
                    "dropped_attributes_count": event.dropped_attributes_count,
                }
                for event in span.events
            ]
        ),
        "links": json_safe(
            [
                {
                    "trace_id": link.trace_id.hex(),
                    "span_id": link.span_id.hex(),
                    "trace_state": link.trace_state,
                    "attributes": link.attributes,
                    "dropped_attributes_count": link.dropped_attributes_count,
                    "flags": link.flags,
                }
                for link in span.links
            ]
        ),
        "otel": json_safe(otel),
        **columns,
    }
