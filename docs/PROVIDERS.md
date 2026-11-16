# Provider configuration

This guide connects providers using environment configuration and the existing AATM provider API. No source-code changes are required. It describes the current adapters, not a claim that an external account or model was exercised in this checkout.

## How AATM stores provider settings

AATM accepts these provider types: `arena_local_bridge`, `anthropic_claude`, and `openai_compatible` (plus the legacy `arena_agent` stub). OpenRouter and Ollama use the generic `openai_compatible` adapter; there are no separate OpenRouter or Ollama adapter types.

Provider credentials are **environment-variable references**, never inline values. For example, store `env:OPENROUTER_API_KEY` in the provider record and make `OPENROUTER_API_KEY` available to the backend process. The API rejects inline credential values. Keep `.env` out of Git, restrict its permissions (`chmod 600 .env`), and use a secret manager for deployments.

The seeded provider rows are `arena-local-bridge` and `anthropic-claude`. The `arena-agent` runtime is not implemented. Provider records can be created or updated through authenticated `POST /api/v1/providers`; the current Providers view has health-check and test controls but no create/edit form.

For local API examples, set `BASE` to the AATM backend URL and obtain an access token using an account configured in AATM. Do not use example/default credentials in a deployed system:

```bash
BASE=http://127.0.0.1:8000
TOKEN="$(curl -fsS "$BASE/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"email":"<AATM account email>","password":"<AATM account password>"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"
```

Replace the two angle-bracket values before running the command. The token is kept in the shell variable and is not written into a provider record.

## Arena Local Bridge

The bundled `bridge/` service is a loopback reference daemon. Its default address is **`127.0.0.1:4100`**; both `backend/app/core/config.py` and `bridge/server.py` use port `4100`. It authenticates with a Bearer token when a token is configured. It does not provide an interactive Arena account sign-in flow or a hosted model. If an OpenAI-compatible upstream is configured, completions are forwarded there; without one, the daemon returns a deterministic, zero-spend local reference response, not a real model completion.

1. Add these settings to the private `.env` used by both the backend and the bridge process:

   ```dotenv
   ARENA_BRIDGE_URL=http://127.0.0.1:4100
   ARENA_BRIDGE_HOST=127.0.0.1
   ARENA_BRIDGE_PORT=4100
   ARENA_BRIDGE_TOKEN=<generate a random token>
   ARENA_BRIDGE_REQUIRE_AUTH=true
   ```

   Generate a token locally (for example, `openssl rand -hex 24`) and put the result in `.env`; do not commit it. The same non-empty `ARENA_BRIDGE_TOKEN` must be visible to the bridge and AATM backend. `ARENA_BRIDGE_REQUIRE_AUTH=true` makes the bridge fail closed if the token is missing. The reference bridge binds to loopback by default; do not expose it directly to an untrusted network.

2. Configure an OpenAI-compatible upstream if you want actual model responses. The bridge posts to `<upstream-root>/v1/chat/completions`, so set the upstream root **without** a trailing `/v1`. Examples are `http://127.0.0.1:11434` for a local Ollama server or `https://openrouter.ai/api` for OpenRouter. If the upstream needs a key, set `ARENA_BRIDGE_UPSTREAM_API_KEY` in the same private environment. When no upstream is configured, `/health` still responds and completions use the local reference behavior described above; this does not test or provide model inference.

3. If starting the bridge from the repository, load the private environment and run the reference server:

   ```bash
   set -a
   . ./.env
   set +a
   .venv/bin/python -m bridge.server
   ```

   The AATM backend must also receive the same settings. Restart the backend after changing its environment. `scripts/ubuntu/start.sh` starts the backend and frontend; it does not start the bridge daemon.

4. Ping the authenticated daemon on port 4100:

   ```bash
   curl -fsS \
     -H "Authorization: Bearer ${ARENA_BRIDGE_TOKEN}" \
     http://127.0.0.1:4100/health
   ```

   HTTP 200 with `status: "ok"` confirms the bridge process and token path responded. The bridge's current `/health` endpoint does not probe upstream model readiness. A real completion request with an upstream model ID is the generation check and can incur provider charges; no `/ready` route is implemented by the bundled daemon.

5. The seeded AATM bridge provider uses id `arena-local-bridge`, endpoint `http://127.0.0.1:4100`, and credential reference `env:ARENA_BRIDGE_TOKEN`. Its seeded default model is `agent`, but the bridge forwards the selected model name unchanged. When proxying to Ollama or OpenRouter, update this provider's `default_model` and `available_models` to the exact model ID accepted by the upstream. The bridge health response advertises `arena-bridge-default`, `arena-reasoning-pro`, and `arena-fast`; these names are not proof that the configured upstream provides them. The seeded `agent` name is also only a forwarding label, not a built-in model.

Example authenticated upsert for an upstream model (replace `YOUR_UPSTREAM_MODEL` with a real model ID):

```bash
curl -fsS -X POST "$BASE/api/v1/providers" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "id":"arena-local-bridge",
    "name":"Arena Local Bridge",
    "provider_type":"arena_local_bridge",
    "endpoint_url":"http://127.0.0.1:4100",
    "credential_ref":"env:ARENA_BRIDGE_TOKEN",
    "default_model":"YOUR_UPSTREAM_MODEL",
    "available_models":["YOUR_UPSTREAM_MODEL"],
    "priority":1,
    "is_enabled":true,
    "daily_limit_usd":20.0
  }'
```

## Anthropic Claude API

The seeded row `anthropic-claude` already uses provider type `anthropic_claude`, endpoint `https://api.anthropic.com/v1`, and credential reference `env:ANTHROPIC_API_KEY` by default.

1. Put the API key in the backend's private environment as `ANTHROPIC_API_KEY` (or configure `ANTHROPIC_API_KEY_REF` to reference another environment-variable name), not in the database or a request body.
2. Choose a model ID currently enabled for your Anthropic account. Set the seeded provider row's `default_model` and `available_models` to that ID if it differs from the seed. The adapter sends Anthropic's `x-api-key` and `anthropic-version` headers.
3. Restart the backend so it receives the environment value. An authenticated `POST /api/v1/providers/health-check` checks the API's `/models` endpoint; generating a completion is a separate, potentially billable request.

Example row update (replace the model ID with one supported by your account):

```bash
curl -fsS -X POST "$BASE/api/v1/providers" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "id":"anthropic-claude",
    "name":"Anthropic Claude API",
    "provider_type":"anthropic_claude",
    "endpoint_url":"https://api.anthropic.com/v1",
    "credential_ref":"env:ANTHROPIC_API_KEY",
    "default_model":"YOUR_ANTHROPIC_MODEL_ID",
    "available_models":["YOUR_ANTHROPIC_MODEL_ID"],
    "priority":3,
    "is_enabled":true,
    "daily_limit_usd":20.0
  }'
```

## OpenRouter

OpenRouter uses `openai_compatible`; AATM sends a Bearer credential and calls `<endpoint_url>/models` for health and `<endpoint_url>/chat/completions` for generation.

1. Put the OpenRouter key in the backend's private environment as `OPENROUTER_API_KEY` and restart the backend.
2. Create a provider row with `provider_type: "openai_compatible"`, `endpoint_url: "https://openrouter.ai/api/v1"`, `credential_ref: "env:OPENROUTER_API_KEY"`, and the exact model ID enabled for your OpenRouter account.

## Ollama

The same `openai_compatible` adapter can use a local Ollama server that exposes its OpenAI-compatible `/v1` routes.

1. Start Ollama and make sure the requested model is present (`ollama list`; pull a model first if needed).
2. Use `endpoint_url: "http://127.0.0.1:11434/v1"`, `provider_type: "openai_compatible"`, and the exact Ollama model name as `default_model` and in `available_models`.
3. AATM requires every provider credential reference to resolve to a non-empty environment variable. Set `credential_ref: "env:OLLAMA_API_KEY"` and define `OLLAMA_API_KEY` in the backend environment. The adapter sends it as `Authorization: Bearer ...`; use a real secret if your endpoint requires one. A local Ollama endpoint may ignore this header, but keep the endpoint bound to loopback and do not assume it is safe to expose without authentication.

Use this body shape to create either an OpenRouter or Ollama row. Replace every `YOUR_...` value with an actual ID/name and set a unique provider `id`:

```bash
curl -fsS -X POST "$BASE/api/v1/providers" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "id":"YOUR_PROVIDER_ID",
    "name":"YOUR_PROVIDER_NAME",
    "provider_type":"openai_compatible",
    "endpoint_url":"YOUR_OPENAI_COMPATIBLE_BASE_URL",
    "credential_ref":"env:YOUR_CREDENTIAL_ENV_NAME",
    "default_model":"YOUR_MODEL_ID",
    "available_models":["YOUR_MODEL_ID"],
    "priority":4,
    "is_enabled":true,
    "daily_limit_usd":20.0
  }'
```

For OpenRouter, use the endpoint and credential reference above. For Ollama, use the loopback `/v1` endpoint and `env:OLLAMA_API_KEY` reference. Provider keys are never passed in this JSON.

## Checking a provider

- `POST /api/v1/providers/health-check` performs an adapter-specific probe. For OpenAI-compatible APIs and Anthropic, it checks `/models`. The bridge adapter tries `/ready` and falls back to `/health` on HTTP 404; the bundled bridge currently implements only `/health`, so this confirms daemon/auth reachability, not upstream model readiness. A passing health check is not a generation test.
- `POST /api/v1/providers/{provider_id}/test` defaults to `dry_run: true`. The current Providers view uses this dry-run mode, which intentionally sends no request and cannot prove connectivity.
- To send a real test completion, explicitly set `dry_run` to `false` in that authenticated request and choose a prompt/model. It may consume quota and incur charges; check the returned `provider_id` because the router may fail over to another enabled provider.

```bash
curl -fsS -X POST "$BASE/api/v1/providers/PROVIDER_ID/test" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Reply with a short connectivity check.","model":"YOUR_MODEL_ID","dry_run":false}'
```

## Important limits

A valid provider row and a green bridge `/health` do not prove that model generation is configured. The bundled bridge has no `/ready` endpoint: when no upstream is configured, it returns a deterministic local reference response rather than calling a model. The default `arena_agent` row reports that its runtime is not implemented. Real manager planning and employee execution require an enabled provider that can return a valid response for AATM's prompts. No provider key or model availability is bundled with this repository.
