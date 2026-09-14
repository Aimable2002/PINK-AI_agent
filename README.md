# Backend — build & test report

Scope: v1 trading-strategy agent (MT5 + GitHub + Linear only, per locked
scope decision). MODE flag switches model backends (dev=OpenRouter,
prod=RunPod) without touching any other code.

## Structure

```
app/
  config.py                 MODE flag, tier configs (incl. classifier), thresholds, safety caps
  main.py                   FastAPI app assembly only — routes live in api/

  api/                      HTTP layer — request/response handling only
    schemas.py               Pydantic request/response models
    dependencies.py           get_current_user (FastAPI Depends)
    routes_chat.py            /v1/chat, /v1/chat/{job_id}

  core/                      the agent brain — no HTTP awareness
    router.py                 LLM-as-classifier tier selection
    llm_client.py             LiteLLM wrapper, MODE-aware
    agent_runtime.py          the reasoning loop: call model -> tool calls -> repeat

  connectors/                MCP layer
    manager.py                generic MCP client (MT5/GitHub/Linear agnostic)

  queue/                     background job infrastructure
    celery_app.py             paid_priority / free_standard queue config
    tasks.py                   Celery tasks wrapping the agent loop

  data/                       persistence layer
    supabase_client.py         auth verification + quota lookup

tests/                        mirrors app/ structure
  fake_mcp_server.py           stand-in MCP server used only for real protocol tests
  test_config.py
  test_api/       test_core/       test_connectors/     test_queue/    test_data/
  47 tests total, all passing
```

## What's genuinely proven (real infra, not mocked)

- **MODE flag**: confirmed it actually redirects the real `litellm.acompletion`
  call target between OpenRouter-shaped and RunPod-shaped configs, and
  fails closed in prod without a RunPod URL set.
- **Classifier tier is its own MODE-aware config entry** (`config.py`),
  independent from the small tier — separate RunPod URL/key in prod,
  fails closed on its own if unset. Confirmed setting one doesn't
  satisfy the other.
- **Router (LLM-as-classifier)**: a cheap classifier-tier model call rates
  each prompt's difficulty and cuts it into 3 tiers. This needs no
  separate hosting, no local checkpoint, no Hugging Face/OpenAI
  dependency — it's just another call through the same call_tier()
  every tier uses. Response-parsing robustness (garbage output,
  out-of-range values, missing content) and threshold-cutting logic are
  tested against a faked classifier response, the same pattern used for
  llm_client tests.
- **MCP protocol layer**: tested against a real subprocess MCP server over
  actual stdio handshake (list_tools, call_tool) — not a mock of the client.
- **Agent loop**: tool-call → result → final-answer flow, connector
  permission enforcement (rejects tools from unconnected apps), and the
  max-iteration safety cap — all exercised with a fake model response,
  since no real model API key exists in this environment. Tier
  selection is dependency-injected (`select_tier_fn`) so this loop's own
  mechanics are tested independently of the classifier's real network call.
- **Redis + Celery**: a real Redis server and a real Celery worker were
  run in this environment. Jobs were enqueued and processed end-to-end
  with an actual result returned. Confirmed `paid_priority` and
  `free_standard` are genuinely separate queues.
- **FastAPI endpoints**: auth gate, quota-exceeded 429, plan-based queue
  dispatch — tested via FastAPI's TestClient with the Supabase auth
  dependency overridden (see below for why).

## What could not be tested here, and needs validation at deployment

1. **Live Supabase project** — no real `SUPABASE_URL`/`SUPABASE_SERVICE_KEY`
   exist here, so `data/supabase_client.py`'s actual network calls (JWT
   verification, `profiles` table lookup) are untested against a real
   project. It's confirmed to fail closed (raises, doesn't silently
   proceed) without credentials — but the real auth/quota round-trip
   needs a live Supabase project to validate.
2. **Real MT5/GitHub/Linear MCP servers** — the connector layer is proven
   against a fake stand-in server, not the real ones. Connecting the
   actual MT5, GitHub, and Linear MCP servers (credentials, real tool
   names/schemas) is the next real integration step, not yet done.
3. **Separate worker-pool starvation guarantee** — queue separation is
   proven; the specific claim "a paid surge can't starve free-tier
   throughput" requires deploying `paid_priority` and `free_standard` as
   two independently-scaled worker processes (commands documented in
   `queue/celery_app.py`), which wasn't set up as two separate pools in
   this test — only as one combined pool for simplicity.
4. **OpenRouter model slugs** — `config.py` uses Qwen3.6 / DeepSeek V4
   Flash / GLM-5.2 slugs based on current model research, not verified
   against a live OpenRouter account in this environment. Confirm exact
   slug strings at openrouter.ai/models before deploying.

## Running it

```
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
redis-server --daemonize yes
celery -A app.queue.celery_app worker -Q paid_priority --concurrency=6 -n paid@%h &
celery -A app.queue.celery_app worker -Q free_standard --concurrency=2 -n free@%h &
uvicorn app.main:app --reload
```

Set `MODE=prod` plus `RUNPOD_*_URL`/`RUNPOD_*_KEY` env vars (including
`RUNPOD_CLASSIFIER_URL`/`RUNPOD_CLASSIFIER_KEY`) to flip to production
inference. `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` required for real auth.
`OPENROUTER_API_KEY` required for dev-mode model calls.
