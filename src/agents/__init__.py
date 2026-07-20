"""L3 Agents: one focused, single-responsibility agent per capability, deployed as Foundry hosted agents."""

from .comms import (
    COMMS_INTENTS,
    CommsAgent,
    CommsAgentResult,
    comms_hosted_agent_spec,
    create_comms_agent,
)
from .hosted import HostedAgentSpec, HostedMCPTool, register_hosted_agent
from .registry import agent_for_intent, register_agent, registrations
from .sepsis import (
    SEPSIS_INTENTS,
    Hour1Step,
    SepsisAgent,
    SepsisAgentResult,
    create_sepsis_agent,
    sepsis_hosted_agent_spec,
)

__all__ = [
    "CommsAgent",
    "CommsAgentResult",
    "COMMS_INTENTS",
    "create_comms_agent",
    "comms_hosted_agent_spec",
    "SepsisAgent",
    "SepsisAgentResult",
    "Hour1Step",
    "SEPSIS_INTENTS",
    "create_sepsis_agent",
    "sepsis_hosted_agent_spec",
    "HostedAgentSpec",
    "HostedMCPTool",
    "register_hosted_agent",
    "agent_for_intent",
    "register_agent",
    "registrations",
]
