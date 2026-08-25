"""Langfuse (v4, OpenTelemetry) + OpenInference google-genai instrumentation, and logging setup.

Tracing is optional: when Langfuse keys are missing nothing is instrumented and no warnings are emitted,
so unit tests and offline runs stay quiet. See docs/gemini-rules.md → Langfuse.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from typing import Any

from .config import Settings, get_settings

_log = logging.getLogger(__name__)
_state: dict[str, Any] = {"initialized": False, "enabled": False}


def configure_logging(level: int = logging.INFO) -> None:
    """Idempotent stdlib logging setup (format includes logger name for layer attribution)."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "urllib3", "google_genai", "opentelemetry"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def init_observability(settings: Settings | None = None) -> bool:
    """Initialise Langfuse and instrument the google-genai SDK once per process.

    Returns True when tracing is active. Safe to call repeatedly (e.g. from Streamlit reruns).
    """
    if _state["initialized"]:
        return bool(_state["enabled"])
    _state["initialized"] = True
    settings = settings or get_settings()
    if not settings.langfuse_enabled:
        os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "false")
        _log.info("Langfuse tracing disabled (no keys configured)")
        return False

    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key or "")
    os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key or "")
    os.environ.setdefault("LANGFUSE_BASE_URL", settings.langfuse_base_url)
    try:
        from langfuse import get_client
        from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor

        client = get_client()
        if not client.auth_check():
            _log.warning("Langfuse auth_check failed for %s — tracing disabled", settings.langfuse_base_url)
            return False
        GoogleGenAIInstrumentor().instrument()
    except Exception:  # pragma: no cover - defensive: observability must never break the app
        _log.exception("Langfuse initialisation failed — tracing disabled")
        return False
    _state["enabled"] = True
    _log.info("Langfuse tracing enabled (%s)", settings.langfuse_base_url)
    return True


def tracing_enabled() -> bool:
    return bool(_state["enabled"])


def flush() -> None:
    """Flush pending spans (call after each user action; Streamlit processes are long-lived)."""
    if not _state["enabled"]:
        return
    from langfuse import get_client

    get_client().flush()


def current_trace_id() -> str | None:
    if not _state["enabled"]:
        return None
    from langfuse import get_client

    return get_client().get_current_trace_id()


@contextmanager
def traced(
    name: str, *, session_id: str | None = None, tags: list[str] | None = None, **metadata: Any
) -> Iterator[None]:
    """Open a Langfuse span named `name` with session/tags/metadata; a no-op when tracing is disabled."""
    if not _state["enabled"]:
        with nullcontext():
            yield
        return
    from langfuse import get_client, propagate_attributes

    client = get_client()
    clean = {k: str(v)[:500] for k, v in metadata.items() if v is not None}
    with client.start_as_current_observation(as_type="span", name=name):
        if session_id is None and tags is None:
            # child span: keep attributes on this observation (propagate_attributes leaks them to the trace)
            if clean:
                client.update_current_span(metadata=clean)
            yield
            return
        attrs: dict[str, Any] = {"tags": tags or []}
        if session_id:
            attrs["session_id"] = session_id
        with propagate_attributes(**attrs, metadata=clean):
            yield
