#!/usr/bin/env python3
"""Offline, explicitly acknowledged database transition; no application imports."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

MARKER = "000_current_baseline"
HERE = Path(__file__).resolve().parent


def docker(*args, input=None):
    result = subprocess.run(
        ["docker", *args], input=input, text=True, capture_output=True
    )
    if result.returncode:
        raise RuntimeError("Docker/psql failed; output suppressed to protect private data. "
                           "Keep services stopped and check the baseline marker before retrying.")
    return result.stdout


def verify_container(container, project):
    info = json.loads(docker("inspect", container))[0]
    labels = info["Config"].get("Labels") or {}
    if (labels.get("com.docker.compose.project") != project
            or labels.get("com.docker.compose.service") != "postgres"
            or not info["State"]["Running"]):
        raise RuntimeError("Expected a running postgres service in the explicitly named Compose project")
    running = docker("ps", "--no-trunc", "--quiet", "--filter",
                     f"label=com.docker.compose.project={project}").split()
    if set(running) != {info["Id"]}:
        raise RuntimeError("Stop every other container in this Compose project before migration")
    return info["Id"]


def build_sql(sql_dir=HERE / "sql"):
    chunks = [
        "\\set ON_ERROR_STOP on",
        "BEGIN;",
        "SET LOCAL search_path = public, pg_catalog;",
        "SELECT pg_advisory_xact_lock(hashtext('assistant_schema_upgrade'));",
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());",
        # A marker identifies a completed transition, not a reason to run legacy SQL again.
        f"SELECT NOT EXISTS (SELECT 1 FROM schema_migrations WHERE filename = '{MARKER}') AS pending \\gset",
        "\\if :pending",
        "DO $$ BEGIN IF to_regclass('public.emails') IS NULL OR "
        "to_regclass('public.jobs') IS NULL OR to_regclass('public.agent_memories') IS NULL "
        "THEN RAISE EXCEPTION 'Not a supported initialized Assistant database'; END IF; END $$;",
        (sql_dir / "feature_schema.sql").read_text(),
    ]
    migrations = sorted(sql_dir.glob("[0-9][0-9][0-9]_*.sql"))
    if len(migrations) != 11:
        raise RuntimeError("Incomplete transition SQL bundle")
    for path in migrations:
        name = path.name
        chunks.extend([
            f"SELECT NOT EXISTS (SELECT 1 FROM schema_migrations WHERE filename = '{name}') AS pending \\gset",
            "\\if :pending",
            path.read_text(),
            f"INSERT INTO schema_migrations(filename) VALUES ('{name}');",
            "\\endif",
        ])
    chunks.extend([
        f"INSERT INTO schema_migrations(filename) VALUES ('{MARKER}');",
        "\\endif",
        "COMMIT;",
    ])
    return "\n".join(chunks) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True, help="Existing postgres container ID")
    parser.add_argument("--project", required=True, help="Exact existing Compose project label")
    parser.add_argument("--database", default="assistant")
    parser.add_argument("--user", default="assistant")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-confirmed", action="store_true")
    parser.add_argument("--accept-destructive-v1-retirement", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and not (args.backup_confirmed and args.accept_destructive_v1_retirement):
        parser.error("--apply requires --backup-confirmed and --accept-destructive-v1-retirement")
    try:
        sql = build_sql()
        container = verify_container(args.container, args.project)
        if not args.apply:
            print("Preflight passed; no database changes. Apply requires both acknowledgements.")
            return 0
        # Recheck immediately before entering the serialized transaction.
        verify_container(container, args.project)
        docker("exec", "-i", container, "psql", "-X", "-v", "ON_ERROR_STOP=1",
               "-U", args.user, "-d", args.database, input=sql)
        print("Database transition committed; current baseline and all migration filenames recorded.")
        return 0
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"Transition refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
