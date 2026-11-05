from __future__ import annotations

from pathlib import Path

import pytest

from copper_pilot_cli.shell_scope import classify_outside_workspace

HOME = Path("/home/maker")
WORKSPACE = HOME / "projects" / "board"
APP_HOME = HOME / ".copper-pilot"


def classify(command: str, *, workspace: Path = WORKSPACE, home: Path = HOME) -> str | None:
    return classify_outside_workspace(command, workspace, home, allowed=(APP_HOME,))


# Commands observed in long --yolo agent runs (2026-11-01/02), plus close variants.
@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("which kicad-cli || find / -name kicad-cli", "`find` searches /"),
        ('grep -rn "VCC_3V3" / 2>/dev/null', "`grep -r` searches /"),
        ('grep -rn "VCC_3V3" /home/maker', "`grep -r` searches ~"),
        ('grep -Rni "usb" ~', "`grep -r` searches ~"),
        ('grep --recursive "usb" $HOME', "`grep -r` searches ~"),
        ('find / -name "*.kicad_sch"', "`find` searches /"),
        ("find -L / -name kicad-cli", "`find` searches /"),
        ("find /usr -name kicad-cli", "`find` searches /usr"),
        ("find /home -name '*.kicad_pro'", "`find` searches /home"),
        ("cat /home/maker/other/power.kicad_sch", "touches ~/other/power.kicad_sch"),
        ("cp ~/other/lib.kicad_sym .", "touches ~/other/lib.kicad_sym"),
        ("head -50 ${HOME}/Downloads/notes.txt", "touches ~/Downloads/notes.txt"),
        ("ls ~", "touches ~"),
        ("echo hi > ~/notes.txt", "writes ~/notes.txt"),
        ("docker exec cp-backend grep -rn foo /", "`docker` reaches outside the workspace"),
        ("podman run --rm alpine ls", "`podman` reaches outside the workspace"),
        ("kubectl get pods", "`kubectl` reaches outside the workspace"),
        ("sudo apt-get install -y kicad", "`sudo` reaches outside the workspace"),
        ("ssh build-box ls", "`ssh` reaches outside the workspace"),
        ("du -a /", "`du` searches /"),
        ("du -sh ~", "`du` searches ~"),
        ("rg VCC ~", "`rg` searches ~"),
        ("fd kicad_sch $HOME", "`fd` searches ~"),
        ("locate kicad-cli", "`locate` searches the whole filesystem"),
        ("ls -R /", "`ls` searches /"),
        ("tree ~", "`tree` searches ~"),
        ("cd / && find . -name kicad-cli", "`find` searches /"),
        ("cd ~ && grep -r foo .", "touches ~"),
        ("grep -r foo ../..", "`grep -r` searches ~"),
        ("bash -lc 'find / -name kicad-cli'", "`find` searches /"),
        ("echo $(find ~ -name '*.kicad_sch')", "`find` searches ~"),
        ("timeout 30 find / -name kicad-cli", "`find` searches /"),
        ("FOO=1 env nice find / -name x", "`find` searches /"),
        ("printf x | xargs -I{} grep -rn {} /", "`grep -r` searches /"),
        ("ls\nfind / -name kicad-cli", "`find` searches /"),
    ],
)
def test_outside_workspace_commands_are_flagged(command: str, reason: str) -> None:
    assert classify(command) == reason


@pytest.mark.parametrize(
    "command",
    [
        "ls",
        "ls -la",
        "pwd",
        "git status",
        "grep -rn VCC .",
        "grep -rn VCC",
        "grep -rn /home/maker/old .",
        "grep VCC board.kicad_sch",
        "grep -c x / 2>/dev/null",
        "rg VCC",
        "rg VCC hardware/",
        "find . -name '*.kicad_sch'",
        "find hardware -name '*.kicad_pcb'",
        "fd kicad_sch",
        "du -sh .",
        "tree -L 2",
        "ls -R hardware",
        "kicad-cli sch erc board.kicad_sch",
        "kicad-cli sch export pdf -o /tmp/board.pdf board.kicad_sch",
        "kicad-cli pcb drc --output .copperpilot/drc.json board.kicad_pcb",
        "which kicad-cli",
        "command -v kicad-cli",
        "type kicad-cli",
        "python3 script.py",
        "python3 .copperpilot/scripts/place.py --board board.kicad_pcb",
        "cat .copperpilot/plans/a.plan.md",
        "cat /home/maker/projects/board/README.md",
        "cat ~/projects/board/README.md",
        "ls /usr/share/kicad/symbols",
        "find /usr/share/kicad/footprints -name '*.kicad_mod'",
        "cat ~/.local/share/kicad/9.0/sym-lib-table",
        "ls ~/.config/kicad/9.0",
        "ls ~/Documents/KiCad/9.0/3rdparty",
        "ls ~/.copper-pilot/logs",
        "ls /tmp/out",
        "cat /etc/os-release",
        "echo 'a && b' | wc -l",
        "awk '{print $(NF)}' nets.txt",
        "sed -n '/home/p' notes.txt",
        "curl -s https://example.com/datasheet.pdf -o datasheet.pdf",
        "make -C firmware",
        "",
    ],
)
def test_in_project_commands_are_not_flagged(command: str) -> None:
    assert classify(command) is None


def test_workspace_at_home_allows_searching_it() -> None:
    assert classify("grep -rn VCC ~", workspace=HOME) is None
    assert classify("find / -name x", workspace=HOME) == "`find` searches /"


def test_workspace_outside_home_still_guards_home() -> None:
    workspace = Path("/srv/work/board")
    assert classify("cat ~/other/x.kicad_sch", workspace=workspace) == (
        "touches ~/other/x.kicad_sch"
    )
    assert classify("cat /srv/work/board/x.kicad_sch", workspace=workspace) is None


def test_non_string_and_unbalanced_quotes() -> None:
    assert classify_outside_workspace(None, WORKSPACE, HOME) is None
    assert classify("grep -rn 'unterminated /") is not None


def test_temp_dir_allowed_unless_it_holds_home(monkeypatch) -> None:
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(HOME / "tmp"))
    assert classify("cat ~/tmp/out.txt") is None
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/home")
    assert classify("cat ~/other/x") == "touches ~/other/x"


_ADVICE = "for this user, not just the agent's; kill the PID of the process you started instead"


# A benchmark agent ran `killall -9 python3` to stop its own background job and
# killed the CLI and an unrelated harness with it (2026-11-04).
@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("killall -9 python3", f"`killall python3` kills every process named python3 {_ADVICE}"),
        ("killall python3", f"`killall python3` kills every process named python3 {_ADVICE}"),
        (
            "killall -s KILL python3 kicad",
            f"`killall python3 kicad` kills every process named python3 or kicad {_ADVICE}",
        ),
        (
            "killall -r 'python.*'",
            f"`killall -r 'python.*'` kills every process named 'python.*' {_ADVICE}",
        ),
        ("pkill python3", f"`pkill python3` kills every process named python3 {_ADVICE}"),
        ("pkill -n python3", f"`pkill python3` kills every process named python3 {_ADVICE}"),
        (
            'pkill -9 -f "python3 run.py"',
            "`pkill -f 'python3 run.py'` kills every process whose command line matches "
            f"'python3 run.py' {_ADVICE}",
        ),
        (
            "pkill --full server.py",
            "`pkill -f server.py` kills every process whose command line matches "
            f"server.py {_ADVICE}",
        ),
        ("pkill -u maker", f"`pkill` kills every matching process {_ADVICE}"),
        ("killall5 -9", f"`killall5` kills every process {_ADVICE}"),
        ("kill -9 -1", f"`kill -9 -1` kills every process {_ADVICE}"),
        ("kill -- -1", f"`kill -- -1` kills every process {_ADVICE}"),
        (
            "kill 0",
            "`kill 0` kills the whole process group, CopperPilot included; "
            "kill the PID of the process you started instead",
        ),
        (
            "kill $(pgrep python3)",
            f"`kill` fed by `pgrep` kills every process found by name {_ADVICE}",
        ),
        (
            "kill -9 `pidof kicad`",
            f"`kill` fed by `pidof` kills every process found by name {_ADVICE}",
        ),
        (
            "pgrep -f run.py | xargs kill -9",
            f"`kill` fed by `pgrep` kills every process found by name {_ADVICE}",
        ),
        (
            "ps aux | grep python | awk '{print $2}' | xargs kill",
            f"`kill` fed by `ps` kills every process found by name {_ADVICE}",
        ),
        (
            "pid=$(pgrep -f run.py); kill $pid",
            f"`kill` fed by `pgrep` kills every process found by name {_ADVICE}",
        ),
        (
            'for p in $(pgrep python3); do kill -9 "$p"; done',
            f"`kill` fed by `pgrep` kills every process found by name {_ADVICE}",
        ),
        (
            "sleep 1 && nohup killall python3",
            f"`killall python3` kills every process named python3 {_ADVICE}",
        ),
        ("ls; timeout 5 pkill x", f"`pkill x` kills every process named x {_ADVICE}"),
        ("(cd build && killall make)", f"`killall make` kills every process named make {_ADVICE}"),
        (
            "bash -c 'killall python3'",
            f"`killall python3` kills every process named python3 {_ADVICE}",
        ),
        ("echo $(killall x)", f"`killall x` kills every process named x {_ADVICE}"),
        (
            "/usr/bin/killall python3",
            f"`killall python3` kills every process named python3 {_ADVICE}",
        ),
        (
            "taskkill /IM python.exe /F",
            f"`taskkill /IM python.exe` kills every process named python.exe {_ADVICE}",
        ),
        (
            "taskkill.exe /f /im kicad.exe",
            f"`taskkill /im kicad.exe` kills every process named kicad.exe {_ADVICE}",
        ),
        (
            'taskkill /FI "IMAGENAME eq python.exe"',
            "`taskkill /FI IMAGENAME eq python.exe` kills every process matching "
            f"IMAGENAME eq python.exe {_ADVICE}",
        ),
        (
            'powershell -Command "Stop-Process -Name python"',
            f"`Stop-Process -Name python` kills every process named python {_ADVICE}",
        ),
    ],
)
def test_kill_by_name_is_flagged(command: str, reason: str) -> None:
    assert classify(command) == reason


@pytest.mark.parametrize(
    "command",
    [
        "kill 1234",
        "kill -9 1234",
        "kill -9 $PID",
        'kill -TERM "$(cat job.pid)"',
        "kill -s TERM 1234",
        "kill -1 1234",
        "kill %1",
        "kill -l",
        "kill $PID; sleep 1; pgrep -f run.py",
        "kill $PID && ps -p $PID",
        "pgrep -f run.py",
        "pidof kicad",
        "ps aux | grep python3",
        "killall -l",
        "pkill -F job.pid",
        "taskkill /F /PID 1234",
        "taskkill /PID 1234 /T",
        "Stop-Process -Id 1234",
        "python3 run.py & echo $! > job.pid",
        "echo killall",
    ],
)
def test_kill_by_pid_is_not_flagged(command: str) -> None:
    assert classify(command) is None
