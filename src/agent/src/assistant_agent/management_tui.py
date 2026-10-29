"""A persistent, stateful terminal management application."""
import asyncio
import json
import math
import textwrap
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import yaml
from prompt_toolkit.application import Application, get_app, run_in_terminal
from prompt_toolkit.cursor_shapes import CursorShape
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer, Dimension, Float, FloatContainer, HSplit, Layout,
    ScrollablePane, VSplit, Window, to_container,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.processors import Processor, Transformation
from prompt_toolkit.layout.utils import explode_text_fragments
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Box, Checkbox, Dialog, TextArea

from .management import GROUPS, Management
from .management_help import ACTION_HELP, SERVICE_HELP, credential_help, field_help, page_help
from .secret_store import SECRET_NAMES
from .setup import SetupError, setup_failure_message, validate_secret
from .setup_privileges import terminal_authorization


STYLE = Style.from_dict({
    "": "fg:default bg:default",
    "frame.label": "ansicyan bold", "title": "bold", "muted": "",
    "nav.current": "ansicyan bold underline", "action.focused": "reverse bold",
    "action": "", "primary": "ansicyan bold", "danger": "ansired bold",
    "ok": "ansigreen", "error": "ansired", "input": "underline",
    "input.focused": "ansicyan underline",
    "input.cursor": "bg:ansicyan fg:ansiblack noreverse nounderline bold",
    "checkbox-selected": "ansicyan bold", "scrollbar.button": "reverse",
    "dialog": "fg:#eeeeee bg:#20252b",
    "dialog.body": "fg:#eeeeee bg:#20252b",
    "dialog.body text-area": "fg:#eeeeee bg:#20252b",
    "dialog.body text-area last-line": "nounderline",
    "dialog.body input.focused": "fg:#87d7ff bg:#20252b underline",
    "dialog.body action.focused": "fg:#20252b bg:#87d7ff noreverse bold",
    "dialog.body error": "fg:#ff8787 bg:#20252b",
    "dialog.body scrollbar.background": "bg:#20252b",
    "dialog.body scrollbar.button": "fg:#eeeeee bg:#5f6873 noreverse",
    "dialog.body scrollbar.arrow": "fg:#eeeeee bg:#20252b",
    "dialog.body shadow": "bg:#101418",
    "dialog frame.label": "fg:#87d7ff bg:#20252b bold",
})
SECTIONS = ("Home", "Services", "Activity", "Connections", "Settings", "Help")
SPINNER = ("|", "/", "-", "\\")


class HelpLexer(Lexer):
    def lex_document(self, document):
        headings = {"Navigation", "Docker and authorization", "Settings and data",
                    "System and functionality questions"}
        def line(number):
            value = document.lines[number]
            return [("class:title" if value in headings else "", value)]
        return line


def text(value="", height=1, style=""):
    return Window(FormattedTextControl(value), height=height, wrap_lines=True,
                  always_hide_cursor=True, style=style)


def clean(value):
    """Records and logs must never supply terminal control sequences."""
    return "".join(char for char in str(value) if char in "\n\t" or ord(char) >= 32 and not 127 <= ord(char) < 160)


class InsertionCursor(Processor):
    def apply_transformation(self, ti):
        fragments = explode_text_fragments(ti.fragments)
        if get_app().layout.has_focus(ti.buffer_control) and ti.lineno == ti.document.cursor_position_row:
            column = ti.source_to_display(ti.document.cursor_position_col)
            if column == len(fragments):
                fragments.append(("class:input.cursor", " "))
            elif column < len(fragments):
                style, char, *rest = fragments[column]
                fragments[column] = (style + " class:input.cursor", char, *rest)
        return Transformation(fragments)


def input_field(value="", *, password=False, multiline=False, height=1, scrollbar=False):
    widget = TextArea(text=value, password=password, multiline=multiline, height=height,
                      wrap_lines=multiline, scrollbar=scrollbar, focus_on_click=True,
                      input_processors=[InsertionCursor()])
    widget.window.style = lambda: "class:text-area class:input" + (
        ".focused" if get_app().layout.has_focus(widget) else "")
    return widget


def checkbox_field(label, checked):
    widget = Checkbox(label, checked=checked)
    bindings = KeyBindings()
    def toggle(event):
        widget.checked = not widget.checked
    bindings.add(" ")(toggle)
    bindings.add("enter")(toggle)
    widget.control.key_bindings = bindings
    widget.window.wrap_lines = Condition(lambda: True)
    widget.window.right_margins = []
    return widget


class Action:
    def __init__(self, label, handler, *, help_text, style="", current=None):
        self.label, self.handler = label, handler
        self.help_text = help_text
        self.style, self.current = style, current
        bindings = KeyBindings()
        bindings.add("enter")(lambda event: self.handler())
        bindings.add(" ")(lambda event: self.handler())
        self.control = FormattedTextControl(self.fragments, focusable=True,
                                            key_bindings=bindings, show_cursor=False)
        self.window = Window(self.control, height=1, wrap_lines=False, always_hide_cursor=True)

    def fragments(self):
        focused = get_app().layout.has_focus(self.control)
        style = "class:action.focused" if focused else (
            "class:nav.current" if self.current and self.current() else self.style or "class:action")
        return [(style, (" > " if focused else "   ") + clean(self.label), self.mouse)]

    def mouse(self, event):
        if event.event_type == MouseEventType.MOUSE_DOWN:
            get_app().layout.focus(self.window)
        elif event.event_type == MouseEventType.MOUSE_UP:
            self.handler()


@dataclass
class Location:
    route: str
    page: int = 0
    query: str = ""
    record: dict | None = None
    focus: str | None = None


class ManagementApp:
    def __init__(self, root, *, input=None, output=None, backend=None):
        self.root = Path(root)
        self.backend = backend or Management(self.root, self.confirm_from_worker)
        self.location = Location("Home")
        self.history = []
        self.cache = {}
        self.read_errors = {}
        self.refresh_pending = False
        self.services = []
        self.services_loaded = False
        self.loaded = False
        self.busy = False
        self.quiet_refresh = False
        self.busy_notice = ""
        self.mutating = False
        self.operation = ""
        self.started = 0.0
        self.status = ""
        self.error = ""
        self.loading_error = ""
        self.active_task = None
        self.dialog_future = None
        self.refresh_task = None
        self.live_logs = False
        self.dialog_focus = None
        self.render_pending = False
        self.log_view = None
        self.log_key = None
        self.form = {}
        self.form_group = None
        self.actions = []
        self.control_topics = {}
        self.dialog_topics = {}
        self.dialog_topic = None
        self.page_title = ""
        self.title = text(lambda: self.page_title, style="class:title")
        self.body = HSplit([])
        self.scroll = ScrollablePane(self.body, show_scrollbar=True, display_arrows=False)
        self.footer_actions = HSplit([])
        self.pagination = HSplit([])
        nav = [Action(label, partial(self.go, label),
                      help_text=page_help(label),
                      current=partial(self.in_section, label)) for label in SECTIONS]
        self.nav = nav
        for action in nav:
            action.window.width = Dimension.exact(len(action.label) + 4)
        surface = HSplit([
            VSplit([*[action.window for action in nav], Window()], height=1), text(),
            self.title,
            text(lambda: "About: " + clean(self.context_topic()[0])[:48] + "  [F1]", style="class:title"),
            text(self.context_preview, height=2, style="class:muted"), text(),
            self.scroll,
            self.footer_actions, self.pagination,
            ConditionalContainer(text(self.status_line, height=Dimension(min=1, max=2)),
                                 Condition(lambda: bool(self.status_line()))),
            text("Tab Move  Enter Open  F1 Help  Esc Back  F5 Refresh  Ctrl-Q Quit"),
        ])
        canvas = Box(surface, padding=0, padding_left=1, padding_right=1)
        fits = Condition(lambda: self.app.output.get_size().columns >= 76
                         and self.app.output.get_size().rows >= 24)
        root_container = HSplit([
            ConditionalContainer(canvas, fits),
            ConditionalContainer(Box(text(
                "Resize to at least 76 columns and 24 rows.\n"
                "Your screen and draft are preserved. Ctrl-Q exits.", height=3), padding=2), ~fits),
        ])
        self.floats = FloatContainer(root_container, floats=[])
        keys = KeyBindings()
        forward = lambda event: self.move_focus(1)
        backward = lambda event: self.move_focus(-1)
        keys.add("tab")(forward)
        keys.add("s-tab")(backward)
        on_action = Condition(lambda: not isinstance(self.app.layout.current_control, BufferControl))
        single_line = Condition(lambda: isinstance(self.app.layout.current_control, BufferControl)
                                and not self.app.layout.current_control.buffer.multiline())
        keys.add("down", filter=on_action | single_line)(forward)
        keys.add("up", filter=on_action | single_line)(backward)
        keys.add("enter", filter=single_line)(forward)
        keys.add("right", filter=on_action)(forward)
        keys.add("left", filter=on_action)(backward)
        keys.add("escape")(lambda event: self.back())
        keys.add("c-q")(lambda event: self.quit())
        keys.add("f5")(lambda event: self.refresh())
        keys.add("f1")(lambda event: self.show_context_help())
        keys.add("c-s")(lambda event: self.review())
        self.app = Application(
            layout=Layout(self.floats), style=STYLE, key_bindings=keys,
            full_screen=True, mouse_support=True, input=input, output=output,
            cursor=CursorShape.BEAM,
            include_default_pygments_style=False, refresh_interval=0.25,
        )
        self.render()

    def move_focus(self, direction):
        layout = self.app.layout
        layout.update_parents_relations()
        windows = list(layout.get_focusable_windows())
        if windows:
            index = windows.index(layout.current_window) if layout.current_window in windows else -direction
            layout.focus(windows[(index + direction) % len(windows)])

    def in_section(self, label):
        route = self.location.route
        return label == (
            "Services" if route.startswith(("Service", "Logs")) else
            "Activity" if route in {"Activity", "Tasks", "Memories", "Reminders", "Projects", "Scripts", "Record", "Events", "Objects"} else
            "Connections" if route == "Connections" or route.startswith("Connection:") else
            "Settings" if route in {"Settings", "Credentials", "Review", "Instructions", "Advanced", "Storage", "Search", "Pending deletions"} or route.startswith("Form:") else
            "Activity" if route == "Project deletion" else
            "Help" if route == "Help" else "Home")

    def breadcrumb(self):
        if not self.loaded:
            return "Welcome  /  Getting connected"
        route = self.location.route
        if route.startswith("Form:"):
            return "Settings  /  " + GROUPS[route[5:]][0]
        if route.startswith("Connection:"):
            return "Connections  /  " + GROUPS[route[11:]][0]
        if route == "Record" and self.location.record:
            return "Activity  /  " + self.location.record["_kind"].title() + "  /  Details"
        return route.replace(":", "  /  ")

    def status_line(self):
        if self.busy and (not self.quiet_refresh or self.busy_notice):
            elapsed = time.monotonic() - self.started
            detail = getattr(self.backend, "progress", "") or self.operation
            return (f"{SPINNER[int(elapsed * 4) % 4]} {detail}   {int(elapsed)}s\n" +
                    (self.busy_notice or ("Applying change; navigation resumes when finished."
                                         if self.mutating else "Navigation remains available.")))
        body_error = (not self.loaded and self.loading_error == self.error or
                      self.error == self.read_errors.get(self.key()) and (
                          self.location.route in {"Tasks", "Memories", "Reminders", "Projects", "Scripts", "Events", "Objects"}
                          and self.key() not in self.cache or
                          self.location.route == "Services" and not self.services_loaded and not self.services))
        if self.error and body_error:
            return ""
        return clean(self.backend.redact(self.error or self.status))[:500]

    def context_topic(self):
        control = self.app.layout.current_control
        if self.dialog_topic is not None:
            return self.dialog_topics.get(control, self.dialog_topic)
        if control in self.control_topics:
            return self.control_topics[control]
        action = next((item for item in [*self.nav, *self.actions] if item.control is control), None)
        if action:
            return action.label, action.help_text
        return "This page", page_help(self.location.route)

    def context_preview(self):
        explanation = " ".join(self.context_topic()[1].split())
        width = max(20, self.app.output.get_size().columns - 2)
        lines = textwrap.wrap(explanation, width)
        if len(lines) > 2:
            lines[1] = textwrap.shorten(lines[1], width=width - 3, placeholder="").rstrip() + "..."
        return "\n".join(lines[:2])

    def show_context_help(self):
        if self.floats.floats:
            return
        title, explanation = self.context_topic()
        message = (title + "\n\n" + explanation + "\n\nThis page\n" + page_help(self.location.route) +
                   "\n\nHelp is read-only. Esc closes it and returns to the same control. "
                   "Use arrows or Page Up / Page Down to scroll."
                   "\n\nDetailed guides:\ndocs/running-assistant.md\ndocs/advanced-configuration.md")
        async def show():
            await self.confirm_dialog("Help", message, "Close", details=True)
        self.app.create_background_task(show())

    def action(self, label, handler, *, primary=False, danger=False, help_text=None):
        if help_text is None:
            help_text = (page_help(handler.args[0]) if isinstance(handler, partial) and handler.func == self.go
                         else ACTION_HELP[label])
        action = Action(label, handler, help_text=help_text,
                        style="class:danger" if danger else "class:primary" if primary else "")
        self.actions.append(action)
        return action.window

    def put(self, title, explanation, items, *, actions=(), total=None):
        self.page_title = title
        def description():
            return textwrap.fill(explanation, max(20, self.app.output.get_size().columns - 3))
        self.body.children = [to_container(item) for item in [
            *([text(description, height=lambda: len(description().splitlines())), text()] if explanation else []),
            *items, Window(height=Dimension(min=0)),
        ]]
        for action in self.actions:
            if action.window in actions:
                action.window.width = Dimension.exact(len(action.label) + 4)
        self.footer_actions.children = [VSplit([*actions, Window()], height=1)] if actions else []
        self.pagination.children = []
        if total is not None:
            count = max(1, math.ceil(total / 8))
            self.pagination.children = [VSplit([
                self.action("Previous", partial(self.turn, -1)) if self.location.page else text(),
                text(f"Page {self.location.page + 1} of {count}  /  {total} items"),
                self.action("Next", partial(self.turn, 1)) if (self.location.page + 1) * 8 < total else text(),
            ], height=1)]
        if self.actions:
            selected = next((action for action in self.actions if action.label == self.location.focus), self.actions[0])
            self.app.layout.focus(selected.window)
        else:
            selected = next(action for action in self.nav if self.in_section(action.label))
            self.app.layout.focus(selected.window)
        self.app.invalidate()

    def save_form(self):
        if self.form_group is not None:
            values = {key: ("true" if widget.checked else "false") if isinstance(widget, Checkbox)
                      else widget.text for key, widget in self.form.items()}
            if self.form_group == "instructions":
                if values["instructions"] != self.backend.instruction_text():
                    self.backend.instructions = values["instructions"].encode()
                    self.backend.dirty = True
                elif not self.backend.installation.prompt_original and values["instructions"].strip():
                    self.backend.instructions = values["instructions"].encode()
                    self.backend.dirty = True
            else:
                self.backend.stage(self.form_group, values)
            self.form_group = None
            self.form = {}
        return True

    def go(self, route, record=None):
        if self.floats.floats:
            return
        if self.busy and self.mutating:
            self.busy_notice = "Wait for the confirmed change to finish before navigating."
            return
        if route == self.location.route and record == self.location.record:
            return
        try:
            self.save_form()
        except SetupError as error:
            self.error = str(error)
            self.app.invalidate()
            return
        self.location.focus = next((action.label for action in self.actions
                                    if self.app.layout.has_focus(action.control)), None)
        self.history.append(self.location)
        self.location = Location(route, record=record)
        self.scroll.vertical_scroll = 0
        self.error = ""
        self.live_logs = False
        self.render()
        self.refresh()

    def back(self):
        if self.floats.floats:
            self.close_dialog(False)
            return
        if self.busy and self.mutating:
            self.busy_notice = "Wait for the confirmed change to finish before navigating."
            return
        try:
            self.save_form()
        except SetupError as error:
            self.error = str(error)
            return
        self.location = self.history.pop() if self.history else Location("Home")
        self.scroll.vertical_scroll = 0
        self.live_logs = False
        self.error = ""
        self.render()
        self.refresh()

    def turn(self, direction):
        self.location.page = max(0, self.location.page + direction)
        self.render()
        self.refresh()

    def task(self, label, operation, done=None, *, report=True, mutating=False, quiet=False):
        if self.busy:
            self.busy_notice = "Wait for the current operation to finish."
            self.app.invalidate()
            return
        self.busy = True
        self.busy_notice = ""
        self.quiet_refresh = quiet
        self.mutating = mutating
        self.backend.progress = ""
        self.operation, self.started = label, time.monotonic()
        if not quiet:
            self.error = ""
        key = self.key()
        previous_error = self.read_errors.get(key)
        if not quiet:
            self.read_errors.pop(key, None)

        async def execute():
            try:
                result = await asyncio.to_thread(operation)
                if quiet:
                    self.read_errors.pop(key, None)
                    if previous_error and self.error == previous_error:
                        self.error = ""
                if report:
                    self.status = label + " completed."
                if done:
                    done(result)
            except SetupError as error:
                self.error = str(error)
                self.read_errors[key] = self.error
                if not self.loaded:
                    self.loading_error = self.error
                    self.render()
            except (OSError, ValueError, yaml.YAMLError) as error:
                self.error = setup_failure_message(error, label)
                self.read_errors[key] = self.error
                if not self.loaded:
                    self.loading_error = self.error
                    self.render()
            finally:
                self.busy = False
                self.quiet_refresh = False
                self.mutating = False
                if self.loaded and not quiet:
                    self.render()
                if self.refresh_pending:
                    self.refresh_pending = False
                    self.app.loop.call_soon(self.refresh)
                self.app.invalidate()

        self.active_task = self.app.create_background_task(execute())
        if self.loaded and not quiet:
            self.render()
        self.app.invalidate()

    def confirm_from_worker(self, label, default=False):
        async def ask():
            return bool(await self.confirm_dialog("Administrator access", label, "Authorize"))
        return asyncio.run_coroutine_threadsafe(ask(), self.loop).result()

    def authorize(self):
        async def prompt():
            allowed = await self.confirm_dialog(
                "Administrator authorization",
                "Only the pending privileged command uses sudo.\n"
                "Your system will request your password; Assistant never reads or saves it.",
                "Authorize")
            if not allowed:
                raise SetupError("Authorization cancelled. The pending command was not run.")
            return await run_in_terminal(partial(terminal_authorization, confirmed=True), in_executor=True)
        return asyncio.run_coroutine_threadsafe(prompt(), self.loop).result()

    def close_dialog(self, result):
        focus_label = next((action.label for action in self.actions if action.control is self.dialog_focus), None)
        if self.dialog_future is not None and not self.dialog_future.done():
            self.dialog_future.set_result(result)
        self.floats.floats.clear()
        self.dialog_topic = None
        self.dialog_topics = {}
        if self.render_pending:
            self.render_pending = False
            self.render()
        if self.dialog_focus in self.app.layout.find_all_controls():
            self.app.layout.focus(self.dialog_focus)
        elif focus_label:
            replacement = next((action for action in self.actions if action.label == focus_label), None)
            if replacement:
                self.app.layout.focus(replacement.window)
        self.app.invalidate()

    async def confirm_dialog(self, title, message, label="Confirm", *, fields=(), validate=None, details=False):
        if self.dialog_future is not None:
            return False
        self.dialog_focus = self.app.layout.current_control
        self.dialog_future = asyncio.get_running_loop().create_future()
        self.dialog_topic = (title, message)
        widgets = {}
        message_view = self.readable(message)
        if not details:
            message_view.height = Dimension(min=2, max=8)
        content = [message_view, text()]
        for key, name, password, default in fields:
            multiline = key == "body"
            widget = input_field(default, password=password, multiline=multiline,
                                 height=5 if multiline else 1)
            widgets[key] = widget
            self.dialog_topics[widget.control] = (name, message + (
                " This input is masked; Assistant does not display saved credentials." if password else
                " Enter the requested value, then move to the confirmation button. Cancel leaves it unchanged."))
            content.extend([text(name), widget, text()])
        validation_error = ""
        def accept_values():
            nonlocal validation_error
            values = {key: widget.text for key, widget in widgets.items()} if widgets else True
            try:
                if validate is not None:
                    validate(values)
            except SetupError as error:
                validation_error = clean(str(error))
                if widgets:
                    self.app.layout.focus(next(iter(widgets.values())))
                self.app.invalidate()
                return
            self.close_dialog(values)
        cancel = Action("Back" if details else "Cancel", partial(self.close_dialog, False),
                        help_text="Close this dialog without applying a change.")
        accept = Action(label, accept_values, help_text=message)
        self.dialog_topics[cancel.control] = (cancel.label, cancel.help_text)
        self.dialog_topics[accept.control] = (accept.label, accept.help_text)
        dialog = Dialog(title=title, body=HSplit([
            *content, text(lambda: validation_error, height=2, style="class:error") if validate else text(),
            VSplit([accept.window] if details else [cancel.window, accept.window], height=1),
        ]), width=Dimension(preferred=68), modal=True)
        self.floats.floats = [Float(content=dialog)]
        focus = content[0] if details else next(iter(widgets.values()), cancel.window)
        self.app.layout.focus(focus)
        self.app.invalidate()
        try:
            return await self.dialog_future
        finally:
            self.dialog_future = None

    def confirm_action(self, title, message, label, operation, done=None, fields=(), validate=None):
        if self.busy or self.floats.floats:
            if self.busy:
                self.busy_notice = "Wait for the current operation before starting another action."
            return

        async def confirm():
            result = await self.confirm_dialog(title, message, label, fields=fields, validate=validate)
            if result is not False:
                self.task(title, partial(operation, result) if fields else operation,
                          done or (lambda value: self.refresh_after_action()), mutating=True)
        self.app.create_background_task(confirm())

    def refresh_after_action(self):
        self.cache.clear()
        self.render()
        self.app.loop.call_soon(self.refresh)

    def key(self):
        loc = self.location
        return (loc.route, loc.page, loc.query, (loc.record or {}).get("id"),
                (loc.record or {}).get("_kind"), (loc.record or {}).get("version_id"))

    def refresh(self, *, background=False):
        if not self.loaded or self.floats.floats:
            return
        if self.busy:
            if not background:
                self.refresh_pending = True
            return
        loc = self.location
        key = self.key()
        route = loc.route
        if route in {"Home", "Services", "Service"}:
            def loaded(rows):
                current = self.key() == key
                changed = rows != self.services
                self.services = rows
                self.services_loaded = True
                last_page = max(0, (len(rows) - 1) // 8)
                if current and route == "Services" and self.location.page > last_page:
                    self.location.page = last_page
                    changed = True
                if current and changed:
                    self.render()
            self.task("Checking services", self.backend.services, loaded, report=False, quiet=background)
        elif route in {"Tasks", "Memories", "Reminders", "Projects", "Scripts", "Events", "Objects"}:
            kind = {"Events": "task-events", "Objects": "project-objects"}.get(route, route.lower())
            request = {"kind": kind, "page": loc.page, "query": loc.query}
            if route in {"Events", "Objects"}:
                request["id"] = loc.record["id"]
            def loaded(result):
                last_page = max(0, (result["total"] - 1) // 8)
                if self.key() == key and loc.page > last_page:
                    self.location.page = last_page
                    self.refresh_pending = True
                    return
                self.cache[key] = result
                if self.key() == key:
                    self.render()
            self.task("Loading " + route.lower(), partial(self.backend.request, request), loaded, report=False)
        elif route == "Logs":
            def loaded(value):
                changed = value != self.cache.get(key)
                self.cache[key] = value
                if self.key() == key and changed:
                    self.render()
            self.task("Reading service logs", partial(self.backend.logs, loc.record["id"]), loaded,
                      report=False, quiet=background)
        elif route == "Credentials":
            self.render()
        elif route == "Pending deletions":
            self.load_pending()
        elif route == "Record":
            record = loc.record
            kind = record["_kind"]
            if kind not in {"Tasks", "Memories", "Reminders", "Projects", "Scripts"}:
                return
            request = ({"kind": "script", "id": record.get("version_id")} if kind == "Scripts" else
                       {"kind": kind.lower(), "id": record["id"]})
            if kind == "Scripts" and not record.get("version_id"):
                return
            def loaded(result):
                if self.key() != key:
                    return
                if kind == "Scripts":
                    self.location.record = {**record, **result}
                elif result["rows"]:
                    self.location.record = {**result["rows"][0], "_kind": kind}
                else:
                    self.error = "This record no longer exists. Return to the list and refresh."
                self.render()
            self.task("Refreshing details", partial(self.backend.request, request), loaded, report=False)

    def search(self):
        if self.busy or self.floats.floats:
            if self.busy:
                self.busy_notice = "Wait for the current read to finish before searching."
            return
        async def edit():
            def validate(values):
                if len(values["query"]) > 500:
                    raise SetupError("Search must be at most 500 characters.")
            result = await self.confirm_dialog(
                "Search", "Search this collection. Leave blank to show everything.", "Search",
                fields=(("query", "Search text", False, self.location.query),), validate=validate)
            if result is not False:
                self.location.query = result["query"]
                self.location.page = 0
                self.render()
                self.refresh()
        self.app.create_background_task(edit())

    def render(self):
        if self.floats.floats:
            self.render_pending = True
            self.app.invalidate()
            return
        focused = self.app.layout.current_control
        label = next((action.label for action in self.actions if action.control is focused), None)
        key = self.key()
        preserve = key == getattr(self, "rendered_key", None) and (
            getattr(self, "rendered_ready", False) or any(action.control is focused for action in self.nav))
        try:
            self._render()
        except SetupError as error:
            self.render_error(str(error))
        except (OSError, ValueError, yaml.YAMLError) as error:
            self.render_error(setup_failure_message(error, self.location.route))
        if self.floats.floats:
            return
        if preserve and focused in self.app.layout.find_all_controls():
            self.app.layout.focus(focused)
        elif preserve and label:
            replacement = next((action for action in self.actions if action.label == label), None)
            if replacement:
                self.app.layout.focus(replacement.window)
        self.rendered_key = key
        route = self.location.route
        self.rendered_ready = self.loaded and (
            bool(self.services) if route in {"Services", "Service"} else
            key in self.cache if route in {"Tasks", "Memories", "Reminders", "Projects", "Scripts", "Events", "Objects", "Logs"} else True)

    def render_error(self, message):
        self.error = message
        self.actions = []
        self.control_topics = {}
        self.put("This screen needs attention", "Resolve the reported problem, then retry. Your draft is preserved.",
                 [self.readable(message)], actions=[
                     self.action("Retry screen", self.render),
                     self.action("Back", self.back),
                 ])

    def _render(self):
        self.actions = []
        self.control_topics = {}
        route = self.location.route
        if not self.loaded:
            message = self.loading_error or "Loading saved configuration. Your data stays on this computer."
            docker_error = "docker" in message.lower()
            actions = [self.action("Retry", self.load, primary=True)]
            if "rotation" in message.lower():
                actions.append(self.action("Recover database credential", self.recover))
            actions.append(self.action("Quit", self.quit))
            self.put(("Docker needs attention" if docker_error else "Configuration needs attention")
                     if self.loading_error else "Welcome to Assistant",
                     ("Start your Docker runtime and retry." if docker_error else
                      "Resolve the configuration problem, then retry.") if self.loading_error else
                     "A calm place to manage your assistant.",
                     [text(clean(message), height=Dimension(min=3, max=10))],
                     actions=actions)
            return
        if route == "Home":
            self.home()
        elif route in {"Services", "Tasks", "Memories", "Reminders", "Projects", "Scripts", "Events", "Objects", "Credentials"}:
            self.collection()
        elif route == "Service":
            self.service()
        elif route == "Record":
            self.record()
        elif route == "Logs":
            self.logs()
        elif route.startswith(("Form:", "Connection:")):
            self.edit_group(route.split(":", 1)[1])
        elif route == "Instructions":
            self.instructions()
        elif route == "Review":
            self.review_screen()
        elif route == "Storage":
            self.storage()
        elif route == "Search":
            self.search_maintenance()
        elif route == "Project deletion":
            self.project_deletion()
        elif route == "Pending deletions":
            self.pending_deletions()
        elif route == "Help":
            self.help()
        else:
            self.hub(route)

    def home(self):
        instance = self.backend.installation
        workers = [row for row in self.services if row.get("worker", True)]
        running = sum(row["state"] == "running" for row in workers)
        summary = (f"{running} of {len(workers)} components running" if self.services else
                   "No configured components." if self.services_loaded else "Runtime health has not been checked.")
        name = self.backend.values.get("AGENT_NAME", "Assistant")
        first = not instance.state.get("setup_complete")
        items = [
            text(clean(name), style="class:title"), text(summary), text(),
            text("Setup is not saved yet." if first else
                 f"{len(instance.state.get('pending', []))} credentials pending activation."),
            text("Unsaved draft changes" if self.backend.dirty else "No unsaved changes"), text(),
        ]
        if first:
            items += [
                text("Use a dedicated mailbox. Models and integrations receive the data they need.\n"
                     "Assistant can take real actions. Keep backups and review permissions.", height=3),
                self.action("[x] I understand the responsibilities" if self.backend.accepted else
                            "[ ] I understand the responsibilities", self.acknowledge),
                self.action("1. Identity and timezone", partial(self.go, "Form:identity")),
                self.action("2. Models and API access", partial(self.go, "Form:models")),
                self.action("3. Choose a channel", partial(self.go, "Connections")),
                self.action("4. Review assistant instructions", partial(self.go, "Instructions")),
            ]
        else:
            items += [
                self.action("Services and health", partial(self.go, "Services")),
                self.action("Tasks, memories and reminders", partial(self.go, "Activity")),
                self.action("Connections", partial(self.go, "Connections")),
                self.action("Settings", partial(self.go, "Settings")),
            ]
        self.put("Set up Assistant" if first else "Home", "",
                 items, actions=[
                     self.action("Review & save", self.review, primary=True),
                     self.action("Refresh", self.refresh),
                 ])

    def acknowledge(self):
        self.backend.accepted = not self.backend.accepted
        self.render()

    def hub(self, route):
        if route == "Activity":
            choices = [
                ("Tasks", "Tasks"), ("Memories", "Memories"), ("Reminders", "Reminders"),
                ("Projects & workstreams", "Projects"), ("Script permissions", "Scripts"),
            ]
            description = "Browse actual records. Open an item to see its details and available actions."
        elif route == "Connections":
            choices = [(GROUPS[key][0] + (" / enabled" if self.backend.value(GROUPS[key][1][0]) == "true" else
                " / not enabled"), "Connection:" + key)
                for key in ("email", "telegram", "calendar", "zoom", "dropbox", "transcription")]
            description = "Enable only what you need. Labels describe your draft, not verified connectivity."
        elif route == "Advanced":
            choices = [(title, "Form:" + key) for key, (title, _) in GROUPS.items() if key.startswith("advanced:")]
            description = "Focused controls for limits, search, privacy, and background behavior."
        else:
            choices = [(GROUPS[key][0], "Form:" + key) for key in ("identity", "models", "security", "embeddings")]
            choices += [("Assistant instructions", "Instructions"), ("Credentials", "Credentials"),
                        ("Search maintenance", "Search"), ("Storage & recovery", "Storage"),
                        ("Advanced settings", "Advanced")]
            description = "Edits stay in your draft until you review and save. Running services are not changed by saving."
        self.put(route, description,
                 [self.action(label, partial(self.go, target)) for label, target in choices],
                 actions=[self.action("Review & save", self.review, primary=True)])

    def storage(self):
        volume = bool(self.backend.installation.credential_volume)
        unfinished = bool(self.backend.installation.state.get("previous_credential_volume"))
        self.put("Storage & recovery", "Deliberate controls for sensitive operations. Back up before making changes.", [
            text("Credential storage   " + ("Private Docker volume" if volume else "Protected host files")),
            text("Neither storage option encrypts credentials. Docker administrators can access them.", height=2),
            self.action("Move credentials to " + ("host files" if volume else "a Docker volume"),
                        partial(self.change_storage, not volume)), text(),
            *([self.action("Finish credential storage change", partial(self.change_storage, False)),
               text("The previous volume is retained until its mounts are safely retired.", height=2), text()]
              if unfinished else []),
            self.action("Recover an interrupted database password rotation", self.recover),
            text("Recovery restores the previous password and restarts its consumers.", height=2), text(),
            self.action("Resume an incomplete project deletion", partial(self.go, "Pending deletions")), text(),
            self.action("Reset bundled database", self.reset_database, danger=True),
            text("Permanently deletes database data. Settings, credentials and workspace files remain.\n"
                 "No backup is made. Custom databases are never reset by this interface.", height=3),
        ])

    def change_storage(self, volume):
        self.confirm_action(
            "Change credential storage",
            "Move credentials to " + ("a private Docker volume?" if volume else "protected host files?") +
            "\nRunning services are recreated to use the new mounts. Brief downtime is expected.\n"
            "Keep a separate backup of a Docker credential volume.",
            "Move", partial(self.backend.storage, volume), lambda result: self.saved())

    def reset_database(self):
        def validate(values):
            if values["confirmation"] != "RESET":
                raise SetupError("Reset cancelled. Type RESET exactly; no data was deleted.")
        self.confirm_action("Reset bundled database",
                            "Permanently destroy all bundled PostgreSQL data and rebuild it?\n"
                            "Settings, credentials and workspace files are preserved. No backup is made.",
                            "Reset database", lambda values: self.backend.reset(),
                            fields=(("confirmation", "Type RESET", False, ""),), validate=validate)

    def recover(self):
        def recovered(result):
            self.loaded = True
            self.loading_error = ""
            self.saved()
        self.confirm_action("Recover database credential",
                            "Restore the previous database password and recreate its consumers?",
                            "Recover", self.backend.recover, recovered)

    def project_preview(self, row):
        key = self.key()
        def loaded(preview):
            if self.key() == key:
                self.go("Project deletion", {**row, "_preview": preview})
        self.task("Previewing project deletion", partial(self.backend.preview_project, row["id"]), loaded)

    def project_deletion(self):
        row = self.location.record
        preview = row["_preview"]
        items = [text("Shared objects retained elsewhere will be preserved.", height=2)]
        for key, value in preview.items():
            if key == "preview_digest":
                continue
            display = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
            items += [text(key.replace("_", " ").capitalize(), style="class:title"),
                      self.readable(display), text()]
        self.put("Preview project deletion", "Review the exact inventory. Writers pause during the confirmed operation.",
                 items, actions=[
                     self.action("Delete previewed project", lambda: self.confirm_action(
                         "Delete project", "Permanently delete this previewed project and its unshared objects?\n"
                         "Shared objects attached elsewhere are preserved. Changed inventories require a new preview.",
                         "Delete", partial(self.backend.delete_project, row["id"], preview["preview_digest"]),
                         lambda result: self.project_deleted()), danger=True),
                     self.action("Keep project", self.back),
                 ])

    def project_deleted(self):
        self.cache.clear()
        self.history = []
        self.location = Location("Projects")
        self.render()
        self.app.loop.call_soon(self.refresh)

    def pending_deletions(self):
        rows = self.cache.get(("pending-deletions",))
        self.put("Pending project deletions", "Writer services stay stopped when a deletion cannot be completed.",
                 [self.action(f"Deletion #{row['id']}  /  {row['status']}", partial(self.resume_project, row["id"]),
                              help_text="Resume this previously confirmed project deletion after confirmation. "
                                        "Writers pause during cleanup and stay stopped if completion cannot be verified.")
                  for row in rows or []] or [text(
                      "No incomplete deletions." if rows is not None else
                      "Check to retrieve the current inventory.", height=2)],
                 actions=[self.action("Check pending", self.load_pending)])

    def load_pending(self):
        def loaded(rows):
            self.cache[("pending-deletions",)] = rows
            if self.location.route == "Pending deletions":
                self.render()
        self.task("Checking incomplete deletions", self.backend.pending_deletions, loaded)

    def resume_project(self, identifier):
        self.confirm_action("Resume project deletion", "Retry the previously confirmed cleanup? Writers will be paused.",
                            "Resume", partial(self.backend.resume_deletion, identifier),
                            lambda result: self.project_deleted())

    def search_maintenance(self):
        result = self.cache.get(("embedding-result",), {})
        items = []
        if result:
            for key, value in result.items():
                display = ", ".join(f"{name}: {count}" for name, count in value.items()) if isinstance(value, dict) else str(value)
                items.append(text(clean(f"{key.replace('_', ' ').capitalize()}   {display}"),
                                  height=Dimension(min=1, max=4)))
        else:
            items.append(text("Check indexing status or verify the embedding model connection.", height=2))
        self.put("Search maintenance", "Source data is preserved. Reindexing fills missing or incompatible vectors.", items,
                 actions=[self.action("Check status", partial(self.embedding_action, "status")),
                          self.action("Verify model", partial(self.embedding_action, "probe")),
                          self.action("Reindex", partial(self.embedding_action, "reindex"))])

    def embedding_action(self, operation):
        def loaded(value):
            self.cache[("embedding-result",)] = value
            if self.location.route == "Search":
                self.render()
        if operation == "reindex":
            self.confirm_action("Reindex search", "Rebuild missing or incompatible vectors? Source records are preserved.",
                                "Reindex", partial(self.backend.embeddings, operation), loaded)
        else:
            self.task("Checking search", partial(self.backend.embeddings, operation), loaded)

    def collection(self):
        route, page = self.location.route, self.location.page
        ready = True
        if route == "Services":
            ready = self.services_loaded or bool(self.services)
            rows, total = self.services[page * 8:(page + 1) * 8], len(self.services) if ready else None
        elif route == "Credentials":
            pending = self.backend.installation.state.get("pending", [])
            all_rows = [{"id": name, "title": name,
                         "status": "activation pending" if name in pending else "configured"}
                        for name in sorted(self.backend.installation.secret_values)]
            rows, total = all_rows[page * 8:(page + 1) * 8], len(all_rows)
        else:
            result = self.cache.get(self.key())
            ready = result is not None
            rows, total = (result["rows"], result["total"]) if ready else ([], None)
        items = []
        if route == "Services":
            items.append(text(f"   {'COMPONENT':<30} {'STATE':<15} HEALTH", style="class:muted"))
        for row in rows:
            if route == "Services":
                label = f"{row['title'][:28]:<30} {row['state'][:13]:<15} {row['health']}"
                handler = partial(self.go, "Service", row)
            elif route == "Credentials":
                label = f"{row['title']:<38} {row['status']}"
                handler = partial(self.credential, row["id"])
            else:
                title = row.get("task_summary") or row.get("content") or row.get("title") or row.get("entry") or row.get("event_type") or row.get("kind") or "Untitled"
                state = row.get("status") or ("pinned" if row.get("pinned") else "")
                if route == "Scripts" and row.get("version_id"):
                    state = f"version #{row['version_id']}" + (" / blocked" if row.get("blocked") else "")
                title = " ".join(clean(title).split()) or "Untitled"
                label = f"#{row.get('id', row.get('sequence', ''))}  {title[:54]}  {state}"
                handler = partial(self.go, "Record", {**row, "_kind": route})
            if route == "Credentials":
                explanation = credential_help(row["id"]) + " " + (
                    "Rotate with coordinated consumer restarts. This may cause downtime; keep a backup."
                    if row["id"] == "POSTGRES_PASSWORD" else
                    "Replace through masked input, then activate it in affected services.")
            elif route == "Services":
                explanation = (SERVICE_HELP.get(row["id"], "Additional Docker Compose component configured for this installation.") +
                               " Open for state, health, logs and lifecycle controls.")
            else:
                explanation = "Open this stored record to read its full details. " + page_help(route)
            items.append(self.action(label, handler, help_text=explanation))
        if not rows:
            failure = self.read_errors.get(self.key())
            message = ("No items found." if ready else
                       "Could not load records. " + failure + " Select Refresh to retry." if failure else
                       "Loading records. Results are not available yet." if self.busy else
                       "Records have not loaded yet. Select Refresh.")
            items.append(self.readable(message) if failure and not ready else
                         text(clean(self.backend.redact(message)), height=2))
        buttons = [self.action("Refresh", self.refresh)]
        if route == "Services":
            buttons += [self.action("Start app", partial(self.lifecycle, "start")),
                        self.action("Stop app", partial(self.lifecycle, "stop"), danger=True)]
        elif route == "Credentials":
            buttons += [self.action("Add credential", partial(self.credential, None)),
                        self.action("Activate pending", self.activate_credentials)]
        elif route not in {"Events", "Objects"}:
            buttons.append(self.action("Search", self.search))
            if route == "Memories":
                buttons.append(self.action("New memory", self.new_memory))
        self.put(route, page_help(route) +
                 (f"  Search: {self.location.query}" if self.location.query else ""),
                 items, actions=buttons, total=total)

    def service(self):
        selected = self.location.record
        row = next((row for row in self.services if row["id"] == selected["id"]), selected)
        self.put(row["title"], "Start may also start required dependencies; other controls target this component.", [
            text("State    " + row["state"]), text("Health   " + row["health"]), text(),
            text("Compose service   " + row["id"]), text(),
            text(SERVICE_HELP.get(row["id"], "Additional Docker Compose component configured for this installation."),
                 height=Dimension(min=1, max=4)), text(),
            text("A restart can interrupt active work. Apply recreates the container using saved settings.", height=3),
            self.action("Recent logs", partial(self.go, "Logs", row)),
            self.action("Refresh status", self.refresh),
        ], actions=[
            self.action("Start", partial(self.lifecycle, "start", row["id"])),
            self.action("Restart", partial(self.lifecycle, "restart", row["id"])),
            self.action("Apply settings", partial(self.lifecycle, "apply", row["id"])),
            self.action("Stop", partial(self.lifecycle, "stop", row["id"]), danger=True),
        ])

    def lifecycle(self, action, service=None):
        name = service or "all enabled components"
        self.confirm_action(
            action.title() + " " + name,
            f"{action.title()} {name}?\nActive work may be interrupted. Starting workers may process existing inbox messages.\n"
            "Only saved configuration is used. Data volumes are preserved.",
            action.title(), partial(self.backend.lifecycle, action, service),
            lambda rows: self.services_changed(rows))

    def services_changed(self, rows):
        self.services = rows
        self.services_loaded = True
        self.render()

    def readable(self, value):
        display = clean(self.backend.redact(value))
        return TextArea(text=display, read_only=True, focus_on_click=True, scrollbar=True,
                        wrap_lines=True, height=Dimension(
                            min=1, preferred=min(10, max(1, len(display) // 65 + display.count("\n") + 1))))

    def logs(self):
        value = self.cache.get(self.key(), "Logs have not been loaded yet.")
        if self.log_key != self.key():
            self.log_key = self.key()
            self.log_view = self.readable(value)
        else:
            display = clean(self.backend.redact(value))
            if self.log_view.text != display:
                position = self.log_view.buffer.cursor_position
                self.log_view.text = display
                self.log_view.buffer.cursor_position = min(position, len(display))
        log_view = self.log_view
        self.put("Logs / " + self.location.record["id"],
                 "Latest 150 lines. Live mode refreshes in place every five seconds.",
                 [log_view], actions=[
                     self.action("Refresh", self.refresh),
                     self.action("Pause live" if self.live_logs else "Live", self.toggle_live),
                 ])
        log_view.height = Dimension(min=5, preferred=14)

    def toggle_live(self):
        self.live_logs = not self.live_logs
        self.render()
        if self.live_logs:
            self.refresh()

    def record(self):
        row = self.location.record
        kind, identifier = row["_kind"], row.get("id")
        items = []
        for key, value in row.items():
            if key.startswith("_") or value is None:
                continue
            display = json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, (dict, list)) else str(value)
            items += [text(key.replace("_", " ").capitalize(), style="class:title"),
                      self.readable(display),
                      text()]
        buttons = []
        if kind == "Tasks":
            buttons = [self.action("Events", partial(self.go, "Events", row)),
                       self.action("Retry task", partial(self.mutate, "retry", identifier)),
                       self.action("Add instruction", partial(self.mutate_text, "instruct", identifier, "")),
                       self.action("Cancel task", partial(self.mutate, "cancel", identifier), danger=True)]
            if row.get("status") == "needs_review":
                items.insert(0, self.action("Approve review", partial(self.mutate, "review", identifier)))
        elif kind == "Memories":
            buttons = [self.action("Edit", partial(self.mutate_text, "update-memory", identifier, row["content"])),
                       self.action("Unpin" if row.get("pinned") else "Pin",
                                   partial(self.mutate, "unpin-memory" if row.get("pinned") else "pin-memory", identifier)),
                       self.action("Delete", partial(self.mutate, "delete-memory", identifier), danger=True)]
            if (row.get("metadata") or {}).get("state") == "suggested":
                items.insert(0, self.action("Approve suggestion", partial(self.mutate, "approve-memory", identifier)))
                items.insert(1, self.action("Reject suggestion", partial(self.mutate, "reject-memory", identifier)))
        elif kind == "Reminders":
            buttons = [self.action("Cancel reminder", partial(self.mutate, "cancel-reminder", identifier), danger=True)]
        elif kind == "Projects":
            buttons = [self.action("Browse objects", partial(self.go, "Objects", row)),
                       self.action("Preview deletion", partial(self.project_preview, row), danger=True)]
        elif kind == "Scripts":
            buttons = [
                self.action("Unblock" if row.get("blocked") else "Block",
                            partial(self.mutate, "unblock-script" if row.get("blocked") else "block-script", identifier)),
                self.action("Revoke approvals", partial(self.mutate, "revoke-script", identifier), danger=True),
            ]
        self.put("Details", "Tab or click into text to scroll it. Actions require confirmation.",
                 items, actions=buttons)

    def mutate(self, action, identifier):
        self.confirm_action(
            action.replace("-", " ").title(), f"Apply {action.replace('-', ' ')} to record #{identifier}?\n"
            "This changes live application data. Completed external actions cannot be undone.",
            "Confirm", partial(self.backend.mutate, action, identifier),
            lambda result: self.mutation_done())

    def mutation_done(self):
        self.cache.clear()
        if self.location.route == "Record":
            self.location = self.history.pop() if self.history else Location("Activity")
        self.render()
        # Refresh after the active worker has released the single-operation slot.
        self.app.loop.call_soon(self.refresh)

    def mutate_text(self, action, identifier, previous):
        def validate(values):
            if not values["body"].strip():
                raise SetupError("Enter text before saving.")
            if len(values["body"]) > 100_000:
                raise SetupError("Text must be at most 100,000 characters.")
        self.confirm_action(action.replace("-", " ").title(),
                            "Text is applied to the live record only after confirmation.", "Save",
                            lambda values: self.backend.mutate(action, identifier, values["body"]),
                            lambda result: self.mutation_done(),
                            fields=(("body", "Text", False, previous),), validate=validate)

    def new_memory(self):
        self.mutate_text("create-memory", None, "")

    def edit_group(self, key):
        title, specs = GROUPS[key]
        if self.form_group != key:
            self.form_group = key
            self.form = {
                spec.key: checkbox_field(spec.label, self.backend.value(spec).lower() == "true") if spec.boolean else
                input_field(self.backend.value(spec), password=spec.secret)
                for spec in specs
            }
        items = []
        for spec in specs:
            widget = self.form[spec.key]
            self.control_topics[widget.control] = (spec.label, field_help(spec))
            if spec.boolean:
                items.extend([widget, text()])
            else:
                suffix = " (configured; blank keeps it)" if spec.secret and spec.key in self.backend.credentials else ""
                items.extend([text(spec.label + suffix, height=Dimension(min=1, max=4)), widget, text()])
        if key == "models" and not self.backend.installation.secret_values.get("POSTGRES_PASSWORD"):
            items.insert(0, self.action("Generate secure database password", self.generate_password))
        self.put(title, page_help("Form:" + key),
                 items, actions=[
                     self.action("Keep draft", self.keep_draft, primary=True),
                     self.action("Review & save", self.review),
                     self.action("Discard this form", self.discard_form),
                 ])
        self.app.layout.focus(next(iter(self.form.values())))

    def generate_password(self):
        try:
            self.form["POSTGRES_PASSWORD"].text = self.backend.generate_database_password()
        except SetupError as error:
            self.error = str(error)
            self.app.invalidate()
            return
        self.status = "Secure database password generated in this form. Keep draft to retain it."
        self.app.invalidate()

    def keep_draft(self):
        try:
            self.save_form()
        except SetupError as error:
            self.error = str(error)
            return
        self.status = "Draft kept. Review & save when ready."
        self.error = ""
        self.location = self.history.pop() if self.history else Location("Settings")
        self.render()

    def discard_form(self):
        self.form = {}
        self.form_group = None
        self.location = self.history.pop() if self.history else Location("Settings")
        self.status = "Form edits discarded; other draft changes are unchanged."
        self.error = ""
        self.render()

    def instructions(self):
        if self.form_group != "instructions":
            self.form_group = "instructions"
            self.form = {"instructions": input_field(
                self.backend.instruction_text(), multiline=True, scrollbar=True,
                height=Dimension(min=6, preferred=16))}
        self.put("Assistant instructions", "Review the example or write your own. No file is changed until you save.",
                 [self.form["instructions"]], actions=[
                     self.action("Keep draft", self.keep_draft), self.action("Review & save", self.review),
                     self.action("Discard this form", self.discard_form)])
        self.control_topics[self.form["instructions"].control] = ("Assistant instructions", page_help("Instructions"))
        self.app.layout.focus(self.form["instructions"])

    def review(self):
        if not self.loaded:
            return
        self.go("Review")

    def review_screen(self):
        problems = self.backend.problems()
        original = self.backend.installation
        items = [text("Draft changes are not active until saved and applied.", height=2)]
        for key in ("AGENT_NAME", "AGENT_TIMEZONE", "AGENT_LLM_MODEL", "COMPOSE_PROFILES"):
            spec = next((spec for _, specs in GROUPS.values() for spec in specs if spec.key == key), None)
            value = (self.backend.draft().values.get(key, "") if key == "COMPOSE_PROFILES" else
                     self.backend.value(spec) if spec else self.backend.values.get(key, "(not set)"))
            items.append(text(clean(f"{key.replace('AGENT_', ''):<24} {value}")))
        items += [text(f"Credentials  {len(self.backend.credentials)} configured; values hidden"), text()]
        changed = [(key, value) for key, value in self.backend.values.items() if original.values.get(key) != value]
        if changed:
            items.append(text("Changed settings", style="class:title"))
            items += [text(clean(f"{key}  {value}"), height=Dimension(min=1, max=3)) for key, value in changed]
        replaced = [key for key, value in self.backend.credentials.items() if original.secret_values.get(key) != value]
        if replaced:
            items.append(text("New / replaced credentials: " + ", ".join(replaced), height=3))
        if self.backend.instructions is not None:
            items.append(text("Assistant instructions will be updated.", style="class:title"))
        if problems:
            items.append(text("Needs attention", style="class:danger"))
            for key, message in problems:
                target = ("Form:" + key if key in GROUPS else "Advanced" if key == "advanced" else
                          "Instructions" if key == "instructions" else "Settings")
                items += [text(clean(message), height=Dimension(min=1, max=3)),
                          self.action("Edit " + GROUPS.get(key, (key, []))[0],
                                      partial(self.go, target))]
        else:
            items.append(text("Ready to save.", style="class:ok"))
        if not self.backend.accepted:
            items.append(self.action("Acknowledge first-run responsibilities", partial(self.go, "Home")))
        if not original.prompt_original and self.backend.instructions is None:
            items.append(self.action("Review required instructions", partial(self.go, "Instructions")))
        self.put("Review & save",
                 f"{len(problems)} configuration issues to resolve before saving." if problems else
                 "Configuration valid. Review changes below. Saving does not restart workers.",
                 items, actions=[
                     self.action("Save configuration", self.save, primary=True) if not problems else
                     self.action("Continue editing", partial(self.go, "Settings")),
                     self.action("Discard all draft changes", self.discard_all, danger=True),
                 ])

    def save(self):
        self.confirm_action("Save configuration", "Write this reviewed draft to local configuration?\n"
                            "Credentials remain hidden. Running workers will not restart.",
                            "Save", self.backend.save, lambda result: self.saved())

    def saved(self):
        self.cache.clear()
        self.render()
        self.status = "Saved. Use Services > Start app for startup, or Apply settings on affected components."

    def discard_all(self):
        def discarded(value):
            self.cache.clear()
            self.render()
            self.status = "Draft discarded. Saved configuration is unchanged."
        self.confirm_action("Discard draft", "Discard all unsaved changes? Saved configuration is preserved.",
                            "Discard", self.backend.load, discarded)

    def credential(self, name):
        if name is None:
            async def choose():
                def validate(values):
                    if values["name"].strip() not in SECRET_NAMES:
                        raise SetupError("Unknown credential name. Use the connection or model form.")
                result = await self.confirm_dialog(
                    "Add credential", "Enter a supported credential name, such as OPENROUTER_API_KEY or IMAP_PASSWORD.\n"
                    "For normal account setup, use the Models or Connections forms instead.", "Continue",
                                                  fields=(("name", "Credential name", False, ""),), validate=validate)
                if result is not False:
                    self.credential(result["name"].strip())
            self.app.create_background_task(choose())
            return
        self.confirm_action(
            "Rotate database password" if name == "POSTGRES_PASSWORD" else "Replace credential",
            name + "\n" + credential_help(name) + "\nThe new value is masked." +
            (" Database rotation briefly stops consumers; keep a backup." if name == "POSTGRES_PASSWORD" else
             " Activate the replacement afterward in affected services."),
            "Save", lambda values: self.backend.replace_credential(name, values["value"]),
            lambda result: self.saved(), fields=(("value", "New value", True, ""),),
            validate=lambda values: validate_secret(name, values["value"]))

    def activate_credentials(self, name=None):
        self.confirm_action("Activate pending credentials",
                            "Recreate affected running services? Brief downtime is expected.",
                            "Activate", partial(self.backend.apply_credentials, name), lambda result: self.saved())

    def help(self):
        guide = self.readable(
            "Navigation\n"
            "The About panel explains the focused option. F1 opens its full help.\n"
            "Tab moves through controls. Enter opens an item. Esc goes back.\n"
            "Use arrows or PgUp/PgDn to scroll long text, including this guide.\n"
            "Forms preserve your draft across sections. Ctrl-S opens review.\n\n"
            "Docker and authorization\n"
            "Start Docker Desktop on macOS, or your Docker daemon on Linux.\n"
            "When sudo is needed, authorize inside the interface. Only the system\n"
            "password prompt leaves the screen. Assistant never receives your password.\n\n"
            "Settings and data\n"
            "Save writes local configuration. Apply recreates services using saved settings.\n"
            "Data is under data/. Back it up, including the credential volume when enabled.\n\n"
            "System and functionality questions\n"
            "Start with docs/README.md, the documentation index, for questions about\n"
            "how Assistant works, its capabilities, integrations and limitations.\n"
            "Setup and everyday use: docs/running-assistant.md\n"
            "Available tools and functionality: docs/tools-reference.md\n"
            "Permissions and safety: docs/security.md")
        guide.lexer = HelpLexer()
        guide.height = Dimension(min=5, preferred=22)
        self.put("Help", "Local administration help. No model is used.", [guide])
        self.app.layout.focus(guide)

    def load(self):
        def loaded(instance):
            self.loaded = True
            self.loading_error = ""
            self.render()
            options = getattr(self, "launch_options", None)
            if options is not None and options.command == "secrets":
                self.launch_options = None
                if options.action == "update":
                    self.app.loop.call_soon(partial(self.credential, options.name))
                elif options.action == "apply":
                    self.app.loop.call_soon(partial(self.activate_credentials, options.name))
            else:
                if instance.state.get("setup_complete") and self.location.route in {"Home", "Services", "Service"}:
                    self.app.loop.call_soon(self.prepare_management)
                elif self.location.route in {"Home", "Services", "Service", "Tasks", "Memories", "Reminders",
                                              "Projects", "Scripts", "Pending deletions", "Record", "Logs"}:
                    self.app.loop.call_soon(self.refresh)
        self.task("Loading local configuration", self.backend.load, loaded, report=False)

    def prepare_management(self):
        self.refresh_pending = True
        self.task("Checking management runtime", self.backend.prepare_management, report=False)

    def quit(self):
        if self.floats.floats:
            self.close_dialog(False)
            return
        if self.busy:
            self.busy_notice = "An operation is still running. Wait for its result before exiting."
            return
        if self.form_group is not None or self.backend.dirty:
            async def confirm():
                if await self.confirm_dialog("Leave Assistant?", "Discard unsaved changes and exit?", "Exit"):
                    self.app.exit()
            self.app.create_background_task(confirm())
        else:
            self.app.exit()

    async def periodic(self):
        while True:
            await asyncio.sleep(5)
            if self.loaded and not self.busy and not self.floats.floats and (
                    self.location.route in {"Home", "Services", "Service"} or
                    self.location.route == "Logs" and self.live_logs):
                self.refresh(background=True)

    def run(self, options=None):
        def start():
            self.loop = asyncio.get_running_loop()
            if options is not None:
                if options.section:
                    self.location = Location("Form:" + options.section if options.section in GROUPS else
                                             "Advanced" if options.section == "advanced" else
                                             "Instructions" if options.section == "instructions" else "Settings")
                elif options.command == "tools":
                    self.location = Location("Services")
                elif options.command == "secrets":
                    self.location = Location("Credentials")
                elif options.command == "reset":
                    self.location = Location("Storage")
                elif options.wizard:
                    self.location = Location("Settings")
            self.launch_options = options
            if options is not None and options.command == "secrets" and options.action == "recover":
                async def recover():
                    if await self.confirm_dialog("Recover database credential",
                                                 "Restore the previous password and restart its consumers?", "Recover"):
                        def recovered(instance):
                            self.loaded = True
                            self.location = Location("Storage")
                            self.render()
                        self.task("Recovering database credential", self.backend.recover, recovered, mutating=True)
                    else:
                        self.app.exit()
                self.app.create_background_task(recover())
            else:
                self.load()
            self.refresh_task = self.app.create_background_task(self.periodic())
        self.app.run(pre_run=start)
