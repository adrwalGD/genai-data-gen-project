#!/usr/bin/env python3
"""Verify the runtime environment with actionable PASS/FAIL lines (docs/environment.md, docs/testing.md).

Checks: Python version, ADC credentials file, Vertex AI generate_content with the configured model, Langfuse
auth + a flushed trace named `check_env`, PostgreSQL connectivity. Exit code 1 if any check fails.
Run via `make check-env` (uses the project virtualenv).
"""

from __future__ import annotations

import os
import platform
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from genai_data_gen_project import observability  # noqa: E402
from genai_data_gen_project.config import Settings  # noqa: E402

RESULTS: list[tuple[str, bool]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name:<9} {detail}")


def redact(url: str) -> str:
    parts = urlsplit(url)
    if parts.password:
        netloc = parts.netloc.replace(f":{parts.password}@", ":***@")
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url


def check_python() -> None:
    ok = sys.version_info[:2] == (3, 14)
    record(
        "python",
        ok,
        platform.python_version()
        + ("" if ok else " — expected 3.14: `uv python install 3.14 && make setup`"),
    )


def check_adc() -> bool:
    path = Path(
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    )
    ok = path.exists()
    record(
        "adc",
        ok,
        str(path)
        if ok
        else f"{path} missing — run `gcloud auth application-default login` "
        "(griddynamics.com account, project gd-gcp-gridu-genai); in Docker mount ~/.config/gcloud to /gcloud",
    )
    return ok


def check_vertex(settings: Settings) -> None:
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(
            vertexai=True, project=settings.google_cloud_project, location=settings.google_cloud_location
        )
        started = time.perf_counter()
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents="Reply with exactly the single word: pong",
            config=types.GenerateContentConfig(
                temperature=0,
                max_output_tokens=16,
                thinking_config=types.ThinkingConfig(thinking_budget=settings.gemini_thinking_budget),
            ),
        )
        elapsed = time.perf_counter() - started
        text = response.text or ""
        ok = "pong" in text.lower()
        record(
            "vertex",
            ok,
            f"{settings.gemini_model} @ {settings.google_cloud_location} → {text.strip()!r} in {elapsed:.1f}s"
            if ok
            else f"unexpected reply {text!r} — empty text usually means thinking consumed the budget: "
            "set GEMINI_THINKING_BUDGET=0",
        )
    except Exception as exc:
        message = str(exc).strip().splitlines()[0][:220] if str(exc).strip() else repr(exc)
        if "404" in message:
            hint = "model not found/retired — set GEMINI_MODEL=gemini-2.5-flash in .env"
        elif any(k in message for k in ("401", "invalid_grant", "Reauthentication", "expired")):
            hint = "credentials expired — run `gcloud auth application-default login`"
        elif "403" in message:
            hint = "no permission on project gd-gcp-gridu-genai — use your griddynamics.com account"
        elif "429" in message:
            hint = "quota exhausted — retry in a minute; lower LLM_MAX_CONCURRENCY"
        else:
            hint = "see docs/environment.md → Troubleshooting"
        record("vertex", False, f"{type(exc).__name__}: {message} — {hint}")


def check_langfuse(settings: Settings) -> None:
    if not settings.langfuse_enabled:
        record(
            "langfuse",
            True,
            "keys not configured — tracing disabled (optional); `make env` injects them from creds.local",
        )
        return
    enabled = observability.init_observability(settings)
    if not enabled:
        record(
            "langfuse",
            False,
            f"auth failed at {settings.langfuse_base_url} — these keys belong to the EU cloud "
            "(LANGFUSE_BASE_URL=https://cloud.langfuse.com); verify LANGFUSE_PUBLIC_KEY/SECRET_KEY in .env",
        )
        return
    from langfuse import get_client, observe

    client = get_client()

    @observe(name="check_env")
    def probe() -> str | None:
        client.update_current_span(
            input={"check": "env"}, output={"ok": True}, metadata={"host": platform.node()}
        )
        return client.get_current_trace_id()

    trace_id = probe()
    client.flush()
    record(
        "langfuse", bool(trace_id), f"auth ok; trace `check_env` id={trace_id} → {settings.langfuse_base_url}"
    )


def check_postgres(settings: Settings) -> None:
    try:
        import psycopg

        with psycopg.connect(settings.database_url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("SELECT version()")
            row = cur.fetchone()
        version = (row[0] if row else "unknown").split(",")[0]
        record("postgres", True, f"{version} at {redact(settings.database_url)}")
    except Exception as exc:
        message = str(exc).strip().splitlines()[0][:200]
        record(
            "postgres",
            False,
            f"{type(exc).__name__}: {message} — run `make db-up` (or fix DATABASE_URL in .env; "
            "inside docker the host is `postgres`)",
        )


def main() -> int:
    settings = Settings()
    print(
        f"check-env — project={settings.google_cloud_project} model={settings.gemini_model} "
        f"langfuse={'on' if settings.langfuse_enabled else 'off'} db={redact(settings.database_url)}"
    )
    check_python()
    if check_adc():
        check_vertex(settings)
    else:
        record("vertex", False, "skipped — no ADC credentials")
    check_langfuse(settings)
    check_postgres(settings)
    failed = [name for name, ok in RESULTS if not ok]
    print()
    if failed:
        print(f"check-env: FAIL ({', '.join(failed)}) — fix the lines above, then re-run `make check-env`")
        return 1
    print("check-env: all PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
