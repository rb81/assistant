"""Static analysis of scripts the agent asks the sandbox to run.

Builds a deterministic manifest (language, imports, risky calls, network
endpoints, file paths, environment access, dependency installs, obfuscation)
for the decision model, plus "hard signals" that always require review.
Static analysis cannot see code built or downloaded at run time; such patterns
are reported as signals rather than resolved.
"""

import ast
import hashlib
import ipaddress
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

MAX_FILE_BYTES = 1_000_000
PYTHON = {"python", "python3", "pypy", "pypy3"}
SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "ash"}
NODE = {"node", "nodejs", "deno", "bun"}
OTHER_INTERPRETERS = {"perl", "ruby", "php", "Rscript", "lua", "osascript", "pwsh"}
WRAPPERS = {"env", "timeout", "nice", "nohup", "time", "stdbuf", "xargs", "exec", "command"}
INSTALLERS = {
    ("pip", "install"), ("pip3", "install"), ("npm", "install"), ("npm", "i"), ("yarn", "add"), ("pnpm", "add"),
    ("gem", "install"), ("apt", "install"), ("apt-get", "install"), ("cargo", "install"), ("go", "install"),
}
NETWORK_COMMANDS = {"curl", "wget", "nc", "ncat", "netcat", "socat", "ssh", "scp", "sftp", "rsync", "ftp", "telnet", "git"}
SENSITIVE_PATHS = (
    "/etc/passwd", "/etc/shadow", "/etc/ssh", "~/.ssh", "/.ssh/", "id_rsa", "id_ed25519", "/run/secrets",
    "/var/run/docker.sock", "docker.sock", "/root/", "~/.aws", ".aws/credentials", ".netrc", ".git-credentials",
    "/run/assistant", "/proc/self/environ",
)
URL_RE = re.compile(r"""\b(?:https?|ftp|wss?|sftp|ssh|git)://[^\s'"<>`)\]}]+""", re.IGNORECASE)
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b")
BLOB_RE = re.compile(r"[A-Za-z0-9+/=]{200,}|(?:\\x[0-9a-fA-F]{2}){40,}|[0-9a-fA-F]{300,}")
REMOTE_PIPE_RE = re.compile(
    r"(?:curl|wget)\b[^|;&\n]*\|\s*(?:sudo\s+)?(?:env\s+)?(?:ba|z|da|k)?sh\b|(?:curl|wget)\b[^|;&\n]*\|\s*(?:sudo\s+)?(?:python3?|perl|ruby|node)\b"
    r"|(?:ba|z)?sh\s+<\(\s*(?:curl|wget)|\$\(\s*(?:curl|wget)[^)]*\)\s*$|(?:eval|source|\.)\s+\"?\$\(\s*(?:curl|wget)",
    re.IGNORECASE | re.MULTILINE,
)
DECODE_PIPE_RE = re.compile(r"base64\s+(?:-d|--decode)\b[^|;\n]*\|\s*(?:ba|z|da)?sh\b|xxd\s+-r[^|;\n]*\|\s*(?:ba|z|da)?sh\b", re.IGNORECASE)

PY_RISKY = {
    "code_execution": ("exec", "eval", "compile", "__import__", "importlib.import_module", "runpy.", "code.interact"),
    "process": ("subprocess.", "os.system", "os.popen", "os.exec", "os.spawn", "os.fork", "pty.spawn", "multiprocessing."),
    "network": (
        "socket.", "requests.", "httpx.", "urllib.request.", "urllib3.", "aiohttp.", "http.client.", "ftplib.",
        "smtplib.", "paramiko.", "telnetlib.", "websocket", "websockets.", "pycurl.", "boto3.", "dns.",
    ),
    "file_deletion": ("shutil.rmtree", "os.remove", "os.unlink", "os.rmdir", "os.removedirs", ".unlink", ".rmdir", ".rmtree"),
    "permissions": ("os.chmod", "os.chown", "os.setuid", "os.setgid", ".chmod"),
    "deserialization": ("pickle.load", "pickle.loads", "marshal.load", "marshal.loads", "dill.load", "yaml.load", "shelve.open"),
    "native_code": ("ctypes.", "cffi."),
    "environment": ("os.environ", "os.getenv", "os.putenv"),
}
DECODERS = ("b64decode", "b32decode", "b85decode", "a85decode", "decompress", "fromhex", "unhexlify", "codecs.decode", "rot13", "decode")
JS_RISKY = {
    "process": re.compile(r"child_process|\bexecSync\b|\bspawn(?:Sync)?\s*\("),
    "code_execution": re.compile(r"\beval\s*\(|new\s+Function\s*\(|\bvm\.run"),
    "network": re.compile(r"\bfetch\s*\(|require\(\s*['\"](?:https?|net|dgram|tls)['\"]\s*\)|\baxios\b|XMLHttpRequest|WebSocket"),
    "file_deletion": re.compile(r"\b(?:rmSync|rmdirSync|unlinkSync|rm|unlink)\s*\("),
    "environment": re.compile(r"process\.env"),
}
JS_IMPORT_RE = re.compile(
    r"""require\(\s*['"]([^'"]+)['"]\s*\)|^\s*(?:import|export)\s[^'"]*?from\s*['"]([^'"]+)['"]"""
    r"""|import\(\s*['"]([^'"]+)['"]\s*\)|^\s*import\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)


@dataclass
class ScriptManifest:
    command: list[str]
    language: str
    entry: Optional[str] = None
    files: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    imports: dict[str, set[str]] = field(default_factory=lambda: {"stdlib": set(), "third_party": set(), "local": set()})
    risky_calls: dict[str, set[str]] = field(default_factory=dict)
    commands: set[str] = field(default_factory=set)
    urls: set[str] = field(default_factory=set)
    hosts: set[str] = field(default_factory=set)
    ips: set[str] = field(default_factory=set)
    sensitive_paths: set[str] = field(default_factory=set)
    installs: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    hard_signals: set[str] = field(default_factory=set)

    def add_risk(self, category: str, name: str) -> None:
        self.risky_calls.setdefault(category, set()).add(name)

    def state(self) -> dict[str, Any]:
        """JSON-safe screening state; the source is included after the manifest."""
        return {
            "command": self.command,
            "language": self.language,
            "entry_script": self.entry,
            "manifest": {
                "imports": {key: sorted(value) for key, value in self.imports.items() if value},
                "risky_calls": {key: sorted(value) for key, value in sorted(self.risky_calls.items())},
                "shell_commands": sorted(self.commands),
                "network": {"urls": sorted(self.urls), "hosts": sorted(self.hosts), "ip_addresses": sorted(self.ips)},
                "sensitive_paths": sorted(self.sensitive_paths),
                "dependency_installs": sorted(self.installs),
                "static_analysis_notes": self.notes,
                "hard_signals": sorted(self.hard_signals),
            },
            "files": [{"path": path, "sha256": digest, "source": self.sources.get(path, "")} for path, digest in self.files.items()],
        }

    def fingerprint_material(self) -> dict[str, Any]:
        return {"command": self.command, "files": dict(sorted(self.files.items()))}


class ScriptAnalyzer:
    def __init__(self, root: Path, workdir: Path, allowed_domains: list[str], max_files: int = 20):
        self.root = root.resolve()
        self.workdir = workdir.resolve()
        self.allowed_domains = [item.strip().lower().lstrip(".") for item in allowed_domains if item.strip()]
        self.max_files = max(1, max_files)

    def analyze(self, argv: list[str]) -> Optional[ScriptManifest]:
        """Return a manifest when argv runs code (a script, inline code or a package install), else None."""
        argv = [str(item) for item in argv]
        args = self._strip_wrappers(argv)
        if not args:
            return None
        program = Path(args[0]).name
        base = re.sub(r"(\d+(\.\d+)*)$", "", program)
        manifest: Optional[ScriptManifest] = None
        if base in PYTHON:
            manifest = self._python_command(argv, args[1:])
        elif base in SHELLS:
            manifest = self._interpreter_command(argv, args[1:], "shell")
        elif base in NODE:
            manifest = self._interpreter_command(argv, args[1:], "javascript")
        elif base in OTHER_INTERPRETERS:
            manifest = self._interpreter_command(argv, args[1:], base.lower())
        elif len(args) > 1 and (program, args[1]) in INSTALLERS:
            manifest = ScriptManifest(argv, "package_install")
            manifest.installs.update(item for item in args[2:] if not item.startswith("-"))
        elif "/" in args[0] or args[0].startswith("."):
            manifest = self._direct_executable(argv, args[0])
        if manifest is not None:
            self._finish(manifest)
        return manifest

    def _strip_wrappers(self, argv: list[str]) -> list[str]:
        args = list(argv)
        while args and Path(args[0]).name in WRAPPERS:
            name = Path(args.pop(0)).name
            while args and (args[0].startswith("-") or (name == "env" and "=" in args[0]) or (name == "timeout" and re.fullmatch(r"[\d.]+[smhd]?", args[0]))):
                args.pop(0)
        return args

    def _python_command(self, argv: list[str], rest: list[str]) -> Optional[ScriptManifest]:
        if rest in (["--version"], ["-V"], ["--help"], ["-h"]):
            return None
        manifest = ScriptManifest(argv, "python")
        index = 0
        while index < len(rest):
            arg = rest[index]
            if arg == "-c" and index + 1 < len(rest):
                self._python_source(manifest, "<inline -c>", rest[index + 1], self.workdir)
                return manifest
            if arg == "-m" and index + 1 < len(rest):
                module = rest[index + 1]
                if module in {"pip", "pip3"} and index + 2 < len(rest) and rest[index + 2] == "install":
                    manifest.language = "package_install"
                    manifest.installs.update(item for item in rest[index + 3:] if not item.startswith("-"))
                    return manifest
                path = self._local_module(module.split(".")[0], self.workdir)
                if path:
                    manifest.entry = self._display(path)
                    self._classify_python_import(manifest, module, self.workdir)
                else:
                    self._classify_python_import(manifest, module, self.workdir)
                    manifest.notes.append("runs module %s" % module)
                return manifest
            if arg in {"-", "-i"}:
                manifest.hard_signals.add("code_not_inspectable")
                manifest.notes.append("reads code from standard input")
                return manifest
            if arg.startswith("-"):
                index += 2 if arg in {"-W", "-X"} else 1
                continue
            self._script_file(manifest, arg)
            return manifest
        manifest.hard_signals.add("code_not_inspectable")
        manifest.notes.append("interactive interpreter")
        return manifest

    def _interpreter_command(self, argv: list[str], rest: list[str], language: str) -> ScriptManifest:
        manifest = ScriptManifest(argv, language)
        inline_flags = {"javascript": {"-e", "--eval", "-p", "--print"}}.get(language, {"-e", "-E", "-r"})
        for index, arg in enumerate(rest):
            inline = arg in inline_flags or (language == "shell" and re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", arg))
            if inline:
                if index + 1 < len(rest):
                    self._source(manifest, "<inline %s>" % arg, rest[index + 1], language, self.workdir)
                    return manifest
                break
            if arg.startswith("-"):
                continue
            self._script_file(manifest, arg, language)
            return manifest
        manifest.hard_signals.add("code_not_inspectable")
        manifest.notes.append("reads code from standard input or an interactive session")
        return manifest

    def _direct_executable(self, argv: list[str], program: str) -> Optional[ScriptManifest]:
        path = self._resolve(program)
        if path is None:
            return None
        data = self._read_bytes(path)
        if data is None:
            return None
        manifest = ScriptManifest(argv, "unknown", entry=self._display(path))
        if b"\x00" in data[:4096]:
            manifest.language = "binary"
            manifest.files[self._display(path)] = hashlib.sha256(data).hexdigest()
            manifest.hard_signals.add("binary_executable")
            manifest.notes.append("executes a compiled or binary file from the workspace")
            return manifest
        text = data.decode("utf-8", errors="replace")
        first = text.splitlines()[0] if text else ""
        language = "unknown"
        if first.startswith("#!"):
            parts = first[2:].split()
            interpreter = Path(parts[1] if parts and Path(parts[0]).name == "env" and len(parts) > 1 else (parts[0] if parts else "")).name
            interpreter = re.sub(r"(\d+(\.\d+)*)$", "", interpreter)
            language = "python" if interpreter in PYTHON else "shell" if interpreter in SHELLS else "javascript" if interpreter in NODE else interpreter or "unknown"
        else:
            language = "shell"
            manifest.notes.append("no shebang; analysed as a shell script")
        manifest.language = language
        self._record_file(manifest, path, text)
        if language == "python":
            self._python_source(manifest, self._display(path), text, path.parent, record=False)
        else:
            self._source(manifest, self._display(path), text, language, path.parent, record=False)
        return manifest

    def _script_file(self, manifest: ScriptManifest, arg: str, language: str = "python") -> None:
        path = self._resolve(arg)
        if path is None:
            manifest.hard_signals.add("code_not_inspectable")
            manifest.notes.append("script %s is missing or outside the shared workspace" % arg)
            return
        manifest.entry = self._display(path)
        if language == "python":
            self._python_file(manifest, path)
            return
        text = self._read_text(manifest, path)
        if text is not None:
            self._record_file(manifest, path, text)
            self._source(manifest, self._display(path), text, language, path.parent, record=False)

    def _python_file(self, manifest: ScriptManifest, path: Path) -> None:
        self._require_regular_code_path(path)
        if self._display(path) in manifest.files:
            return
        if len(manifest.files) >= self.max_files:
            manifest.notes.append("local import limit reached; %s not analysed" % self._display(path))
            manifest.hard_signals.add("code_not_inspectable")
            return
        text = self._read_text(manifest, path)
        if text is None:
            return
        self._record_file(manifest, path, text)
        self._python_source(manifest, self._display(path), text, path.parent, record=False)

    def _source(self, manifest: ScriptManifest, label: str, text: str, language: str, base: Path, record: bool = True) -> None:
        if record:
            manifest.sources[label] = text
            manifest.files[label] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if language == "python":
            self._python_source(manifest, label, text, base, record=False)
            return
        if language == "shell":
            self._shell_source(manifest, text, base)
        elif language == "javascript":
            self._javascript_source(manifest, text, base)
        self._scan_text(manifest, text)

    def _python_source(self, manifest: ScriptManifest, label: str, text: str, base: Path, record: bool = True) -> None:
        if record:
            manifest.sources[label] = text
            manifest.files[label] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self._scan_text(manifest, text)
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError) as exc:
            manifest.hard_signals.add("unparseable_code")
            manifest.notes.append("%s could not be parsed: %s" % (label, exc))
            return
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
                    self._classify_python_import(manifest, alias.name, base)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    manifest.imports["local"].add("." * node.level + (node.module or ""))
                    target = base / (node.module or "").replace(".", "/") if node.module else base
                    for candidate in (target.with_suffix(".py"), target / "__init__.py"):
                        if candidate.is_file() and self._inside(candidate):
                            self._python_file(manifest, candidate)
                    continue
                module = node.module or ""
                self._classify_python_import(manifest, module, base)
                for alias in node.names:
                    aliases[alias.asname or alias.name] = module + "." + alias.name
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = self._resolve_name(node.func, aliases)
                if not name:
                    continue
                self._match_python_risk(manifest, name)
                if name in {"exec", "eval", "compile"} or name.endswith(("subprocess.run", "subprocess.call", "subprocess.Popen", "os.system")):
                    if any(self._contains_decoder(arg, aliases) for arg in node.args):
                        manifest.hard_signals.add("obfuscated_code_execution")
                        manifest.notes.append("%s runs decoded or decompressed data" % name)
                if name in {"__import__", "importlib.import_module"} and node.args and not isinstance(node.args[0], ast.Constant):
                    manifest.notes.append("dynamic import of a computed module name")
            elif isinstance(node, (ast.Attribute, ast.Name)) and not isinstance(getattr(node, "ctx", None), ast.Store):
                name = self._resolve_name(node, aliases)
                if name in {"os.environ"}:
                    manifest.add_risk("environment", name)

    def _classify_python_import(self, manifest: ScriptManifest, module: str, base: Path) -> None:
        top = module.split(".")[0]
        if not top:
            return
        local = self._local_module(top, base)
        if local:
            manifest.imports["local"].add(module)
            self._python_file(manifest, local)
            if local.name == "__init__.py":
                for child in sorted(local.parent.rglob("*.py")):
                    if len(manifest.files) >= self.max_files:
                        manifest.hard_signals.add("code_not_inspectable")
                        manifest.notes.append("local package exceeds file inspection limit")
                        break
                    if self._inside(child):
                        self._python_file(manifest, child)
        elif top in getattr(sys, "stdlib_module_names", ()):
            manifest.imports["stdlib"].add(module)
        else:
            manifest.imports["third_party"].add(module)

    def _local_module(self, name: str, base: Path) -> Optional[Path]:
        for directory in dict.fromkeys((base, self.workdir)):
            for candidate in (directory / (name + ".py"), directory / name / "__init__.py"):
                if candidate.is_file() and self._inside(candidate):
                    self._require_regular_code_path(candidate)
                    return candidate.resolve()
        return None

    @staticmethod
    def _resolve_name(node: ast.AST, aliases: dict[str, str]) -> str:
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(aliases.get(node.id, node.id))
        elif parts:
            parts.append("")
        else:
            return ""
        return ".".join(reversed(parts))

    @staticmethod
    def _match_python_risk(manifest: ScriptManifest, name: str) -> None:
        for category, patterns in PY_RISKY.items():
            for pattern in patterns:
                if pattern.startswith("."):
                    matched = name.endswith(pattern)
                elif pattern.endswith("."):
                    matched = name.startswith(pattern)
                else:
                    matched = name == pattern or name.startswith(pattern + ".") or (pattern in {"os.exec", "os.spawn"} and name.startswith(pattern))
                if matched:
                    manifest.add_risk(category, name.lstrip("."))
                    return

    def _contains_decoder(self, node: ast.AST, aliases: dict[str, str]) -> bool:
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and self._resolve_name(child.func, aliases).endswith(DECODERS):
                return True
        return False

    def _shell_source(self, manifest: ScriptManifest, text: str, base: Path) -> None:
        if REMOTE_PIPE_RE.search(text):
            manifest.hard_signals.add("remote_code_execution")
            manifest.notes.append("downloads code and pipes or substitutes it into an interpreter")
        if DECODE_PIPE_RE.search(text):
            manifest.hard_signals.add("obfuscated_code_execution")
            manifest.notes.append("decodes data and pipes it into a shell")
        statements: list[list[str]] = []
        for line in text.replace("\\\n", " ").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|()<>")
                lexer.whitespace_split = True
                lexer.commenters = "#"
                tokens = list(lexer)
            except ValueError:
                tokens = line.split()
            statements.append([])
            for token in tokens:
                if token and set(token) <= set(";&|()<>"):
                    statements.append([])
                else:
                    statements[-1].append(token)
        for words in statements:
            while words and (words[0] in {"sudo", "then", "do", "else", "if", "while", "until", "!", "{", "time", "env"}
                             or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0])):
                words = words[1:]
            if not words:
                continue
            name = Path(words[0]).name
            manifest.commands.add(name)
            if name in NETWORK_COMMANDS:
                manifest.add_risk("network", name)
            if name in {"rm", "shred", "dd", "mkfs", "truncate"}:
                flags = "".join(word for word in words[1:] if word.startswith("-"))
                if name != "rm" or "r" in flags or "f" in flags:
                    manifest.add_risk("file_deletion", " ".join(words[:3]))
                if name == "rm" and any(word in {"/", "/*", "~", "~/", "$HOME", "*", ".", ".."} for word in words[1:]):
                    manifest.hard_signals.add("destructive_command")
            if name in {"chmod", "chown", "setfacl"}:
                manifest.add_risk("permissions", name)
            if name in {"eval", "source", "."}:
                manifest.add_risk("code_execution", name)
            if name in {"crontab", "systemctl", "at", "launchctl"}:
                manifest.add_risk("persistence", name)
            if len(words) > 1 and (name, words[1]) in INSTALLERS:
                manifest.installs.update(word for word in words[2:] if not word.startswith("-"))
            if name in SHELLS | PYTHON | NODE and "-c" in words[1:-1]:
                nested = words[words.index("-c") + 1]
                self._source(manifest, "<nested %s -c>" % name, nested, "python" if name in PYTHON else "shell" if name in SHELLS else "javascript", base, record=False)

    def _javascript_source(self, manifest: ScriptManifest, text: str, base: Path) -> None:
        for match in JS_IMPORT_RE.finditer(text):
            module = next(group for group in match.groups() if group)
            category = "local" if module.startswith((".", "/")) else "stdlib" if module.startswith("node:") or module in {"fs", "path", "os", "http", "https", "net", "child_process", "crypto", "url", "util", "zlib", "stream", "dgram", "tls", "vm"} else "third_party"
            manifest.imports[category].add(module)
            if category == "local":
                target = base / module
                path = next((candidate for candidate in (
                    target, Path(str(target) + ".js"), Path(str(target) + ".mjs"),
                    Path(str(target) + ".cjs"), target / "index.js",
                ) if candidate.is_file() and self._inside(candidate)), None)
                if path is None or path.suffix not in {".js", ".mjs", ".cjs"}:
                    manifest.hard_signals.add("code_not_inspectable")
                    manifest.notes.append("local JavaScript dependency could not be pinned: " + module)
                elif self._display(path) not in manifest.files:
                    self._require_regular_code_path(path)
                    if len(manifest.files) >= self.max_files:
                        manifest.hard_signals.add("code_not_inspectable")
                        manifest.notes.append("local JavaScript dependency limit reached")
                    else:
                        source = self._read_text(manifest, path)
                        if source is not None:
                            self._record_file(manifest, path, source)
                            self._source(manifest, self._display(path), source, "javascript", path.parent, record=False)
        if re.search(r"\b(?:require|import)\s*\(\s*[^'\"\s]", text):
            manifest.hard_signals.add("code_not_inspectable")
            manifest.notes.append("dynamic JavaScript dependency")
        for category, pattern in JS_RISKY.items():
            for match in pattern.finditer(text):
                manifest.add_risk(category, match.group(0).strip()[:60])

    def _scan_text(self, manifest: ScriptManifest, text: str) -> None:
        for url in URL_RE.findall(text):
            url = url.rstrip(".,;:")
            manifest.urls.add(url)
            host = (urlsplit(url).hostname or "").lower()
            if host:
                manifest.hosts.add(host)
        for candidate in IPV4_RE.findall(text):
            address = candidate.split(":")[0]
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if not ip.is_loopback and str(ip) not in {"0.0.0.0", "255.255.255.255"}:
                manifest.ips.add(candidate)
        lowered = text.lower()
        for path in SENSITIVE_PATHS:
            if path.lower() in lowered:
                manifest.sensitive_paths.add(path)
        if BLOB_RE.search(text):
            manifest.notes.append("contains a long encoded blob")
            manifest.add_risk("obfuscation", "encoded_blob")

    def _finish(self, manifest: ScriptManifest) -> None:
        for host in list(manifest.hosts):
            try:
                ipaddress.ip_address(host)
                manifest.ips.add(host)
            except ValueError:
                pass
        hard_paths = [path for path in manifest.sensitive_paths if path not in {".env"}]
        if hard_paths:
            manifest.hard_signals.add("sensitive_path_access")
        if self.allowed_domains:
            outside = sorted(host for host in manifest.hosts | {ip.split(":")[0] for ip in manifest.ips} if not self._host_allowed(host))
            if outside:
                manifest.hard_signals.add("host_not_allowed")
                manifest.notes.append("contacts hosts outside the allowed domains: %s" % ", ".join(outside[:20]))
            if manifest.installs:
                manifest.notes.append("installs packages from public registries")

    def _host_allowed(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        return any(host == domain or host.endswith("." + domain) for domain in self.allowed_domains)

    def _resolve(self, value: str) -> Optional[Path]:
        candidate = Path(value)
        candidate = candidate if candidate.is_absolute() else self.workdir / candidate
        try:
            self._require_regular_code_path(candidate)
            resolved = candidate.resolve()
        except OSError:
            return None
        return resolved if resolved.is_file() and self._inside(resolved) else None

    @staticmethod
    def _require_regular_code_path(path: Path) -> None:
        if Path(os.path.abspath(path)) != path.resolve():
            raise ValueError("Script code paths must not use symlinks: %s" % path)

    def _inside(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root)
            return True
        except ValueError:
            return False

    def _display(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root))
        except ValueError:
            return str(path)

    def _read_bytes(self, path: Path) -> Optional[bytes]:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return None
            return path.read_bytes()
        except OSError:
            return None

    def _read_text(self, manifest: ScriptManifest, path: Path) -> Optional[str]:
        data = self._read_bytes(path)
        if data is None:
            manifest.hard_signals.add("code_not_inspectable")
            manifest.notes.append("%s is unreadable or larger than %s bytes" % (self._display(path), MAX_FILE_BYTES))
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            manifest.hard_signals.add("code_not_inspectable")
            manifest.notes.append("%s is not valid UTF-8" % self._display(path))
            return None

    def _record_file(self, manifest: ScriptManifest, path: Path, text: str) -> None:
        label = self._display(path)
        manifest.files[label] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        manifest.sources[label] = text
