import json
from pathlib import Path
from types import SimpleNamespace

from assistant_agent import config_check
from assistant_agent.setup import docker_failure, overrides_problems

REPOSITORY = Path(__file__).resolve().parents[3]


def instance(tmp_path, env="", overrides="", secrets=(), state=None, rows=(), probe=None, grants=None):
    (tmp_path / "config").mkdir(exist_ok=True)
    calls = []

    def compose(arguments, timeout=180):
        calls.append(arguments)
        return json.dumps(probe(arguments[2]))

    return SimpleNamespace(
        data=tmp_path, source=REPOSITORY / "src", env_text=env, overrides_text=overrides,
        secret_values={name: "value" for name in secrets}, state=state or {"pending": []},
        container_rows=lambda: list(rows), compose=compose, credential_grants=lambda: grants or {}, calls=calls,
    )


def test_saved_files_report_misplaced_unknown_and_stale_names_only(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/agent.yaml").write_text("agent: {}\n")
    checked = instance(
        tmp_path,
        env="COMPOSE_PROJECT_NAME=x\nTASK_AGENT_WORKERS=2\nAGENT_LLM_MODEL=m\nAGENT_EMAIL_SMTP_PASSWORD=hunter2\nLEFTOVER=1\n",
        overrides="AGENT_IDENTITY_NAME=Ada\nAGENT_CALENDAR_GOOGLE_2_CALENDAR_ID=work\nAGENT_NOT_A_SETTING=1\n",
        secrets=("AGENT_DATABASE_PASSWORD", "IMAP_PASSWORD"),
    )
    problems = config_check.saved_problems(checked)
    assert any("data/config/agent.yaml" in p for p in problems)
    assert "data/.env: AGENT_LLM_MODEL belongs in data/overrides.env" in problems
    assert "data/.env: AGENT_EMAIL_SMTP_PASSWORD belongs in ./run.sh secrets update" in problems
    assert "data/.env: LEFTOVER is not used by Assistant" in problems
    assert "overrides.env: unknown setting AGENT_NOT_A_SETTING" in problems
    assert "credential store: IMAP_PASSWORD is not a credential in agent.yaml" in problems
    assert len(problems) == 6
    assert "hunter2" not in "".join(problems)


def test_numbered_calendar_overrides_are_valid():
    assert overrides_problems("AGENT_CALENDAR_OUTLOOK_3_TENANT_ID=org\n") == []


def test_email_setup_flag_is_allowed_but_unknown_setup_flags_are_reported(tmp_path):
    for value in ("true", "false"):
        checked = instance(tmp_path, env=f"SETUP_EMAIL_ENABLED={value}\nSETUP_UNKNOWN=1\n")
        assert config_check.saved_problems(checked) == ["data/.env: SETUP_UNKNOWN is not used by Assistant"]


def test_runtime_reports_sanitized_docker_failure(tmp_path):
    checked = instance(tmp_path)
    failure = docker_failure(["compose"], SimpleNamespace(
        returncode=1, stderr="permission denied connecting to docker.sock private-fixture-value", stdout="",
    ))

    def failed_container_rows():
        raise failure

    checked.container_rows = failed_container_rows
    problems, services = config_check.runtime_problems(checked)
    assert services == []
    assert "Access to the Docker socket was denied." in problems[0]
    assert "private-fixture-value" not in problems[0]


def test_runtime_reports_stale_containers_and_missing_credentials(tmp_path):
    checked = instance(tmp_path, overrides="AGENT_IDENTITY_NAME=Ada\n", secrets=("AGENT_DATABASE_URL",))
    expected = config_check.expected_settings(checked)
    assert expected["agent.identity.name"] == "Ada"
    shipped = __import__("hashlib").sha256((REPOSITORY / "src/config/agent.yaml").read_bytes()).hexdigest()

    def probe(service):
        if service == "supervisor":
            return {"settings": expected, "secrets": ["AGENT_DATABASE_URL"], "defaults": shipped}
        return {"settings": {**expected, "agent.identity.name": "Old"}, "secrets": [], "defaults": "old"}

    checked = instance(tmp_path, overrides="AGENT_IDENTITY_NAME=Ada\n", secrets=("AGENT_DATABASE_URL",), probe=probe,
                       rows=[{"Service": "supervisor", "State": "running"}, {"Service": "task-agent", "State": "running"},
                             {"Service": "heartbeat", "State": "exited"}],
                       grants={"supervisor": ["AGENT_DATABASE_URL"], "task-agent": ["AGENT_DATABASE_URL"]})
    problems, services = config_check.runtime_problems(checked)
    assert services == ["task-agent", "supervisor"]
    assert problems == [
        "task-agent: runs an image built from a different agent.yaml; run ./run.sh server start",
        "task-agent: AGENT_IDENTITY_NAME differs from data/overrides.env; restart with ./run.sh server start",
        "task-agent: credential AGENT_DATABASE_URL is granted but unreadable or empty",
    ]
