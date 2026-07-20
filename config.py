"""
Configuration module for SIRE Voice Agent.
Loads environment variables and provides typed config access.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv(override=True)


@dataclass(frozen=True)
class VoiceLiveConfig:
    """Azure VoiceLive API configuration."""
    endpoint: str
    api_key: str | None
    model: str
    voice: str
    use_token_credential: bool

    @classmethod
    def from_env(cls) -> "VoiceLiveConfig":
        api_key = os.getenv("AZURE_VOICELIVE_API_KEY")
        use_token = os.getenv("AZURE_VOICELIVE_USE_TOKEN", "false").lower() == "true"
        return cls(
            endpoint=os.environ["AZURE_VOICELIVE_ENDPOINT"],
            api_key=api_key,
            model=os.getenv("AZURE_VOICELIVE_MODEL", "gpt-realtime"),
            voice=os.getenv("AZURE_VOICELIVE_VOICE", "en-US-Ava:DragonHDLatestNeural"),
            use_token_credential=use_token or not api_key,
        )


@dataclass(frozen=True)
class SearchConfig:
    """Azure AI Search configuration."""
    endpoint: str
    api_key: str
    group_index: str
    user_index: str
    api_version: str

    @classmethod
    def from_env(cls) -> "SearchConfig":
        return cls(
            endpoint=os.environ["AZURE_SEARCH_ENDPOINT"],
            api_key=os.environ["AZURE_SEARCH_API_KEY"],
            group_index=os.getenv("AZURE_SEARCH_GROUP_INDEX", "group-slot-mapping-index"),
            user_index=os.getenv("AZURE_SEARCH_USER_INDEX", "user-slot-mapping-index"),
            api_version=os.getenv("AZURE_SEARCH_API_VERSION", "2024-07-01"),
        )


@dataclass(frozen=True)
class FoundryConfig:
    """Microsoft Foundry project configuration (hosted agents, Foundry IQ)."""
    project_endpoint: str | None
    model_deployment_name: str
    agent_name: str | None
    use_token_credential: bool

    @classmethod
    def from_env(cls) -> "FoundryConfig":
        # Foundry is optional locally: the orchestrator can run against mock MCP
        # tools without a live project, so we never require the endpoint at import.
        use_token = os.getenv("FOUNDRY_USE_TOKEN", "true").lower() == "true"
        return cls(
            project_endpoint=os.getenv("FOUNDRY_PROJECT_ENDPOINT") or None,
            model_deployment_name=os.getenv("FOUNDRY_MODEL_NAME", "gpt-realtime"),
            agent_name=os.getenv("FOUNDRY_AGENT_NAME") or None,
            use_token_credential=use_token,
        )


@dataclass(frozen=True)
class LatencyBudgets:
    """Per-node soft timeouts (milliseconds) for the orchestrator.

    Defaults mirror the budget table in docs/01-architecture.md §4. On breach a
    node emits a "still working on X" intermediate event rather than blocking.
    """
    spoken_ack_ms: int          # emergency acknowledgment — must always be met
    router_ms: int
    comms_tool_ms: int
    labs_tool_ms: int
    knowledge_ms: int
    patient_context_ms: int

    @classmethod
    def from_env(cls) -> "LatencyBudgets":
        def _ms(name: str, default: int) -> int:
            return int(os.getenv(name, str(default)))

        return cls(
            spoken_ack_ms=_ms("BUDGET_SPOKEN_ACK_MS", 300),
            router_ms=_ms("BUDGET_ROUTER_MS", 10),
            comms_tool_ms=_ms("BUDGET_COMMS_TOOL_MS", 800),
            labs_tool_ms=_ms("BUDGET_LABS_TOOL_MS", 2000),
            knowledge_ms=_ms("BUDGET_KNOWLEDGE_MS", 1000),
            patient_context_ms=_ms("BUDGET_PATIENT_CONTEXT_MS", 700),
        )


@dataclass(frozen=True)
class TelemetryConfig:
    """OpenTelemetry / Azure Monitor configuration for Control Plane observability."""
    service_name: str
    connection_string: str | None
    console_export: bool

    @classmethod
    def from_env(cls) -> "TelemetryConfig":
        return cls(
            service_name=os.getenv("OTEL_SERVICE_NAME", "nightingale"),
            connection_string=os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING") or None,
            console_export=os.getenv("OTEL_CONSOLE_EXPORT", "false").lower() == "true",
        )


@dataclass(frozen=True)
class ToolsConfig:
    """L4 MCP tool configuration (mock adapters + realistic latency simulation).

    ``use_real_adapter`` flips a tool from its demo mock to the real Vocera Engage
    adapter without a code change. ``mock_latency_ms`` / ``mock_jitter_ms`` make the
    mock's latency demoable; ``timeout_ms`` bounds every tool call so a slow adapter
    surfaces as a typed timeout result instead of hanging the orchestrator.
    """
    use_real_adapter: bool
    mock_latency_ms: int
    mock_jitter_ms: int
    timeout_ms: int

    @classmethod
    def from_env(cls) -> "ToolsConfig":
        return cls(
            use_real_adapter=os.getenv("TOOLS_USE_REAL_ADAPTER", "false").lower() == "true",
            mock_latency_ms=int(os.getenv("TOOLS_MOCK_LATENCY_MS", "250")),
            mock_jitter_ms=int(os.getenv("TOOLS_MOCK_JITTER_MS", "150")),
            timeout_ms=int(os.getenv("TOOLS_TIMEOUT_MS", "3000")),
        )


@dataclass(frozen=True)
class AppConfig:
    """Top-level application configuration."""
    voicelive: VoiceLiveConfig
    search: SearchConfig
    foundry: FoundryConfig
    budgets: LatencyBudgets
    telemetry: TelemetryConfig
    tools: ToolsConfig

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            voicelive=VoiceLiveConfig.from_env(),
            search=SearchConfig.from_env(),
            foundry=FoundryConfig.from_env(),
            budgets=LatencyBudgets.from_env(),
            telemetry=TelemetryConfig.from_env(),
            tools=ToolsConfig.from_env(),
        )
