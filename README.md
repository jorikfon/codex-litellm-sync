# codex-litellm-sync

Builds a [Codex CLI](https://github.com/openai/codex) model catalog from a LiteLLM proxy, so
`/model` lists what your LiteLLM key can actually use.

Codex has no plugin hook for models. It takes a static catalog through `model_catalog_json` in
`config.toml`, and it wants its own format (`{"models": [ModelInfo…]}` with reasoning levels,
context window and the agent prompt), not OpenAI's `/v1/models`. This script reads LiteLLM's
`GET /model_group/info` (limits, vision, reasoning levels), keeps only the models
`GET /model/info` lists for your key — so a model the key may not call does not show up only to
answer 403 — and writes that file. If `/model/info` fails or is empty, `/v1/models` is used
instead (without deployment-level reasoning overrides). If both fail or are empty, every group
is kept in the default mode; explicit selection stops with an error.

## Use

```sh
export LITELLM_API_KEY=…            # the same variable as env_key of the provider
python3 codex_litellm_sync.py        # preview
python3 codex_litellm_sync.py --write
```

`--write` writes `~/.codex/litellm-models.json` and adds `model_catalog_json = "…"` to
`~/.codex/config.toml`, updating a previous catalog path if necessary. Output paths are stored
as absolute paths. Fully quit and reopen Codex Desktop after writing: its running model manager
can retain the old catalog. Options: `--url` (default `$LITELLM_URL` or
`https://litellm.miko.ru`), `--key-env`, `--out`, `--config`, `--default-context` (128000).

### Choose models when generating the catalog

```sh
python3 codex_litellm_sync.py --select          # choose and preview
python3 codex_litellm_sync.py --select --write  # choose and save
```

`--select` shows numbered chat models available to your key. Enter numbers separated by
commas or spaces, ranges such as `1,3-5`, or `all`. Press Enter to keep the models marked `*`:
these are the entries in the existing output catalog that are still available. On the first
run, all models are marked. Invalid input is retried; Ctrl-C or EOF cancels without writing.
At least one model must be selected. The catalog keeps LiteLLM's model order.

For scripts, pass exact slugs instead of opening the interactive selector:

```sh
python3 codex_litellm_sync.py --models gpt-6-luna glm-5.3 --write
```

`--models` rejects unknown, inaccessible, and non-chat models. Both selection options require
a successful, nonempty `/model/info` or `/v1/models` response to verify choices against your key.
They cannot be combined. `--select` requires an interactive terminal.

Run with `--select` again to adjust the saved set, or repeat `--models` for automated updates.
Without either option, the script includes every available chat model as before.

### macOS environment setup

If the variable named by `--key-env` (default `LITELLM_API_KEY`) is missing, the script reads
the password with the same service name from `/Library/Keychains/System.keychain`.
The value is loaded into the script's environment without printing it.
Create that entry in Keychain Access first if it does not exist.

With `--write`, a missing variable also triggers persistent setup: a Keychain lookup is added
to `.zshrc` (`$ZDOTDIR` is respected) or `.bash_profile`, and a user LaunchAgent loads it into
the GUI login environment. Files contain only lookup commands, never the key itself.
Reopen terminals and fully quit/reopen Desktop apps afterwards. An existing parent terminal's
environment cannot be changed by a child process. Preview mode does not install these loaders.
Use `--setup-env --write` to install them explicitly even if the variable already exists;
the matching System Keychain entry must be present.

The script uses stdlib Python and escapes Windows paths in TOML. Windows user-environment
setup and testing on Windows are the next stage; `--setup-env` currently supports macOS only.

The provider itself is configured once by hand:

```toml
model_provider = "litellm_api"

[model_providers.litellm_api]
name = "LiteLLM API"
base_url = "https://litellm.miko.ru/v1"
wire_api = "responses"
env_key = "LITELLM_API_KEY"
```

`LITELLM_API_KEY` is the normal Codex/model-discovery credential.
`LITELLM_MMR_KEY` is reserved for multi-model review. On this Mac, the existing
`litellm` provider keeps `env_key = "LITELLM_MMR_KEY"` so review scripts that
select it explicitly continue to use their own key; `litellm_api` is the default.
Both credentials are read from System Keychain. The login LaunchAgent
`ru.miko.codex-litellm-env` loads them into the GUI environment; `.zshrc` loads
them for interactive shells. Fully quit and reopen Desktop apps after changing
their login environment.

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
- The file is regenerated from scratch on each run — use `--select` or `--models` to choose
  entries and edit LiteLLM to change their capabilities.
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

**`model/info: empty list; trying /v1/models`** — LiteLLM supplies no deployment metadata for
the key. The script uses the key-scoped `/v1/models` list instead. Deployment-level reasoning
overrides are unavailable in that case; group-level capabilities still apply.

Requirements: Codex with `model_catalog_json` (checked on 0.155.1 and 0.156.1), stdlib Python ≥ 3.9.
