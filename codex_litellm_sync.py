#!/usr/bin/env python3
"""Build a Codex CLI model catalog from a LiteLLM proxy.

Reads LiteLLM `GET /model_group/info` (limits, reasoning levels), narrows it to the models
`GET /model/info` lists for the key, and writes a `model_catalog_json` file for Codex.
Without --write it only prints what it would do.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

SKIP_MODES = {"embedding", "moderation", "moderations", "image_generation", "audio_transcription",
              "audio_speech", "rerank"}
CATALOG_KEY = "model_catalog_json"


def fetch_groups(base_url, key):
    req = urllib.request.Request(base_url.rstrip("/") + "/model_group/info",
                                 headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["data"]


def bundled_models():
    out = subprocess.run(["codex", "debug", "models", "--bundled"], check=True,
                         capture_output=True, text=True).stdout
    return json.loads(out)["models"]


def reasoning_pinned_off(deployment):
    """Reasoning switched off on the deployment itself. Only these litellm_params fields are read:
    the rest of it references provider credentials and is never kept or printed."""
    params = deployment.get("litellm_params") or {}
    thinking = params.get("thinking")
    return (params.get("reasoning_effort") == "none" or params.get("enable_thinking") is False
            or (isinstance(thinking, dict) and thinking.get("type") == "disabled"))


def key_models(deployments):
    """Models the key may call -> True when reasoning is pinned off on every deployment."""
    out = {}
    for d in deployments:
        name = d.get("model_name")
        if name:
            out[name] = out.get(name, True) and reasoning_pinned_off(d)
    return out


def fetch_key_models(base_url, key):
    """`/model_group/info` also lists groups the key may not call (403 at request time);
    `/model/info` is key-scoped. None when it fails or is empty: then nothing is filtered."""
    req = urllib.request.Request(base_url.rstrip("/") + "/model/info",
                                 headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            models = key_models(json.load(resp).get("data") or [])
    except (OSError, ValueError) as exc:
        print("model/info: {}; listing every model group, some may answer 403".format(exc), file=sys.stderr)
        return None
    if not models:
        print("model/info: empty list; listing every model group", file=sys.stderr)
        return None
    return models


def to_model(group, priority, default_context, template, no_reasoning=False):
    # No levels for a model without reasoning: LiteLLM says so, or the deployment pins it off.
    reasoning = group.get("supports_reasoning") is not False and not no_reasoning
    efforts = (group.get("supported_reasoning_efforts") or []) if reasoning else []
    default = next((e for e in ("medium", "high") if e in efforts), efforts[0] if efforts else None)
    context = int(group.get("max_input_tokens") or default_context)
    model = {
        "slug": group["model_group"],
        "display_name": group["model_group"],
        "description": "LiteLLM: " + group["model_group"],
        "supported_reasoning_levels": [{"effort": e, "description": e} for e in efforts],
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": priority,
        "availability_nux": None,
        "upgrade": None,
        "support_verbosity": False,
        "default_verbosity": None,
        "apply_patch_tool_type": "freeform",
        "truncation_policy": {"mode": "bytes", "limit": 10000},
        "context_window": context,
        "max_context_window": context,
        "supports_parallel_tool_calls": bool(group.get("supports_parallel_function_calling")),
        "experimental_supported_tools": [],
        "input_modalities": ["text", "image"] if group.get("supports_vision") else ["text"],
    }
    if default:
        model["default_reasoning_level"] = default
    # Codex refuses a model without instructions; reuse its own agent prompt.
    for field in ("base_instructions", "model_messages"):
        if field in template:
            model[field] = template[field]
    return model


def build_catalog(groups, bundled, default_context=128000, allowed=None):
    """LiteLLM chat groups as Codex models; a slug Codex ships keeps its bundled entry.

    allowed: result of key_models(); None means the key's list is unknown and nothing is filtered.
    """
    known = {m["slug"]: m for m in bundled}
    template = next(m for m in bundled if m["visibility"] == "list")
    chat = [g for g in groups if str(g.get("mode") or "").lower() not in SKIP_MODES
            and (allowed is None or g["model_group"] in allowed)]
    models = []
    for i, g in enumerate(chat):
        if g["model_group"] in known:
            models.append(dict(known[g["model_group"]], priority=i, visibility="list"))
        else:
            models.append(to_model(g, i, default_context, template,
                                   no_reasoning=bool(allowed and allowed.get(g["model_group"]))))
    return {"models": models}


def with_catalog_key(config_text, catalog_path):
    """Return config text with a top-level model_catalog_json, or None if it is already set."""
    lines = config_text.splitlines(keepends=True)
    for line in lines:
        if line.lstrip().startswith("["):
            break
        if line.split("=")[0].strip() == CATALOG_KEY:
            return None
    entry = '{} = "{}"\n'.format(CATALOG_KEY, catalog_path)
    # Top-level keys must come before the first [table].
    first_table = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), len(lines))
    return "".join(lines[:first_table] + [entry] + lines[first_table:])


def main():
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default=os.environ.get("LITELLM_URL", "https://litellm.miko.ru"))
    p.add_argument("--key-env", default="LITELLM_MMR_KEY", help="env var holding the LiteLLM key")
    p.add_argument("--out", type=Path, default=home / "litellm-models.json")
    p.add_argument("--config", type=Path, default=home / "config.toml")
    p.add_argument("--default-context", type=int, default=128000)
    p.add_argument("--write", action="store_true", help="apply (default: preview only)")
    args = p.parse_args()

    key = os.environ.get(args.key_env)
    if not key:
        sys.exit("${} is not set".format(args.key_env))
    catalog = build_catalog(fetch_groups(args.url, key), bundled_models(), args.default_context,
                            fetch_key_models(args.url, key))
    new = [m["slug"] for m in catalog["models"]]
    old = []
    if args.out.exists():
        old = [m["slug"] for m in json.loads(args.out.read_text())["models"]]
    for slug in new:
        print(("  " if slug in old else "+ ") + slug)
    for slug in old:
        if slug not in new:
            print("- " + slug)

    config_text = args.config.read_text() if args.config.exists() else ""
    new_config = with_catalog_key(config_text, args.out)
    if new_config is not None:
        print("{}: will add {}".format(args.config, CATALOG_KEY))
    if not args.write:
        print("(preview; run with --write to apply)")
        return
    args.out.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n")
    if new_config is not None:
        args.config.write_text(new_config)
    print("wrote {} ({} models)".format(args.out, len(new)))


if __name__ == "__main__":
    main()
