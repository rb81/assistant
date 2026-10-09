import os
import shutil
import ssl
import stat
import tempfile
from pathlib import Path


def read_source(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("Sandbox initialization requires regular input files")
        content = handle.read(65537)
    if not content or len(content) > 65536:
        raise ValueError("Sandbox initialization input is empty or too large")
    return content


def stage(source: Path, token_path: Path, destination: Path) -> None:
    files = {name: read_source(source / name) for name in ("sandbox.crt", "sandbox.key")}
    token = read_source(token_path).decode("utf-8").strip()
    if len(token) < 32 or not token.isascii() or any(character.isspace() for character in token):
        raise ValueError("Sandbox initialization requires a valid token")
    files["SANDBOX_API_TOKEN"] = token.encode("utf-8")
    if destination.is_symlink():
        raise ValueError("Sandbox runtime directory must not be a symlink")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    generation = Path(tempfile.mkdtemp(prefix=".generation-", dir=destination))
    published = False
    pointer = destination / ".next"
    try:
        for name, content in files.items():
            descriptor = os.open(generation / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), 0o644 if name.endswith(".crt") else 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(generation / "sandbox.crt", generation / "sandbox.key")
        verifier = ssl.create_default_context(cafile=str(generation / "sandbox.crt"))
        client_input, client_output = ssl.MemoryBIO(), ssl.MemoryBIO()
        server_input, server_output = ssl.MemoryBIO(), ssl.MemoryBIO()
        client = verifier.wrap_bio(client_input, client_output, server_hostname="sandbox")
        server = context.wrap_bio(server_input, server_output, server_side=True)
        completed = set()
        for attempt in range(10):
            for name, endpoint in (("client", client), ("server", server)):
                try:
                    endpoint.do_handshake()
                    completed.add(name)
                except ssl.SSLWantReadError:
                    pass
            for output, incoming in ((client_output, server_input), (server_output, client_input)):
                payload = output.read()
                if payload:
                    incoming.write(payload)
            if len(completed) == 2:
                break
        else:
            raise ValueError("Sandbox certificate validation did not complete")
        pointer.unlink(missing_ok=True)
        pointer.symlink_to(generation.name)
        os.replace(pointer, destination / "current")
        published = True
        for previous in destination.iterdir():
            if previous != generation and previous.name.startswith(".generation-") and previous.is_dir() and not previous.is_symlink():
                shutil.rmtree(previous)
    finally:
        pointer.unlink(missing_ok=True)
        if not published:
            shutil.rmtree(generation)


def main() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Sandbox initialization must run as container root")
    stage(Path("/run/assistant/source"), Path("/run/secrets/SANDBOX_API_TOKEN"), Path("/run/assistant/runtime"))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, ssl.SSLError):
        raise SystemExit("Sandbox initialization failed; check protected inputs and Docker file permissions") from None