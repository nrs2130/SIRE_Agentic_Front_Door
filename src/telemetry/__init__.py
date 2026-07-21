"""L5 Telemetry: OpenTelemetry setup, correlation, and latency spans feeding the Foundry Control Plane."""

from .tracing import ATTR_PREFIX, get_tracer

__all__ = ["get_tracer", "ATTR_PREFIX"]
