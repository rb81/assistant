import argparse
import asyncio
import fcntl
import os
import pty
import select
import socket
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.widgets import Checkbox

from assistant_agent.management import GROUPS, Management
from assistant_agent.management_api import configuration_groups
from assistant_agent.management_help import GROUP_HELP, credential_help, field_help, page_help
from assistant_agent.management_tui import ManagementApp
from assistant_agent.secret_store import SECRET_NAMES
from assistant_agent.setup import DockerCommandError, Installation, SetupError


class TerminalOutput(DummyOutput):
    def __init__(self):
        self.size = Size(rows=28, columns=100)

    def get_size(self):
        return self.size


def snapshot(ui):
    screen = ui.app.renderer._last_screen
    size = ui.app.output.get_size()
    return ["".join(screen.data_buffer[y][x].char for x in range(size.columns))
            for y in range(size.rows)]


async def wait_for(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("UI did not reach the expected state")


@asynccontextmanager
async def running(root):
    with create_pipe_input() as pipe:
        backend = Management(root, Mock(return_value=False))
        backend.groups = configuration_groups()
        backend.load()
        backend.installation.state["setup_complete"] = True
        backend.values.update(AGENT_IDENTITY_NAME="Assistant", AGENT_APP_TIMEZONE="UTC")
        ui = ManagementApp(root, input=pipe, output=TerminalOutput(), backend=backend)
        ui.loaded = True
        ui.loop = asyncio.get_running_loop()
        ui.render()
        task = asyncio.create_task(ui.app.run_async())
        await asyncio.sleep(0.08)
        try:
            yield ui, pipe
        finally:
            if ui.busy and ui.active_task:
                await asyncio.wait_for(ui.active_task, 3)
            if ui.app.is_running:
                ui.app.exit()
            await asyncio.wait_for(task, 3)


def test_native_services_render_structured_rows_and_keep_one_application():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            app = ui.app
            rows = [{"id": "task-agent", "title": "Task worker", "state": "running", "health": "healthy"},
                    {"id": "postgres", "title": "Database", "state": "exited", "health": "not checked"}]
            with patch.object(ui.backend, "services", return_value=rows), \
                    patch("assistant_agent.setup.interactive", side_effect=AssertionError("Legacy menu invoked")):
                ui.go("Services")
                await wait_for(lambda: not ui.busy)
                await asyncio.sleep(0.08)
                screen = "\n".join(snapshot(ui))
                assert "Task worker" in screen
                assert "running" in screen and "healthy" in screen
                assert "$ docker" not in screen
                assert "Command output" not in screen
                pipe.send_text("\r")
                await asyncio.sleep(0.08)
                assert ui.location.route == "Service"
                assert ui.app is app
                assert "Recent logs" in "\n".join(snapshot(ui))
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_skills_load_preferences_on_open_and_edit_instructions_only(tmp_path):
    async def scenario():
        async with running(tmp_path) as (ui, _pipe):
            card = {"id": "EMAIL", "title": "Email", "description": "Compose email.",
                    "revision": "locked", "locked": True}
            preference = {"id": "EMAIL.LEGACY", "revision": "3", "title": "Signature",
                          "description": "Signoff", "body": "Sign as the Assistant."}
            ui.backend.client = Mock()
            ui.backend.client.query.side_effect = [
                {"skills": [card], "total": 1}, {**card, "body": "Verify the recipient."},
            ]
            ui.go("Skills")
            await wait_for(lambda: not ui.busy and ui.key() in ui.cache)
            ui.backend.client.query.assert_called_once_with(
                "skills.list", {"offset": 0, "limit": 8, "query": "", "include_overlays": False})
            ui.go("Skill", card)
            await wait_for(lambda: not ui.busy and ui.key() in ui.cache)
            assert ui.backend.client.query.call_args.args == ("skills.read", {"id": "EMAIL"})
            labels = {action.label for action in ui.actions}
            assert "Add preferences" in labels
            assert "Delete skill" not in labels
            ui.confirm_action = Mock()
            next(action.handler for action in ui.actions if action.label == "Add preferences")()
            assert ui.confirm_action.call_args.kwargs["fields"] == (("body", "Preference instructions", False, ""),)
            ui.confirm_action.call_args.args[3]({"body": preference["body"]})
            assert ui.backend.client.operation.call_args.args[0]["arguments"] == {
                "overlay_for": "EMAIL", "body": preference["body"]}
            ui.cache[ui.key()]["preferences"] = preference
            ui.render()
            actions = {action.label: action.handler for action in ui.actions}
            assert "Add preferences" not in actions
            actions["Edit preferences"]()
            assert ui.confirm_action.call_args.kwargs["fields"][0][-1] == preference["body"]
            ui.confirm_action.call_args.args[3]({"body": "New signoff."})
            assert ui.backend.client.operation.call_args.args[0]["arguments"] == {
                **preference, "body": "New signoff."}
            actions["Remove preferences"]()
            ui.confirm_action.call_args.args[3]()
            assert ui.backend.client.operation.call_args.args[0]["arguments"] == {"id": preference["id"], "revision": "3"}

    asyncio.run(scenario())


def test_navigation_stays_available_while_a_background_read_is_in_progress():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            release = threading.Event()
            def read(request):
                release.wait(timeout=2)
                return {"rows": [], "total": 0}
            with patch.object(ui.backend, "request", side_effect=read):
                ui.go("Tasks")
                assert ui.busy
                try:
                    ui.app.layout.focus(ui.nav[3].window)
                    pipe.send_text("\r")
                    await wait_for(lambda: ui.location.route == "Connections")
                    assert ui.busy
                    assert "Email" in "\n".join(snapshot(ui))
                finally:
                    release.set()
                await wait_for(lambda: not ui.busy)
                assert ui.location.route == "Connections"
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_native_form_navigation_preserves_a_draft_and_hides_api_keys():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            original = ui.backend.installation.env_original
            ui.go("Form:identity")
            pipe.send_text("\x01\x0bNew assistant")
            await asyncio.sleep(0.08)
            ui.go("Connections")
            assert ui.backend.values["AGENT_IDENTITY_NAME"] == "New assistant"
            assert ui.backend.dirty
            ui.go("Form:identity")
            assert ui.form["AGENT_IDENTITY_NAME"].text == "New assistant"
            ui.go("Form:providers")
            ui.app.layout.focus(ui.form["AGENT_LLM_OPENROUTER_API_KEY"])
            pipe.send_text("PRIVATE-API-KEY")
            await asyncio.sleep(0.08)
            assert "PRIVATE-API-KEY" not in "\n".join(snapshot(ui))
            ui.go("Settings")
            assert ui.backend.credentials["AGENT_LLM_OPENROUTER_API_KEY"] == "PRIVATE-API-KEY"
            assert ui.backend.installation.env_original == original
            assert not Path(root, "data/.env").exists()
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_model_field_accepts_mouse_focus_and_typing():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.go("Form:models")
            await asyncio.sleep(0.08)
            lines = snapshot(ui)
            model_line = next(i for i, line in enumerate(lines) if "Primary model" in line) + 2
            pipe.send_text(f"\x1b[<0;12;{model_line}M\x1b[<0;12;{model_line}m")
            await asyncio.sleep(0.08)
            assert ui.app.layout.has_focus(ui.form["AGENT_LLM_MODEL"])
            pipe.send_text("clicked-model")
            await wait_for(lambda: ui.form["AGENT_LLM_MODEL"].text == "clicked-model")
            assert "AGENT_LLM_BASE_URL" not in ui.form
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_keyboard_model_edit_review_and_save_preserves_other_fields_and_existing_credentials():
    async def scenario(root):
        instance = Installation(root)
        instance.values.update(AGENT_IDENTITY_NAME="Assistant", AGENT_APP_TIMEZONE="UTC", AGENT_LLM_MODEL="old-model",
                               AGENT_LLM_BASE_URL="https://openrouter.ai/api/v1")
        instance.secret_values.update(AGENT_DATABASE_PASSWORD="database-password", AGENT_LLM_OPENROUTER_API_KEY="existing-key")
        instance.state["setup_complete"] = True
        instance.prompt_copy = b"Ask for approval before taking external actions."
        instance.save()
        async with running(root) as (ui, pipe):
            ui.go("Settings")
            await asyncio.sleep(0.08)
            pipe.send_text("\x1b[B\x1b[B\r")
            await wait_for(lambda: ui.location.route == "Form:models")
            await asyncio.sleep(0.08)
            assert ui.app.layout.has_focus(ui.form["AGENT_LLM_MODEL"])
            pipe.send_text("\x01\x0bnew-model\r")
            await asyncio.sleep(0.08)
            assert ui.app.layout.has_focus(ui.form["AGENT_LLM_FALLBACK_MODEL"])
            pipe.send_text("fallback-model\x13")
            await wait_for(lambda: ui.location.route == "Review")
            await wait_for(lambda: not ui.busy and ui.key() in ui.cache)
            assert ui.backend.values["AGENT_LLM_MODEL"] == "new-model"
            assert ui.backend.values["AGENT_LLM_FALLBACK_MODEL"] == "fallback-model"
            assert Installation(root).values["AGENT_LLM_MODEL"] == "old-model"
            pipe.send_text("\r")
            await wait_for(lambda: bool(ui.floats.floats))
            pipe.send_text("\t\r")
            await wait_for(lambda: not ui.floats.floats and not ui.busy and not ui.backend.dirty)
            saved = Installation(root)
            assert saved.values["AGENT_LLM_MODEL"] == "new-model"
            assert saved.values["AGENT_LLM_FALLBACK_MODEL"] == "fallback-model"
            assert saved.values["AGENT_LLM_BASE_URL"] == "https://openrouter.ai/api/v1"
            assert saved.secret_values["AGENT_LLM_OPENROUTER_API_KEY"] == "existing-key"
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_multiline_instructions_keep_enter_and_arrow_keys_for_text_editing():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.go("Instructions")
            await asyncio.sleep(0.08)
            pipe.send_text("\x01\x0bFirst line\rSecond line\x1b[A")
            await asyncio.sleep(0.08)
            field = ui.form["instructions"]
            assert field.text == "First line\nSecond line"
            assert field.buffer.document.cursor_position_row == 0
            assert ui.app.layout.has_focus(field)
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_native_confirmation_cancellation_and_submission_only_run_selected_action_once():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.services = [{"id": "task-agent", "title": "Task worker", "state": "running", "health": "healthy"}]
            with patch.object(ui.backend, "lifecycle", return_value=ui.services) as lifecycle:
                ui.lifecycle("restart", "task-agent")
                await wait_for(lambda: bool(ui.floats.floats))
                pipe.send_text("\r")
                await wait_for(lambda: not ui.floats.floats)
                lifecycle.assert_not_called()
                ui.lifecycle("restart", "task-agent")
                await wait_for(lambda: bool(ui.floats.floats))
                pipe.send_text("\t\r")
                await wait_for(lambda: not ui.floats.floats and not ui.busy)
                lifecycle.assert_called_once_with("restart", "task-agent")
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_task_lists_paginate_records_not_terminal_output_and_restore_the_list_on_back():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            def read(request):
                rows = [{"id": i + 1, "task_summary": "Task " + str(i + 1), "status": "queued"}
                        for i in range(9)]
                if request.get("id"):
                    return {"rows": [rows[request["id"] - 1]], "total": 1}
                return {"rows": rows[request["page"] * 8:(request["page"] + 1) * 8], "total": 9}
            with patch.object(ui.backend, "request", side_effect=read):
                ui.go("Tasks")
                await wait_for(lambda: not ui.busy)
                await asyncio.sleep(0.08)
                first = snapshot(ui)
                next_action = next(action for action in ui.actions if action.label == "Next")
                ui.app.layout.focus(next_action.window)
                pipe.send_text("\r")
                await wait_for(lambda: not ui.busy and ui.location.page == 1)
                await asyncio.sleep(0.08)
                second = snapshot(ui)
                assert next(i for i, line in enumerate(first) if "Page 1 of 2" in line) == \
                    next(i for i, line in enumerate(second) if "Page 2 of 2" in line)
                assert "Task 9" in "\n".join(second)
                pipe.send_text("\r")
                await wait_for(lambda: ui.location.route == "Record")
                pipe.send_text("\x1b")
                await wait_for(lambda: ui.location.route == "Tasks" and not ui.busy)
                assert ui.location.page == 1
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_minimum_size_keeps_fixed_footer_and_resize_keeps_the_draft():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.app.output.size = Size(rows=24, columns=76)
            ui.go("Form:email")
            ui.form["AGENT_EMAIL_IMAP_HOST"].text = "mail.example.test"
            ui.app.invalidate()
            await asyncio.sleep(0.08)
            assert "Keep draft" in "\n".join(snapshot(ui)[-10:])
            assert "Ctrl-Q Quit" in snapshot(ui)[-1]
            ui.app.output.size = Size(rows=18, columns=60)
            ui.app.invalidate()
            await asyncio.sleep(0.08)
            assert "Resize to at least" in "\n".join(snapshot(ui))
            ui.app.output.size = Size(rows=28, columns=100)
            ui.app.invalidate()
            await asyncio.sleep(0.08)
            assert ui.form["AGENT_EMAIL_IMAP_HOST"].text == "mail.example.test"
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_compact_layout_keeps_help_above_content_and_the_footer_clear():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            for size in (Size(rows=24, columns=76), Size(rows=28, columns=100), Size(rows=36, columns=140)):
                ui.app.output.size = size
                ui.go("Settings")
                ui.error = ui.status = ""
                selected = next(item for item in ui.actions if item.label == "Advanced settings")
                ui.app.layout.focus(selected.window)
                ui.app.invalidate()
                await asyncio.sleep(0.08)
                screen = snapshot(ui)
                assert "Home" in screen[0] and "Help" in screen[0]
                assert screen[2].strip() == "Settings"
                assert "About: Advanced settings" in screen[3]
                assert "Tune privacy" in screen[4]
                assert all(len(line) <= size.columns - 2 for line in ui.context_preview().splitlines())
                assert "About:" not in "\n".join(screen[-4:])
                assert "Review & save" in "\n".join(screen[-5:-1])
                assert "Ctrl-Q Quit" in screen[-1]
                assert "ASSISTANT" not in "\n".join(screen)
                assert "┌" not in "\n".join(screen) and "│" not in "\n".join(screen)
                ui.error = "Unable to refresh. Check Docker and retry."
                ui.app.invalidate()
                await asyncio.sleep(0.08)
                assert ui.error in "\n".join(snapshot(ui)[-4:])
                ui.error = ""
                ui.app.invalidate()
                await asyncio.sleep(0.08)
                assert "Review & save" in "\n".join(snapshot(ui)[-5:-1])
            ui.go("Form:models")
            ui.app.output.size = Size(rows=24, columns=76)
            ui.app.invalidate()
            await asyncio.sleep(0.08)
            description = " ".join(line.strip() for line in snapshot(ui)[7:11])
            assert GROUP_HELP["models"] in description
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_native_security_form_exposes_and_stages_development_supervisor_settings():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.go("Settings")
            action = next(item for item in ui.actions if item.label == "Safety & screening")
            ui.app.layout.focus(action.window)
            pipe.send_text("\r")
            await wait_for(lambda: ui.form_group == "security")
            key = "AGENT_SECURITY_DEVELOPMENT_MODEL"
            assert ui.form[key].text == ""
            ui.app.layout.focus(ui.form[key])
            assert "OpenRouter model ID" in ui.context_topic()[1]
            ui.form[key].text = "openai/gpt-sol-6.1"
            ui.form["AGENT_SECURITY_DEVELOPMENT_MAX_MINUTES"].text = "15"
            ui.form["AGENT_SECURITY_DEVELOPMENT_MAX_RUNS"].text = "8"
            ui.keep_draft()
            assert ui.backend.values[key] == "openai/gpt-sol-6.1"
            assert ui.backend.values["AGENT_SECURITY_DEVELOPMENT_MAX_MINUTES"] == "15"
            assert ui.backend.values["AGENT_SECURITY_DEVELOPMENT_MAX_RUNS"] == "8"
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_every_checkbox_can_be_toggled_and_left_with_arrows_or_tab():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.app.output.size = Size(rows=24, columns=76)
            for group, (_, specs) in ui.groups.items():
                booleans = [spec for spec in specs if spec.boolean]
                if not booleans:
                    continue
                ui.go("Form:" + group)
                for spec in booleans:
                    widget = ui.form[spec.key]
                    ui.app.layout.focus(widget)
                    checked = widget.checked
                    for key in (" ", "\r"):
                        pipe.send_text(key)
                        await wait_for(lambda: widget.checked != checked)
                        checked = widget.checked
                        assert ui.app.layout.current_control is widget.control
                    for key, direction in (("\x1b[A", -1), ("\x1b[B", 1),
                                           ("\x1b[D", -1), ("\x1b[C", 1),
                                           ("\t", 1), ("\x1b[Z", -1)):
                        ui.app.layout.focus(widget)
                        ui.app.layout.update_parents_relations()
                        windows = list(ui.app.layout.get_focusable_windows())
                        index = windows.index(ui.app.layout.current_window)
                        target = windows[(index + direction) % len(windows)]
                        pipe.send_text(key)
                        await wait_for(lambda: ui.app.layout.current_window is target)
                        assert widget.checked == checked, (group, spec.key, key)
                ui.discard_form()
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_help_sections_have_spacing_and_documentation_is_reachable_by_keyboard():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            for size in (Size(rows=28, columns=100), Size(rows=24, columns=76)):
                ui.app.output.size = size
                ui.go("Help")
                ui.render()
                await asyncio.sleep(0.08)
                control = ui.app.layout.current_control
                assert control.buffer.read_only()
                assert "Ctrl-S opens review.\n\nConnection and services" in control.buffer.text
                lines = snapshot(ui)
                heading = next(i for i, line in enumerate(lines) if "Connection and services" in line)
                assert not lines[heading - 1].strip()
                screen = ui.app.renderer._last_screen
                column = lines[heading].index("Connection and services")
                cell = screen.data_buffer[heading][column]
                assert ui.app._merged_style.get_attrs_for_style_str(cell.style).bold
                assert not ui.app._merged_style.get_attrs_for_style_str(
                    screen.data_buffer[heading + 1][column].style).bold
                pipe.send_text("\x1b[1;5F")
                await asyncio.sleep(0.08)
                screen = "\n".join(snapshot(ui))
                assert "docs/README.md" in screen
                assert "docs/tools-reference.md" in screen
                assert "docs/security.md" in screen
                assert "Ctrl-Q Quit" in lines[-1]
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_all_modal_widgets_have_explicit_readable_colors():
    def luminance(color):
        channels = [int(color[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                  for value in channels]
        return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    def assert_contrast(ui):
        styles = {cell.style for row in ui.app.renderer._last_screen.data_buffer.values()
                  for cell in row.values() if "class:dialog.body" in cell.style and cell.char.strip()}
        assert styles
        for style in styles:
            attrs = ui.app._merged_style.get_attrs_for_style_str(style)
            assert len(attrs.color) == len(attrs.bgcolor) == 6, (style, attrs)
            light, dark = sorted((luminance(attrs.color), luminance(attrs.bgcolor)), reverse=True)
            assert (light + 0.05) / (dark + 0.05) >= 4.5, (style, attrs)
        return styles

    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.go("Form:models")
            ui.quit()
            await wait_for(lambda: bool(ui.floats.floats))
            await asyncio.sleep(0.08)
            styles = assert_contrast(ui)
            assert any("class:action.focused" in style for style in styles)
            assert any("class:text-area" in style for style in styles)
            pipe.send_text("\t")
            await asyncio.sleep(0.08)
            assert_contrast(ui)
            pipe.send_text("\x1b")
            await wait_for(lambda: not ui.floats.floats)
            ui.credential("AGENT_LLM_OPENROUTER_API_KEY")
            await wait_for(lambda: bool(ui.floats.floats))
            pipe.send_text("private-key")
            await asyncio.sleep(0.08)
            styles = assert_contrast(ui)
            assert any("class:input.focused" in style for style in styles)
            assert "private-key" not in "\n".join(snapshot(ui))
            pipe.send_text("\x1b")
            await wait_for(lambda: not ui.floats.floats)
            ui.new_memory()
            await wait_for(lambda: bool(ui.floats.floats))
            pipe.send_text("\t\t\r")
            await asyncio.sleep(0.08)
            styles = assert_contrast(ui)
            assert any("class:error" in style for style in styles)
            pipe.send_text("\x1b")
            await wait_for(lambda: not ui.floats.floats)
            ui.show_context_help()
            await wait_for(lambda: bool(ui.floats.floats))
            await asyncio.sleep(0.08)
            assert_contrast(ui)
            pipe.send_text("\x1b")
            await wait_for(lambda: not ui.floats.floats)
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_docker_offline_has_a_real_retry_action_in_the_same_application():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.loaded = False
            instance = ui.backend.installation
            with patch.object(ui.backend, "load", side_effect=[
                DockerCommandError("Docker is unavailable. Open Docker Desktop."), instance,
            ]) as load, patch.object(ui.backend, "services", return_value=[]), \
                    patch.object(ui.backend, "prepare_management"):
                ui.load()
                await wait_for(lambda: not ui.busy)
                await asyncio.sleep(0.08)
                screen = "\n".join(snapshot(ui))
                assert "Docker Desktop" in screen and "Retry" in screen
                pipe.send_text("\r")
                await wait_for(lambda: ui.loaded and not ui.busy)
                assert load.call_count == 2
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_api_failure_leaves_the_ui_responsive_without_local_authorization():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            operation = Mock(side_effect=SetupError("Management API is unavailable."))
            with patch("assistant_agent.setup_privileges.terminal_authorization") as authorize:
                ui.task("Service operation", operation)
                await wait_for(lambda: not ui.busy)
                authorize.assert_not_called()
                assert "Management API is unavailable" in ui.error
                assert ui.app.is_running
                ui.go("Settings")
                assert ui.location.route == "Settings"
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_review_drops_validation_for_a_draft_edited_while_the_api_was_busy():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            release = threading.Event()
            started = threading.Event()
            calls = []

            def validate():
                calls.append(ui.backend.values["AGENT_IDENTITY_NAME"])
                if len(calls) == 1:
                    started.set()
                    assert release.wait(3)
                    return [("identity", "Old draft validation")]
                return []

            with patch.object(ui.backend, "problems", side_effect=validate):
                ui.review()
                await wait_for(started.is_set)
                try:
                    ui.go("Form:identity")
                    ui.form["AGENT_IDENTITY_NAME"].text = "Changed during validation"
                    ui.review()
                finally:
                    release.set()
                await wait_for(lambda: not ui.busy and len(calls) == 2)
                assert calls == ["Assistant", "Changed during validation"]
                assert ui.cache[ui.key()] == []
                assert "Old draft validation" not in "\n".join(snapshot(ui))
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_first_run_native_forms_review_and_confirm_save_persist_without_running_commands():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.backend.installation.state["setup_complete"] = False
            ui.render()
            ui.app.layout.focus(next(action.window for action in ui.actions
                                     if "understand the responsibilities" in action.label))
            pipe.send_text("\r")
            await wait_for(lambda: ui.backend.accepted)
            ui.go("Form:identity")
            pipe.send_text("\x01\x0bNative assistant")
            await asyncio.sleep(0.05)
            ui.go("Form:models")
            ui.form["AGENT_LLM_MODEL"].text = "chosen-model"
            ui.go("Form:providers")
            ui.form["AGENT_LLM_OPENROUTER_API_KEY"].text = "PRIVATE-MODEL-KEY"
            ui.go("Form:catalog:database")
            ui.app.layout.focus(next(action.window for action in ui.actions
                                     if action.label == "Generate secure database password"))
            pipe.send_text("\r")
            await wait_for(lambda: bool(ui.form["AGENT_DATABASE_PASSWORD"].text))
            ui.go("Instructions")
            ui.form["instructions"].text = "Use clear answers and request approval before external actions."
            ui.review()
            await asyncio.sleep(0.08)
            assert ui.backend.problems() == []
            assert "Save configuration" in "\n".join(snapshot(ui)[-7:])
            assert "PRIVATE-MODEL-KEY" not in "\n".join(snapshot(ui))
            assert not (root / "data/.env").exists()
            with patch.object(Installation, "compose", side_effect=AssertionError("Save ran Docker")):
                ui.save()
                await wait_for(lambda: bool(ui.floats.floats))
                pipe.send_text("\r")
                await wait_for(lambda: not ui.floats.floats)
                assert not (root / "data/.env").exists()
                ui.save()
                await wait_for(lambda: bool(ui.floats.floats))
                pipe.send_text("\t\r")
                await wait_for(lambda: not ui.busy and (root / "data/.env").exists())
            reopened = Installation(root)
            assert reopened.values["AGENT_IDENTITY_NAME"] == "Native assistant"
            assert reopened.values["AGENT_LLM_MODEL"] == "chosen-model"
            assert reopened.secret_values["AGENT_LLM_OPENROUTER_API_KEY"] == "PRIVATE-MODEL-KEY"
            assert reopened.prompt_original.startswith(b"Use clear answers")
            assert not ui.backend.dirty
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_service_refresh_preserves_the_selected_control_without_stealing_navigation_focus():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            rows = [{"id": "task-agent", "title": "Task worker", "state": "running", "health": "healthy"}]
            with patch.object(ui.backend, "services", return_value=rows):
                ui.go("Services")
                await wait_for(lambda: not ui.busy)
                ui.app.layout.focus(next(action.window for action in ui.actions if action.label == "Stop app"))
                ui.refresh()
                await wait_for(lambda: not ui.busy)
                assert next(action.label for action in ui.actions
                            if ui.app.layout.has_focus(action.control)) == "Stop app"
                ui.app.layout.focus(ui.nav[3].window)
                ui.refresh()
                await wait_for(lambda: not ui.busy)
                assert ui.app.layout.has_focus(ui.nav[3].control)
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_native_log_live_toggle_refreshes_in_place_masks_secrets_and_can_pause():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.backend.credentials["AGENT_LLM_OPENROUTER_API_KEY"] = "PRIVATE-KEY"
            with patch.object(ui.backend, "logs", side_effect=[
                "First log line PRIVATE-KEY", "Second log line",
            ]) as logs:
                ui.go("Logs", {"id": "task-agent"})
                await wait_for(lambda: not ui.busy)
                await asyncio.sleep(0.08)
                assert "First log line [hidden]" in "\n".join(snapshot(ui))
                assert "PRIVATE-KEY" not in "\n".join(snapshot(ui))
                ui.toggle_live()
                await wait_for(lambda: not ui.busy)
                await asyncio.sleep(0.08)
                assert "Second log line" in "\n".join(snapshot(ui))
                assert "Pause live" in "\n".join(snapshot(ui))
                ui.toggle_live()
                assert not ui.live_logs
                assert logs.call_count == 2
                assert ui.app.is_running
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_automatic_service_refresh_does_not_flash_status_or_rebuild_unchanged_controls():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            rows = [{"id": "task-agent", "title": "Task worker", "state": "running", "health": "healthy"}]
            with patch.object(ui.backend, "services", return_value=rows):
                ui.go("Services")
                await wait_for(lambda: not ui.busy)
            ui.status = "Ready."
            ui.app.layout.focus(next(action.window for action in ui.actions if action.label == "Stop app"))
            ui.app.invalidate()
            await asyncio.sleep(0.08)
            before = snapshot(ui)
            focused = ui.app.layout.current_control
            release = threading.Event()
            def read():
                release.wait(timeout=2)
                return [dict(row) for row in rows]
            with patch.object(ui.backend, "services", side_effect=read), patch.object(
                ui, "render", wraps=ui.render
            ) as render:
                ui.refresh(background=True)
                try:
                    await asyncio.sleep(0.08)
                    assert ui.status_line() == "Ready."
                    assert snapshot(ui) == before
                    assert ui.app.layout.current_control is focused
                    render.assert_not_called()
                finally:
                    release.set()
                await wait_for(lambda: not ui.busy)
                await asyncio.sleep(0.08)
                render.assert_not_called()
                assert snapshot(ui) == before
                assert ui.app.layout.current_control is focused
            changed = [{**rows[0], "state": "exited", "health": "no healthcheck"}]
            with patch.object(ui.backend, "services", return_value=changed), patch.object(
                ui, "render", wraps=ui.render
            ) as render:
                ui.refresh(background=True)
                await wait_for(lambda: not ui.busy)
                await asyncio.sleep(0.08)
                assert "exited" in "\n".join(snapshot(ui))
                render.assert_called_once()
                assert next(action.label for action in ui.actions
                            if ui.app.layout.has_focus(action.control)) == "Stop app"
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_automatic_refresh_surfaces_errors_until_a_successful_poll():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            rows = [{"id": "task-agent", "title": "Task worker", "state": "running", "health": "healthy"}]
            with patch.object(ui.backend, "services", side_effect=[
                rows, DockerCommandError("Docker is unavailable."), rows,
            ]):
                ui.go("Services")
                await wait_for(lambda: not ui.busy)
                ui.refresh(background=True)
                await wait_for(lambda: not ui.busy)
                assert "Docker is unavailable" in ui.status_line()
                ui.refresh(background=True)
                assert "Docker is unavailable" in ui.status_line()
                await wait_for(lambda: not ui.busy)
                assert ui.error == ""
                assert ui.services == rows
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_automatic_log_refresh_keeps_the_existing_view_when_logs_are_unchanged():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            with patch.object(ui.backend, "logs", return_value="First line\nSecond line"):
                ui.go("Logs", {"id": "task-agent"})
                await wait_for(lambda: not ui.busy)
                await asyncio.sleep(0.08)
                controls = list(ui.app.layout.find_all_controls())
                with patch.object(ui, "render", wraps=ui.render) as render:
                    ui.refresh(background=True)
                    await wait_for(lambda: not ui.busy)
                    render.assert_not_called()
                    assert list(ui.app.layout.find_all_controls()) == controls
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_confirmed_record_mutation_returns_to_and_refreshes_the_actual_list():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            row = {"id": 7, "content": "Remember me", "pinned": False}
            with patch.object(ui.backend, "request", return_value={"rows": [row], "total": 1}) as read, \
                    patch.object(ui.backend, "mutate", return_value={"result": {"id": 7}}) as mutate:
                ui.go("Memories")
                await wait_for(lambda: not ui.busy)
                pipe.send_text("\r")
                await wait_for(lambda: ui.location.route == "Record" and not ui.busy)
                ui.mutate("pin-memory", 7)
                await wait_for(lambda: bool(ui.floats.floats))
                pipe.send_text("\t\r")
                await wait_for(lambda: ui.location.route == "Memories" and not ui.busy
                               and ui.key() in ui.cache)
                mutate.assert_called_once_with("pin-memory", 7)
                assert read.call_args.args[0]["kind"] == "memories"
                assert ui.cache[ui.key()]["total"] == 1
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_record_page_distinguishes_loading_failure_and_confirmed_empty_results():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            release = threading.Event()
            def read(payload):
                release.wait(timeout=2)
                raise DockerCommandError("Database is unavailable.")
            with patch.object(ui.backend, "request", side_effect=read):
                ui.go("Memories")
                try:
                    await asyncio.sleep(0.08)
                    screen = "\n".join(snapshot(ui))
                    assert "Loading records" in screen
                    assert "No items" not in screen and "0 items" not in screen
                    assert "Page 1 of" not in screen
                finally:
                    release.set()
                await wait_for(lambda: not ui.busy)
                await asyncio.sleep(0.08)
                screen = "\n".join(snapshot(ui))
                assert "Could not load records" in screen
                assert "Database is unavailable" in screen
                assert screen.count("Database is unavailable") == 1
                assert ui.status_line() == ""
                assert "No items" not in screen
            with patch.object(ui.backend, "request", return_value={"rows": [], "total": 0}):
                ui.refresh()
                await wait_for(lambda: not ui.busy)
                await asyncio.sleep(0.08)
                screen = "\n".join(snapshot(ui))
                assert "No items found" in screen
                assert "0 items" in screen
                assert "Could not load" not in screen
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_failed_record_refresh_keeps_cached_rows_and_reports_the_error_in_the_footer():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            with patch.object(ui.backend, "request", side_effect=[
                {"rows": [{"id": 7, "content": "Existing memory"}], "total": 1},
                DockerCommandError("Database is unavailable."),
            ]):
                ui.go("Memories")
                await wait_for(lambda: not ui.busy)
                ui.refresh()
                await wait_for(lambda: not ui.busy)
                await asyncio.sleep(0.08)
                screen = "\n".join(snapshot(ui))
                assert "Existing memory" in screen
                assert screen.count("Database is unavailable") == 1
                assert ui.status_line() == "Database is unavailable."
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_startup_checks_management_runtime_and_loads_a_page_selected_during_preparation():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            release = threading.Event()
            def prepare():
                release.wait(timeout=2)
            with patch.object(ui.backend, "load", return_value=ui.backend.installation), \
                    patch.object(ui.backend, "prepare_management", side_effect=prepare) as preparation, \
                    patch.object(ui.backend, "request", return_value={"rows": [], "total": 0}) as read:
                ui.load()
                await wait_for(lambda: preparation.called)
                try:
                    ui.go("Memories")
                    await asyncio.sleep(0.08)
                    assert "Loading records" in "\n".join(snapshot(ui))
                    read.assert_not_called()
                finally:
                    release.set()
                await wait_for(lambda: ui.key() in ui.cache and not ui.busy)
                read.assert_called_once_with({"kind": "memories", "page": 0, "query": ""})
                assert ui.location.route == "Memories"
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_insertion_marker_tracks_partial_edits_and_horizontal_scrolling_without_exposing_passwords():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.app.output.size = Size(rows=24, columns=76)
            ui.go("Form:providers")
            await asyncio.sleep(0.08)
            field = ui.form["AGENT_PROVIDERS_OPENROUTER_BASE_URL"]
            ui.app.layout.focus(field)
            pipe.send_text("\x05\x1b[D\x1b[D")
            await asyncio.sleep(0.08)
            screen = ui.app.renderer._last_screen
            point = screen.cursor_positions[ui.app.layout.current_window]
            assert screen.show_cursor
            assert "class:input.cursor" in screen.data_buffer[point.y][point.x].style
            position = field.buffer.cursor_position
            pipe.send_text("X")
            await wait_for(lambda: field.text[position:position + 1] == "X")
            assert field.text == "https://openrouter.ai/api/Xv1"
            pipe.send_text("\x01\x0b" + "https://example.test/" + "a" * 140)
            await asyncio.sleep(0.08)
            assert field.window.horizontal_scroll > 0
            screen = ui.app.renderer._last_screen
            point = screen.cursor_positions[ui.app.layout.current_window]
            assert 0 <= point.x < 76 and 0 <= point.y < 24
            assert "class:input.cursor" in screen.data_buffer[point.y][point.x].style
            ui.app.layout.focus(ui.form["AGENT_LLM_OPENROUTER_API_KEY"])
            pipe.send_text("PRIVATE-KEY\x1b[D")
            await asyncio.sleep(0.08)
            screen = ui.app.renderer._last_screen
            point = screen.cursor_positions[ui.app.layout.current_window]
            assert "class:input.cursor" in screen.data_buffer[point.y][point.x].style
            assert screen.data_buffer[point.y][point.x].char == "*"
            assert "PRIVATE-KEY" not in "\n".join(snapshot(ui))
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_background_completion_does_not_steal_modal_focus_or_leave_detached_focus_on_cancel():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            release = threading.Event()
            def read(payload):
                release.wait(timeout=2)
                return {"rows": [], "total": 0}
            with patch.object(ui.backend, "request", side_effect=read):
                ui.go("Memories")
                dialog = ui.app.create_background_task(ui.confirm_dialog(
                    "Edit text", "Keep typing while a previous read completes.", fields=(("body", "Text", False, ""),)))
                await wait_for(lambda: bool(ui.floats.floats))
                focused = ui.app.layout.current_control
                release.set()
                await wait_for(lambda: not ui.busy)
                assert ui.app.layout.current_control is focused
                pipe.send_text("Still editing")
                await wait_for(lambda: focused.buffer.text == "Still editing")
                pipe.send_text("\x1b")
                assert await dialog is False
                assert ui.app.layout.current_control in ui.app.layout.find_all_controls()
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_repeated_confirmation_requests_cannot_replace_the_active_dialog():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            first = ui.app.create_background_task(ui.confirm_dialog("First", "Keep this dialog."))
            await wait_for(lambda: bool(ui.floats.floats))
            focused = ui.app.layout.current_control
            assert await ui.confirm_dialog("Second", "Do not replace it.") is False
            assert ui.app.layout.current_control is focused
            pipe.send_text("\r")
            assert await first is False
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_long_record_text_is_readable_to_the_end_without_moving_the_fixed_footer():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.app.output.size = Size(rows=24, columns=76)
            content = "\n".join("Line " + str(i) for i in range(60)) + "\nEND OF RECORD"
            with patch.object(ui.backend, "request", return_value={
                "rows": [{"id": 7, "content": content}], "total": 1,
            }):
                ui.go("Record", {"id": 7, "content": content, "_kind": "Memories"})
                await wait_for(lambda: not ui.busy)
                await asyncio.sleep(0.08)
                control = next(control for control in ui.app.layout.find_all_controls()
                               if hasattr(control, "buffer") and control.buffer.text == content)
                ui.app.layout.focus(control)
                pipe.send_text("\x1b[1;5F")
                await asyncio.sleep(0.08)
                assert "END OF RECORD" in "\n".join(snapshot(ui))
                assert "Delete" in "\n".join(snapshot(ui)[-10:])
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_log_updates_preserve_the_active_text_control_and_cursor_position():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            with patch.object(ui.backend, "logs", side_effect=["One\nTwo\nThree", "One\nTwo\nThree\nFour"]):
                ui.go("Logs", {"id": "task-agent"})
                await wait_for(lambda: not ui.busy)
                ui.app.layout.focus(ui.log_view)
                pipe.send_text("\x1b[B\x1b[C")
                await asyncio.sleep(0.08)
                control = ui.app.layout.current_control
                position = control.buffer.cursor_position
                ui.refresh(background=True)
                await wait_for(lambda: not ui.busy)
                assert ui.app.layout.current_control is control
                assert control.buffer.cursor_position == position
                assert control.buffer.text.endswith("Four")
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_record_pagination_recovers_when_the_last_page_disappears():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            def read(payload):
                return {"rows": [] if payload["page"] else [{"id": 7, "content": "Remaining memory"}],
                        "total": 1}
            with patch.object(ui.backend, "request", side_effect=read):
                ui.go("Memories")
                await wait_for(lambda: not ui.busy)
                ui.location.page = 1
                ui.refresh()
                await wait_for(lambda: ui.location.page == 0 and not ui.busy and ui.key() in ui.cache)
                await asyncio.sleep(0.08)
                assert "Remaining memory" in "\n".join(snapshot(ui))
                assert "Page 1 of 1" in "\n".join(snapshot(ui))
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_confirmed_mutation_blocks_navigation_until_the_saved_state_is_reloaded():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.go("Settings")
            release = threading.Event()
            ui.task("Applying credentials", lambda: release.wait(timeout=2), mutating=True)
            try:
                ui.go("Form:models")
                assert ui.location.route == "Settings"
                assert "Wait for the confirmed change" in ui.status_line()
                ui.back()
                assert ui.location.route == "Settings"
            finally:
                release.set()
            await wait_for(lambda: not ui.busy)
            ui.go("Form:models")
            assert ui.location.route == "Form:models"
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_invalid_dialog_input_keeps_the_editor_open_for_correction():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            with patch.object(ui.backend, "mutate") as mutate, patch.object(
                ui.backend, "request", return_value={"rows": [], "total": 0}
            ):
                ui.new_memory()
                await wait_for(lambda: bool(ui.floats.floats))
                editor = ui.app.layout.current_control
                pipe.send_text("\t\t\r")
                await asyncio.sleep(0.08)
                assert ui.floats.floats
                assert "Enter text before saving" in "\n".join(snapshot(ui))
                mutate.assert_not_called()
                assert ui.app.layout.current_control is editor
                pipe.send_text("Useful preference\t\t\r")
                await wait_for(lambda: not ui.floats.floats and not ui.busy)
                mutate.assert_called_once_with("create-memory", None, "Useful preference")
                assert editor.buffer.text == "Useful preference"
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_connection_labels_use_saved_yaml_defaults_instead_of_assuming_disabled():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            from assistant_agent.config import AppConfig
            ui.backend.installation.defaults = AppConfig({"agent": {"telegram": {"enabled": True}}})
            ui.go("Connections")
            await asyncio.sleep(0.08)
            assert "Telegram / enabled" in "\n".join(snapshot(ui))
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_opening_and_leaving_unchanged_instructions_does_not_create_unsaved_changes():
    async def scenario(root):
        instructions = root / "data/config/AGENT.md"
        instructions.parent.mkdir(parents=True)
        instructions.write_text("Existing instructions.")
        async with running(root) as (ui, pipe):
            ui.go("Instructions")
            ui.go("Settings")
            assert not ui.backend.dirty
            assert ui.backend.instructions is None
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_fast_keyboard_navigation_uses_the_new_screen_without_waiting_for_a_redraw():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.go("Settings")
            await asyncio.sleep(0.08)
            pipe.send_text("\x1b[B\x1b[B\rfast-model\rfast-fallback")
            await wait_for(lambda: ui.location.route == "Form:models" and
                           ui.form["AGENT_LLM_FALLBACK_MODEL"].text == "fast-fallback")
            assert ui.form["AGENT_LLM_MODEL"].text == "fast-model"
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_discarding_model_form_also_discards_its_generated_password():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.go("Form:catalog:database")
            ui.generate_password()
            await wait_for(lambda: not ui.busy)
            assert ui.form["AGENT_DATABASE_PASSWORD"].text
            assert "AGENT_DATABASE_PASSWORD" not in ui.backend.credentials
            ui.discard_form()
            assert not ui.backend.dirty
            assert "AGENT_DATABASE_PASSWORD" not in ui.backend.credentials
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_old_script_version_refresh_cannot_replace_the_newly_selected_version():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            release = threading.Event()
            def read(request):
                release.wait(timeout=2)
                return {"version": {"id": 11, "script_id": 7}, "id": 7, "entry": "old.py"}
            with patch.object(ui.backend, "request", side_effect=read):
                ui.go("Record", {"id": 7, "version_id": 11, "entry": "old.py", "_kind": "Scripts"})
                await wait_for(lambda: ui.busy)
                ui.go("Record", {"id": 7, "version_id": 12, "entry": "new.py", "_kind": "Scripts"})
                ui.refresh_pending = False
                release.set()
                await wait_for(lambda: not ui.busy)
                assert ui.location.record["version_id"] == 12
                assert ui.location.record["entry"] == "new.py"
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_native_launcher_opens_in_a_real_terminal_and_quits_without_saving():
    import uvicorn
    from assistant_agent.management_api import create_app, ensure_token
    repository = Path(__file__).resolve().parents[3]
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "src/scripts").mkdir(parents=True)
        (root / "src/agent").mkdir()
        shutil.copytree(repository / "src/config", root / "src/config")
        (root / "data").mkdir()
        shutil.copy2(repository / "run.sh", root / "run.sh")
        shutil.copy2(repository / "src/scripts/run.sh", root / "src/scripts/run.sh")
        (root / "src/agent/src").symlink_to(repository / "src/agent/src", target_is_directory=True)
        (root / "data/.venv-setup").symlink_to(sys.prefix, target_is_directory=True)
        token = ensure_token(root)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(root, token), lifespan="off", log_level="error"))
        api_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
        api_thread.start()
        deadline = time.monotonic() + 5
        while not server.started:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        process = subprocess.Popen(
            [str(root / "run.sh"), "--section", "models"], cwd=root,
            stdin=slave, stdout=slave, stderr=slave,
            env=dict(os.environ, PYTHON=sys.executable, TERM="xterm-256color", ASSISTANT_HOST_ADMIN="1",
                     ASSISTANT_MANAGEMENT_URL=f"http://127.0.0.1:{port}"),
        )
        os.close(slave)
        transcript = b""
        def wait_for_text(expected):
            nonlocal transcript
            deadline = time.monotonic() + 10
            while expected not in transcript:
                assert process.poll() is None, transcript.decode(errors="replace")
                assert time.monotonic() < deadline, transcript.decode(errors="replace")
                if select.select([master], [], [], 0.1)[0]:
                    transcript += os.read(master, 65536)
        try:
            wait_for_text(b"Primary model")
            wait_for_text(b"[F1]")
            transcript = b""
            os.write(master, b"\x1bOP")
            wait_for_text(b"Provider model identifier")
            transcript = b""
            os.write(master, b"\x1b")
            wait_for_text(b"Primary model")
            os.write(master, b"\x11")
            wait_for_text(b"Leave Assistant?")
            os.write(master, b"\t\r")
            assert process.wait(timeout=10) == 0
            assert not (root / "data/.env").exists()
            assert not (root / "data/.secrets").exists()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            os.close(master)
            server.should_exit = True
            api_thread.join(timeout=5)
            listener.close()


def test_every_screen_and_form_renders_at_minimum_size_with_reachable_controls():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.app.output.size = Size(rows=24, columns=76)
            service = {"id": "task-agent", "title": "Task worker", "state": "running", "health": "healthy"}
            ui.services = [service]
            ui.services_loaded = True
            row = {"id": 7, "content": "Saved memory", "title": "Saved item", "status": "needs_review",
                   "metadata": {"state": "suggested"}, "version_id": 11}
            routes = [(name, None) for name in (
                "Home", "Services", "Activity", "Connections", "Settings", "Help", "Advanced",
                "Instructions", "Review", "Storage", "Search", "Credentials", "Pending deletions",
                "Tasks", "Memories", "Reminders", "Projects", "Scripts",
            )]
            routes += [("Form:" + group, None) for group in ui.groups]
            routes += [("Connection:" + group, None) for group in
                       ("email", "telegram", "calendar", "zoom", "dropbox", "transcription")]
            routes += [("Service", service), ("Logs", service), ("Events", row), ("Objects", row)]
            routes += [("Record", {**row, "_kind": kind}) for kind in
                       ("Tasks", "Memories", "Reminders", "Projects", "Scripts", "Events", "Objects")]
            routes += [("Project deletion", {**row, "_preview": {"preview_digest": "reviewed", "objects": [1, 2]}})]
            with patch.object(ui.backend, "services", return_value=[service]), \
                    patch.object(ui.backend, "request", return_value={"rows": [row], "total": 1}), \
                    patch.object(ui.backend, "logs", return_value="Recent component logs"), \
                    patch.object(ui.backend, "pending_deletions", return_value=[{"id": 8, "status": "failed"}]):
                for route, record in routes:
                    ui.go(route, record)
                    await wait_for(lambda: not ui.busy)
                    assert not ui.error, (route, ui.error)
                    assert ui.location.route == route
                    assert page_help(route)
                    for action in list(ui.actions):
                        ui.app.layout.focus(action.window)
                        ui.app.invalidate()
                        await asyncio.sleep(0.01)
                        screen = "\n".join(snapshot(ui))
                        assert "Ctrl-Q Quit" in screen, (route, screen)
                        assert action.label in screen, (route, action.label, screen)
                        assert ui.context_topic() == (action.label, action.help_text), (route, action.label)
                        assert "About:" in screen and "[F1]" in screen, (route, screen)
                        assert action.help_text, (route, action.label)
                    if ui.form and ui.form_group != "instructions":
                        for spec in ui.groups[ui.form_group][1]:
                            ui.app.layout.focus(ui.form[spec.key])
                            assert ui.context_topic() == (spec.label, field_help(spec)), (route, spec.key)
                    if ui.form:
                        last = list(ui.form.values())[-1]
                        ui.app.layout.focus(last)
                        ui.app.invalidate()
                        await asyncio.sleep(0.01)
                        assert ui.app.layout.current_window in list(ui.app.layout.get_visible_focusable_windows())
                    focused = ui.app.layout.current_control
                    topic = ui.context_topic()
                    ui.show_context_help()
                    await wait_for(lambda: bool(ui.floats.floats))
                    help_control = ui.app.layout.current_control
                    assert help_control.buffer.read_only(), route
                    assert topic[1] in help_control.buffer.text, route
                    assert page_help(route) in help_control.buffer.text, route
                    pipe.send_text("\x1b")
                    await wait_for(lambda: not ui.floats.floats)
                    assert ui.app.layout.current_control is focused, route
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_context_help_changes_with_keyboard_focus_and_f1_preserves_edits_at_minimum_size():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.app.output.size = Size(rows=24, columns=76)
            ui.go("Advanced")
            calendar = next(item for item in ui.actions if item.label == "Calendar privacy")
            limits = next(item for item in ui.actions if item.label == "Task limits")
            ui.app.layout.focus(calendar.window)
            assert ui.context_topic()[1] == GROUP_HELP["advanced:Calendar privacy"]
            pipe.send_text("\t")
            await wait_for(lambda: ui.app.layout.current_control is limits.control)
            assert ui.context_topic()[1] == GROUP_HELP["advanced:Task limits"]
            pipe.send_text("\r")
            await wait_for(lambda: ui.location.route == "Form:advanced:Task limits")
            ui.form["AGENT_LIMITS_MAX_ITERATIONS_PER_TASK"].text = "75"
            focused = ui.app.layout.current_control
            before_values = dict(ui.backend.values)
            pipe.send_text("\x1bOP")
            await wait_for(lambda: bool(ui.floats.floats))
            await asyncio.sleep(0.08)
            screen = "\n".join(snapshot(ui))
            assert "Maximum model/tool-loop iterations" in screen
            assert "Close" in screen and "Help" in screen
            help_control = ui.app.layout.current_control
            assert help_control.buffer.read_only()
            pipe.send_text("\x1b[1;5F")
            await asyncio.sleep(0.08)
            assert "docs/advanced-configuration.md" in "\n".join(snapshot(ui)), "\n".join(snapshot(ui))
            pipe.send_text("\x1b")
            await wait_for(lambda: not ui.floats.floats)
            assert ui.app.layout.current_control is focused
            assert ui.form["AGENT_LIMITS_MAX_ITERATIONS_PER_TASK"].text == "75"
            assert ui.backend.values == before_values
            assert not (root / "data/.env").exists()
            assert not ui.dialog_topics and ui.dialog_topic is None
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_context_help_explains_every_field_and_keeps_secret_values_out_of_help():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            for group, (_, specs) in ui.groups.items():
                ui.go("Form:" + group)
                for spec in specs:
                    widget = ui.form[spec.key]
                    ui.app.layout.focus(widget)
                    assert ui.context_topic() == (spec.label, field_help(spec))
                    if spec.secret:
                        widget.text = "private-credential-not-for-help"
                        assert "private-credential-not-for-help" not in ui.context_topic()[1]
                        assert "Blank keeps an existing credential" in ui.context_topic()[1]
                ui.discard_form()
            for item in ui.nav:
                ui.app.layout.focus(item.window)
                assert ui.context_topic() == (item.label, page_help(item.label))
            for name in sorted(SECRET_NAMES | {"AGENT_CALENDAR_GOOGLE_2_REFRESH_TOKEN"}):
                ui.backend.installation.secret_values = {name: "private-credential-not-for-help"}
                ui.go("Credentials")
                ui.render()
                item = next(item for item in ui.actions if item.label.startswith(name))
                ui.app.layout.focus(item.window)
                assert credential_help(name) in ui.context_topic()[1]
                assert "private-credential-not-for-help" not in ui.context_topic()[1]
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_help_dialog_does_not_replace_an_active_confirmation_or_submit_it():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            operation = Mock()
            ui.confirm_action("Delete memory", "Permanently remove this memory?", "Delete", operation)
            await wait_for(lambda: bool(ui.floats.floats))
            original = ui.dialog_future
            pipe.send_text("\x1bOP")
            await asyncio.sleep(0.08)
            assert ui.dialog_future is original
            assert ui.context_topic()[0] == "Cancel"
            operation.assert_not_called()
            pipe.send_text("\x1b")
            await wait_for(lambda: not ui.floats.floats)
            operation.assert_not_called()
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_help_restores_the_selected_action_after_a_background_refresh_rebuilds_the_page():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            service = {"id": "task-agent", "title": "Task worker", "state": "running", "health": "healthy"}
            with patch.object(ui.backend, "services", return_value=[service]):
                ui.go("Services")
                await wait_for(lambda: not ui.busy)
            selected = next(item for item in ui.actions if item.label == "Stop app")
            ui.app.layout.focus(selected.window)
            release = threading.Event()
            def read_services():
                release.wait(timeout=3)
                return [{**service, "health": "unhealthy"}]
            with patch.object(ui.backend, "services", side_effect=read_services):
                ui.refresh()
                await wait_for(lambda: ui.busy)
                ui.show_context_help()
                await wait_for(lambda: bool(ui.floats.floats))
                help_control = ui.app.layout.current_control
                release.set()
                await wait_for(lambda: not ui.busy and ui.render_pending)
                assert ui.app.layout.current_control is help_control
                pipe.send_text("\x1b")
                await wait_for(lambda: not ui.floats.floats)
                replacement = next(item for item in ui.actions if item.label == "Stop app")
                assert replacement.control is not selected.control
                assert ui.app.layout.current_control is replacement.control
                assert ui.context_topic() == (replacement.label, replacement.help_text)
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_long_confirmation_explanations_are_scrollable_without_submitting_the_action():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.app.output.size = Size(rows=24, columns=76)
            operation = Mock()
            message = "\n".join("Consequence " + str(i) for i in range(30)) + "\nEND OF WARNING"
            ui.confirm_action("Review consequences", message, "Confirm", operation)
            await wait_for(lambda: bool(ui.floats.floats))
            pipe.send_text("\x1b[Z")
            await asyncio.sleep(0.08)
            assert ui.app.layout.current_control.buffer.read_only()
            pipe.send_text("\x1b[1;5F")
            await asyncio.sleep(0.08)
            assert "END OF WARNING" in "\n".join(snapshot(ui))
            pipe.send_text("\x1b")
            await wait_for(lambda: not ui.floats.floats)
            operation.assert_not_called()
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_every_record_mutation_is_wired_to_the_expected_confirmed_backend_action():
    cases = [
        ("Tasks", "Retry task", "retry", False), ("Tasks", "Cancel task", "cancel", False),
        ("Tasks", "Approve review", "review", False), ("Tasks", "Add instruction", "instruct", True),
        ("Memories", "Edit", "update-memory", True), ("Memories", "Pin", "pin-memory", False),
        ("Memories", "Unpin", "unpin-memory", False), ("Memories", "Delete", "delete-memory", False),
        ("Memories", "Approve suggestion", "approve-memory", False),
        ("Memories", "Reject suggestion", "reject-memory", False),
        ("Reminders", "Cancel reminder", "cancel-reminder", False),
        ("Scripts", "Block", "block-script", False), ("Scripts", "Unblock", "unblock-script", False),
        ("Scripts", "Revoke approvals", "revoke-script", False),
    ]
    async def scenario(root):
        async with running(root) as (ui, pipe):
            for kind, label, action, editing in cases:
                row = {"id": 7, "_kind": kind, "version_id": 11, "content": "Existing preference",
                       "status": "needs_review", "metadata": {"state": "suggested"},
                       "pinned": label == "Unpin", "blocked": label == "Unblock"}
                def read(request):
                    return {**row, "version": {"id": 11}} if request["kind"] == "script" else {"rows": [row], "total": 1}
                with patch.object(ui.backend, "request", side_effect=read), \
                        patch.object(ui.backend, "mutate", return_value={"result": {"status": "updated"}}) as mutate:
                    ui.go("Record", row)
                    await wait_for(lambda: not ui.busy)
                    selected = next(item for item in ui.actions if item.label == label)
                    selected.handler()
                    await wait_for(lambda: bool(ui.floats.floats))
                    pipe.send_text("\x1b")
                    await wait_for(lambda: not ui.floats.floats)
                    mutate.assert_not_called()
                    selected.handler()
                    await wait_for(lambda: bool(ui.floats.floats))
                    if editing:
                        ui.app.layout.current_control.buffer.text = "Updated preference"
                    pipe.send_text("\t\t\r" if editing else "\t\r")
                    await wait_for(lambda: mutate.called and not ui.busy and not ui.floats.floats)
                    mutate.assert_called_once_with(action, 7, *("Updated preference",) if editing else ())
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_new_memory_and_administration_screen_actions_confirm_before_writing():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            service = {"id": "task-agent", "title": "Task worker", "state": "running", "health": "healthy"}
            with patch.object(ui.backend, "request", return_value={"rows": [], "total": 0}), \
                    patch.object(ui.backend, "services", return_value=[service]):
                ui.services = [service]
                cases = [
                    ("Services", "Start app", "lifecycle", ("start", None), [service], None),
                    ("Services", "Stop app", "lifecycle", ("stop", None), [service], None),
                    ("Service", "Start", "lifecycle", ("start", "task-agent"), [service], None),
                    ("Service", "Restart", "lifecycle", ("restart", "task-agent"), [service], None),
                    ("Service", "Apply settings", "lifecycle", ("apply", "task-agent"), [service], None),
                    ("Service", "Stop", "lifecycle", ("stop", "task-agent"), [service], None),
                    ("Memories", "New memory", "mutate", ("create-memory", None, "A new preference"), {}, "A new preference"),
                    ("Search", "Reindex", "embeddings", ("reindex",), {"refreshed": 1}, None),
                    ("Credentials", "Activate pending", "apply_credentials", (None,), [], None),
                    ("Storage", "Move credentials to a Docker volume", "storage", (True,), [], None),
                    ("Storage", "Recover an interrupted database password rotation", "recover", (), None, None),
                    ("Storage", "Reset bundled database", "reset", (), "Reset complete", "RESET"),
                ]
                for route, label, method, arguments, result, text in cases:
                    ui.go(route, service if route == "Service" else None)
                    await wait_for(lambda: not ui.busy)
                    with patch.object(ui.backend, method, return_value=result) as operation:
                        selected = next(item for item in ui.actions if item.label == label)
                        selected.handler()
                        await wait_for(lambda: bool(ui.floats.floats))
                        pipe.send_text("\x1b")
                        await wait_for(lambda: not ui.floats.floats)
                        operation.assert_not_called()
                        selected.handler()
                        await wait_for(lambda: bool(ui.floats.floats))
                        if text is not None:
                            ui.app.layout.current_control.buffer.text = text
                        pipe.send_text(("\t\t\t\r" if method == "reset" else "\t\t\r") if text is not None else "\t\r")
                        await wait_for(lambda: operation.called and not ui.busy and not ui.floats.floats)
                        if method == "reset":
                            operation.assert_called_once_with(clean_install=False, keep_mailbox_position=True)
                        else:
                            operation.assert_called_once_with(*arguments)
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_storage_actions_fit_small_terminals_and_clean_install_requires_confirmation():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            for rows, columns in ((25, 100), (24, 76)):
                ui.app.output.size = Size(rows=rows, columns=columns)
                ui.backend.installation.state["previous_credential_volume"] = "old-credentials"
                ui.go("Storage")
                await asyncio.sleep(0.08)
                rendered = "\n".join(snapshot(ui))
                for label in ("Credentials", "Recovery", "Factory reset", "Finish credential storage change",
                              "Reset bundled database", "Clean install (also delete files)"):
                    assert label in rendered
            with patch.object(ui.backend, "reset", return_value="Clean install complete") as reset, \
                    patch.object(ui.backend, "services", return_value=[]):
                selected = next(item for item in ui.actions if item.label == "Clean install (also delete files)")
                selected.handler()
                await wait_for(lambda: bool(ui.floats.floats))
                assert "data/share/" in ui.dialog_topic[1]
                assert "data/private/artifacts/" in ui.dialog_topic[1]
                assert "data/config/AGENT.md are preserved" in ui.dialog_topic[1]
                pipe.send_text("\x1b")
                await wait_for(lambda: not ui.floats.floats)
                reset.assert_not_called()
                selected.handler()
                await wait_for(lambda: bool(ui.floats.floats))
                ui.app.layout.current_control.buffer.text = "reset"
                pipe.send_text("\t\t\t\r")
                await asyncio.sleep(0.08)
                assert ui.floats.floats
                reset.assert_not_called()
                ui.app.layout.current_control.buffer.text = "RESET"
                pipe.send_text("\t\t\t\r")
                await wait_for(lambda: reset.called and not ui.busy and not ui.floats.floats)
                reset.assert_called_once_with(clean_install=True, keep_mailbox_position=True)
                reset.reset_mock()
                selected.handler()
                await wait_for(lambda: bool(ui.floats.floats))
                ui.app.layout.current_control.buffer.text = "RESET"
                pipe.send_text("\t \t\t\r")
                await wait_for(lambda: reset.called and not ui.busy and not ui.floats.floats)
                reset.assert_called_once_with(clean_install=True, keep_mailbox_position=False)
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_invalid_search_and_credential_names_stay_in_their_dialog_for_correction():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            with patch.object(ui.backend, "request", return_value={"rows": [], "total": 0}) as request:
                ui.go("Memories")
                await wait_for(lambda: not ui.busy)
                calls = request.call_count
                ui.search()
                await wait_for(lambda: bool(ui.floats.floats))
                ui.app.layout.current_control.buffer.text = "x" * 501
                pipe.send_text("\t\t\r")
                await asyncio.sleep(0.08)
                assert ui.floats.floats
                assert request.call_count == calls
                pipe.send_text("\x1b")
                await wait_for(lambda: not ui.floats.floats)
                ui.credential(None)
                await wait_for(lambda: bool(ui.floats.floats))
                ui.app.layout.current_control.buffer.text = "UNKNOWN"
                pipe.send_text("\t\t\r")
                await asyncio.sleep(0.08)
                assert ui.floats.floats
                pipe.send_text("\x1b")
                await wait_for(lambda: not ui.floats.floats)
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_targeted_settings_launch_does_not_require_a_management_image_or_docker():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.go("Form:identity")
            with patch.object(ui.backend, "prepare_management", side_effect=AssertionError("Unrequested Docker check")), \
                    patch.object(ui.backend, "services", side_effect=AssertionError("Unrequested Docker check")):
                ui.load()
                await wait_for(lambda: not ui.busy)
                await asyncio.sleep(0.05)
                assert not ui.error
                assert ui.location.route == "Form:identity"
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_long_advanced_field_labels_are_not_truncated_at_minimum_width():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.app.output.size = Size(rows=24, columns=76)
            for group, key in (
                ("advanced:Calendar privacy", "AGENT_CALENDAR_POLICY_ALLOW_READ_EVENT_DETAILS"),
                ("advanced:Model preset tool", "AGENT_MODEL_SERVICE_BUDGET_USD"),
            ):
                ui.go("Form:" + group)
                ui.app.layout.focus(ui.form[key])
                ui.app.invalidate()
                await asyncio.sleep(0.08)
                label = next(spec.label for spec in GROUPS[group][1] if spec.key == key)
                screen = "".join("".join(snapshot(ui)).replace("│", "").split())
                assert "".join(label.split()) in screen, "\n".join(snapshot(ui))
                if isinstance(ui.form[key], Checkbox):
                    checked = ui.form[key].checked
                    pipe.send_text(" ")
                    await wait_for(lambda: ui.form[key].checked != checked)
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_credential_replacement_and_database_rotation_require_confirmation_and_mask_input():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            ui.go("Credentials")
            for name in ("AGENT_LLM_OPENROUTER_API_KEY", "AGENT_DATABASE_PASSWORD"):
                with patch.object(ui.backend, "replace_credential") as replace:
                    ui.credential(name)
                    await wait_for(lambda: bool(ui.floats.floats))
                    pipe.send_text("\x1b")
                    await wait_for(lambda: not ui.floats.floats)
                    replace.assert_not_called()
                    ui.credential(name)
                    await wait_for(lambda: bool(ui.floats.floats))
                    value = "replacement-value-1234567890"
                    ui.app.layout.current_control.buffer.text = value
                    await asyncio.sleep(0.08)
                    assert value not in "\n".join(snapshot(ui))
                    pipe.send_text("\t\t\r")
                    await wait_for(lambda: replace.called and not ui.busy and not ui.floats.floats)
                    replace.assert_called_once_with(name, value)
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))


def test_project_preview_deletion_and_resumption_use_the_confirmed_inventory():
    async def scenario(root):
        async with running(root) as (ui, pipe):
            row = {"id": 7, "title": "Project"}
            with patch.object(ui.backend, "request", return_value={"rows": [row], "total": 1}), \
                    patch.object(ui.backend, "pending_deletions", return_value=[{"id": 19, "status": "incomplete"}]):
                ui.go("Projects")
                await wait_for(lambda: not ui.busy)
                with patch.object(ui.backend, "preview_project", return_value={"preview_digest": "reviewed"}) as preview:
                    ui.project_preview(row)
                    await wait_for(lambda: not ui.busy and ui.location.route == "Project deletion")
                    preview.assert_called_once_with(7)
                for resume in (False, True):
                    if resume:
                        ui.go("Pending deletions")
                        await wait_for(lambda: not ui.busy)
                        action = lambda: ui.resume_project(19)
                        method, arguments = "resume_deletion", (19,)
                    else:
                        action = next(item.handler for item in ui.actions if item.label == "Delete previewed project")
                        method, arguments = "delete_project", (7, "reviewed")
                    with patch.object(ui.backend, method) as operation:
                        action()
                        await wait_for(lambda: bool(ui.floats.floats))
                        pipe.send_text("\x1b")
                        await wait_for(lambda: not ui.floats.floats)
                        operation.assert_not_called()
                        action()
                        await wait_for(lambda: bool(ui.floats.floats))
                        pipe.send_text("\t\r")
                        await wait_for(lambda: operation.called and not ui.busy and not ui.floats.floats)
                        operation.assert_called_once_with(*arguments)
                        assert ui.location.route == "Projects"
    with tempfile.TemporaryDirectory() as root:
        asyncio.run(scenario(Path(root)))
