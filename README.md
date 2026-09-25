# codex-litellm-sync

Builds a [Codex CLI](https://github.com/openai/codex) model catalog from a LiteLLM proxy, so
`/model` lists what your LiteLLM key can actually use.

Codex has no plugin hook for models. It takes a static catalog through `model_catalog_json` in
`config.toml`, and it wants its own format (`{"models": [ModelInfo…]}` with reasoning levels,
context window and the agent prompt), not OpenAI's `/v1/models`. This script reads LiteLLM's
`GET /model_group/info` and writes that file.

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
- The file is regenerated from scratch on each run — edit LiteLLM, not the file.
- Codex sends `client_metadata` and a `reasoning` object on every request, whatever the catalog
  says; each LiteLLM deployment must accept or drop them (`additional_drop_params`).
- Check the result with `codex debug models`.

Requirements: Codex with `model_catalog_json` (checked on 0.155.1 and 0.156.1), stdlib Python ≥ 3.9.
