"""Exercise source updates with real Git and shell orchestration, without a Docker daemon."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]


def git(root, *arguments):
    return subprocess.check_output(["git", "-C", str(root), *arguments], text=True).strip()


@pytest.fixture
def update_installation(tmp_path):
    repository = tmp_path / "source"
    repository.mkdir()
    git(repository, "init", "-q", "--initial-branch=main")
    git(repository, "config", "user.email", "installer@example.invalid")
    git(repository, "config", "user.name", "Installer fixture")
    (repository / "src/scripts").mkdir(parents=True)
    (repository / "src/scripts/server.sh").write_text("# marker\n")
    (repository / "src/docker-compose.yml").write_text("name: assistant\nservices: {}\n")
    (repository / ".gitignore").write_text("data/\n")
    (repository / "run.sh").write_text(
        '#!/bin/sh\nprintf "runtime %s %s\\n" "$*" "$(cat "$(dirname "$0")/src/version")" >> "$TRACE"\n'
        'if [ "$2" = start ] && [ "${FAIL_START:-0}" = 1 ]; then exit 7; fi\n'
        'if [ "$2" = verify ] && [ "${FAIL_VERIFY:-0}" = 1 ]; then exit 8; fi\n'
        'if [ "$2" = start ]; then printf "Assistant and its management API are running.\\n"; fi\n'
    )
    (repository / "run.sh").chmod(0o755)
    shutil.copyfile(ROOT / "install.sh", repository / "install.sh")
    (repository / "src/version").write_text("one")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "Initial fixture")
    tools = tmp_path / "bin"
    tools.mkdir()
    docker = tools / "docker"
    docker.write_text(
        '#!/bin/sh\nprintf "docker %s\\n" "$*" >> "$TRACE"\n'
        'if [ "$1" = build ] && [ "${FAIL_BUILD:-0}" = 1 ]; then exit 9; fi\n'
    )
    docker.chmod(0o700)
    environment = dict(
        os.environ, HOME=str(tmp_path / "home"), PATH=str(tools) + ":" + os.environ["PATH"],
        ASSISTANT_INSTALL_DIR=str(tmp_path / "installed"), ASSISTANT_SOURCE=str(repository),
        ASSISTANT_VERSION="main", TRACE=str(tmp_path / "trace"), NO_COLOR="1",
    )
    for key in ("ASSISTANT_UPDATE", "ASSISTANT_ADOPT", "ASSISTANT_MANAGEMENT_IMAGE", "ASSISTANT_VERBOSE"):
        environment.pop(key, None)

    def install(*, script=ROOT / "install.sh", **overrides):
        return subprocess.run(
            ["sh", str(script)], input="", text=True, capture_output=True,
            env=environment | overrides, timeout=20,
        )

    return repository, Path(environment["ASSISTANT_INSTALL_DIR"]), environment, install


def next_revision(repository):
    (repository / "src/version").write_text("two")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "Second fixture")
    return git(repository, "rev-parse", "HEAD")


def fail_pending_write(environment):
    wrapper = r"""
        printf() {
            case "$1" in
                'source=%s\nref=%s\nrevision=%s\nimage=%s\nprevious=%s\nwork=%s\n')
                    command printf 'source=partial\n'
                    return 1 ;;
            esac
            command printf "$@"
        }
        . "$1"
    """
    return subprocess.run(
        ["sh", "-c", wrapper, "sh", str(ROOT / "install.sh")],
        input="", text=True, capture_output=True, env=environment, timeout=20,
    )


def test_update_remembers_source_discovers_custom_install_and_preserves_data(update_installation):
    repository, destination, environment, install = update_installation
    result = install()
    assert result.returncode == 0, result.stderr
    data = destination / "data"
    (data / "keep-private").write_bytes(b"private configuration\n")
    (data / ".env").write_text("COMPOSE_PROJECT_NAME=isolated-fixture\n")
    revision = next_revision(repository)
    for key in ("ASSISTANT_INSTALL_DIR", "ASSISTANT_SOURCE", "ASSISTANT_VERSION"):
        environment.pop(key)
    Path(environment["TRACE"]).write_text("")
    # Execute the installed copy to exercise updating the script that is running.
    result = install(script=destination / "install.sh")
    assert result.returncode == 0, result.stderr
    assert (destination / "src/version").read_text() == "two"
    assert (data / "keep-private").read_bytes() == b"private configuration\n"
    metadata = (data / ".installation").read_text()
    assert f"revision={revision}\n" in metadata
    assert f"source={repository}\n" in metadata
    assert "ref=main\n" in metadata
    trace = Path(environment["TRACE"]).read_text().splitlines()
    build = next(i for i, line in enumerate(trace) if line.startswith("docker build "))
    stop = next(i for i, line in enumerate(trace) if line.endswith(" stop"))
    start = trace.index("runtime server start two")
    verify = trace.index("runtime server verify two")
    assert build < stop < start < verify
    assert not (data / ".install-pending").exists()
    assert not Path(str(destination) + ".install-lock").exists()
    assert "Installed revision: " + revision in result.stdout
    previous = Path(next(line.removeprefix("previous=") for line in metadata.splitlines() if line.startswith("previous=")))
    assert (previous / "src/version").read_text() == "one"
    assert not (previous / "data").exists()


def test_update_refuses_modified_managed_source_without_overwriting_it(update_installation):
    repository, destination, environment, install = update_installation
    assert install().returncode == 0
    (destination / "src/version").write_text("local customization")
    next_revision(repository)
    Path(environment["TRACE"]).write_text("")
    result = install()
    assert result.returncode != 0
    assert "source was modified" in result.stderr
    assert (destination / "src/version").read_text() == "local customization"
    assert "docker build " not in Path(environment["TRACE"]).read_text()
    assert not (destination / "data/.install-pending").exists()


def test_git_checkout_fast_forwards_and_retains_git_metadata(update_installation):
    repository, destination, environment, install = update_installation
    subprocess.run(["git", "clone", "-q", str(repository), str(destination)], check=True)
    environment.pop("ASSISTANT_SOURCE")
    environment.pop("ASSISTANT_VERSION")
    revision = next_revision(repository)
    result = install()
    assert result.returncode == 0, result.stderr
    assert git(destination, "rev-parse", "HEAD") == revision
    assert git(destination, "status", "--porcelain") == ""
    assert git(destination, "remote", "get-url", "origin") == str(repository)


@pytest.mark.parametrize("diverged", [False, True])
def test_git_checkout_refuses_dirty_or_diverged_source(update_installation, diverged):
    repository, destination, _, install = update_installation
    subprocess.run(["git", "clone", "-q", str(repository), str(destination)], check=True)
    (destination / "src/version").write_text("local change")
    if diverged:
        git(destination, "config", "user.email", "installer@example.invalid")
        git(destination, "config", "user.name", "Installer fixture")
        git(destination, "add", ".")
        git(destination, "commit", "-qm", "Local branch")
        next_revision(repository)
    before = git(destination, "rev-parse", "HEAD")
    result = install()
    assert result.returncode != 0
    assert ("diverges" if diverged else "local changes") in result.stderr
    assert git(destination, "rev-parse", "HEAD") == before
    assert (destination / "src/version").read_text() == "local change"


def test_failed_build_leaves_active_source_and_services_untouched(update_installation):
    repository, destination, environment, install = update_installation
    assert install().returncode == 0
    before = (destination / "data/.installation").read_bytes()
    next_revision(repository)
    Path(environment["TRACE"]).write_text("")
    result = install(FAIL_BUILD="1")
    assert result.returncode != 0
    assert "running installation was not stopped" in result.stderr
    assert (destination / "src/version").read_text() == "one"
    assert (destination / "data/.installation").read_bytes() == before
    assert " stop\n" not in Path(environment["TRACE"]).read_text()
    assert not (destination / "data/.install-pending").exists()


def test_failed_pending_write_does_not_create_invalid_recovery_record(update_installation):
    repository, destination, environment, install = update_installation
    assert install().returncode == 0
    next_revision(repository)
    Path(environment["TRACE"]).write_text("")
    result = fail_pending_write(environment)
    assert result.returncode != 0
    assert "Could not write update recovery metadata" in result.stderr
    assert "recording update recovery information" in result.stderr
    assert not (destination / "data/.install-pending").exists()
    assert not list((destination / "data").glob(".install-pending.*"))
    assert not list(destination.parent.glob(destination.name + ".update.*"))
    assert (destination / "src/version").read_text() == "one"
    assert " stop\n" not in Path(environment["TRACE"]).read_text()


def test_failed_replacement_pending_write_preserves_previous_recovery_record(update_installation):
    repository, destination, environment, install = update_installation
    subprocess.run(["git", "clone", "-q", str(repository), str(destination)], check=True)
    assert install().returncode == 0
    next_revision(repository)
    assert install(FAIL_START="1").returncode != 0
    pending = destination / "data/.install-pending"
    original_pending = pending.read_bytes()
    original_work = list(destination.parent.glob(destination.name + ".update.*"))

    (repository / "src/version").write_text("three")
    git(repository, "commit", "-qam", "Fix startup")
    git(destination, "pull", "--ff-only")
    Path(environment["TRACE"]).write_text("")
    result = fail_pending_write(environment)
    assert result.returncode != 0
    assert "Could not write update recovery metadata" in result.stderr
    assert pending.read_bytes() == original_pending
    assert list(destination.parent.glob(destination.name + ".update.*")) == original_work
    assert not list((destination / "data").glob(".install-pending.*"))
    assert " stop\n" not in Path(environment["TRACE"]).read_text()
    assert install().returncode == 0


@pytest.mark.parametrize("failure", ["FAIL_START", "FAIL_VERIFY"])
def test_incomplete_update_retains_recovery_and_can_finish_without_downloading(update_installation, failure):
    repository, destination, environment, install = update_installation
    assert install().returncode == 0
    before = (destination / "data/.installation").read_bytes()
    revision = next_revision(repository)
    result = install(**{failure: "1"})
    assert result.returncode != 0
    assert (destination / "src/version").read_text() == "two"
    assert (destination / "data/.installation").read_bytes() == before
    assert (destination / "data/.install-pending").exists()
    assert "Installation complete" not in result.stdout
    assert install(**{failure: "1"}).returncode != 0
    Path(environment["TRACE"]).write_text("")
    result = install()
    assert result.returncode == 0, result.stderr
    assert f"revision={revision}\n" in (destination / "data/.installation").read_text()
    assert not (destination / "data/.install-pending").exists()
    assert "docker build " not in Path(environment["TRACE"]).read_text()
    assert not list(destination.parent.glob(destination.name + ".update.*"))


def test_interrupted_git_update_recovers_at_newer_checked_out_revision(update_installation):
    repository, destination, environment, install = update_installation
    subprocess.run(["git", "clone", "-q", str(repository), str(destination)], check=True)
    assert install().returncode == 0
    data = destination / "data"
    (data / "keep-private").write_text("private data")
    pending_revision = next_revision(repository)
    assert install(FAIL_START="1").returncode != 0
    pending = (data / ".install-pending").read_bytes()
    assert f"revision={pending_revision}\n".encode() in pending
    original_work = list(destination.parent.glob(destination.name + ".update.*"))
    assert len(original_work) == 1

    (repository / "src/version").write_text("three")
    git(repository, "commit", "-qam", "Fix startup")
    revision = git(repository, "rev-parse", "HEAD")
    git(destination, "pull", "--ff-only")
    (destination / "src/version").write_text("local customization")
    assert "checkout has local changes" in install().stderr
    assert (destination / "src/version").read_text() == "local customization"
    (destination / "src/version").write_text("three")

    Path(environment["TRACE"]).write_text("")
    result = install(FAIL_BUILD="1")
    assert result.returncode != 0
    assert (data / ".install-pending").read_bytes() == pending
    assert list(destination.parent.glob(destination.name + ".update.*")) == original_work
    assert "runtime server start" not in Path(environment["TRACE"]).read_text()

    result = install(script=destination / "install.sh", FAIL_START="1")
    assert result.returncode != 0
    assert "Recovering interrupted update" in result.stdout
    assert f"revision={revision}\n" in (data / ".install-pending").read_text()
    assert not original_work[0].exists()

    result = install(script=destination / "install.sh")
    assert result.returncode == 0, result.stderr
    assert f"Finishing interrupted update: {revision}" in result.stdout
    assert f"revision={revision}\n" in (data / ".installation").read_text()
    assert (data / "keep-private").read_text() == "private data"
    assert not (data / ".install-pending").exists()
    assert not list(destination.parent.glob(destination.name + ".update.*"))
    assert "runtime server start three" in Path(environment["TRACE"]).read_text()
    assert "runtime server verify three" in Path(environment["TRACE"]).read_text()
    assert git(destination, "rev-parse", "HEAD") == revision


def test_interrupted_git_update_rejects_checkout_outside_pending_history(update_installation):
    repository, destination, environment, install = update_installation
    subprocess.run(["git", "clone", "-q", str(repository), str(destination)], check=True)
    original = git(destination, "rev-parse", "HEAD")
    next_revision(repository)
    assert install(FAIL_START="1").returncode != 0
    pending = destination / "data/.install-pending"
    before = pending.read_bytes()
    git(destination, "switch", "--detach", original)
    Path(environment["TRACE"]).write_text("")
    result = install()
    assert result.returncode != 0
    assert "not a descendant of the pending Git revision" in result.stderr
    assert pending.read_bytes() == before
    assert git(destination, "rev-parse", "HEAD") == original
    assert "runtime server start" not in Path(environment["TRACE"]).read_text()


def test_incomplete_source_activation_is_not_mislabeled_as_repaired(update_installation):
    repository, destination, _, install = update_installation
    assert install().returncode == 0
    next_revision(repository)
    assert install(FAIL_START="1").returncode != 0
    (destination / "src/version").write_text("partial source")
    result = install(ASSISTANT_UPDATE="0")
    assert result.returncode != 0
    assert "Source activation was interrupted" in result.stderr
    assert (destination / "data/.install-pending").exists()


def test_failed_download_does_not_stop_existing_installation(update_installation):
    _, destination, environment, install = update_installation
    assert install().returncode == 0
    before = (destination / "data/.installation").read_bytes()
    Path(environment["TRACE"]).write_text("")
    result = install(ASSISTANT_VERSION="missing-branch")
    assert result.returncode != 0
    assert "Could not fetch" in result.stderr
    assert (destination / "data/.installation").read_bytes() == before
    assert " stop\n" not in Path(environment["TRACE"]).read_text()


def test_same_revision_reconciles_without_stopping_or_replacing_source(update_installation):
    _, destination, environment, install = update_installation
    assert install().returncode == 0
    (destination / "data/.env").write_text("COMPOSE_PROJECT_NAME=isolated-fixture\n")
    before_inode = (destination / "src/version").stat().st_ino
    Path(environment["TRACE"]).write_text("")
    result = install()
    assert result.returncode == 0, result.stderr
    assert (destination / "src/version").stat().st_ino == before_inode
    trace = Path(environment["TRACE"]).read_text()
    assert " stop\n" not in trace
    assert "runtime server start one" in trace
    assert "runtime server verify one" in trace


def test_link_in_update_archive_is_rejected_before_touching_running_source(update_installation):
    repository, destination, environment, install = update_installation
    assert install().returncode == 0
    (repository / "src/link").symlink_to("../../outside")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "Unsupported link")
    Path(environment["TRACE"]).write_text("")
    result = install()
    assert result.returncode != 0
    assert "unsupported paths or links" in result.stderr
    assert (destination / "src/version").read_text() == "one"
    assert not (destination / "src/link").exists()
    assert " stop\n" not in Path(environment["TRACE"]).read_text()


def test_new_source_file_does_not_replace_an_unrelated_existing_file(update_installation):
    repository, destination, environment, install = update_installation
    assert install().returncode == 0
    (destination / "operator-notes").write_text("keep my notes")
    (repository / "operator-notes").write_text("new shipped file")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "New source path")
    Path(environment["TRACE"]).write_text("")
    result = install()
    assert result.returncode != 0
    assert "unrelated file blocks" in result.stderr
    assert (destination / "operator-notes").read_text() == "keep my notes"
    assert (destination / "src/version").read_text() == "one"
    assert " stop\n" not in Path(environment["TRACE"]).read_text()
    assert not (destination / "data/.install-pending").exists()


def test_executable_only_source_update_is_activated(update_installation):
    repository, destination, _, install = update_installation
    assert install().returncode == 0
    assert not os.access(destination / "src/version", os.X_OK)
    (repository / "src/version").chmod(0o755)
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "Executable mode")
    result = install()
    assert result.returncode == 0, result.stderr
    assert os.access(destination / "src/version", os.X_OK)


def test_failed_first_download_can_be_retried_without_manually_removing_destination(update_installation):
    _, destination, _, install = update_installation
    result = install(ASSISTANT_VERSION="missing-branch")
    assert result.returncode != 0
    assert not (destination / "src").exists()
    result = install()
    assert result.returncode == 0, result.stderr
    assert (destination / "src/version").read_text() == "one"
    assert not (destination / "data/.install-initializing").exists()
