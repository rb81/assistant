"""Fold a stale data/config/agent.yaml into data/overrides.env (run by 002-shipped-agent-yaml.sh).

Settings now come only from the agent.yaml shipped with each build. Older installations kept a copy taken at
install time; any value in that copy that differs from both the shipped default and every earlier shipped default
was a deliberate edit and becomes an override. Everything else is dropped so newer defaults apply.
"""
import shutil
import sys
import time
from pathlib import Path

import yaml

from assistant_agent import settings_schema
from assistant_agent.setup import SetupError, parse_environment, render_environment

# Defaults that earlier releases shipped for paths whose default has since changed (from git history).
PREVIOUS_DEFAULTS = {'agent.app.base_url': ['https://localhost:8000', 'http://localhost:8000'],
 'agent.app.referer_url': ['https://github.com/rb81/assistant', 'https://assistant.local'],
 'agent.calendar.default_alert_minutes': [30, 15],
 'agent.calendar.google.calendar_id': ['primary', None],
 'agent.calendar.outlook.tenant_id': ['common', None],
 'agent.calendar.sync.before_read': [True, False],
 'agent.sandbox.base_url': ['https://sandbox:8080', 'http://sandbox:8080'],
 'agent.sandbox.network.allowed_hosts': [[], ['pypi.org', 'files.pythonhosted.org', 'registry.npmjs.org']],
 'agent.security.email_authentication.enabled': [True, False]}


def env_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    if isinstance(value, list) and all(isinstance(item, (str, int, float)) and "," not in str(item) for item in value):
        return ",".join(str(item) for item in value)
    return None


def main(data):
    installed_path = data / "config/agent.yaml"
    installed = yaml.safe_load(installed_path.read_text(encoding="utf-8")) or {}
    if not isinstance(installed, dict):
        raise SystemExit("data/config/agent.yaml is not a YAML mapping; fix or remove it, then rerun.")
    catalog = {item["path"]: item for item in settings_schema.catalog()}
    overrides_path = data / "overrides.env"
    overrides_text = overrides_path.read_text(encoding="utf-8") if overrides_path.exists() else ""
    try:
        existing, _ = parse_environment(overrides_text)
    except SetupError as error:
        raise SystemExit("data/overrides.env: " + str(error))
    updates, kept, retired, refreshed, blocked = {}, [], [], [], []
    for path, value in settings_schema.leaves(installed):
        item = catalog.get(path)
        if item is None:
            retired.append(path)
            continue
        if value == item["default"]:
            continue
        if value in PREVIOUS_DEFAULTS.get(path, []):
            refreshed.append(path)
            continue
        if item["secret"]:
            blocked.append(path + " is a credential; store it with ./run.sh secrets update " + item["env"])
            continue
        rendered = env_value(value)
        if rendered is None or (rendered == "" and item["default"] not in (None, "", [])):
            blocked.append(path + " has a value overrides.env cannot express; set it manually")
            continue
        if item["env"] in existing:
            kept.append(item["env"])
        else:
            updates[item["env"]] = rendered
    if blocked:
        raise SystemExit("Nothing was changed. Resolve these in data/config/agent.yaml first:\n  " + "\n  ".join(blocked))
    backup = data / "backups" / ("agent-yaml-" + time.strftime("%Y%m%d%H%M%S"))
    backup.mkdir(mode=0o700, parents=True)
    shutil.copy2(installed_path, backup / "agent.yaml")
    if updates:
        if overrides_path.exists():
            shutil.copy2(overrides_path, backup / "overrides.env")
        temporary = overrides_path.with_name(".overrides.env.tmp")
        temporary.write_text(render_environment(overrides_text, updates, set()), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(overrides_path)
    installed_path.unlink()
    print("Settings now come from the shipped agent.yaml (backup of the old copy: " + str(backup.relative_to(data.parent)) + ").")
    for name in sorted(updates):
        print("  kept your edit as an override: " + name)
    for name in kept:
        print("  data/overrides.env already sets " + name + "; kept that value")
    for path in refreshed:
        print("  now uses the current default: " + path)
    for path in retired:
        print("  dropped retired setting: " + path)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
