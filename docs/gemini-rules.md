# Gemini (Vertex AI) and Langfuse usage rules — all snippets verified live on 2026-08-25

## Client (only in `llm/client.py`)
```python
from google import genai
from google.genai import types
client = genai.Client(vertexai=True, project=settings.google_cloud_project, location=settings.google_cloud_location)
```
Auth = Application Default Credentials (`gcloud auth application-default login`, account @griddynamics.com,
project `gd-gcp-gridu-genai`). The warning "authenticated using end user credentials ... without a quota project" is
harmless. No API keys anywhere.

## Models
- `gemini-2.5-flash` (default), `gemini-2.5-pro`, `gemini-2.5-flash-lite` respond. `gemini-2.0-flash-001`, `gemini-3*`,
  `gemini-flash-latest` → 404 on this project. Model id only via `Settings.gemini_model`.
- 2.5 models think by default and consume `max_output_tokens` doing so → `.text` is `None`. Always pass
  `thinking_config=types.ThinkingConfig(thinking_budget=settings.gemini_thinking_budget)` (default 0).

## Generation config
```python
cfg = types.GenerateContentConfig(
    temperature=temperature, max_output_tokens=max_tokens, system_instruction=system,
    thinking_config=types.ThinkingConfig(thinking_budget=0),
)
```
## Structured output (machine-consumed results)
```python
# dynamic JSON schema (per-table rows): nullable via {"type": ["string","null"]}, enums via "enum": [...]
cfg.response_mime_type = "application/json"; cfg.response_json_schema = schema_dict   # → json.loads(r.text)
# or a Pydantic model:
cfg.response_schema = MyModel                                                          # → r.parsed (MyModel)
```
Do NOT combine `tools` with JSON response mode in one request (Gemini rejects it) — charts are a tool call instead.
Measured: 25 rows × 6 cols ≈ 2.2k output tokens ≈ 12 s at temperature 0.9. Keep batches ≤ 50 rows / ≤ 8k tokens.

## Streaming (user-facing text)
```python
for chunk in client.models.generate_content_stream(model=m, contents=contents, config=cfg):
    if chunk.text: yield chunk.text
```
## Function calling (talk-to-data) — manual loop, AFC disabled
```python
run_sql = types.FunctionDeclaration(name="run_sql", description="Run one read-only SELECT ...",
    parameters=types.Schema(type=types.Type.OBJECT, properties={"sql": types.Schema(type=types.Type.STRING)}, required=["sql"]))
cfg.tools = [types.Tool(function_declarations=[run_sql, render_chart])]
cfg.automatic_function_calling = types.AutomaticFunctionCallingConfig(disable=True)
r = client.models.generate_content(model=m, contents=history, config=cfg)
for fc in r.function_calls or []:                      # fc.name, dict(fc.args)
    result = dispatch(fc.name, dict(fc.args))
    history.append(r.candidates[0].content)            # the model turn containing the call(s)
    history.append(types.Content(role="user", parts=[types.Part.from_function_response(name=fc.name, response=result)]))
# when r.function_calls is empty → stream the final answer with generate_content_stream(contents=history)
```
`client.chats.create(..., config=types.GenerateContentConfig(tools=[python_callable]))` + `send_message` does AFC
automatically (verified) — acceptable for tests, but the UI uses the manual loop to surface SQL/results and stream.
In google-genai ≥ 2.x AFC from `models.generate_content` is deprecated; use chats or the manual loop.

## Errors and retries
- `google.genai.errors.ClientError` (4xx: 404 model, 429 quota, 400 schema) / `ServerError` (5xx).
- Retry only 429 and 5xx: exponential backoff 1, 2, 4, 8, 16 s (+ jitter), max 5 attempts; log attempt + status.
- Concurrency ≤ `settings.llm_max_concurrency` (default 4) via `ThreadPoolExecutor`.
- Validate every structured response with pydantic; on validation error retry once with the error appended.

## Langfuse (v4, OpenTelemetry-based) — `observability.py`
```python
from langfuse import get_client, observe, propagate_attributes
from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor
lf = get_client()            # reads LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL from env
assert lf.auth_check()
GoogleGenAIInstrumentor().instrument()   # once per process → every Gemini call = GENERATION (model, usage)

@observe(name="data_generation")
def run(...):
    with propagate_attributes(session_id=sid, user_id=uid, tags=["data-generation"], metadata={...}):
        lf.update_current_span(input={...}); ...; lf.update_current_span(output={...})
lf.flush()   # after each user action (Streamlit is long-lived) so traces show up immediately
```
- `LANGFUSE_BASE_URL=https://cloud.langfuse.com` (EU). `LANGFUSE_HOST` also works but BASE_URL is the documented name.
- v4 has **no** `update_current_trace`; trace attributes go through `propagate_attributes`.
- Verified via API: trace `harness-probe-2` with `sessionId`, `userId`, `tags`, input/output and a nested
  GENERATION `GenerateContent` model `gemini-2.5-flash` usage total 6.
- Tracing must be optional: no keys → no instrumentation, no warnings in unit tests.

### Talk-to-data traces (F6.5, verified live)
- One trace per question: `observability.traced("talk_to_data_turn", session_id=<UI session id>, tags=["talk-to-data"], ...)`
  around `Agent.ask`; `current_trace_id()` is read inside the span and returned on the final/error event.
- Tool executions are child spans `tool.run_sql` / `tool.render_chart` (arguments as `arg_*` attributes). Do NOT pass
  `tags=` to child spans — Langfuse tags are trace-level and a child tag leaks onto the trace.
- GENERATIONs come from the openinference instrumentor (`GenerateContent`, `GenerateContentStream`); `flush()` after
  every turn so short-lived sessions still export.

### Streaming errors (F7.2, verified with the SDK source)
- `models.generate_content_stream` is a *generator function*: the HTTP request happens at the first `next()`, not at the
  call. `GeminiClient.stream_text` therefore fetches the first chunk inside `_call` (retries/backoff/classification apply)
  and wraps the remaining iteration in `classify_error` — callers only ever see `LLMError`.
- The agent turns *any* exception into an `error` event with a short explanation; pages catch `Exception` as a last
  resort and log it. Raw tracebacks must never reach the UI.

