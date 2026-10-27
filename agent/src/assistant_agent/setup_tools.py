"""Troubleshooting menus and factory reset for an installed Assistant."""
import json
import os
import shlex
from datetime import datetime

from .setup import AGENT_SERVICES, LONG_RUNNING_SERVICES, OVERRIDES_FILE, DockerCommandError, SetupError, ensure_docker_access


ADMIN = ["run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "task-agent", "-m", "assistant_agent.admin_commands"]
EMBEDDINGS = ["run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "task-agent", "-m", "assistant_agent.embedding_maintenance"]

CHEAT_SHEET = """\
Run these from the repository directory (prefix with sudo if your user cannot reach Docker):
  docker compose ps --all                              service state and health
  docker compose logs --tail 200 SERVICE               recent logs (add -f to follow)
  docker compose restart SERVICE                       restart one service
  docker compose up -d --force-recreate                apply .env / overrides.env changes
  docker compose up -d --wait --wait-timeout 180       start everything and wait for health
  docker compose stop                                  stop everything (data is kept)
  docker compose run --rm --no-deps -T --entrypoint python task-agent -m assistant_agent.admin_commands "/admin help"
  docker compose run --rm --no-deps -T --entrypoint python task-agent -m assistant_agent.embedding_maintenance status
  ./scripts/setup.sh secrets status | ./scripts/setup.sh secrets apply
Admin commands that change data need --yes when run locally (the menus ask first)."""


class Tools:
    def __init__(self, instance, answer, confirm, console):
        import questionary

        self.questionary = questionary
        self.instance = instance
        self.answer = answer
        self.confirm = confirm
        self.console = console

    def select(self, label, choices):
        return self.answer(self.questionary.select(label, choices=[*choices, "Back"]))

    def number(self, label):
        return int(self.answer(self.questionary.text(label, validate=lambda value: value.strip().isdigit() or "Enter a number")).strip())

    def text(self, label, required=True):
        return self.answer(self.questionary.text(label, validate=lambda value: bool(value.strip()) or not required or "Required")).strip()

    def run(self, arguments, mutating=False):
        shown = ("sudo " if getattr(self.instance, "use_sudo", False) else "") + "docker compose " + shlex.join(arguments)
        self.console.print("$ " + shown, markup=False, highlight=False, soft_wrap=True)
        if mutating and not self.confirm("Run this command?", False):
            self.console.print("Skipped.")
            return None
        try:
            code = self.instance.compose_interactive(arguments)
        except KeyboardInterrupt:
            self.console.print("\nStopped.")
            return None
        if code:
            self.console.print("Command exited with status " + str(code) + ". See the command output above.", markup=False)
            if arguments[:len(ADMIN)] == ADMIN:
                self.console.print("PostgreSQL must be running for job, memory and reminder tools; "
                                   "if a command is reported as unknown, rebuild the tool image: docker compose build task-agent", markup=False)
        return code

    def admin(self, subject, body=None, mutating=False):
        arguments = [*ADMIN, subject]
        if body:
            arguments += ["--body", body]
        if mutating:
            arguments.append("--yes")
        return self.run(arguments, mutating=mutating)

    def services(self):
        try:
            names = sorted(self.instance.compose(["config", "--services"]).split())
        except DockerCommandError:
            names = []
        return names or sorted(LONG_RUNNING_SERVICES)

    def main(self):
        ensure_docker_access(self.instance, self.confirm)
        sections = {
            "Service status": lambda: self.run(["ps", "--all"]),
            "Recent logs": self.recent_logs,
            "Follow live logs (Ctrl-C returns here)": self.follow_logs,
            "Restart / start / stop services": self.lifecycle,
            "Jobs and tasks": self.jobs,
            "Workstreams and data deletion": self.workstreams,
            "Memories": self.memories,
            "Reminders": self.reminders,
            "Embeddings and search index": self.embeddings,
            "Credentials": self.credentials,
            "Show command cheat-sheet": lambda: self.console.print(CHEAT_SHEET, markup=False, highlight=False),
        }
        while True:
            selected = self.select("Tools and troubleshooting", list(sections))
            if selected == "Back":
                return
            sections[selected]()

    def workstreams(self):
        from .setup_workstreams import WorkstreamTools
        WorkstreamTools(self.instance, self.answer, self.confirm, self.console).menu()

    def recent_logs(self):
        service = self.select("Service", ["All services", *self.services()])
        if service == "Back":
            return
        lines = str(self.number("Number of lines per service"))
        self.run(["logs", "--no-color", "--tail", lines] + ([] if service == "All services" else [service]))

    def follow_logs(self):
        service = self.select("Service", self.services())
        if service != "Back":
            self.run(["logs", "--follow", "--tail", "50", service])

    def lifecycle(self):
        actions = {
            "Restart one service": lambda: self.pick_service(lambda name: ["restart", name]),
            "Recreate one service (applies saved configuration)": lambda: self.pick_service(lambda name: ["up", "-d", "--no-deps", "--force-recreate", name]),
            "Recreate all services (applies saved configuration)": lambda: self.run(["up", "-d", "--force-recreate", "--wait", "--wait-timeout", "180"], mutating=True),
            "Start all services and wait for health": lambda: self.run(["up", "-d", "--wait", "--wait-timeout", "180"], mutating=True),
            "Stop all services (data is kept)": lambda: self.run(["stop"], mutating=True),
        }
        selected = self.select("Services", list(actions))
        if selected != "Back":
            actions[selected]()

    def pick_service(self, build):
        service = self.select("Service", self.services())
        if service != "Back":
            self.run(build(service), mutating=True)

    def jobs(self):
        actions = {
            "Job counts by status": lambda: self.admin("/admin status"),
            "Most recent jobs": lambda: self.admin("/admin jobs --recent --limit " + str(self.number("How many"))),
            "Job logs": lambda: self.admin("/admin logs %d --limit 50 --cursor %d" % (self.number("Job ID"), self.number("Start after log sequence (0 = beginning)"))),
            "Cancel a job": lambda: self.admin("/admin cancel %d" % self.number("Job ID"), mutating=True),
            "Requeue a job (retry it)": lambda: self.admin("/admin requeue %d" % self.number("Job ID"), mutating=True),
            "Add an instruction to a job (requeues finished jobs)": lambda: self.instruct(),
            "Approve a job waiting for review": lambda: self.review(),
        }
        self.submenu("Jobs and tasks", actions)

    def instruct(self):
        job = self.number("Job ID")
        self.admin("/admin instruct %d" % job, body=self.text("Instruction"), mutating=True)

    def review(self):
        job = self.number("Job ID")
        self.admin("/admin review %d" % job, body=self.text("Instructions for the retry (optional)", required=False), mutating=True)

    def memories(self):
        def act(action):
            return lambda: self.admin("/admin memory %s %d" % (action, self.number("Memory ID")), mutating=True)

        actions = {
            "Search memories": lambda: self.admin("/admin memory search --query " + shlex.quote(self.text("Search text", required=False))),
            "Pending suggestions": lambda: self.admin("/admin memory suggestions"),
            "Show a memory": lambda: self.admin("/admin memory show %d" % self.number("Memory ID")),
            "Create a memory": lambda: self.admin("/admin memory create", body=self.text("Memory text"), mutating=True),
            "Approve a suggestion": act("approve"),
            "Reject a suggestion": act("reject"),
            "Pin a memory": act("pin"),
            "Unpin a memory": act("unpin"),
            "Expire a memory now": act("expire"),
            "Delete a memory": act("delete"),
        }
        self.submenu("Memories", actions)

    def reminders(self):
        actions = {
            "List reminders": lambda: self.admin("/admin reminders --limit 100"),
            "Cancel a reminder": lambda: self.admin("/admin reminder-cancel %d" % self.number("Reminder ID"), mutating=True),
        }
        self.submenu("Reminders", actions)

    def embeddings(self):
        actions = {
            "Index status (pending/incompatible vectors)": lambda: self.run([*EMBEDDINGS, "status"]),
            "Probe the embedding model": lambda: self.run([*EMBEDDINGS, "probe"]),
            "Rebuild missing or incompatible vectors": lambda: self.run([*EMBEDDINGS, "reindex"], mutating=True),
        }
        self.submenu("Embeddings and search index", actions)

    def credentials(self):
        pending = self.instance.state.get("pending", [])
        for name in sorted(self.instance.secret_values):
            self.console.print(name + ": configured" + (" (activation pending)" if name in pending else ""), markup=False)
        if pending and self.confirm("Recreate affected running services to activate pending credentials?", False):
            targets = self.instance.apply(pending)
            self.console.print("Recreated: " + (", ".join(targets) or "none; credentials remain pending until startup"))
        self.console.print("Replace a credential with: ./scripts/setup.sh secrets update NAME")

    def submenu(self, title, actions):
        while True:
            selected = self.select(title, list(actions))
            if selected == "Back":
                return
            actions[selected]()


def tools_menu(instance, answer, confirm, console):
    Tools(instance, answer, confirm, console).main()


RESET = ["run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "downloader", "-m", "assistant_agent.factory_reset"]
PG_DUMP = ["exec", "-T", "postgres", "pg_dump", "--no-password", "-U", "assistant", "-d", "assistant", "--format=custom"]


def custom_database(instance):
    overrides = (instance.root / OVERRIDES_FILE).read_text() if (instance.root / OVERRIDES_FILE).is_file() else ""
    names = ("AGENT_DATABASE_HOST", "AGENT_DATABASE_NAME", "AGENT_DATABASE_USER")
    return "DATABASE_URL" in instance.secret_values or any(instance.values.get(name) for name in names) or any(name + "=" in overrides for name in names)


def choose_reset_categories(answer, console):
    import questionary
    from rich.table import Table

    from .factory_reset import CATEGORIES, expand

    selected = []
    while True:
        selected = answer(questionary.checkbox(
            "Data to delete (Space ticks, a toggles all, Enter confirms)",
            choices=[questionary.Choice(category.title, value=key, checked=key in selected) for key, category in CATEGORIES.items()]))
        if not selected:
            return None
        categories, added = expand(selected)
        table = Table(title="Will be deleted")
        for heading in ("Data", "Contents", "Why"):
            table.add_column(heading)
        for key in categories:
            category = CATEGORIES[key]
            contents = ", ".join([*category.tables, *category.paths]) or "runtime_state rows"
            table.add_row(category.title, contents + ("\n" + category.notes if category.notes else ""), added.get(key, "selected"))
        console.print(table)
        if added:
            console.print("Some items were added automatically so the remaining data stays consistent.")
        choice = answer(questionary.select("Selection", choices=["Continue", "Change selection", "Cancel"]))
        if choice == "Continue":
            return categories
        if choice == "Cancel":
            return None
        selected = categories


def factory_reset_menu(instance, answer, confirm, console):
    import questionary

    from .factory_reset import PRESERVED

    console.print("Factory reset permanently deletes the data you select and restarts with a clean slate.\nAlways kept: " + PRESERVED, markup=False)
    categories = choose_reset_categories(answer, console)
    if not categories:
        console.print("Nothing selected; nothing was deleted.")
        return
    keep_position = True
    if "emails" in categories or "runtime_state" in categories:
        keep_position = confirm("Keep the mailbox position, so messages already in the inbox are not downloaded and processed again?", True)
    backup = confirm("Back up the database and every file being deleted to backups/ first? (strongly recommended)", True)
    if backup and custom_database(instance):
        console.print("A custom database is configured; the built-in backup only covers the bundled PostgreSQL. Back it up yourself first.")
        backup = False
        if not confirm("Continue WITHOUT a database backup?", False):
            return
    if answer(questionary.text("Type RESET to permanently delete the selected data")).strip() != "RESET":
        console.print("Cancelled; nothing was deleted.")
        return
    ensure_docker_access(instance, confirm)
    run_factory_reset(instance, console, categories, keep_position, backup)


def run_factory_reset(instance, console, categories, keep_position, backup):
    def step(label, arguments, stdout=None):
        console.print(label + "\n$ " + ("sudo " if getattr(instance, "use_sudo", False) else "") + "docker compose " + shlex.join(arguments),
                      markup=False, highlight=False, soft_wrap=True)
        if instance.compose_interactive(arguments, stdout=stdout):
            raise SetupError(label + " failed; see the output above")

    selection = ",".join(categories)
    step("Building the maintenance image (includes the reset tool)", ["build", "downloader"])
    step("Starting PostgreSQL", ["up", "-d", "--wait", "--wait-timeout", "120", "postgres"])
    running = sorted(set(instance.compose(["ps", "--status", "running", "--services"]).split()) & set(AGENT_SERVICES))
    destination = None
    if backup:
        backups = instance.root / "backups"
        if backups.is_symlink():
            raise SetupError("backups/ must be a real directory, not a symlink")
        destination = backups / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-factory-reset")
        destination.mkdir(mode=0o700, parents=True)
        for name, arguments in (("database.dump", PG_DUMP), ("files.tar.gz", [*RESET, "archive", "--categories", selection])):
            with os.fdopen(os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as handle:
                step("Backing up to " + str(destination / name), arguments, stdout=handle)
        (destination / "manifest.json").write_text(json.dumps({"categories": categories, "keep_mailbox_position": keep_position,
                                                               "created": datetime.now().isoformat()}, indent=2))
        console.print("Backup complete: " + str(destination), markup=False)
    if running:
        step("Stopping services that write data", ["stop", *running])
    try:
        arguments = [*RESET, "apply", "--categories", selection] + ([] if keep_position else ["--discard-mailbox-position"])
        step("Deleting the selected data", arguments)
    finally:
        if running:
            try:
                step("Restarting the services that were running", ["start", *running])
            except SetupError as exc:
                console.print(str(exc) + ". Start them with: docker compose up -d", markup=False)
    console.print("Factory reset complete. The workspace index rebuilds automatically; settings, credentials and AGENT.md were not touched.")
    if destination:
        console.print("To undo, stop the writer services, then from the repository directory:\n"
                      "  docker compose exec -T postgres pg_restore --clean --if-exists -U assistant -d assistant < " + str(destination / "database.dump") + "\n"
                      "  tar -xzf " + str(destination / "files.tar.gz") + " -C " + str(instance.root), markup=False, highlight=False, soft_wrap=True)
