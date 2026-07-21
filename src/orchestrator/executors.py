"""Executors (graph nodes) for the L2 orchestration workflow.

Built on Microsoft Agent Framework primitives (``Executor``, ``@handler``,
``WorkflowContext``, ``@executor``) — agent-framework==1.11.0, verified against
https://learn.microsoft.com/agent-framework/workflows/ (executors, edges,
concurrent orchestration). Documented patterns only; no invented symbols.

Each node threads ``correlation_id`` (via the envelope) into its logs. Progress is
emitted with ``ctx.yield_output(...)`` — designated *intermediate* at build time so
the orchestrator can stream it to the gateway's ``speak()`` while the run continues;
the two summary nodes are designated *output* and produce the final spoken summary.
"""

from __future__ import annotations

import asyncio
import logging

from agent_framework import Executor, WorkflowContext, executor, handler
from typing_extensions import Never

from src.gateway.intent_envelope import IntentEnvelope, Urgency
from src.telemetry import node_span

from .messages import (
    BranchResult,
    BranchTask,
    FastPathRequest,
    StandardPathRequest,
    StandardProgress,
)

logger = logging.getLogger("nightingale.orchestrator")


def summarize_branches(results: list[BranchResult]) -> str:
    """Custom aggregator: fold out-of-order branch results into 'done: …; pending: …'."""
    done = [r.branch for r in results if r.status == "done"]
    pending = [r.branch for r in results if r.status != "done"]
    parts: list[str] = []
    if done:
        parts.append("done: " + ", ".join(done))
    if pending:
        parts.append("pending: " + ", ".join(pending))
    return ("; ".join(parts) + ".") if parts else "nothing to report."


# --- Router -----------------------------------------------------------------
class RouterExecutor(Executor):
    """Deterministic switch on urgency. No LLM — pure dispatch, ~0 ms."""

    @handler
    async def route(
        self,
        envelope: IntentEnvelope,
        ctx: WorkflowContext[FastPathRequest | StandardPathRequest],
    ) -> None:
        cid = envelope.correlation_id
        with node_span("router", cid, urgency=envelope.urgency.value, intent=envelope.intent):
            if envelope.urgency is Urgency.EMERGENCY:
                logger.info("router->FAST correlation_id=%s intent=%s", cid, envelope.intent)
                await ctx.send_message(FastPathRequest(envelope))
            else:
                logger.info("router->STANDARD correlation_id=%s intent=%s", cid, envelope.intent)
                await ctx.send_message(StandardPathRequest(envelope))


# --- Placeholder agent (mock) ----------------------------------------------
# The 'echo/mock agent' archetype: proves routing + concurrency + streaming
# before real agents exist. /foundry-agent and /mcp-tool replace these nodes
# with AgentExecutor(Agent(FoundryChatClient(...))) + MCP tools, same wiring.
def make_mock_agent(
    *, id: str, name: str, action: str, delay: float, budget: float
):
    """Build a function-based executor that simulates one agent/tool branch.

    Emits an intermediate progress cue, then 'works' for ``delay`` seconds bounded
    by ``budget``. On budget breach it returns ``status='pending'`` (a 'still
    working on X' cue) instead of blocking the fan-in barrier.
    """

    @executor(id=id)
    async def run_branch(task: BranchTask, ctx: WorkflowContext[BranchResult, str]) -> None:
        cid = task.envelope.correlation_id
        with node_span(f"branch.{name}", cid, branch=name, budget_ms=budget * 1000):
            await ctx.yield_output(f"[{name}] {action}…")
            if delay > budget:
                await asyncio.sleep(budget)
                logger.info("branch PENDING name=%s correlation_id=%s", name, cid)
                await ctx.yield_output(f"Still working on {name}…")
                await ctx.send_message(
                    BranchResult(
                        task.envelope,
                        name,
                        "pending",
                        f"{name} exceeded {int(budget * 1000)}ms budget",
                    )
                )
            else:
                await asyncio.sleep(delay)
                logger.info("branch DONE name=%s correlation_id=%s", name, cid)
                await ctx.send_message(
                    BranchResult(task.envelope, name, "done", f"{name} {action} complete")
                )

    return run_branch


# --- Fast path (emergency) --------------------------------------------------
class FastPathDispatch(Executor):
    """Acknowledge by voice FIRST, then fan out to concurrent branches.

    The spoken acknowledgment is yielded in this (earlier) superstep, so it is
    always emitted before any slow branch — the non-negotiable emergency rule.
    Full hardening lands in /emergency-fastpath.
    """

    @handler
    async def dispatch(
        self, req: FastPathRequest, ctx: WorkflowContext[BranchTask, str]
    ) -> None:
        env = req.envelope
        ack = f"Starting {env.intent.replace('_', ' ')} now; paging the team."
        await ctx.yield_output(ack)  # spoken acknowledgment (intermediate)
        logger.info("fast-path ack correlation_id=%s", env.correlation_id)
        await ctx.send_message(BranchTask(env))  # broadcast to fan-out branches


class FastSummary(Executor):
    """Fan-in barrier + custom aggregator for the final spoken summary."""

    @handler
    async def summarize(
        self, results: list[BranchResult], ctx: WorkflowContext[Never, str]
    ) -> None:
        cid = results[0].correlation_id if results else "?"
        summary = "Emergency response — " + summarize_branches(results)
        logger.info("fast-path summary correlation_id=%s", cid)
        await ctx.yield_output(summary)


# --- Standard path (routine) ------------------------------------------------
class StandardEnrich(Executor):
    """Kick off Patient Context + on-call lookup concurrently (fan-out)."""

    @handler
    async def enrich(
        self, req: StandardPathRequest, ctx: WorkflowContext[BranchTask, str]
    ) -> None:
        env = req.envelope
        with node_span("std_enrich", env.correlation_id, urgency=env.urgency.value):
            await ctx.yield_output(
                f"Looking up patient context and on-call for {env.intent.replace('_', ' ')}…"
            )
            logger.info("standard enrich correlation_id=%s", env.correlation_id)
            await ctx.send_message(BranchTask(env))


class StandardResolve(Executor):
    """Fan-in the enrichers, then resolve the entity via SIRE (mock here)."""

    @handler
    async def resolve(
        self, results: list[BranchResult], ctx: WorkflowContext[StandardProgress, str]
    ) -> None:
        env = results[0].envelope
        with node_span("std_resolve", env.correlation_id):
            await ctx.yield_output("Resolving entity via SIRE…")
            logger.info("standard resolve correlation_id=%s", env.correlation_id)
            prog = StandardProgress(env, [r.detail for r in results])
            prog.notes.append("SIRE resolved entity (mock)")
            await ctx.send_message(prog)


class StandardAct(Executor):
    """Act on the resolved entity (comms). Mock; real comms via /mcp-tool."""

    @handler
    async def act(
        self, prog: StandardProgress, ctx: WorkflowContext[StandardProgress, str]
    ) -> None:
        with node_span("std_act", prog.correlation_id):
            await ctx.yield_output("Placing the call…")
            logger.info("standard act correlation_id=%s", prog.correlation_id)
            prog.notes.append("comms placed (mock)")
            await ctx.send_message(prog)


class StandardSummary(Executor):
    """Speak a read-back confirmation (terminal output)."""

    @handler
    async def summarize(
        self, prog: StandardProgress, ctx: WorkflowContext[Never, str]
    ) -> None:
        env = prog.envelope
        with node_span("std_summary", env.correlation_id):
            readback = f"Confirmed: {env.intent.replace('_', ' ')}"
            if env.entities:
                readback += " for " + ", ".join(f"{k} {v}" for k, v in env.entities.items())
            readback += ". " + "; ".join(prog.notes) + "."
            logger.info("standard summary correlation_id=%s", env.correlation_id)
            await ctx.yield_output(readback)
