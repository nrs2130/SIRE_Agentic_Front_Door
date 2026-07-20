"""CLI to register an L3 agent as a Foundry hosted agent.

    python -m src.agents.register --capability comms --dry-run
    python -m src.agents.register --capability comms          # real (needs Azure + endpoint)

``--dry-run`` prints the plan and touches no Azure. A real run uses DefaultAzureCredential
and the FOUNDRY_PROJECT_ENDPOINT from config/env.
"""

from __future__ import annotations

import argparse

from config import FoundryConfig

from .comms import comms_hosted_agent_spec
from .hosted import HostedAgentSpec, register_hosted_agent
from .sepsis import sepsis_hosted_agent_spec

# Capability -> hosted-agent spec factory. New agents add themselves here.
_SPECS: dict[str, callable] = {
    "comms": comms_hosted_agent_spec,
    "sepsis": sepsis_hosted_agent_spec,
}


def _build_spec(capability: str) -> HostedAgentSpec:
    try:
        return _SPECS[capability]()
    except KeyError:
        raise SystemExit(
            f"Unknown capability {capability!r}. Available: {', '.join(sorted(_SPECS))}"
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Register a Foundry hosted agent.")
    parser.add_argument("--capability", required=True, help="e.g. comms")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan without calling Azure (default off).",
    )
    args = parser.parse_args(argv)

    spec = _build_spec(args.capability)
    foundry = FoundryConfig.from_env()
    register_hosted_agent(
        spec, project_endpoint=foundry.project_endpoint, dry_run=args.dry_run
    )


if __name__ == "__main__":
    main()
