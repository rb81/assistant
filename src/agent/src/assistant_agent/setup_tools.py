"""Troubleshooting menus and factory reset for an installed Assistant."""
import shlex

from .setup import LONG_RUNNING_SERVICES, OVERRIDES_FILE, DockerCommandError, SetupError, ensure_docker_access, parse_environment


ADMIN = ["run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "task-agent", "-m", "assistant_agent.admin_commands"]
EMBEDDINGS = ["run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "task-agent", "-m", "assistant_agent.embedding_maintenance"]

CHEAT_SHEET = """\
Run these from the repository directory (prefix with sudo if your user cannot reach Docker):
  docker compose --env-file data/.env ps --all                              service state and health
  docker compose --env-file data/.env logs --tail 200 SERVICE               recent logs (add -f to follow)
  docker compose --env-file data/.env restart SERVICE                       restart one service
  docker compose --env-file data/.env up -d --force-recreate                apply .env / overrides.env changes
  docker compose --env-file data/.env up -d --wait --wait-timeout 180       start everything and wait for health
  docker compose --env-file data/.env stop                                  stop everything (data is kept)
  docker compose --env-file data/.env run --rm --no-deps -T --entrypoint python task-agent -m assistant_agent.admin_commands "/admin help"
  docker compose --env-file data/.env run --rm --no-deps -T --entrypoint python task-agent -m assistant_agent.embedding_maintenance status
  ./run.sh secrets status | ./run.sh secrets apply
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
        shown = ("sudo " if getattr(self.instance, "use_sudo", False) else "") + "docker compose --env-file data/.env " + shlex.join(arguments)
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
                                   "if a command is reported as unknown, rebuild the tool image: docker compose --env-file data/.env build task-agent", markup=False)
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
            "Script and website permissions": self.permissions,
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

    def permissions(self):
        def mutate(operation, label):
            return lambda: self.admin("/admin permissions %s %s" %
                                      (operation, shlex.quote(self.text(label))), mutating=True)
        self.console.print("Permission changes stop active sandbox runs. Completed actions are not undone.")
        self.submenu("Script and website permissions", {
            "List scripts and versions": lambda: self.admin("/admin permissions scripts"),
            "Inspect version, code and grants": lambda: self.admin("/admin permissions show %d" % self.number("Version ID")),
            "List website grants, defaults and legacy rules": lambda: self.admin("/admin permissions hosts"),
            "Approval history": lambda: self.admin("/admin permissions history"),
            "Block a script (including future versions)": mutate("block-script", "Script ID"),
            "Unblock a script": mutate("unblock-script", "Script ID"),
            "Revoke a script's run and website approvals": mutate("revoke-script", "Script ID"),
            "Block a website": mutate("block-host", "Exact host (or explicit *.example.com pattern)"),
            "Remove a global website block": mutate("unblock-host", "Host or wildcard pattern"),
            "Revoke a grant": mutate("revoke-grant", "Grant ID"),
            "Remove a legacy network rule": mutate("remove-legacy", "Legacy rule ID"),
        })

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
        self.console.print("Replace a credential with: ./run.sh secrets update NAME")

    def submenu(self, title, actions):
        while True:
            selected = self.select(title, list(actions))
            if selected == "Back":
                return
            actions[selected]()


def tools_menu(instance, answer, confirm, console):
    Tools(instance, answer, confirm, console).main()


def custom_database(instance):
    overrides = (instance.data / OVERRIDES_FILE).read_text() if (instance.data / OVERRIDES_FILE).is_file() else ""
    override_values, _ = parse_environment(overrides)
    names = ("AGENT_DATABASE_HOST", "AGENT_DATABASE_NAME", "AGENT_DATABASE_USER", "AGENT_DATABASE_PORT")
    configured = any(instance.values.get(name) or override_values.get(name) for name in names)
    configured = configured or any(instance.defaults.get("agent.database." + key, default) != default
                                   for key, default in (("host", "postgres"), ("name", "assistant"),
                                                        ("user", "assistant"), ("port", 5432)))
    return "DATABASE_URL" in instance.secret_values or configured


def factory_reset_menu(instance, answer, confirm, console):
    import questionary

    if custom_database(instance):
        console.print("Factory reset cannot destroy a custom database. Reset the configured database manually.", markup=False)
        return
    postgres_root = instance.data / "postgres"
    pgdata = postgres_root / "pgdata"
    if postgres_root.is_symlink() or pgdata.is_symlink() or (pgdata.exists() and not pgdata.is_dir()):
        console.print("Refusing to reset PostgreSQL: data/postgres/pgdata must be a real directory.", markup=False)
        return
    console.print(
        "Factory reset permanently destroys and rebuilds the bundled PostgreSQL cluster, including all database data, "
        "indexes, sequences and migration history. It preserves settings, secrets, AGENT.md and files in the workspace. "
        "No backup will be made.",
        markup=False,
    )
    if answer(questionary.text("Type RESET to destroy and rebuild the database")).strip() != "RESET":
        console.print("Cancelled; nothing was deleted.")
        return
    ensure_docker_access(instance, confirm)
    run_factory_reset(instance, console)


def run_factory_reset(instance, console):
    if custom_database(instance):
        raise SetupError("Factory reset cannot destroy a custom database; reset it manually first")
    postgres_root = instance.data / "postgres"
    pgdata = postgres_root / "pgdata"
    if postgres_root.is_symlink() or pgdata.is_symlink() or (pgdata.exists() and not pgdata.is_dir()):
        raise SetupError("Refusing to reset PostgreSQL: data/postgres/pgdata must be a real directory")

    def step(label, arguments, stdout=None):
        console.print(label + "\n$ " + ("sudo " if getattr(instance, "use_sudo", False) else "") + "docker compose --env-file data/.env " + shlex.join(arguments),
                      markup=False, highlight=False, soft_wrap=True)
        code = (console.run_command(instance, arguments)
                if callable(getattr(type(console), "run_command", None))
                else instance.compose_interactive(arguments, stdout=stdout))
        if code:
            raise SetupError(label + " failed; see the output above")

    running = sorted(set(instance.compose(["ps", "--status", "running", "--services"]).split()))
    if running:
        step("Stopping services before destroying the database", ["stop", *running])
    try:
        step("Destroying the PostgreSQL data directory",
             ["run", "--rm", "--no-deps", "--user", "0:0", "--entrypoint", "/bin/rm", "postgres", "-rf",
              "/var/lib/postgresql/data/pgdata"])
        step("Rebuilding PostgreSQL and applying the base schema",
             ["up", "-d", "--wait", "--wait-timeout", "120", "postgres"])
        services = sorted((set(running) - {"postgres"}) | {"workspace-indexer"})
        step("Starting services and reindexing the preserved workspace",
             ["up", "-d", "--wait", "--wait-timeout", "180", *services])
    finally:
        if running:
            try:
                still_running = set(instance.compose(["ps", "--status", "running", "--services"]).split())
                missing = sorted(set(running) - still_running)
                if missing:
                    step("Restoring services that were running before reset", ["up", "-d", *missing])
            except SetupError as exc:
                console.print(str(exc) + ". Start them with: docker compose --env-file data/.env up -d", markup=False)
    console.print("Factory reset complete. PostgreSQL was rebuilt; settings, secrets, AGENT.md and workspace files were preserved.", markup=False)
