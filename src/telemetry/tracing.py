"""OpenTelemetry tracer access for orchestrator/tool latency spans (L5).

Thin wrapper over the OpenTelemetry API (opentelemetry-api==1.44.0, pinned in
requirements.txt). ``get_tracer`` returns a tracer from the globally configured provider;
with no provider installed it is a **no-op** tracer, so importing this and creating spans is
safe with zero setup (local dev / tests). Full Azure Monitor export wiring lands in
/observability; this module gives the fast path a place to record per-branch latency spans now.

Span attributes use the ``nightingale.*`` namespace (:data:`ATTR_PREFIX`) so the Control Plane
can filter Nightingale spans (correlation_id, urgency, branch, latency_ms).
"""

from __future__ import annotations

from opentelemetry import trace  # opentelemetry-api==1.44.0

ATTR_PREFIX = "nightingale"


def get_tracer(name: str = "nightingale") -> trace.Tracer:
    """Return an OpenTelemetry tracer (no-op until a provider is configured)."""
    return trace.get_tracer(name)
