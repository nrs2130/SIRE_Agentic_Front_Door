"""OpenTelemetry setup + span helpers for Nightingale (L5, docs/01-architecture.md §6).

Gives every orchestrator node and every MCP tool call a span carrying ``correlation_id``,
``urgency``, the node/tool name, and **measured latency** — so a single run is one trace you can
follow gateway → orchestrator → agents → tools, and the **Foundry Control Plane** story lands
(see infra/CONTROL_PLANE.md).

Configuration (:func:`configure_telemetry`, driven by :class:`config.TelemetryConfig`):
* if ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is set → the **Azure Monitor OpenTelemetry
  Distro** exports traces to Application Insights, which the Foundry portal reads under its
  **Tracing** tab (verified against
  https://learn.microsoft.com/azure/azure-monitor/app/opentelemetry-enable and
  https://learn.microsoft.com/azure/ai-foundry/concepts/trace, fetched 2026-07-20),
* else if console export is on → a local ``ConsoleSpanExporter`` (great for the workshop / CI),
* else → a **no-op** tracer, so importing this and creating spans is always safe with zero setup.

All Azure/SDK imports are **lazy** (inside :func:`configure_telemetry`) so local dev and tests
need no exporter installed.

Pinned (requirements.txt): opentelemetry-api/sdk==1.43.0, azure-monitor-opentelemetry==1.8.9
(the distro requires opentelemetry-sdk~=1.43.0). Span attributes use the ``nightingale.*``
namespace (:data:`ATTR_PREFIX`).
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from opentelemetry import trace  # opentelemetry-api==1.43.0

from config import TelemetryConfig

logger = logging.getLogger("nightingale.telemetry")

ATTR_PREFIX = "nightingale"

_CONFIGURED = False


def get_tracer(name: str = "nightingale") -> trace.Tracer:
    """Return an OpenTelemetry tracer (no-op until :func:`configure_telemetry` runs)."""
    return trace.get_tracer(name)


def is_configured() -> bool:
    """True once a real tracer provider (Azure Monitor or console) has been installed."""
    return _CONFIGURED


def configure_telemetry(config: TelemetryConfig | None = None, *, force: bool = False) -> bool:
    """Install the global tracer provider from ``config``. Idempotent; returns True if a real
    exporter was configured.

    Precedence mirrors the Azure Monitor distro guidance: a connection string wins (exports to
    Application Insights → Foundry Control Plane); otherwise console export if requested; else
    no-op.
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return True
    config = config or TelemetryConfig.from_env()

    if config.connection_string:
        # Azure Monitor OpenTelemetry Distro — azure-monitor-opentelemetry==1.8.9.
        from azure.monitor.opentelemetry import configure_azure_monitor  # noqa: PLC0415

        # Scope log collection to our namespace (docs: pass logger_name so we don't collect
        # logging telemetry from the SDK itself, which otherwise triggers a recursive-logging
        # warning against the root logger).
        configure_azure_monitor(
            connection_string=config.connection_string, logger_name=ATTR_PREFIX
        )
        _CONFIGURED = True
        logger.info("telemetry: Azure Monitor exporter configured (service=%s)", config.service_name)
        return True

    if config.console_export:
        # Local console tracing for the workshop / CI (opentelemetry-sdk==1.44.0).
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import (  # noqa: PLC0415
            ConsoleSpanExporter,
            SimpleSpanProcessor,
        )

        provider = TracerProvider(
            resource=Resource.create({"service.name": config.service_name})
        )
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(provider)
        _CONFIGURED = True
        logger.info("telemetry: console exporter configured (service=%s)", config.service_name)
        return True

    logger.info("telemetry: no exporter configured (no-op tracer)")
    return False


@contextmanager
def node_span(
    node: str,
    correlation_id: str | None,
    *,
    urgency: str | None = None,
    tracer: trace.Tracer | None = None,
    **attributes: Any,
) -> Iterator[trace.Span]:
    """Span for one orchestrator node: tags correlation_id/urgency/node + measured latency."""
    tr = tracer or get_tracer("nightingale.orchestrator")
    t0 = time.perf_counter()
    with tr.start_as_current_span(f"orchestrator.node.{node}") as span:
        span.set_attribute(f"{ATTR_PREFIX}.correlation_id", correlation_id or "")
        span.set_attribute(f"{ATTR_PREFIX}.node", node)
        if urgency:
            span.set_attribute(f"{ATTR_PREFIX}.urgency", urgency)
        for key, value in attributes.items():
            span.set_attribute(f"{ATTR_PREFIX}.{key}", value)
        try:
            yield span
        finally:
            span.set_attribute(
                f"{ATTR_PREFIX}.latency_ms", round((time.perf_counter() - t0) * 1000, 2)
            )


_T = TypeVar("_T")


def traced_tool(
    tool_name: str,
) -> Callable[[Callable[..., Awaitable[_T]]], Callable[..., Awaitable[_T]]]:
    """Decorator: wrap an async MCP-tool entry point in a ``tool.<name>`` span.

    Reads ``correlation_id`` from kwargs so the tool span nests under the orchestrator/branch
    span for the same run (single trace). Span is a no-op until telemetry is configured, and
    the wrapper preserves the wrapped signature.
    """

    def decorator(fn: Callable[..., Awaitable[_T]]) -> Callable[..., Awaitable[_T]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> _T:
            cid = kwargs.get("correlation_id")
            tr = get_tracer("nightingale.tools")
            t0 = time.perf_counter()
            with tr.start_as_current_span(f"tool.{tool_name}") as span:
                span.set_attribute(f"{ATTR_PREFIX}.correlation_id", cid or "")
                span.set_attribute(f"{ATTR_PREFIX}.tool", tool_name)
                try:
                    return await fn(*args, **kwargs)
                finally:
                    span.set_attribute(
                        f"{ATTR_PREFIX}.latency_ms",
                        round((time.perf_counter() - t0) * 1000, 2),
                    )

        return wrapper

    return decorator
