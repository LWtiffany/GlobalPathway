# Agent2 LLM boundary

Agent2 uses an LLM in two places and keeps recommendation decisions deterministic:

1. `UserUnderstandingPort` extracts intents and explicit facts from free text.
2. RDS data, eligibility rules, borrowing costs, ranking, and follow-up selection remain code-owned.
3. `AnswerWriterPort` rewrites an already gated answer for readability.
4. The application validates model output and falls back to the deterministic result on any failure.

The initial user question may call `UserUnderstandingPort`. Once Agent2 has issued follow-up questions, all
answers use `input_mode=structured_selection`: they update the Profile through the existing validation path and
skip both model and regex fact extraction. The final answer may still use the writer.

## Provider

The current provider is Alibaba Cloud Model Studio through its OpenAI-compatible Chat Completions endpoint.
The SDK is only the protocol client; requests go to `AGENT2_LLM_BASE_URL`.

Default configuration:

```text
AGENT2_LLM_MODEL=deepseek-v4.1-flash
AGENT2_LLM_STRUCTURED_MODE=json_object
AGENT2_LLM_EXTRA_BODY={"enable_thinking":false}
```

The default uses provider-side JSON Object plus application-side Pydantic and business validation.
The 2026-11-07 workspace probe measured DeepSeek understanding at 5.608 seconds versus
Qwen3.8 Flash at 8.166 seconds; both completed the structured benchmark. This is one
sample, not a production latency guarantee. Agent2 caps each model call at 30 seconds
without SDK retries and falls back to its deterministic answer on failure.

`qwen3.8-flash` can be selected when provider-side JSON Schema is preferred:

```text
AGENT2_LLM_MODEL=qwen3.8-flash
AGENT2_LLM_STRUCTURED_MODE=json_schema
```

Secrets belong in ignored `local_secrets.py` as `AGENT2_LLM_API_KEY` and `AGENT2_LLM_BASE_URL`, or in the
same-named environment variables. No secret is stored in prompts, answer snapshots, traces, or Git.
The understanding prompt receives only the current message and field catalog. The writer receives the verified
recommendation object, without the complete Profile.

## Prompts

Jinja templates live in `agent2/llm/prompt_templates/`. `PromptRenderer` uses `StrictUndefined`, so a missing
template variable fails before an API request. Prompt versions are recorded in the immutable answer snapshot
and `answer_runs` audit row.

## Validation and fallback

Understanding candidates must pass all of these checks:

- registered Profile field whitelist;
- confidence at least `0.80`;
- exact evidence substring from the current user message;
- existing field value validator.

The writer may only replace `summary` and `steps`. Materials and limitations remain byte-for-byte locked, and
new numeric claims are rejected. On timeout, provider error, invalid JSON, schema failure, or application gate
failure, the workflow returns the deterministic answer and adds an `llm_fallback` notice.

## Tests

Each boundary has its own test location:

- `tests/llm/test_prompts.py`: external Jinja loading and missing variables;
- `tests/llm/test_provider.py`: Alibaba-compatible JSON Object and JSON Schema transport;
- `tests/llm/test_ports.py`: understanding and writer prompt contracts;
- `tests/llm/test_validation.py`: fact and answer safety gates;
- `tests/llm/test_factory.py`: environment composition;
- `tests/llm/test_workflow.py`: call routing, structured-choice bypass, audit versions, and fallback.

Run them without provider traffic:

```powershell
python -m unittest discover -s tests/llm -t . -v
```

The test model is scripted and records calls. Real provider behavior belongs in a separately invoked smoke test,
so normal unit tests are deterministic and do not spend quota.

Run the real follow-up conversation replay after disconnecting the VPN:

```powershell
python scripts\test_agent2_followups.py
```

It tests the structured follow-up sequence, checks LLM routing and Profile updates, times every turn, and writes
a JSON report under `data/agent2/eval/`.
