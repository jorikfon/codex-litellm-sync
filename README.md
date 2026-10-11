# codex-litellm-sync

Builds a [Codex CLI](https://github.com/openai/codex) model catalog from a LiteLLM proxy, so
`/model` lists what your LiteLLM key can actually use.

Codex has no plugin hook for models. It takes a static catalog through `model_catalog_json` in
`config.toml`, and it wants its own format (`{"models": [ModelInfo…]}` with reasoning levels,
context window and the agent prompt), not OpenAI's `/v1/models`. This script reads LiteLLM's
`GET /model_group/info` (limits, vision, reasoning levels), keeps only the models
`GET /model/info` lists for your key — so a model the key may not call does not show up only to
answer 403 — and writes that file. If `/model/info` fails or is empty, every group is kept.

## Use

```sh
export LITELLM_MMR_KEY=…            # the same variable as env_key of the provider
python3 codex_litellm_sync.py        # preview
python3 codex_litellm_sync.py --write
```

`--write` writes `~/.codex/litellm-models.json` and adds `model_catalog_json = "…"` to
`~/.codex/config.toml` if it isn't there. Options: `--url` (default `$LITELLM_URL` or
`https://litellm.miko.ru`), `--key-env`, `--out`, `--config`, `--default-context` (128000).

The provider itself is configured once by hand:

```toml
model_provider = "litellm"

[model_providers.litellm]
name = "LiteLLM"
base_url = "https://litellm.miko.ru/v1"
wire_api = "responses"
env_key = "LITELLM_MMR_KEY"
```

## What to know

- **The catalog replaces Codex's bundled list** (it becomes a static, authoritative catalog; the
  ChatGPT `/models` refresh stops). Models not in LiteLLM disappear from `/model`.
- Codex rejects a model without instructions, so every model gets the agent prompt of the first
  listed bundled model (from `codex debug models --bundled`; `codex` must be on `PATH`). A slug
  Codex already ships (e.g. `gpt-6-luna`) keeps its bundled entry as is.
- Embedding/moderation groups are skipped; `mode: null` groups are kept.
- Reasoning levels are exactly LiteLLM's `supported_reasoning_efforts`. A model gets none when
  LiteLLM reports `supports_reasoning: false` or its deployments pin reasoning off
  (`reasoning_effort: "none"`, `enable_thinking: false`, `thinking.type: "disabled"` in
  `litellm_params`, as `/model/info` shows them; only these fields are read).
- An empty level list does **not** stop Codex from sending an effort: for a non-OpenAI provider
  it sends whatever the session or `model_reasoning_effort` in `config.toml` says, unchecked
  against the model. A global `model_reasoning_effort` therefore reaches models without levels,
  and a client effort can override a deployment's pinned `reasoning_effort: "none"` (seen on
  DeepSeek, not on Qwen's `enable_thinking: false`). Prefer choosing the effort per model in
  `/model`, or pin reasoning off on the LiteLLM side in a way the client cannot override.
- The file is regenerated from scratch on each run — edit LiteLLM, not the file.
- Codex sends `client_metadata` and a `reasoning` object on every request, whatever the catalog
  says; each LiteLLM deployment must accept or drop them (`additional_drop_params`).
- Check the result with `codex debug models`.

## Troubleshooting

**`SSL: CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`** — your Python's CA
bundle is older than the proxy's certificate chain. Typical case: python.org Python 3.11 ships
`certifi` without *ISRG Root YR*, the Let's Encrypt root issued since late 2025, while the system
`curl` validates fine because it uses the macOS keychain. Fixes, any of:

```sh
/Applications/Python\ 3.11/Install\ Certificates.command   # python.org builds
python3 -m pip install --upgrade certifi                   # then re-run the script above
/opt/homebrew/bin/python3 codex_litellm_sync.py --write    # or just use a newer Python
```

**`model/info: empty list; listing every model group`** — the key calls `/v1/models` fine but
`GET /model/info` answers `{"data": []}`, so the script cannot narrow the catalog to what the key
may call and keeps every chat group (some may answer 403 at request time). This is a LiteLLM-side
permission/routing question, not a script bug; ask the proxy admin why the key sees no entries in
`/model/info`.

Requirements: Codex with `model_catalog_json` (checked on 0.155.1 and 0.156.1), stdlib Python ≥ 3.9.
