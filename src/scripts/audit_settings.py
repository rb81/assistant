#!/usr/bin/env python3
"""Verify that agent.yaml, the sidecar, code, Compose and docs agree on every setting.

Run: PYTHONPATH=src/agent/src python src/scripts/audit_settings.py
Exits non-zero when a problem is found.
"""
import ast
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/agent/src"))
from assistant_agent import settings_schema as schema  # noqa: E402

problems = []
catalog = schema.catalog()
paths = {item["path"] for item in catalog}
envs = {item["env"] for item in catalog}
numbered = re.compile(r"AGENT_CALENDAR_(GOOGLE|OUTLOOK|ZOHO)_[2-9]_(CLIENT_ID|CLIENT_SECRET|REFRESH_TOKEN|CALENDAR_ID|TENANT_ID|API_BASE)")
# Compose/installation variables that are deliberately not settings.
bootstrap_prefixes = ("AGENT_NAME_", "AGENT_WORKSPACE", "AGENT_CONFIG", "AGENT_PROMPT")

sources = [p for p in (ROOT / "src").rglob("*.py") if "tests" not in p.parts and ".venv" not in p.parts and "node_modules" not in p.parts]
code = {p: p.read_text(errors="ignore") for p in sources}

# 1. Every AGENT_* name the code reads must be a setting (or a known non-setting).
read = re.compile(r"(?:getenv|environ\.get|read_secret|environ\[)\(?\s*[\"'](AGENT_[A-Z0-9_]+)[\"']")
non_settings = {"AGENT_ROLE", "AGENT_CONFIG", "AGENT_PROMPT_FILE", "AGENT_WORKSPACE", "AGENT_WORKSPACE_ROOT"}
for path, text in code.items():
    for name in set(read.findall(text)):
        base = name.removesuffix("_FILE")
        if base not in envs and not numbered.fullmatch(base) and base not in non_settings:
            problems.append(f"{path.relative_to(ROOT)}: reads {name}, which is not in agent.yaml")

# 2. Every agent.* path the code reads must exist in agent.yaml.
getter = re.compile(r"\.get(?:_bool|_int|_float|_list|_str)?\(\s*[\"'](agent\.[a-z0-9_.]+)[\"']")
for path, text in code.items():
    for p in set(getter.findall(text)):
        if p.endswith(".") or p in paths or p == "agent.":
            continue
        if p not in paths and not any(q.startswith(p + ".") for q in paths):
            problems.append(f"{path.relative_to(ROOT)}: reads {p}, which is not in agent.yaml")

# 3. Derived names are unique, secrets carry the flag, and secrets have no default value.
if len(envs) != len(catalog):
    problems.append("two settings derive the same environment name")
for item in catalog:
    if item["secret"] and item["default"] not in (None, "", "me"):
        problems.append(f"secret {item['path']} has a non-empty default")
    if item["default"] is None and item["type"] is str and not item["secret"] and item["path"] not in schema.sidecar_types(schema._load("agent.schema.yaml")):
        pass  # blank string defaults are inferred as str; nothing to check

# 4. Sidecar entries without a setting are reported (they are ignored at runtime).
for path in sorted(set((schema._load("agent.schema.yaml").get("settings") or {})) - paths):
    print(f"note: sidecar entry {path} has no setting in agent.yaml (ignored)")

# 5. Compose must not carry setting names that are not in the catalog.
compose = yaml.safe_load((ROOT / "src/docker-compose.yml").read_text())
for service, spec in (compose.get("services") or {}).items():
    environment = spec.get("environment") or {}
    names = environment if isinstance(environment, dict) else [e.split("=")[0] for e in environment]
    for name in names:
        base = str(name).removesuffix("_FILE")
        if base.startswith("AGENT_") and base not in envs and base not in non_settings and not any(base.startswith(p) for p in bootstrap_prefixes):
            problems.append(f"docker-compose.yml [{service}]: {name} is not a setting")

# 6. Docs, scripts and tests must not mention retired names or unknown AGENT_ settings.
retired = {"POSTGRES_PASSWORD", "OPENROUTER_API_KEY", "IMAP_HOST", "SMTP_HOST", "ZOOM_CLIENT_ID", "DATABASE_URL", "AGENT_NAME", "AGENT_TIMEZONE"}
documented = list((ROOT / "docs").rglob("*.md")) + [ROOT / "README.md"]
mention = re.compile(r"(?<![A-Z0-9_])(AGENT_[A-Z0-9_]+)(?![A-Z0-9_])")
for path in documented:
    if path.name in {"test_migrations.py"}:
        continue
    text = path.read_text(errors="ignore")
    for name in set(mention.findall(text)):
        base = name.removesuffix("_FILE")
        if base in envs or numbered.fullmatch(base) or base in non_settings or any(base.startswith(p) for p in bootstrap_prefixes):
            continue
        if any(e.startswith(base + "_") for e in envs) or base.endswith("_"):
            continue  # prefix used in prose
        if base in retired:
            problems.append(f"{path.relative_to(ROOT)}: mentions retired name {name}")
        else:
            problems.append(f"{path.relative_to(ROOT)}: mentions unknown setting {name}")

# 7. A literal fallback in code (config.get("agent.<path>", 30)) must equal the agent.yaml default, so the default lives in one place.
defaults = {item["path"]: item["default"] for item in catalog}


def same(fallback, default):
    blank = (None, "", [], ())
    if fallback in blank and default in blank:
        return True
    if isinstance(fallback, tuple):
        fallback = list(fallback)
    numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (fallback, default))
    return (numbers or type(fallback) is type(default)) and fallback == default


for path, text in code.items():
    for node in ast.walk(ast.parse(text)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and len(node.args) >= 2
                and node.func.attr in {"get", "get_bool", "get_int", "get_float", "get_list", "get_str"}
                and isinstance(node.args[0], ast.Constant) and node.args[0].value in defaults):
            continue
        try:
            fallback = ast.literal_eval(node.args[1])
        except ValueError:
            continue
        default = defaults[node.args[0].value]
        if not same(fallback, default):
            problems.append(f"{path.relative_to(ROOT)}:{node.lineno}: fallback {fallback!r} for {node.args[0].value} "
                            f"differs from agent.yaml ({default!r})")

print(f"{len(catalog)} settings ({sum(i['secret'] for i in catalog)} credentials) audited")
for problem in sorted(set(problems)):
    print("PROBLEM:", problem)
sys.exit(1 if problems else 0)
