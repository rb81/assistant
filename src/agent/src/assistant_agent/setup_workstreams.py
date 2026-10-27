"""Workstream operator menus, using the existing trusted local Compose transport."""
import json

from .setup import AGENT_SERVICES, DockerCommandError, SetupError
from .setup_tools import Tools


MODES = {
    "1. Delete workstream; preserve objects shared elsewhere (recommended)": "preserve_shared",
    "2. Delete workstream AND shared objects; other workstreams may break": "delete_objects",
    "3. Delete every workstream connected through shared objects": "connected",
}


class WorkstreamTools(Tools):
    def command(self, action, payload=None):
        if not getattr(self, "image_ready", False):
            try:
                self.instance.compose(["build", "task-agent"], timeout=1800)
            except DockerCommandError as exc:
                raise SetupError("Workstream helper image build failed. " + str(exc)) from None
            self.image_ready = True
        # task-agent has integration credentials; this one-shot helper additionally
        # needs the complete private attachment store, never just Dropbox staging.
        mount = str(self.instance.root / "data/private/artifacts") + ":/data/private/artifacts"
        try:
            output = self.instance.compose(
                ["run", "--rm", "--no-deps", "-T", "--volume", mount, "--entrypoint", "python",
                 "task-agent", "-m", "assistant_agent.workstream_operator", action],
                input_text=json.dumps(payload or {}), timeout=1800,
            )
        except DockerCommandError as exc:
            try:
                failure = json.loads(exc.stdout or "")
            except ValueError:
                failure = None
            if (isinstance(failure, dict) and set(failure) == {"workstream_error"}
                    and isinstance(failure["workstream_error"], str)):
                raise SetupError(f"Workstream {action} failed: {failure['workstream_error']}") from None
            raise SetupError(f"Workstream {action} helper failed. " + str(exc)) from None
        try:
            result = json.loads(output)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except ValueError as exc:
            raise SetupError("Workstream operation was not confirmed; inspect pending deletions before retrying.") from exc

    def show(self, value):
        self.console.print(json.dumps(value, ensure_ascii=False, indent=2), markup=False, highlight=False)

    def choose(self):
        cursor = None
        while True:
            rows = self.command("list", {"before_id": cursor})["workstreams"]
            if not rows:
                self.console.print("No workstreams.")
                return None
            labels = {f"#{row['id']} {row['title']} ({row['object_count']} objects, {row['status']}; parent {row['parent_id']})": row
                      for row in rows}
            choices = list(labels) + (["Older workstreams"] if len(rows) == 50 else [])
            selected = self.select("Workstream", choices)
            if selected == "Back":
                return None
            if selected != "Older workstreams":
                return labels[selected]
            cursor = rows[-1]["id"]

    def menu(self):
        while True:
            action = self.select("Workstreams", ["Browse workstreams and objects", "Attach an existing object",
                                               "Delete a workstream", "Resume an incomplete deletion"])
            if action == "Back":
                return
            try:
                if action == "Resume an incomplete deletion":
                    self.resume()
                    continue
                stream = self.choose()
                if stream is None:
                    continue
                if action == "Browse workstreams and objects":
                    self.show(self.command("objects", {"workstream_id": stream["id"]}))
                elif action == "Attach an existing object":
                    self.attach(stream)
                else:
                    self.delete(stream)
            except (SetupError, ValueError, KeyError) as exc:
                self.console.print(str(exc) + "\nOperation not confirmed. Inspect pending deletions before retrying.",
                                   markup=False, highlight=False)

    def attach(self, stream):
        kind = self.select("Reusable object type", ["contact", "file"])
        if kind == "Back":
            return
        key = self.text("Existing object key (shown by Browse; not the registry ID)")
        if self.confirm(f"Attach {kind} {key} to workstream #{stream['id']}? This retains the live object, not a copy.", False):
            self.show(self.command("attach", {"workstream_id": stream["id"], "kind": kind, "key": key, "confirmed": True}))

    def delete(self, stream):
        selected = self.select("Deletion policy", list(MODES))
        if selected == "Back":
            return
        payload = {"workstream_id": stream["id"], "mode": MODES[selected]}
        preview = self.command("preview", payload)
        self.show(preview)
        if not self.confirm("Stop all writer services and prepare this deletion? No deletion yet.", False):
            return
        self.with_stopped_writers(lambda: self.confirm_delete(payload))

    def confirm_delete(self, payload):
        # Quiescing may have changed the inventory; this is the authoritative preview.
        preview = self.command("preview", payload)
        self.show(preview)
        if preview["running_jobs"] or preview["running_research"] or preview["blocked_files"] or preview["blocked_reminders"]:
            self.console.print("Running work or unverified files remain. Resolve the listed blockers before deleting; nothing was deleted.")
            return
        warning = ("Shared objects will also be removed from surviving workstreams."
                   if payload["mode"] == "delete_objects" else
                   "Every listed connected workstream will be deleted." if payload["mode"] == "connected" else
                   "Shared objects attached outside this tree will be preserved.")
        if not self.confirm(warning + " Permanently delete exactly the previewed data?", False):
            return
        result = self.command("delete", {**payload, "preview_digest": preview["preview_digest"],
                                         "confirmed": True, "writers_stopped": True})
        if result.get("status") != "completed":
            raise SetupError("Deletion was not confirmed complete.")
        self.show(result)

    def resume(self):
        rows = self.command("pending")["deletions"]
        self.show(rows)
        if not rows:
            return
        identifier = self.number("Deletion ID to retry")
        if not any(row["id"] == identifier for row in rows):
            raise ValueError("Choose a listed pending deletion")
        if self.confirm("Stop writer services and retry the previously confirmed cleanup scope?", False):
            self.with_stopped_writers(lambda: self.show(self.command(
                "resume", {"deletion_id": identifier, "confirmed": True, "writers_stopped": True})))

    def with_stopped_writers(self, operation):
        running = sorted(set(self.instance.compose(["ps", "--status", "running", "--services"]).split()) & set(AGENT_SERVICES))
        if running:
            self.instance.compose(["stop", "--timeout", "120", *running], timeout=180)
        try:
            operation()
        finally:
            pending = self.command("pending")["deletions"]
            if pending:
                self.console.print("Incomplete deletion remains. Writer services stay stopped; resolve it and use Resume.",
                                   markup=False)
            elif running:
                self.instance.compose(["start", *running], timeout=180)
