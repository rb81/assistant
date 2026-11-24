from assistant_agent.execution_control import ToolProgress


def test_changed_arguments_or_results_are_progress_but_discovery_does_not_reset_a_cycle():
    progress = ToolProgress(2)
    assert not progress.observe("web_fetch", {"url": "https://example.com"}, {"content": "same"})
    assert not progress.observe("skill_load", {"id": "WEB.SEARCH"}, {"status": "loaded"})
    assert progress.observe("web_fetch", {"url": "https://example.com"}, {"content": "same"})
    assert not progress.should_stop
    assert progress.observe("web_fetch", {"url": "https://example.com"}, {"content": "same"})
    assert progress.should_stop
    assert not progress.observe("web_fetch", {"url": "https://example.com"}, {"content": "changed"})
    assert not progress.observe("file_read", {"offset": 1}, {"content": "same"})
    assert not progress.observe("file_read", {"offset": 2}, {"content": "same"})


def test_guidance_deferrals_and_successful_mutations_are_not_repeated_read_failures():
    progress = ToolProgress(2)
    for _ in range(4):
        assert not progress.observe("web_fetch", {}, {"status": "not_executed"})
        assert not progress.observe("email_send", {}, {"status": "sent"})
    assert not progress.observe("file_write", {}, {"error": "denied"})
    assert progress.observe("file_write", {}, {"error": "denied"})
