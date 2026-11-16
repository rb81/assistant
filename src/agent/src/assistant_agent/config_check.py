"""Verify that an installation's saved configuration and its running containers agree with agent.yaml.

Reports setting and credential names only; values are never printed.
"""
import hashlib
import json
import os
import re
from unittest import mock

from . import settings_schema
from .config import load_config
from .secret_store import is_secret_name
from .setup import (AGENT_SERVICES, NUMBERED_CALENDAR_SETTING, OVERRIDES_FILE, DockerCommandError, SetupError,
                    container_variable, overrides_problems, parse_environment, read_regular)

# Variables data/.env may hold besides those Compose interpolates: Compose's own and the installer's.
BOOTSTRAP_PREFIXES = ("COMPOSE_", "ASSISTANT_")
BOOTSTRAP_VARIABLES = {"SETUP_EMAIL_ENABLED"}
NUMBERED_CALENDAR_SECRET = re.compile(r"AGENT_CALENDAR_(GOOGLE|OUTLOOK|ZOHO)_[2-9]_(CLIENT_ID|CLIENT_SECRET|REFRESH_TOKEN)")

# Runs inside an agent container: the effective non-secret settings, which credentials resolve, and the shipped defaults.
PROBE = r"""
import hashlib, json
from assistant_agent import settings_schema
from assistant_agent.config import load_config
from assistant_agent.secret_store import read_secret
config = load_config()
catalog = settings_schema.catalog()
print(json.dumps({
    "settings": {item["path"]: config.get(item["path"]) for item in catalog if not item["secret"]},
    "secrets": sorted(item["env"] for item in catalog if item["secret"] and (read_secret(item["env"]) or "").strip()),
    "defaults": hashlib.sha256((settings_schema.defaults_dir() / "agent.yaml").read_bytes()).hexdigest(),
}))
"""


def compose_variables(source):
    text = (read_regular(source / "docker-compose.yml") or b"").decode("utf-8")
    return set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", text))


def saved_problems(instance):
    """Problems in data/ that need no running containers."""
    problems = []
    if (instance.data / "config/agent.yaml").exists():
        problems.append("data/config/agent.yaml exists but is ignored; run migrations/002-shipped-agent-yaml.sh")
    catalog = {item["env"]: item for item in settings_schema.catalog()}
    allowed = compose_variables(instance.source) - set(catalog)
    env_values, _ = parse_environment(instance.env_text)
    for name in sorted(env_values):
        base = name.removesuffix("_FILE")
        if base in catalog or NUMBERED_CALENDAR_SETTING.fullmatch(base) or NUMBERED_CALENDAR_SECRET.fullmatch(base):
            where = "./run.sh secrets update" if is_secret_name(base) else "data/" + OVERRIDES_FILE
            problems.append("data/.env: " + name + " belongs in " + where)
        elif name not in allowed and name not in BOOTSTRAP_VARIABLES and not name.startswith(BOOTSTRAP_PREFIXES):
            problems.append("data/.env: " + name + " is not used by Assistant")
    problems += overrides_problems(instance.overrides_text)
    stored = set(instance.secret_values) | set(instance.state.get("pending", [])) | set(instance.state.get("removed", []))
    for name in sorted(stored):
        if not (name in catalog and catalog[name]["secret"]) and not NUMBERED_CALENDAR_SECRET.fullmatch(name):
            problems.append("credential store: " + name + " is not a credential in agent.yaml")
    return problems


def expected_settings(instance):
    """Shipped defaults plus data/overrides.env, resolved exactly as containers resolve them."""
    overrides, _ = parse_environment(instance.overrides_text)
    overrides = {name: value for name, value in overrides.items() if not is_secret_name(name.removesuffix("_FILE"))}
    overrides["AGENT_CONFIG"] = str(instance.source / "config/agent.yaml")
    with mock.patch.dict(os.environ, overrides, clear=True):
        config = load_config()
    return {item["path"]: config.get(item["path"]) for item in settings_schema.catalog() if not item["secret"]}


def runtime_problems(instance):
    """Compare each running agent service with the saved configuration."""
    problems, checked = [], []
    try:
        running = {row.get("Service") for row in instance.container_rows() if row.get("State") == "running"}
    except DockerCommandError as error:
        return ["containers: " + str(error)], checked
    except SetupError:
        return ["containers: could not list them; is the server running?"], checked
    expected = expected_settings(instance)
    shipped = hashlib.sha256(read_regular(instance.source / "config/agent.yaml") or b"").hexdigest()
    grants = instance.credential_grants()
    for service in AGENT_SERVICES:
        if service not in running:
            continue
        try:
            report = json.loads(instance.compose(["exec", "-T", service, "python", "-c", PROBE], timeout=60))
        except (SetupError, json.JSONDecodeError):
            problems.append(service + ": could not read its configuration")
            continue
        checked.append(service)
        if report.get("defaults") != shipped:
            problems.append(service + ": runs an image built from a different agent.yaml; run ./run.sh server start")
        actual = report.get("settings") or {}
        for path in sorted(set(expected) | set(actual)):
            if path not in actual:
                problems.append(service + ": " + settings_schema.derived_env_name(path) + " is unknown to its image")
            elif path not in expected:
                problems.append(service + ": " + settings_schema.derived_env_name(path) + " is not in the shipped agent.yaml")
            elif actual[path] != expected[path]:
                problems.append(service + ": " + settings_schema.derived_env_name(path)
                                + " differs from data/overrides.env; restart with ./run.sh server start")
        resolved = set(report.get("secrets") or [])
        for name in grants.get(service, []):
            if container_variable(service, name) == name and name in instance.secret_values and name not in resolved:
                problems.append(service + ": credential " + name + " is granted but unreadable or empty")
    return problems, checked


def check(instance, runtime=True):
    problems = saved_problems(instance)
    checked = []
    if runtime:
        found, checked = runtime_problems(instance)
        problems += found
    return problems, checked


def report(result):
    """Print a check result from ManagementService.configuration_check; returns the exit status."""
    print(f"{result['settings']} settings ({result['credentials']} credentials) in the shipped agent.yaml")
    if result["runtime_checked"]:
        print("Containers checked: " + (", ".join(result["containers_checked"]) or "none running"))
    else:
        print("Containers not checked; run ./run.sh config check on the server")
    for problem in result["problems"]:
        print("PROBLEM: " + problem)
    print("Configuration OK" if result["ok"] else f"{len(result['problems'])} problem(s) found")
    return 0 if result["ok"] else 1
