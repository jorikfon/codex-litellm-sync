#!/usr/bin/env python3
"""Build a Codex CLI model catalog from a LiteLLM proxy.

Reads LiteLLM `GET /model_group/info` and writes a `model_catalog_json` file for Codex.
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


def to_model(group, priority, default_context, template):
    efforts = group.get("supported_reasoning_efforts") or []
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


def build_catalog(groups, bundled, default_context=128000):
    """LiteLLM chat groups as Codex models; a slug Codex ships keeps its bundled entry."""
    known = {m["slug"]: m for m in bundled}
    template = next(m for m in bundled if m["visibility"] == "list")
    chat = [g for g in groups if str(g.get("mode") or "").lower() not in SKIP_MODES]
    models = []
    for i, g in enumerate(chat):
        if g["model_group"] in known:
            models.append(dict(known[g["model_group"]], priority=i, visibility="list"))
        else:
            models.append(to_model(g, i, default_context, template))
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
    catalog = build_catalog(fetch_groups(args.url, key), bundled_models(), args.default_context)
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
