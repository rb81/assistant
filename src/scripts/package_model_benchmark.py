#!/usr/bin/env python3
"""Package candidate harness code as one isolated, runnable Docker benchmark script."""
import argparse
import base64
import io
import os
from pathlib import Path
import zipfile


RUNNER = '''#!/bin/sh
set -eu
umask 077
set -C
CONTAINER="${1:?Pass the running task-agent container ID, followed by benchmark options}"
shift
if [ -z "$CONTAINER" ] || [ "$(printf '%s\\n' "$CONTAINER" | wc -l)" -ne 1 ]; then
    echo "Specify exactly one running task-agent container." >&2
    exit 2
fi
OUTPUT="${ASSISTANT_BENCHMARK_OUTPUT:-benchmark-results.json}"
echo "Paid synthetic benchmark; saved credentials stay inside Docker. Results: $OUTPUT" >&2
docker exec -i "$CONTAINER" python - "$@" > "$OUTPUT" <<'ASSISTANT_BENCHMARK_PY'
import base64
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import zipfile

ARCHIVE = "__ARCHIVE__"
with tempfile.TemporaryDirectory(prefix="assistant-candidate-benchmark-") as scratch:
    root = Path(scratch)
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(ARCHIVE))) as archive:
        archive.extractall(root)
    sys.path.insert(0, str(root / "src"))
    os.environ["ASSISTANT_DEFAULTS_DIR"] = str(root / "defaults")
    os.environ["AGENT_CONFIG"] = str(root / "defaults" / "agent.yaml")
    from assistant_agent.config import load_config
    from assistant_agent.llm_providers import provider_is_disabled
    from assistant_agent import model_benchmark
    saved = load_config()
    if provider_is_disabled(saved, "openrouter"):
        raise RuntimeError("OpenRouter is disabled in saved Assistant settings")
    saved.values["agent"]["llm"]["provider"] = "openrouter"
    saved.values["agent"]["llm"]["base_url"] = "https://openrouter.ai/api/v1"
    saved.values["agent"]["model_routes"]["task"]["provider"] = "openrouter"
    saved.values["agent"]["model_routes"]["task"]["fallbacks"] = []
    model_benchmark.load_config = lambda: saved
    result = root / "results.json"
    with contextlib.redirect_stdout(sys.stderr):
        code = model_benchmark.main([*sys.argv[1:], "--output", str(result)])
    print(result.read_text(), end="")
    raise SystemExit(code)
ASSISTANT_BENCHMARK_PY
'''


def package(output):
    server = Path(__file__).resolve().parents[2]
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        source = server / "src/agent/src"
        for path in sorted((source / "assistant_agent").rglob("*")):
            if path.is_file() and path.suffix in {".py", ".skill"} and "__pycache__" not in path.parts:
                archive.write(path, "src/" + path.relative_to(source).as_posix())
        for name in ("agent.yaml", "agent.schema.yaml"):
            archive.write(server / "src/config" / name, "defaults/" + name)
    script = RUNNER.replace("__ARCHIVE__", base64.b64encode(data.getvalue()).decode("ascii"))
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(script)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="New shell-script path outside the repository")
    args = parser.parse_args()
    package(args.output)
    print("Packaged candidate harness; no credentials or installation data included.")


if __name__ == "__main__":
    main()
