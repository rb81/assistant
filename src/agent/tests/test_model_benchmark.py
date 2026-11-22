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
    assert result["judge"]["error"] == "model API key is required"


def test_malformed_model_response_is_reported(monkeypatch):
    class MalformedClient(FixtureClient):
        def chat(self, messages, tools):
            return ["not an object"]
    monkeypatch.setattr(benchmark, "LlmClient", MalformedClient)
    monkeypatch.setattr(benchmark, "system_prompt", lambda config, job: "Fixture")
    result = benchmark.evaluate(AppConfig({}), "model", benchmark.SCENARIOS[0], "judge")
    assert result["status"] == "error"
    assert "must be an object" in result["error"]
    assert result["input_tokens"] is None
    assert result["judge"]["grade"] is None
