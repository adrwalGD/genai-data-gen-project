import logging

from genai_data_gen_project import observability
from genai_data_gen_project.config import Settings


def test_init_without_keys_is_disabled_and_idempotent(monkeypatch) -> None:
    monkeypatch.setattr(observability, "_state", {"initialized": False, "enabled": False})
    s = Settings(_env_file=None, langfuse_public_key=None, langfuse_secret_key=None)
    assert observability.init_observability(s) is False
    assert observability.init_observability(s) is False
    assert observability.tracing_enabled() is False
    observability.flush()  # no-op, must not raise
    assert observability.current_trace_id() is None


def test_configure_logging_is_idempotent() -> None:
    observability.configure_logging(logging.DEBUG)
    handlers = len(logging.getLogger().handlers)
    observability.configure_logging(logging.INFO)
    assert len(logging.getLogger().handlers) == handlers
    assert logging.getLogger("httpx").level == logging.WARNING
