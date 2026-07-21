"""Observability tests: single-run trace (gateway→orchestrator→agents→tools) + run summary.

Uses an in-memory OpenTelemetry span exporter (no Azure) to assert the trace shape and the
correlation_id threading; asserts the compact run summary for a routine and an emergency run.
"""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from config import ToolsConfig
from src.gateway import IntentEnvelope, TextStubGateway, Urgency
from src.orchestrator import FastPath, Orchestrator
from src.telemetry import BranchTiming, RunSummary, configure_telemetry

_FAST_TOOLS = ToolsConfig(
    use_real_adapter=False, mock_latency_ms=5, mock_jitter_ms=0, timeout_ms=3000
)


@pytest.fixture
def span_exporter():
    """Capture spans via an in-memory exporter.

    The Agent Framework installs a global SDK tracer provider at import (OpenTelemetry only
    allows setting it once), so we attach our exporter to the existing provider rather than
    replacing it; if none is installed we install one.
    """
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if hasattr(provider, "add_span_processor"):
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:  # pragma: no cover - only if no SDK provider is present
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    exporter.clear()
    yield exporter
    exporter.clear()


def _emergency() -> IntentEnvelope:
    return IntentEnvelope.create(
        "sepsis_screen", Urgency.EMERGENCY, {"patient_ref": "bed 12"}, "bed 12 looks septic"
    )


def _routine() -> IntentEnvelope:
    return IntentEnvelope.create(
        "contact_provider", Urgency.ROUTINE, {"role": "cardiologist"}, "call the cardiologist"
    )


def _fast_orch(gateway: TextStubGateway) -> Orchestrator:
    return Orchestrator(gateway, fast_path=FastPath(gateway, tools=_FAST_TOOLS))


# --- Single trace: gateway → orchestrator → fast path → branches → tools ------
async def test_emergency_run_is_one_correlated_trace(span_exporter) -> None:
    """One emergency run produces conversation→fastpath→branch→tool spans, one correlation_id."""
    gateway = TextStubGateway()
    env = _emergency()
    await _fast_orch(gateway).handle(env)

    spans = span_exporter.get_finished_spans()
    names = {s.name for s in spans}
    # The nesting the demo shows off.
    assert "conversation" in names
    assert "fastpath" in names
    assert any(n.startswith("fastpath.branch.") for n in names)
    assert any(n.startswith("tool.") for n in names)  # e.g. tool.comms_page

    # Every Nightingale span carries the SAME correlation_id (single trace, one run).
    run_spans = [s for s in spans if "nightingale.correlation_id" in s.attributes]
    cids = {s.attributes["nightingale.correlation_id"] for s in run_spans}
    assert cids == {env.correlation_id}

    # All of the run's spans share one trace id (gateway→orchestrator→tools is one trace).
    # (The Agent Framework emits a separate ``workflow.build`` span at graph-construction time;
    # it isn't part of the run, so we scope to spans carrying our correlation_id.)
    trace_ids = {s.context.trace_id for s in run_spans}
    assert len(trace_ids) == 1


async def test_tool_spans_carry_latency_and_nest_under_branches(span_exporter) -> None:
    """Each MCP tool call has a tool.<name> span with a measured latency, nested in the run."""
    gateway = TextStubGateway()
    await _fast_orch(gateway).handle(_emergency())
    spans = {s.name: s for s in span_exporter.get_finished_spans()}

    tool_spans = [s for n, s in spans.items() if n.startswith("tool.")]
    assert tool_spans, "expected at least one MCP tool span"
    for s in tool_spans:
        assert "nightingale.latency_ms" in s.attributes
        assert "nightingale.tool" in s.attributes
    # comms_page (escalation) tool was invoked.
    assert "tool.comms_page" in spans


async def test_ack_latency_on_fastpath_span(span_exporter) -> None:
    """The emergency acknowledgment latency is recorded as a span attribute."""
    gateway = TextStubGateway()
    await _fast_orch(gateway).handle(_emergency())
    spans = {s.name: s for s in span_exporter.get_finished_spans()}
    assert "nightingale.ack_latency_ms" in spans["fastpath"].attributes


async def test_routine_run_has_node_spans(span_exporter) -> None:
    """A routine run traces the router + standard-path nodes (no fast path)."""
    gateway = TextStubGateway()
    await Orchestrator(gateway, branch_delay=0.01).handle(_routine())
    names = {s.name for s in span_exporter.get_finished_spans()}
    assert "conversation" in names
    assert "orchestrator.node.router" in names
    assert "orchestrator.node.std_summary" in names
    assert "fastpath" not in names


# --- Run summary --------------------------------------------------------------
async def test_emergency_run_summary(span_exporter) -> None:
    """The emergency run summary reports routing, branch latencies, and no breaches."""
    gateway = TextStubGateway()
    result = await _fast_orch(gateway).handle(_emergency())

    rs = result.run_summary
    assert rs is not None and rs.path == "fast"
    assert rs.ack_latency_ms is not None and rs.ack_budget_ms == 300
    assert {b.name for b in rs.branches} == {"comms", "labs", "knowledge", "context"}
    assert rs.breaches == []  # fast tools, nothing over budget
    text = result.run_summary_text
    assert "path=fast" in text and "branches (concurrent):" in text
    assert "budget breaches: none" in text


async def test_routine_run_summary(span_exporter) -> None:
    """The routine run summary reports the standard path with a total time."""
    gateway = TextStubGateway()
    result = await Orchestrator(gateway, branch_delay=0.01).handle(_routine())
    rs = result.run_summary
    assert rs is not None and rs.path == "standard"
    assert rs.total_ms is not None
    assert "path=standard" in result.run_summary_text


def test_run_summary_flags_budget_breach() -> None:
    """A branch over budget (or pending) is flagged in the summary's breaches."""
    rs = RunSummary(
        correlation_id="cid-1", path="fast", intent="sepsis_screen", urgency="EMERGENCY",
        ack_latency_ms=0.1, ack_budget_ms=300,
        branches=[
            BranchTiming("comms", 250, 800, "done"),
            BranchTiming("blood_bank", 100, 100, "pending"),  # pending → breach
        ],
    )
    assert rs.breaches == ["blood_bank"]
    out = rs.format()
    assert "⚠pending" in out
    assert "budget breaches: blood_bank" in out


# --- Configuration ------------------------------------------------------------
def test_configure_telemetry_console(monkeypatch) -> None:
    """configure_telemetry installs a console exporter when requested (no Azure)."""
    from config import TelemetryConfig

    cfg = TelemetryConfig(service_name="nightingale-test", connection_string=None, console_export=True)
    # Reset the module flag so this test drives a fresh configure.
    import src.telemetry.tracing as tracing

    monkeypatch.setattr(tracing, "_CONFIGURED", False)
    assert tracing.configure_telemetry(cfg, force=True) is True


def test_configure_telemetry_noop_without_exporter() -> None:
    """With neither a connection string nor console export, configuration is a no-op."""
    from config import TelemetryConfig

    import src.telemetry.tracing as tracing

    cfg = TelemetryConfig(service_name="n", connection_string=None, console_export=False)
    # force=True bypasses the idempotency guard so we test the no-op branch directly.
    assert tracing.configure_telemetry(cfg, force=True) is False
