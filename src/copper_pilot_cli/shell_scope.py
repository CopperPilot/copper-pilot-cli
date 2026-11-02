"""Classify agent shell commands that reach outside the workspace.

The hosted agent works on one project. Commands that search the whole disk or
the home directory, read other projects, or step into containers and remote
hosts need the user's explicit approval in every approval mode. This module is
the pure classifier; ``LocalToolBroker`` enforces the policy.

The classifier is a heuristic over shell words, not a sandbox. It errs toward
leaving ordinary in-project commands alone: relative paths, ``.copperpilot/``,
``kicad-cli sch erc board.kicad_sch``, ``which kicad-cli`` and ``python3
script.py`` never trip it.
"""

from __future__ import annotations

import os
import re
import shlex
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path

# Executables that reach outside the workspace by design.
_ESCAPE_EXECUTABLES = frozenset(
    {
        "docker",
        "docker-compose",
        "doas",
        "kubectl",
        "nerdctl",
        "podman",
        "scp",
        "sftp",
        "ssh",
        "su",
        "sudo",
    }
)
# Whole-filesystem index searches; no root argument to check.
_INDEX_SEARCHES = frozenset({"locate", "mdfind", "mlocate", "plocate"})
# Searches that recurse by default. Their first positional is the pattern.
_PATTERN_SEARCHES = frozenset({"ack", "ag", "rg"})
_PATH_SEARCHES = frozenset({"fd", "fdfind", "find", "tree", "du"})
_GREPS = frozenset({"egrep", "fgrep", "grep", "zgrep"})
_SHELLS = frozenset({"bash", "dash", "fish", "ksh", "sh", "zsh"})
# Prefix commands that run the rest of the line; value-taking options listed.
_WRAPPERS: dict[str, frozenset[str]] = {
    "builtin": frozenset(),
    "caffeinate": frozenset(),
    "command": frozenset(),
    "env": frozenset({"-u", "-C", "-S", "--unset", "--chdir"}),
    "exec": frozenset({"-a"}),
    "ionice": frozenset({"-c", "-n", "-p"}),
    "nice": frozenset({"-n"}),
    "nohup": frozenset(),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "time": frozenset({"-f", "-o"}),
    "timeout": frozenset({"-k", "-s", "--kill-after", "--signal"}),
    "watch": frozenset({"-n", "-d", "--interval"}),
    "xargs": frozenset({"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s"}),
}
# System top-level directories; a recursive search rooted at one scans the disk.
_SYSTEM_TOP_LEVEL = frozenset(
    {
        "Applications",
        "Library",
        "System",
        "Users",
        "Volumes",
        "bin",
        "boot",
        "dev",
        "etc",
        "home",
        "lib",
        "lib64",
        "media",
        "mnt",
        "nix",
        "opt",
        "private",
        "proc",
        "root",
        "run",
        "sbin",
        "snap",
        "srv",
        "sys",
        "usr",
        "var",
    }
)
# KiCad install, library, and settings locations under the home directory.
_KICAD_HOME_DIRS = (
    ".cache/kicad",
    ".config/kicad",
    ".local/share/kicad",
    ".var/app/org.kicad.KiCad",
    "AppData/Local/kicad",
    "AppData/Roaming/kicad",
    "Documents/KiCad",
    "Library/Application Support/kicad",
    "Library/Caches/kicad",
    "Library/Preferences/kicad",
    "snap/kicad",
)
_SEPARATOR_CHARS = frozenset("&|;()")
_REDIRECT_RE = re.compile(r"^[<>&|]*[<>][<>&|]*$")
_HOME_VAR_RE = re.compile(r"^(?:\$HOME|\$\{HOME\})(?=/|$)")
_SUBSTITUTION_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
_MAX_DEPTH = 4


def _display(path: Path, home: Path) -> str:
    try:
        relative = path.relative_to(home)
    except ValueError:
        return str(path)
    return "~" if not relative.parts else f"~/{relative.as_posix()}"


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _forms(path: Path) -> tuple[Path, ...]:
    """The lexical and the symlink-resolved form of an absolute path."""
    lexical = Path(os.path.normpath(path))
    try:
        resolved = lexical.resolve(strict=False)
    except (OSError, RuntimeError):
        return (lexical,)
    return (lexical,) if resolved == lexical else (lexical, resolved)


class _Scope:
    def __init__(self, workspace: Path, home: Path, allowed: Iterable[Path]) -> None:
        self.home = Path(os.path.normpath(home.expanduser()))
        self.workspaces = _forms(workspace.expanduser().absolute())
        self.private = _forms(self.home)
        # Searching the directory that holds every home (/home, /Users) is as
        # broad as searching ours; never treat "/" as that directory.
        parent = self.home.parent
        self.homes = _forms(parent) if len(parent.parts) > 1 else ()
        # TMPDIR may sit under the home directory (macOS keeps it elsewhere).
        temp_dirs = [Path(tempfile.gettempdir())]
        self.allowed = tuple(
            form
            for root in (
                *(self.home / relative for relative in _KICAD_HOME_DIRS),
                *temp_dirs,
                *(Path(p).expanduser() for p in allowed),
            )
            for form in _forms(root.absolute())
        )

    def resolve(self, token: str, cwd: Path) -> Path | None:
        if not token or "\0" in token:
            return None
        expanded = _HOME_VAR_RE.sub(str(self.home).replace("\\", "\\\\"), token)
        if expanded == "~" or expanded.startswith("~/"):
            expanded = str(self.home) + expanded[1:]
        elif expanded.startswith("~"):
            return None  # ~user: unknown, left alone
        if "$" in expanded.split("/", 1)[0]:
            return None  # leading variable we cannot expand
        path = Path(expanded)
        if not path.is_absolute():
            path = cwd / path
        return Path(os.path.normpath(path))

    def inside_workspace(self, path: Path) -> bool:
        forms = _forms(path)
        return any(_within(form, root) for form in forms for root in self.workspaces)

    def private_outside(self, path: Path) -> bool:
        """Under a home directory, outside the workspace and the allowlist."""
        if self.inside_workspace(path):
            return False
        forms = _forms(path)
        if any(_within(form, root) for form in forms for root in self.allowed):
            return False
        return any(_within(form, root) for form in forms for root in self.private)

    def broad_root(self, path: Path) -> bool:
        """The filesystem root, a system top-level dir, or a home dir or its parent."""
        if self.inside_workspace(path):
            return False
        for form in _forms(path):
            if form == Path(form.anchor):
                return True
            if len(form.parts) == 2 and form.parts[1] in _SYSTEM_TOP_LEVEL:
                return True
            if form in self.homes or form in self.private:
                return True
            if any(_within(root, form) for root in self.private):
                return True
        return False


def _tokens(command: str) -> list[str]:
    text = command.replace("`", " ; ").replace("\r", "\n").replace("\n", " ; ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return text.split()


def _simple_commands(tokens: Sequence[str]) -> list[tuple[list[str], list[str]]]:
    """Split shell words into ``(argv, redirect_targets)`` per simple command."""
    commands: list[tuple[list[str], list[str]]] = []
    argv: list[str] = []
    redirects: list[str] = []
    expect_target = False
    for token in tokens:
        if token and set(token) <= _SEPARATOR_CHARS | {"<", ">"} and set(token) & {"(", ")"}:
            # "(", ")", "$(", "<(": substitution or subshell boundaries.
            commands.append((argv, redirects))
            argv, redirects, expect_target = [], [], False
        elif token and set(token) <= _SEPARATOR_CHARS:
            commands.append((argv, redirects))
            argv, redirects, expect_target = [], [], False
        elif _REDIRECT_RE.match(token):
            expect_target = True
        elif expect_target:
            redirects.append(token)
            expect_target = False
        elif token in {"$"}:
            continue
        else:
            argv.append(token)
    commands.append((argv, redirects))
    return [(a, r) for a, r in commands if a or r]


def _strip_wrappers(argv: list[str]) -> list[str]:
    index = 0
    while index < len(argv):
        word = argv[index]
        if "=" in word and not word.startswith("-") and word.split("=", 1)[0].isidentifier():
            index += 1  # VAR=value prefix
            continue
        name = Path(word).name
        value_options = _WRAPPERS.get(name)
        if value_options is None:
            break
        if name == "command" and any(arg in {"-v", "-V"} for arg in argv[index + 1 : index + 3]):
            return []  # `command -v tool` only looks the tool up
        index += 1
        while index < len(argv) and argv[index].startswith("-") and argv[index] != "-":
            option = argv[index]
            index += 2 if option in value_options else 1
        if name == "timeout" and index < len(argv):
            index += 1  # the duration
        if name == "env":
            while index < len(argv) and "=" in argv[index]:
                index += 1
    return argv[index:]


def _shell_script(args: Sequence[str]) -> str | None:
    for position, arg in enumerate(args):
        if arg.startswith("-") and not arg.startswith("--") and arg.endswith("c"):
            return args[position + 1] if position + 1 < len(args) else None
        if not arg.startswith("-"):
            return None
    return None


def _recursive_grep(args: Sequence[str]) -> bool:
    for position, arg in enumerate(args):
        if arg in {"--recursive", "--dereference-recursive", "--directories=recurse"}:
            return True
        if arg in {"-d", "--directories"} and args[position + 1 : position + 2] == ["recurse"]:
            return True
        if arg.startswith("-") and not arg.startswith("--") and set(arg[1:]) & {"r", "R"}:
            # A short cluster such as -rn or -Rli; stop at a value-taking letter.
            letters = arg[1:]
            for letter in letters:
                if letter in {"r", "R"}:
                    return True
                if letter in {"e", "f", "m", "A", "B", "C", "d", "D"}:
                    break
    return False


def _positionals(args: Sequence[str]) -> list[str]:
    values: list[str] = []
    options_done = False
    for arg in args:
        if not options_done and arg == "--":
            options_done = True
        elif options_done or not arg.startswith("-") or arg == "-":
            values.append(arg)
    return values


def _find_roots(args: Sequence[str]) -> list[str]:
    roots: list[str] = []
    for arg in args:
        if arg in {"-H", "-L", "-P"} or arg.startswith("-O") or arg.startswith("-D"):
            continue
        if arg.startswith("-") or arg in {"(", "!", ","}:
            break
        roots.append(arg)
    return roots


def _search_roots(name: str, args: list[str]) -> list[str] | None:
    """Root paths a recursive search walks, or None when it does not recurse."""
    if name == "find":
        return _find_roots(args) or ["."]
    if name in {"fd", "fdfind"}:
        explicit = [
            args[i + 1]
            for i, arg in enumerate(args[:-1])
            if arg in {"--search-path", "--base-directory"}
        ]
        return explicit + _positionals(args)[1:] or ["."]
    if name in {"tree", "du"}:
        return _positionals(args) or ["."]
    if name in _PATTERN_SEARCHES:
        return _positionals(args)[1:] or ["."]
    if name in _GREPS:
        if not _recursive_grep(args):
            return None
        positionals = _positionals(args)
        explicit_pattern = any(
            arg in {"-e", "-f"} or arg.startswith(("--regexp", "--file")) for arg in args
        )
        return (positionals if explicit_pattern else positionals[1:]) or ["."]
    if name == "ls":
        recursive = any(
            arg == "--recursive"
            or (arg.startswith("-") and not arg.startswith("--") and "R" in arg)
            for arg in args
        )
        return (_positionals(args) or ["."]) if recursive else None
    return None


def _path_candidates(name: str, args: list[str]) -> list[str]:
    """Words that may name a file. Grep-style patterns are skipped."""
    candidates: list[str] = []
    skip_pattern = name in _GREPS | _PATTERN_SEARCHES and not any(
        arg in {"-e", "-f"} for arg in args
    )
    for arg in args:
        if arg.startswith("-"):
            if "=" in arg:
                candidates.append(arg.split("=", 1)[1])
            continue
        if skip_pattern:
            skip_pattern = False
            continue
        candidates.append(arg)
    return candidates


def _classify(command: str, scope: _Scope, depth: int) -> str | None:
    if depth > _MAX_DEPTH or not command.strip():
        return None
    for match in _SUBSTITUTION_RE.finditer(command):
        inner = match.group(1) if match.group(1) is not None else match.group(2)
        reason = _classify(inner, scope, depth + 1)
        if reason:
            return reason
    cwd = scope.workspaces[0]
    for raw_argv, redirects in _simple_commands(_tokens(command)):
        for target in redirects:
            path = scope.resolve(target, cwd)
            if path is not None and scope.private_outside(path):
                return f"writes {_display(path, scope.home)}"
        argv = _strip_wrappers(raw_argv)
        if not argv:
            continue
        name = Path(argv[0]).name.lower()
        args = argv[1:]
        if name in _ESCAPE_EXECUTABLES:
            return f"`{name}` reaches outside the workspace"
        if name in _INDEX_SEARCHES:
            return f"`{name}` searches the whole filesystem"
        if name in _SHELLS:
            script = _shell_script(args)
            if script:
                reason = _classify(script, scope, depth + 1)
                if reason:
                    return reason
            continue
        if name == "eval":
            reason = _classify(" ".join(args), scope, depth + 1)
            if reason:
                return reason
            continue
        roots = _search_roots(name, args)
        for root in roots or ():
            path = scope.resolve(root, cwd)
            if path is not None and scope.broad_root(path):
                label = f"{name} -r" if name in _GREPS else name
                return f"`{label}` searches {_display(path, scope.home)}"
        for candidate in _path_candidates(name, args):
            path = scope.resolve(candidate, cwd)
            if path is not None and scope.private_outside(path):
                return f"touches {_display(path, scope.home)}"
        if name in {"cd", "pushd"} and args:
            target = scope.resolve(_positionals(args)[0] if _positionals(args) else args[-1], cwd)
            if target is not None:
                cwd = target
    return None


def classify_outside_workspace(
    command: object,
    workspace_root: Path,
    home: Path,
    *,
    allowed: Iterable[Path] = (),
) -> str | None:
    """A short reason when ``command`` reaches outside the workspace, else None.

    Flags recursive searches rooted at ``/``, a system top-level directory, or
    a home directory; paths under a home directory outside ``workspace_root``
    (KiCad locations, temp dirs and ``allowed`` excepted); filesystem index
    searches; and ``docker``/``podman``/``kubectl``/``sudo``/``ssh``-style
    commands.
    """
    if not isinstance(command, str) or not command.strip():
        return None
    return _classify(command, _Scope(workspace_root, home, allowed), 0)


__all__ = ["classify_outside_workspace"]
