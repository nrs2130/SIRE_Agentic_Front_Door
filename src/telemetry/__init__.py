"""L5 Telemetry: OpenTelemetry setup, correlation, and latency spans feeding the Foundry Control Plane."""

from .run_summary import BranchTiming, RunSummary
from .tracing import (
    ATTR_PREFIX,
    configure_telemetry,
    get_tracer,
    is_configured,
    node_span,
    traced_tool,
)

__all__ = [
    "get_tracer",
    "ATTR_PREFIX",
    "configure_telemetry",
    "is_configured",
    "node_span",
    "traced_tool",
    "RunSummary",
    "BranchTiming",
]
