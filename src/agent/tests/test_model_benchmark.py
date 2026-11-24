import json
import stat

import pytest

from assistant_agent.config import AppConfig
from assistant_agent import model_benchmark as benchmark


def tool_call(name, arguments, call_id):
    return {"role": "assistant", "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}


class FixtureClient:
    def __init__(self, config, model, allow_fallback, request_observer, **kwargs):
        assert not allow_fallback
        self.judge = model == "judge"
        self.observer = request_observer
        self.index = 0

    def chat(self, messages, tools):
        self.observer({"messages": messages, "tools": tools})
        if self.judge:
            message = {"role": "assistant", "content": '{"score": 4, "rationale": "Correct."}'}
        else:
            messages_by_step = [
                tool_call("get_tool_specs", {"tools": ["file_read"]}, "1"),
                tool_call("file_read", {"path": "notes/project.txt"}, "2"),
                tool_call("task_complete", {"summary": "Summary", "response": "Launch Friday; Mira owns it; budget unresolved."}, "3"),
            ]
            message = messages_by_step[self.index]
            self.index += 1
        return {"choices": [{"message": message}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.01}}


def test_real_task_loop_uses_simulated_tools_and_separates_judge_usage(monkeypatch, tmp_path):
    monkeypatch.setattr(benchmark, "LlmClient", FixtureClient)
    monkeypatch.setattr(benchmark, "system_prompt", lambda config, job: "Fixture system prompt")
    config = AppConfig({"agent": {"skills": {"enabled": True}, "limits": {"max_iterations_per_task": 5},
                                  "filesystem": {"shared_root": str(tmp_path), "require_mount": False}}})
    result = benchmark.evaluate(config, "tested-model", benchmark.SCENARIOS[1], "judge")
    assert result["status"] == "completed"
    assert result["tool_calls"] == 3
    assert result["tool_errors"] == 0
    assert result["input_tokens"] == 30
    assert result["cost_usd"] == 0.03
    assert result["judge"]["grade"]["score"] == 4
    assert result["judge"]["cost_usd"] == 0.01
    assert any("Launch: Friday" in str(event.get("output_data")) for event in result["events"])
    assert result["requests"][0]["payload"]["messages"][0]["content"] == "Fixture system prompt"


def test_unknown_tools_and_unavailable_usage_are_explicit():
    runtime = benchmark.SimulatedRuntime(benchmark.SCENARIOS[1])
    assert "error" in runtime.run("email_send", {"to": "example.com"})
    assert "error" in runtime.run("file_read", {"path": "other.txt"})
    assert benchmark.usage_total([{"response": {"usage": {"prompt_tokens": 1}}}, {"response": {}}], "prompt_tokens") is None


def test_invalid_judge_is_unscored(monkeypatch, tmp_path):
    class InvalidJudge(FixtureClient):
        def chat(self, messages, tools):
            if self.judge:
                self.observer({"messages": messages})
                return {"choices": [{"message": {"content": '{"score": 9, "rationale": "invalid"}'}}]}
            return super().chat(messages, tools)
    monkeypatch.setattr(benchmark, "LlmClient", InvalidJudge)
    monkeypatch.setattr(benchmark, "system_prompt", lambda config, job: "Fixture")
    config = AppConfig({"agent": {"skills": {"enabled": True}, "limits": {"max_iterations_per_task": 5},
                                  "filesystem": {"shared_root": str(tmp_path), "require_mount": False}}})
    result = benchmark.evaluate(config, "tested-model", benchmark.SCENARIOS[1], "judge")
    assert result["judge"]["grade"] is None
    assert result["judge"]["error"]
    assert benchmark.summarize([result])[0]["scored_runs"] == 0


def test_command_protects_results_and_refuses_overwrite(monkeypatch, tmp_path):
    monkeypatch.setattr(benchmark, "LlmClient", FixtureClient)
    monkeypatch.setattr(benchmark, "system_prompt", lambda config, job: "Fixture")
    config = AppConfig({"agent": {"skills": {"enabled": True}, "limits": {"max_iterations_per_task": 5},
                                  "filesystem": {"shared_root": str(tmp_path), "require_mount": False}}})
    monkeypatch.setattr(benchmark, "load_config", lambda: config)
    monkeypatch.setattr(benchmark, "SCENARIOS", (benchmark.SCENARIOS[1],))
    path = tmp_path / "results.json"
    args = ["--model", "tested-model", "--judge-model", "judge", "--output", str(path)]
    assert benchmark.main(args) == 0
    data = json.loads(path.read_text())
    assert data["summary"][0]["runs"] == 1
    assert data["results"][0]["judge"]["grade"]["score"] == 4
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        benchmark.main(args)


def test_missing_credentials_are_recorded_and_unscored(monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("model API key is required")
    monkeypatch.setattr(benchmark, "LlmClient", unavailable)
    result = benchmark.evaluate(AppConfig({}), "model", benchmark.SCENARIOS[0], "judge")
    assert result["status"] == "error"
    assert result["error"] == "model API key is required"
    assert result["judge"]["grade"] is None
    assert result["judge"]["error"] == "Task did not complete; judge was not called"


def test_malformed_model_response_is_reported(monkeypatch):
    class MalformedClient(FixtureClient):
        def chat(self, messages, tools):
            return ["not an object"]
    monkeypatch.setattr(benchmark, "LlmClient", MalformedClient)
    monkeypatch.setattr(benchmark, "system_prompt", lambda config, job: "Fixture")
    result = benchmark.evaluate(AppConfig({}), "model", benchmark.SCENARIOS[0], "judge")
    assert result["status"] == "needs_review"
    assert "must be an object" in result["error"]
    assert result["input_tokens"] is None
    assert result["judge"]["grade"] is None


def test_empty_truncated_responses_spend_budget_before_any_more_tools(monkeypatch):
    class EmptyClient(FixtureClient):
        def chat(self, messages, tools):
            if self.judge:
                return super().chat(messages, tools)
            return {"choices": [{"message": {"role": "assistant", "content": ""},
                                  "finish_reason": "length"}], "usage": {"total_tokens": 30}}
    monkeypatch.setattr(benchmark, "LlmClient", EmptyClient)
    monkeypatch.setattr(benchmark, "system_prompt", lambda config, job: "Fixture")
    config = AppConfig({"agent": {"limits": {"max_tokens_per_task": 50}}})
    result = benchmark.evaluate(config, "model", benchmark.SCENARIOS[0], "judge")
    assert result["status"] == "needs_review"
    assert result["error"] == "token budget exceeded"
    assert len(result["requests"]) == 2 and result["tool_calls"] == 0


def test_a_resumed_task_has_no_fresh_allowance(monkeypatch):
    monkeypatch.setattr(benchmark, "LlmClient", FixtureClient)
    monkeypatch.setattr(benchmark, "system_prompt", lambda config, job: "Fixture")
    agent = benchmark.BenchmarkAgent(
        AppConfig({"agent": {"limits": {"max_tokens_per_task": 50}}}), "model", benchmark.SCENARIOS[0],
    )
    try:
        agent.db.log_event(0, "llm_request")
        agent.db.log_event(0, "llm_response", tokens_used={"total_tokens": 50})
        agent.process_job({"id": 0, "metadata": {}, "task_summary": "Resume"})
        assert agent.db.status == "needs_review" and not agent.client.calls
        checkpoint = next(event for event in agent.db.events if event["event"] == "checkpoint")
        assert checkpoint["output_data"]["tokens"] == 50
    finally:
        agent.scratch.cleanup()


def test_no_progress_warning_waits_until_the_entire_tool_batch_finishes(monkeypatch):
    class BatchClient(FixtureClient):
        def chat(self, messages, tools):
            if self.judge:
                return super().chat(messages, tools)
            steps = [
                tool_call("get_tool_specs", {"tools": ["file_read", "calculator"]}, "load"),
                tool_call("file_read", {"path": "notes/project.txt"}, "first"),
                {"role": "assistant", "tool_calls": [
                    *tool_call("file_read", {"path": "notes/project.txt"}, "repeat")["tool_calls"],
                    *tool_call("calculator", {"expression": "1"}, "different")["tool_calls"],
                ]},
                tool_call("task_complete", {"summary": "Reviewed", "response": "Launch Friday."}, "done"),
            ]
            message = steps[self.index]
            self.index += 1
            return {"choices": [{"message": message}], "usage": {"total_tokens": 1}}
    monkeypatch.setattr(benchmark, "LlmClient", BatchClient)
    monkeypatch.setattr(benchmark, "system_prompt", lambda config, job: "Fixture")
    scenario = {**benchmark.SCENARIOS[1], "tools": {**benchmark.SCENARIOS[1]["tools"], "calculator": {"answer": 1}}}
    result = benchmark.evaluate(
        AppConfig({"agent": {"limits": {"repeated_tool_result_limit": 2}}}), "model", scenario, "judge",
    )
    assert result["status"] == "completed"
    roles = [message["role"] for message in result["requests"][3]["messages"]]
    assert roles[-4:] == ["assistant", "tool", "tool", "user"]


def test_review_fixture_requires_pagination_and_has_no_mutation_dispatch():
    scenario = next(item for item in benchmark.SCENARIOS if item["id"] == "task-reminder-review")
    runtime = benchmark.SimulatedRuntime(scenario)
    tasks = []
    cursor = None
    while True:
        page = runtime.run("job_list", {"before_id": cursor})
        tasks.extend(page["jobs"])
        cursor = page["next_before_id"]
        if page["complete"]:
            break
    assert len(tasks) == 23
    assert sum(task["status"] == "needs_review" for task in tasks) == 8
    assert runtime.run("reminder_list", {"offset": 20})["complete"]
    assert "error" in runtime.run("reminder_cancel", {"reminder_id": 1})


def test_candidate_package_contains_current_code_and_protects_existing_output(tmp_path):
    import importlib.util
    from pathlib import Path
    import subprocess
    script = Path(__file__).resolve().parents[2] / "scripts/package_model_benchmark.py"
    spec = importlib.util.spec_from_file_location("package_model_benchmark", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "candidate.sh"
    module.package(output)
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    subprocess.run(["sh", "-n", str(output)], check=True)
    with pytest.raises(FileExistsError):
        module.package(output)


@pytest.mark.parametrize("text", [
    '{"score":5,"rationale":"Correct"}',
    '```json\n{"score":5,"rationale":"Correct"}\n```',
    '```json\n{"score":5,"rationale":"Correct"}```',
])
def test_plain_and_fenced_judge_json_are_validated(text):
    assert benchmark.parse_grade(text) == {"score": 5, "rationale": "Correct"}


@pytest.mark.parametrize("text", [
    '```json\n{"score":9,"rationale":"Invalid"}\n```',
    '{"score":true,"rationale":"Invalid"}',
    'Here is the score: {"score":5,"rationale":"Not pure JSON"}',
])
def test_judge_wrapper_does_not_relax_grade_validation(text):
    with pytest.raises(ValueError):
        benchmark.parse_grade(text)


def test_review_detail_reads_are_available_without_live_integrations(tmp_path):
    scenario = next(item for item in benchmark.SCENARIOS if item["id"] == "task-reminder-review")
    runtime = benchmark.SimulatedRuntime(scenario)
    assert runtime.run("job_read", {"job_id": 1})["job"]["status"] == "waiting"
    assert runtime.run("job_read", {"job_id": 2})["job"]["last_error"]
    reminder = runtime.run("reminder_read", {"reminder_id": 24})["reminder"]
    assert reminder["last_error"] and len(reminder["task"]) > 50000
    assert "error" in runtime.run("job_read", {"job_id": 99})
    config = AppConfig({"agent": {"filesystem": {"shared_root": str(tmp_path)}}})
    cache = benchmark.ToolResultCache(config)
    pointer = cache.model_result(0, "reminder_read", {"reminder": reminder})
    reader = benchmark.SimulatedRuntime(scenario, cache.root, cache.shared_root)
    chunk = reader.run("file_read", {"path": pointer["cached_output_path"], "max_bytes": 100000})
    assert chunk["bytes_read"] == 8192 and chunk["truncated"]
    next_chunk = reader.run("file_read", {"path": pointer["cached_output_path"], "offset": chunk["next_offset"]})
    assert next_chunk["offset"] == 8192
    relative_chunk = reader.run("file_read", {"path": pointer["cached_output_relative_path"], "max_bytes": 2000})
    absolute_chunk = reader.run("file_read", {"path": pointer["cached_output_path"], "max_bytes": 2000})
    assert relative_chunk == absolute_chunk
    (tmp_path / "AGENT.md").write_text("This file is outside the simulated cache.")
    escape = str(cache.root / ".." / ".." / ".." / "AGENT.md")
    assert "error" in reader.run("file_read", {"path": escape})
    assert "error" in reader.run("file_read", {"path": str(tmp_path / "AGENT.md")})
