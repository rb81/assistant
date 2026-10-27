#!/usr/bin/env python3
"""Maintainer-only: freeze archived SQL without importing application dependencies."""
import ast
from pathlib import Path
import shutil

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def evaluate(node, values):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return values[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return evaluate(node.left, values) + evaluate(node.right, values)
    raise ValueError("Unsupported SQL expression")


def main():
    source = ROOT / "agent/src/assistant_agent/database.py"
    values = {}
    for node in ast.parse(source.read_text()).body:
        if isinstance(node, ast.Assign):
            for name in node.targets:
                if isinstance(name, ast.Name) and name.id in {"UPDATED_AT_SQL", "FEATURE_SCHEMA_SQL"}:
                    values[name.id] = evaluate(node.value, values)
    destination = HERE / "sql"
    destination.mkdir(exist_ok=True)
    (destination / "feature_schema.sql").write_text(values["FEATURE_SCHEMA_SQL"].rstrip() + "\n")
    for path in (source.parent / "migrations").glob("*.sql"):
        shutil.copy2(path, destination / path.name)


if __name__ == "__main__":
    main()
