"""L4 Tools: thin MCP wrappers over Vocera Engage adapters (mock simulators for the demo); I/O only."""

from .comms_page import (
    CommsAdapter,
    PageReceipt,
    create_comms_adapter,
    send_page,
)
from .comms_page import build_server as build_comms_server
from .oncall_lookup import (
    OnCallAdapter,
    OnCallLookupResult,
    OnCallProvider,
    create_oncall_adapter,
    lookup_oncall,
)
from .oncall_lookup import build_server as build_oncall_server

__all__ = [
    # oncall_lookup
    "OnCallAdapter",
    "OnCallLookupResult",
    "OnCallProvider",
    "build_oncall_server",
    "create_oncall_adapter",
    "lookup_oncall",
    # comms_page
    "CommsAdapter",
    "PageReceipt",
    "build_comms_server",
    "create_comms_adapter",
    "send_page",
]
