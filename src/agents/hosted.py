"""Foundry hosted-agent registration helpers (shared by all L3 agents).

Declares an agent in **Foundry Agent Service** so it appears as a hosted agent in the
**Control Plane**, attaching its L4 MCP tools as **hosted MCP tools** ("bring your own MCP
server endpoint"). Pattern follows
https://learn.microsoft.com/agent-framework/agents/tools/hosted-mcp-tools and
https://learn.microsoft.com/azure/foundry/ (fetched 2026-07-20): a declarative agent +
MCP tool definitions (``server_label`` / ``server_url`` / allowed tools), created via
``AIProjectClient`` with ``DefaultAzureCredential``.

SDK: ``azure-ai-projects==2.3.0`` (pinned in requirements.txt; hosted agents are GA in
2.3.0). Azure imports are **lazy** and only touched on a real (non-dry-run) registration,
so importing this module (and running agents locally against mocks) needs no Azure.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("nightingale.agents.hosted")


@dataclass(frozen=True)
class HostedMCPTool:
    """One MCP server endpoint attached to a hosted agent (bring-your-own-MCP)."""

    server_label: str  # unique id for the MCP server instance
    server_url: str  # URL of the hosted MCP server endpoint
    allowed_tools: tuple[str, ...]  # which tools from that server the agent may call
    require_approval: str = "never"  # never | always (tool-call approval workflow)
    # Foundry project connection id, required for a Foundry IQ knowledge-base MCP tool
    # (RemoteTool/ProjectManagedIdentity connection). None for plain bring-your-own MCP.
    project_connection_id: str | None = None


def _hosted_tool_plan(t: HostedMCPTool) -> dict:
    """Serialize one hosted MCP tool for the plan (omits unset optional fields)."""
    plan = {
        "type": "mcp",
        "server_label": t.server_label,
        "server_url": t.server_url,
        "allowed_tools": list(t.allowed_tools),
        "require_approval": t.require_approval,
    }
    if t.project_connection_id is not None:
        plan["project_connection_id"] = t.project_connection_id
    return plan


@dataclass(frozen=True)
class HostedAgentSpec:
    """Everything needed to create one Foundry hosted agent."""

    name: str
    model: str  # model deployment name in the Foundry project
    instructions: str
    mcp_tools: tuple[HostedMCPTool, ...] = field(default_factory=tuple)

    def to_plan(self, project_endpoint: str | None) -> dict:
        """A JSON-serializable description of what registration would create."""
        return {
            "action": "create_hosted_agent",
            "project_endpoint": project_endpoint or "<FOUNDRY_PROJECT_ENDPOINT unset>",
            "agent": {
                "name": self.name,
                "model": self.model,
                "instructions": self.instructions,
                "tools": [_hosted_tool_plan(t) for t in self.mcp_tools],
            },
            "auth": "DefaultAzureCredential",
        }


def register_hosted_agent(
    spec: HostedAgentSpec,
    *,
    project_endpoint: str | None,
    dry_run: bool = True,
) -> dict:
    """Create ``spec`` as a Foundry hosted agent, or (dry run) print the plan.

    Returns the plan dict either way. In dry-run mode nothing touches Azure. A real run
    lazily imports the Foundry SDK and creates the agent version; the exact create call is
    marked for verification against the current SDK.
    """
    plan = spec.to_plan(project_endpoint)
    if dry_run:
        logger.info("[dry-run] would create hosted agent %r", spec.name)
        print(json.dumps(plan, indent=2))
        return plan

    if not project_endpoint:
        raise ValueError(
            "FOUNDRY_PROJECT_ENDPOINT is required for a real registration (set it in .env)."
        )

    # --- Real registration (lazy Azure imports; no Azure needed for dry-run/local) ---
    from azure.ai.projects import AIProjectClient  # noqa: PLC0415  (azure-ai-projects==2.3.0)
    from azure.ai.projects.models import (  # noqa: PLC0415
        MCPTool,
        PromptAgentDefinition,
    )
    from azure.identity import DefaultAzureCredential  # noqa: PLC0415

    logger.info("creating hosted agent %r in %s", spec.name, project_endpoint)
    with DefaultAzureCredential() as credential, AIProjectClient(
        endpoint=project_endpoint, credential=credential
    ) as client:
        # Declarative (prompt) agent: model + instructions + MCP tool defs. Verified pattern
        # (azure-ai-projects>=2.0.0; repo pins 2.3.0) from
        # https://learn.microsoft.com/azure/foundry/agents/how-to/foundry-iq-connect
        # (fetched 2026-07-21). A Foundry IQ knowledge-base tool additionally needs a RemoteTool
        # project connection (ProjectManagedIdentity) created via ARM BEFORE this call — see
        # infra/deploy.ps1 / src/knowledge/ingest.py kb_connection_plan().
        # TODO: verify MCPTool/PromptAgentDefinition/create_version signatures against the
        # installed azure-ai-projects==2.3.0 (doc examples can lag the SDK); adjust kwargs if so.
        tools = []
        for t in spec.mcp_tools:
            kwargs: dict = {
                "server_label": t.server_label,
                "server_url": t.server_url,
                "require_approval": t.require_approval,
                "allowed_tools": list(t.allowed_tools),
            }
            if t.project_connection_id is not None:
                kwargs["project_connection_id"] = t.project_connection_id
            tools.append(MCPTool(**kwargs))

        agent = client.agents.create_version(
            agent_name=spec.name,
            definition=PromptAgentDefinition(
                model=spec.model, instructions=spec.instructions, tools=tools
            ),
        )
        version = getattr(agent, "version", None)
        logger.info("hosted agent %r created (version=%s)", spec.name, version)
        return {**plan, "created": True, "version": version}
