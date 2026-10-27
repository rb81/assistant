#!/usr/bin/env python3
"""Move local runtime files only, with dry-run default and no container startup."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys

RETIRED_ENV_NAMES = frozenset({
    "AGENT_APP_BASE_URL", "AGENT_API_AUTH_TOKEN", "AGENT_API_TLS_CERT_FILE",
    "AGENT_API_TLS_KEY_FILE", "AGENT_API_BIND_HOST", "AGENT_API_PORT",
    "AGENT_API_DOCS_ENABLED", "AGENT_API_OPENAPI_ENABLED",
    "AGENT_API_ALLOW_PUBLIC_BIND", "AGENT_API_PUBLISHED_PORT",
    "AGENT_API_PUBLISHED_HOST", "AGENT_API_DASHBOARD_ENABLED",
    "AGENT_API_WORKSPACE_ENABLED", "AGENT_API_MAX_UPLOAD_BYTES",
    "SETUP_API_ENABLED", "ADMIN_COMMAND_SIGNING_KEY",
    "ADMIN_TRUSTED_AUTHSERV_IDS", "ASSISTANT_ACCEPT_V2_MIGRATION",
})
ENV_ARCHIVE = "layout-transition-env"
RETIRED_SECRET_NAMES = frozenset({"AGENT_API_AUTH_TOKEN", "ADMIN_COMMAND_SIGNING_KEY"})


def regular_tree(path):
    mode = path.lstat().st_mode
    if stat.S_ISREG(mode):
        return
    if not stat.S_ISDIR(mode):
        raise RuntimeError("Symlink or non-regular entry refused")
    for child in path.iterdir():
        regular_tree(child)


def exists(path):
    return os.path.lexists(path)


def plan(root):
    root = Path(os.path.abspath(root))
    for ancestor in [root, *root.parents]:
        if ancestor.is_symlink():
            raise RuntimeError("Symlink in installation root refused")
    if not root.is_dir():
        raise RuntimeError("Installation root must be a directory")
    for required in (root / ".env", root / "config/agent.yaml"):
        if not required.is_file():
            raise RuntimeError("Old .env and config/agent.yaml are required")
    regular_tree(root / ".env")
    # Inspect only supported config files; unknown custom files remain untouched.
    if (root / "config").is_symlink():
        raise RuntimeError("Symlinked config directory refused")
    data = root / "data"
    if exists(data) and (data.is_symlink() or not data.is_dir()):
        raise RuntimeError("data must be a real directory")
    config = data / "config"
    if exists(config):
        raise RuntimeError("Destination data/config already exists")
    for backups in (root / "backups", data / "backups"):
        if exists(backups):
            regular_tree(backups)
            if not backups.is_dir() or exists(backups / ENV_ARCHIVE):
                raise RuntimeError("Environment backup destination collision")
    actions = []
    for name in (".env", "overrides.env", ".secrets", ".compose-secrets.yaml", ".setup.lock", "backups"):
        source, target = root / name, data / name
        if exists(target):
            raise RuntimeError("Destination collision; no changes made")
        if exists(source):
            regular_tree(source)
            if source.stat().st_dev != root.stat().st_dev:
                raise RuntimeError("Runtime relocation must stay on the same filesystem")
            if name in (".secrets", "backups") and not source.is_dir():
                raise RuntimeError("Expected a directory for credentials/backups")
            if name not in (".secrets", "backups") and not source.is_file():
                raise RuntimeError("Expected a regular local runtime file")
            actions.append(("move", source, target))
    for name in ("agent.yaml", "AGENT.md"):
        source = root / "config" / name
        if exists(source):
            regular_tree(source)
            if not source.is_file():
                raise RuntimeError("Expected a regular configuration file")
            actions.append(("copy", source, config / name))
    # Precompute rewrites before moving anything (including decoding failures).
    rewrites = {}
    for _, source, target in actions:
        if source.name in (".env", "overrides.env"):
            rewrites[target] = rewrite_env(source.read_text(), root)
    state_path = root / ".secrets/state.json"
    if exists(state_path):
        state = json.loads(state_path.read_bytes())
        if not isinstance(state, dict):
            raise RuntimeError("Credential setup state must be an object")
        changed = False
        for key in ("pending", "removed"):
            if key in state:
                names = state[key]
                if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                    raise RuntimeError("Credential setup tracking lists must contain names")
                filtered = [name for name in names if name not in RETIRED_SECRET_NAMES]
                if filtered != names:
                    state[key] = filtered
                    changed = True
        if changed:
            rewrites[data / ".secrets/state.json"] = json.dumps(state, indent=2) + "\n"
    if root.stat().st_dev != (data if data.exists() else root).stat().st_dev:
        raise RuntimeError("Runtime relocation must stay on the same filesystem")
    return root, actions, rewrites


def env_records(text):
    """Keep quoted multiline assignments together, including retired credentials."""
    records = iter(text.splitlines(keepends=True))
    for line in records:
        assignment = re.match(r"^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*(['\"])", line)
        if assignment:
            quote = assignment.group(1)
            start = assignment.end()
            while True:
                body = line[start:]
                closing = re.search(r"(?<!\\)(?:\\\\)*" + re.escape(quote), body) if quote == '"' else re.search("'", body)
                if closing:
                    break
                try:
                    line += next(records)
                except StopIteration:
                    raise RuntimeError("Unterminated quoted environment value") from None
        yield line


def rewrite_env(text, root):
    replacements = {
        "COMPOSE_FILE": f"{root}/src/docker-compose.yml:{root}/data/.compose-secrets.yaml",
        "SANDBOX_SHARED_ROOT_HOST": f"{root}/data/share",
        "SANDBOX_DOCS_ROOT_HOST": f"{root}/docs",
    }
    lines = []
    for line in env_records(text):
        match = re.match(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*?)(\r?\n)?$", line, re.DOTALL)
        if match:
            prefix, key, equals, raw, newline = match.groups()
            if key in RETIRED_ENV_NAMES or key.removesuffix("_FILE") in RETIRED_ENV_NAMES:
                continue
            value = replacements.get(key)
            if key == "COMPOSE_PROFILES":
                profile = re.fullmatch(r'''\s*(?:"([^"]*)"|'([^']*)'|([^#'"]*?))\s*(?:#.*)?''', raw)
                if not profile:
                    raise RuntimeError("Complex Compose profiles require manual review")
                names = next(group for group in profile.groups() if group is not None)
                value = ",".join(name.strip() for name in names.split(",")
                                 if name.strip() and name.strip() != "api")
            if value is None and key.endswith("_FILE"):
                # Only standard, unambiguous old host-secret pointers are rewritten.
                pointer = raw.strip().strip("'\"")
                for old in (str(root / ".secrets") + "/", "./.secrets/", ".secrets/"):
                    if pointer.startswith(old) and re.fullmatch(r"[A-Za-z0-9_.-]+", pointer[len(old):]):
                        value = str(root / "data/.secrets" / pointer[len(old):])
                        break
            if value is not None:
                escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
                line = f'{prefix}{key}{equals}"{escaped}"{newline or ""}'
        lines.append(line)
    return "".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-confirmed", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and not args.backup_confirmed:
        parser.error("--apply requires --backup-confirmed")
    try:
        root, actions, rewrites = plan(args.root)
        if not args.apply:
            print(f"Dry run: {len(actions)} supported runtime entries; no changes made.")
            print("Unknown config files and old .venv-setup are left in place for manual review.")
            return 0
        (root / "data").mkdir(mode=0o700, exist_ok=True)
        (root / "data/config").mkdir(mode=0o700)
        for operation, source, target in actions:
            if operation == "move":
                source.rename(target)
            else:
                shutil.copy2(source, target)
        archive = root / "data/backups" / ENV_ARCHIVE
        archive.mkdir(mode=0o700, parents=True)
        for target in rewrites:
            shutil.copy2(target, archive / target.name)
            (archive / target.name).chmod(0o600)
        state = root / "data/.secrets/state.json"
        if state.is_file() and state not in rewrites:
            shutil.copy2(state, archive / state.name)
            (archive / state.name).chmod(0o600)
        for target, content in rewrites.items():
            # Opening the moved file in place retains its existing ownership and mode.
            with target.open("w") as stream:
                stream.write(content)
        print("Runtime layout moved. No containers started. Update main, then save with ./setup.sh.")
        print("Review custom paths; setup must regenerate the secrets overlay before any startup.")
        return 0
    except (OSError, RuntimeError, UnicodeError, ValueError):
        # Do not echo paths or parser input that could contain credential material.
        print("Layout transition refused; verify backups, file types, permissions and collisions. "
              "An I/O error during apply may require restoration.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
