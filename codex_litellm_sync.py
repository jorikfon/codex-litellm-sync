#!/usr/bin/env python3
"""Build a Codex CLI model catalog from a LiteLLM proxy.

Reads LiteLLM `GET /model_group/info` (limits, reasoning levels), narrows it to the models
`GET /model/info` lists for the key, and writes a `model_catalog_json` file for Codex.
Without --write it only prints what it would do.
"""
import argparse
import json
import os
import plistlib
import re
import shlex
import subprocess
import sys
import urllib.request
from pathlib import Path

SKIP_MODES = {"embedding", "moderation", "moderations", "image_generation", "audio_transcription",
              "audio_speech", "rerank"}
CATALOG_KEY = "model_catalog_json"


def resolve_key(name):
    """Read credentials without putting a literal secret in files or command arguments."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("invalid environment variable name")
    if os.environ.get(name):
        return os.environ[name], False
    if sys.platform == "darwin":
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", name, "-w",
             "/Library/Keychains/System.keychain"], capture_output=True, text=True)
        key = result.stdout.rstrip("\n") if result.returncode == 0 else ""
        if key:
            os.environ[name] = key
            print("{}: loaded from System Keychain".format(name))
            return key, True
        raise ValueError("${0} is not set; add a password with service name {0} to "
                         "System Keychain, then rerun".format(name))
    raise ValueError("${} is not set; configure it in your user environment, then rerun".format(name))


def persist_macos_env(name):
    """Install shell/login-session loaders. Only the Keychain service name is persisted."""
    lookup = ('/usr/bin/security find-generic-password -s {} -w '
              '/Library/Keychains/System.keychain 2>/dev/null').format(shlex.quote(name))
    shell = Path(os.environ.get("SHELL", "/bin/zsh")).name
    if shell not in ("zsh", "bash"):
        raise ValueError("automatic environment setup supports zsh and bash on macOS")
    rc = (Path(os.environ.get("ZDOTDIR", str(Path.home()))) / ".zshrc" if shell == "zsh"
          else Path.home() / ".bash_profile")
    marker = "# codex-litellm-sync: " + name
    text = rc.read_text() if rc.exists() else ""
    if marker not in text:
        rc.parent.mkdir(parents=True, exist_ok=True)
        with rc.open("a") as stream:
            stream.write('\n{}\nif [ -z "${{{}}}" ]; then\n  export {}="$({})"\nfi\n'.format(
                marker, name, name, lookup))
    label = "local.codex-litellm-sync.env." + name
    # The login agent resolves the secret at runtime, never from a plist literal.
    command = 'key="$({})" && [ -n "$key" ] && /bin/launchctl setenv {} "$key"'.format(
        lookup, shlex.quote(name))
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    agent = agents / (label + ".plist")
    agent.write_bytes(plistlib.dumps({"Label": label, "RunAtLoad": True,
                                     "ProgramArguments": ["/bin/sh", "-c", command]}))
    subprocess.run(["/bin/sh", "-c", command], check=True, capture_output=True)
    print("{}: configured in {} and {}; reopen terminals and Desktop apps".format(name, rc, agent))


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
    prefer deployment metadata, then the key-scoped OpenAI model list."""
    req = urllib.request.Request(base_url.rstrip("/") + "/model/info",
                                 headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            models = key_models(json.load(resp).get("data") or [])
    except (OSError, ValueError) as exc:
        print("model/info: {}; trying /v1/models".format(exc), file=sys.stderr)
    else:
        if models:
            return models
        print("model/info: empty list; trying /v1/models", file=sys.stderr)
    req = urllib.request.Request(base_url.rstrip("/") + "/v1/models",
                                 headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            models = {m["id"]: False for m in json.load(resp).get("data", []) if m.get("id")}
    except (OSError, ValueError) as exc:
        print("v1/models: {}; key's model list is unknown".format(exc), file=sys.stderr)
        return None
    if not models:
        print("v1/models: empty list; key's model list is unknown", file=sys.stderr)
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
    """Point the top-level catalog key at the output, updating a previous path if needed."""
    lines = config_text.splitlines(keepends=True)
    # JSON quoting is also valid for TOML basic strings, including Windows backslashes.
    entry = '{} = {}\n'.format(CATALOG_KEY, json.dumps(str(catalog_path), ensure_ascii=False))
    for i, line in enumerate(lines):
        if line.lstrip().startswith("["):
            break
        if line.split("=")[0].strip() == CATALOG_KEY:
            if line.rstrip("\r\n") == entry.rstrip("\n"):
                return None
            lines[i] = entry
            return "".join(lines)
    # Top-level keys must come before the first [table].
    first_table = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), len(lines))
    return "".join(lines[:first_table] + [entry] + lines[first_table:])


def select_models(models, names):
    """Keep requested slugs in catalog order and assign contiguous priorities."""
    available = {m["slug"] for m in models}
    unknown = set(names) - available
    if unknown:
        raise ValueError("models unavailable for this key: " + ", ".join(sorted(unknown)))
    selected = [dict(m, priority=i) for i, m in enumerate(
        m for m in models if m["slug"] in names)]
    if not selected:
        raise ValueError("select at least one model")
    return selected


def parse_selection(text, count):
    """Parse one-based numbers/ranges, or 'all', into zero-based indices."""
    if text.strip().lower() == "all":
        return set(range(count))
    indices = set()
    for token in text.replace(",", " ").split():
        bounds = token.split("-")
        if len(bounds) not in (1, 2) or not all(b.isdecimal() for b in bounds):
            raise ValueError("use numbers and ranges, e.g. 1,3-5, or all")
        start, end = int(bounds[0]), int(bounds[-1])
        if not 1 <= start <= end <= count:
            raise ValueError("model numbers must be between 1 and {}".format(count))
        indices.update(range(start - 1, end))
    if not indices:
        raise ValueError("select at least one model")
    return indices


def interactive_models(models, previous=None):
    if not models:
        raise ValueError("no available chat models for this key")
    defaults = ({m["slug"] for m in models} if previous is None else
                {m["slug"] for m in models} & set(previous))
    print("Available models (* = current selection):")
    for i, model in enumerate(models, 1):
        print("{:3}. [{}] {}".format(i, "*" if model["slug"] in defaults else " ", model["slug"]))
    while True:
        try:
            text = input("Select numbers/ranges (1,3-5), all, or Enter to keep [*]: ")
        except (EOFError, KeyboardInterrupt):
            raise ValueError("model selection cancelled") from None
        try:
            names = defaults if not text.strip() else {
                models[i]["slug"] for i in parse_selection(text, len(models))}
            return select_models(models, names)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)


def main():
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default=os.environ.get("LITELLM_URL", "https://litellm.miko.ru"))
    p.add_argument("--key-env", default="LITELLM_API_KEY", help="env var holding the LiteLLM key")
    p.add_argument("--out", type=Path, default=home / "litellm-models.json")
    p.add_argument("--config", type=Path, default=home / "config.toml")
    p.add_argument("--default-context", type=int, default=128000)
    p.add_argument("--write", action="store_true", help="apply (default: preview only)")
    p.add_argument("--setup-env", action="store_true",
                   help="install macOS Keychain environment loaders (requires --write)")
    selection = p.add_mutually_exclusive_group()
    selection.add_argument("--select", action="store_true", help="interactively choose models")
    selection.add_argument("--models", nargs="+", metavar="SLUG", help="include only these model slugs")
    args = p.parse_args()
    args.out = args.out.expanduser().resolve()
    args.config = args.config.expanduser().resolve()

    if args.select and not sys.stdin.isatty():
        p.error("--select requires an interactive terminal; use --models for scripts")
    try:
        key, missing_env = resolve_key(args.key_env)
    except ValueError as exc:
        p.error(str(exc))
    setup_env = sys.platform == "darwin" and (missing_env or args.setup_env)
    if args.setup_env and sys.platform != "darwin":
        p.error("--setup-env currently supports macOS only")
    allowed = fetch_key_models(args.url, key)
    if (args.select or args.models is not None) and allowed is None:
        p.error("cannot verify models available for this key: /model/info and /v1/models returned no models")
    catalog = build_catalog(fetch_groups(args.url, key), bundled_models(), args.default_context,
                            allowed)
    old = []
    if args.out.exists():
        old = [m["slug"] for m in json.loads(args.out.read_text())["models"]]
    try:
        if args.select:
            catalog["models"] = interactive_models(catalog["models"], old if args.out.exists() else None)
        elif args.models is not None:
            catalog["models"] = select_models(catalog["models"], args.models)
    except ValueError as exc:
        p.error(str(exc))
    new = [m["slug"] for m in catalog["models"]]
    for slug in new:
        print(("  " if slug in old else "+ ") + slug)
    for slug in old:
        if slug not in new:
            print("- " + slug)

    config_text = args.config.read_text() if args.config.exists() else ""
    new_config = with_catalog_key(config_text, args.out)
    if new_config is not None:
        print("{}: will set {} = {}".format(args.config, CATALOG_KEY, args.out))
    if not args.write:
        if setup_env:
            print("{}: will configure shell and GUI environment from System Keychain".format(args.key_env))
        print("(preview; run with --write to apply)")
        return
    if setup_env:
        try:
            persist_macos_env(args.key_env)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            p.error("environment setup failed ({})".format(type(exc).__name__))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n")
    if new_config is not None:
        args.config.parent.mkdir(parents=True, exist_ok=True)
        args.config.write_text(new_config)
    print("wrote {} ({} models)".format(args.out, len(new)))
    print("Fully quit and reopen Codex Desktop to reload the model catalog.")


if __name__ == "__main__":
    main()
