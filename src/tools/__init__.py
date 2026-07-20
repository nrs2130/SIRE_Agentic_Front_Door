"""L4 Tools: thin MCP wrappers over Vocera Engage adapters (mock simulators for the demo); I/O only."""

from .oncall_lookup import (
    OnCallAdapter,
    OnCallLookupResult,
    OnCallProvider,
    build_server,
    create_oncall_adapter,
    lookup_oncall,
)

__all__ = [
    "OnCallAdapter",
    "OnCallLookupResult",
    "OnCallProvider",
    "build_server",
    "create_oncall_adapter",
    "lookup_oncall",
]
