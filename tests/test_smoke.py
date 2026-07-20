"""Smoke tests that verify the scaffold is wired up correctly."""

from __future__ import annotations


def test_scaffold_imports() -> None:
    """Every source layer package imports cleanly."""
    import src.agents  # noqa: F401
    import src.gateway  # noqa: F401
    import src.knowledge  # noqa: F401
    import src.orchestrator  # noqa: F401
    import src.telemetry  # noqa: F401
    import src.tools  # noqa: F401

    assert True


def test_config_loads_from_env(mock_env: None) -> None:
    """AppConfig reads everything from env, including the new latency budgets."""
    from config import AppConfig

    config = AppConfig.from_env()

    # Existing SIRE config still resolves.
    assert config.voicelive.model == "gpt-realtime"
    assert config.search.endpoint.endswith(".search.windows.net")
    # New scaffold config: emergency acknowledgment budget default (docs §4).
    assert config.budgets.spoken_ack_ms == 300
    assert config.telemetry.service_name == "nightingale"
