"""#700 slice 2: under a ``[stack]`` every verification command runs in a proven rung.

``ks factory`` proves both zones once per run, before the base gates, in
the policy its commands then run in: the setup in the setup zone (egress
open, writes confined), every check in the test zone (egress blocked,
localhost open). Below a proven rung it refuses with exit 2 before any
engineer runs; there is no opt-in to run a stack's commands on the host.

End to end: the real ``ks factory`` and ``ks doctor --measure`` as
subprocesses on a real git repository after the real ``ks init``, with a
stub engineer that logs each call (``tests/test_stack_e2e.py``). The
tests that need the real sandbox skip unless nono 0.79 or later runs on
macOS; the ones built on a fake ``nono`` need only macOS.

Where no prover exists for the platform (today Linux, gap G7) a run under
a stack runs its commands on the host, and every record of it carries one
label naming the platform (owner decision 2026-10-05). Those tests name
the platform through ``KSTRL_ISOLATION_PLATFORM``, the seam the platform
decision reads (:func:`kstrl.rung.host_fallback`), so they run on macOS
and on Linux alike; on Linux the seam names the real platform.

What a check does is observed on the host: a file it wrote or did not
write, and the row the base-gates record keeps for it.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.helpers.executables import write_executable
from tests.helpers.gitrepo import git_in
from tests.helpers.stack_confirmation import confirm_stack
from tests.test_inbox_waivers import FAKE_GH
from tests.test_isolation_rung import _fake_nono, needs_nono, on_macos
from tests.test_stack_e2e import (
    HEADLINE,
    PY,
    _commit,
    _factory,
    _measure,
    _record,
    _repo,
    _row,
    _spawn,
    _stack,
)

#: Exits 0 only when a connect to TEST-NET-1 by IP is refused with EPERM,
#: the rung's answer (DNS is open in the test zone, gap G1, so the probe
#: names no host), and a loopback bind and connect work.
NET_PROBE = """\
import errno, socket, sys
try:
    socket.create_connection(("192.0.2.1", 80), timeout=2).close()
    sys.exit(10)
except OSError as exc:
    if exc.errno != errno.EPERM:
        sys.exit(11)
server = socket.socket()
server.bind(("127.0.0.1", 0))
server.listen(1)
socket.create_connection(server.getsockname(), timeout=2).close()
"""

#: Exits 4 when it can write the file its argument names in $HOME (the
#: setup zone confines writes, so only a setup on the host can), and 3
#: when a connect to TEST-NET-1 is refused with EPERM: egress is blocked,
#: which the setup zone must not do. A timeout or an unreachable network is
#: egress left open, so it exits 0.
SETUP_PROBE = """\
import errno, os, socket, sys
try:
    with open(os.path.join(os.environ["HOME"], sys.argv[1]), "w") as handle:
        handle.write("x")
    sys.exit(4)
except OSError:
    pass
try:
    socket.create_connection(("192.0.2.1", 80), timeout=1).close()
except OSError as exc:
    sys.exit(3 if exc.errno == errno.EPERM else 0)
"""

REFUSAL = "Refusing to run: the isolation rung is not proven"

#: The platform the no-prover tests name, and the one label every record
#: of such a run must carry, written out so a change to it is seen here.
NO_PROVER = {"KSTRL_ISOLATION_PLATFORM": "linux"}
FALLBACK_LABEL = (
    "none: no isolation rung exists on linux, so every command ran on the host "
    "and nothing was isolated"
)
#: What the host fallback records for each stage: the platform and the
#: label, and nothing a rung proves (no zone, canaries, policy or refusal).
FALLBACK_RECORD = {"platform": "linux", "label": FALLBACK_LABEL}


def _isolation_record(root: Path) -> dict[str, Any]:
    (path,) = sorted((root / ".kstrl" / "runs").glob("*/isolation.json"))
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def _policy(zone: dict[str, Any]) -> dict[str, Any]:
    policy: dict[str, Any] = json.loads(Path(zone["policy_path"]).read_text(encoding="utf-8"))
    return policy


def _verification_isolation(root: Path) -> list[str]:
    (events,) = sorted((root / ".kstrl" / "runs").glob("*/events.jsonl"))
    return [
        line["data"]["isolation"]
        for line in map(json.loads, events.read_text(encoding="utf-8").splitlines())
        if line["event"] == "verification_result"
    ]


def _ignoring_nono(tmp_path: Path) -> dict[str, str]:
    """A `nono` first on PATH that answers --version and runs every
    `wrap -s -p <policy> -- <argv>` with no sandbox, so each canary escapes."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    write_executable(
        bin_dir / "nono",
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "nono 0.0.0-fake"; exit 0; fi\n'
        'shift 5\nexec "$@"\n',
    )
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "KSTRL_NONO": ""}


@needs_nono
def test_a_check_runs_in_the_test_zone_with_only_the_declared_paths(tmp_path: Path) -> None:
    """Each check is one thing a command in the test zone may or may not do.
    Writing $HOME, writing the checkout outside kstrl's worktree trees,
    reaching a non-loopback address by IP, and writing or reading a path
    the stack did not declare all fail; loopback, a declared
    writable path, a declared readable path and a declared variable that
    nono strips all work. The base refuses on the failing rows only.

    The two read targets sit under the home directory, removed afterwards:
    nono's default groups grant reads of all of /private, the system
    temporary directories that hold ``tmp_path`` included (measured), so
    a path there is readable declared or not."""
    token = secrets.token_hex(6)
    escape = Path.home() / f"kstrl-escape-{token}"
    home_reads = Path.home() / f".kstrl-reads-{token}"
    declared, undeclared = tmp_path / "declared-write", tmp_path / "undeclared-write"
    readable, unreadable = home_reads / "declared", home_reads / "undeclared"
    for directory in (declared, undeclared, readable, unreadable):
        directory.mkdir(parents=True)
    for directory in (readable, unreadable):
        (directory / "data.txt").write_text("data\n", encoding="utf-8")
    checks = {
        "escape": f'echo x > "$HOME/{escape.name}"',
        "checkout_write": f'echo x > "{tmp_path / "proj" / "kstrl-checkout-write"}"',
        "network": f"{PY} -I -S net_probe.py",
        "declared_write": f'echo x > "{declared}/made"',
        "undeclared_write": f'echo x > "{undeclared}/made"',
        "declared_read": f'cat "{readable}/data.txt"',
        "undeclared_read": f'cat "{unreadable}/data.txt"',
        "stripped_env": 'test "$CDPATH" = /kstrl-declared',
    }
    stack = _stack(
        checks,
        env=["CDPATH"],
        rung={"writable": [str(declared)], "readable": [f"~/{home_reads.name}/declared"]},
    )
    root = _repo(tmp_path, stack)
    _commit(root, "net_probe.py", NET_PROBE)

    try:
        run = _factory(tmp_path, root, env={"CDPATH": "/kstrl-declared"})
        escaped = escape.exists()
    finally:
        escape.unlink(missing_ok=True)
        shutil.rmtree(home_reads)

    assert run.code == 2, run.out
    assert run.calls == 0, run.out
    assert not escaped, "a check wrote the operator's home directory"
    record = _record(root)
    passed = {row["name"]: row["passed"] for row in record["checks"]}
    assert passed == {
        "stack:escape": False,
        "stack:checkout_write": False,
        "stack:network": True,
        "stack:declared_write": True,
        "stack:undeclared_write": False,
        "stack:declared_read": True,
        "stack:undeclared_read": False,
        "stack:stripped_env": True,
    }, run.out
    assert (declared / "made").exists()
    assert not (undeclared / "made").exists()
    assert not (root / "kstrl-checkout-write").exists()
    test_zone = _isolation_record(root)["test"]
    assert record["isolation"] == test_zone["label"]
    assert "test zone: writes confined, egress blocked" in record["isolation"], record


@needs_nono
def test_the_setup_runs_in_the_setup_zone_and_every_result_names_the_rung(
    tmp_path: Path,
) -> None:
    """The setup refuses itself when it can write $HOME (exit 4, so it did
    not run in a rung) or when egress is blocked (exit 3, so it ran in the
    test zone), and the check passes only when egress is blocked, so a run
    that reaches its engineer and passes Phase 3 ran each in its own zone:
    the setup in the base worktree, the component's and the contract
    worktree, the check on the base, in Phase 1 and in Phase 3. Both
    records name the test zone's rung, and the rungs' scratch is gone once
    the run ends."""
    escape = Path.home() / f"kstrl-escape-setup-{secrets.token_hex(6)}"
    stack = _stack(
        {"network": f"{PY} -I -S net_probe.py"},
        setup=f"{PY} -I -S setup_probe.py {escape.name}",
    )
    root = _repo(tmp_path, stack)
    _commit(root, "net_probe.py", NET_PROBE)
    _commit(root, "setup_probe.py", SETUP_PROBE)

    try:
        run = _factory(tmp_path, root, contract="final")
        escaped = escape.exists()
    finally:
        escape.unlink(missing_ok=True)

    assert not escaped, "a setup wrote the operator's home directory"
    assert REFUSAL not in run.out, run.out
    assert run.calls == 1, run.out
    assert "contract tests passed" in run.out, run.out
    record = _record(root)
    assert (record["refused"], record["setupError"]) == (False, ""), record
    rungs = _isolation_record(root)
    setup, test = rungs["setup"], rungs["test"]
    assert (setup["refusal"], test["refusal"]) == ("", ""), rungs
    assert setup["label"].endswith("setup zone: writes confined, egress open"), setup
    assert record["isolation"] == test["label"]
    assert _verification_isolation(root) == [test["label"]], run.out
    for zone in (setup, test):
        assert not Path(zone["scratch"]).exists(), zone
        assert not Path(zone["policy_path"]).is_relative_to(root), zone
    assert _policy(setup)["network"] == {"block": False}
    assert _policy(test)["network"] == {"block": True, "open_port": [0]}


#: git with no operator or system config: the zones do not grant ~/.gitconfig.
GIT = "GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 git"

#: What a command that fails in a proven zone says, written out so a change
#: to it is seen here (#700, the #625 trial).
SANDBOX_HINT = (
    "the {zone} zone of the isolation sandbox can be the cause: grant a path the "
    "command needs with `writable` or `readable` in the [stack] table of kstrl.toml"
)


@needs_nono
def test_git_runs_in_every_kstrl_worktree_in_both_zones_and_cannot_write_the_git_dir(
    tmp_path: Path,
) -> None:
    """A kstrl worktree's ``.git`` file points into the repository's git
    common directory, so ``git`` there must read it (#700, the #625 trial).
    The setup runs ``git`` in the setup zone and the checks in the test
    zone, in the base, component and contract worktrees, and each passes;
    a write into the git directory is refused in both zones.

    The repository sits under the home directory, removed afterwards:
    nono's default groups grant reads of all of /private, which holds
    ``tmp_path``, so a git directory there is readable with no grant."""
    token = secrets.token_hex(6)
    home = Path.home() / f".kstrl-gitdir-{token}"
    probe = f"kstrl-write-{token}"
    write = f'! touch "$({GIT} rev-parse --git-common-dir)/{probe}"'
    stack = _stack(
        {"git": f"{GIT} rev-parse HEAD && {GIT} status --porcelain", "git_dir_write": write},
        setup=f"{GIT} log -1 --format=%H && {write}",
    )
    try:
        root = _repo(home, stack)
        run = _factory(tmp_path, root, contract="final")
        written = (root / ".git" / probe).exists()
        record = _record(root)
    finally:
        shutil.rmtree(home, ignore_errors=True)

    assert "Refusing to run" not in run.out, run.out
    assert not written, "a command wrote the git directory"
    assert run.calls == 1, run.out
    assert "contract tests passed" in run.out, run.out
    assert record["setupError"] == "", record
    passed = {row["name"]: row["passed"] for row in record["checks"]}
    assert passed == {"stack:git": True, "stack:git_dir_write": True}, record


@needs_nono
def test_git_runs_in_the_replay_of_a_linked_worktree_root_and_of_a_root_named_from_elsewhere(
    tmp_path: Path,
) -> None:
    """The git directory both zones read is the root's common one, as an
    absolute path. `ks doctor --measure` replays a stack that runs ``git``
    for a root that is itself a linked worktree, whose own ``.git`` is a
    file, and for a root named by ``--root`` from a directory outside it,
    where a relative ``.git`` names a path that is not there. Each replay
    passes in both zones. Both roots sit under the home directory, for the
    reason the test above gives."""
    home = Path.home() / f".kstrl-gitroot-{secrets.token_hex(6)}"
    stack = _stack(
        {"git": f"{GIT} rev-parse HEAD && {GIT} status --porcelain"},
        setup=f"{GIT} log -1 --format=%H",
    )
    try:
        main = _repo(home / "main", stack)
        linked = home / "linked"
        git_in(main, "worktree", "add", "-q", "-b", "linked", str(linked))
        readings = {
            "a linked worktree": _spawn(
                ["doctor", "--root", str(linked), "--measure", "--json"], linked, None
            ),
            "a root named from elsewhere": _spawn(
                ["doctor", "--root", str(main), "--measure", "--json"], tmp_path, None
            ),
        }
    finally:
        shutil.rmtree(home, ignore_errors=True)

    for case, (_code, out) in readings.items():
        document = json.loads(out[out.index("{") :])
        replay = document["replay"]
        assert (replay["failed"], replay["error"]) == ("", ""), (case, replay)
        stages = [stage["name"] for stage in replay["stages"]]
        assert stages == ["setup", "check:git"], (case, replay)
        assert _row(document, "replay")["status"] == "ok", (case, document["checks"])


#: The refusal of both zones when git names a git common directory that
#: neither zone may read (#700, the security review of #765), written out
#: so a change to it is seen here.
GIT_DIR_REFUSAL = "refused: the git common directory {path} cannot be granted to read: it {why}"


def _move_git_dir(root: Path, to: Path) -> Path:
    """Move the git directory of ``root`` into ``to`` (made when absent)
    and leave the ``.git`` file that points git at it; return ``to``."""
    to.mkdir(parents=True, exist_ok=True)
    for entry in (root / ".git").iterdir():
        shutil.move(entry, to / entry.name)
    (root / ".git").rmdir()
    (root / ".git").write_text(f"gitdir: {to}\n", encoding="utf-8")
    return to


@needs_nono
def test_git_variables_in_the_environment_of_kstrl_do_not_move_the_git_dir_both_zones_read(
    tmp_path: Path,
) -> None:
    """`GIT_DIR` and `GIT_COMMON_DIR` in the environment of `ks doctor
    --measure` name the git directory of another repository. Both zones
    still read only the git directory of the root, and both are proven."""
    root = _repo(tmp_path / "a", "", confirm=False)
    link = tmp_path / "link"
    link.symlink_to(root)
    other = tmp_path / "other"
    other.mkdir()
    git_in(other, "init", "-q")
    redirect = {"GIT_DIR": str(other / ".git"), "GIT_COMMON_DIR": str(other / ".git")}

    readings = {"the root": _measure(root, redirect), "a symlink to it": _measure(link, redirect)}

    own, foreign = os.path.realpath(root / ".git"), os.path.realpath(other)
    for case, (_code, document) in readings.items():
        for zone in ("setup", "test"):
            rung = document["isolation"][zone]
            assert rung["refusal"] == "", (case, rung)
            read = _policy(rung)["filesystem"]["read"]
            assert own in read, (case, read)
            assert not [path for path in read if path.startswith(foreign)], (case, read)


@needs_nono
def test_a_git_dir_that_is_home_the_root_a_parent_of_it_or_not_a_git_dir_refuses_both_zones(
    tmp_path: Path,
) -> None:
    """The root's ``.git`` file names a git common directory that git
    accepts and that neither zone may read: the home directory (`HOME`
    names it through a symlink), the root, the parent of the root, a
    parent two levels up, a directory with no HEAD, and one with no
    objects/ (git finds the objects through `GIT_OBJECT_DIRECTORY`).
    `ks doctor --measure` refuses both zones before any canary runs and
    names the path, and `ks factory` refuses before the engineer runs."""
    top = tmp_path.resolve()
    roots = {
        case: _repo(
            top / case / "deep" if case == "grandparent" else top / case,
            _stack({"tests": "true"}) if case == "parent" else "",
            confirm=case == "parent",
        )
        for case in ("home", "root", "parent", "grandparent", "no HEAD", "no objects")
    }
    family, not_git = "is the root or a parent of the root", "does not hold HEAD and objects/"
    home = _move_git_dir(roots["home"], top / "home" / "h")
    home_link = top / "home" / "link"
    home_link.symlink_to(home)
    no_head = _move_git_dir(roots["no HEAD"], top / "no HEAD" / "common")
    linked = top / "no HEAD" / "wt"
    linked.mkdir()
    shutil.move(no_head / "HEAD", linked / "HEAD")
    (linked / "commondir").write_text(f"{no_head}\n", encoding="utf-8")
    (roots["no HEAD"] / ".git").write_text(f"gitdir: {linked}\n", encoding="utf-8")
    no_objects = _move_git_dir(roots["no objects"], top / "no objects" / "common")
    objects = shutil.move(no_objects / "objects", top / "no objects" / "objects")
    cases = {
        "home": (home, "is the home directory", {"HOME": str(home_link)}),
        "root": (_move_git_dir(roots["root"], roots["root"]), family, {}),
        "parent": (_move_git_dir(roots["parent"], top / "parent"), family, {}),
        "grandparent": (_move_git_dir(roots["grandparent"], top / "grandparent"), family, {}),
        "no HEAD": (no_head, not_git, {}),
        "no objects": (no_objects, not_git, {"GIT_OBJECT_DIRECTORY": str(objects)}),
    }
    refusals = {
        case: GIT_DIR_REFUSAL.format(path=path, why=why) for case, (path, why, _) in cases.items()
    }

    run = _factory(top / "parent", roots["parent"])
    readings = {case: _measure(roots[case], env) for case, (_, _, env) in cases.items()}

    assert (run.code, run.calls) == (2, 0), run.out
    assert REFUSAL in run.out, run.out
    assert f"the setup zone is {refusals['parent']}" in run.out, run.out
    for case, (_code, document) in readings.items():
        for zone in ("setup", "test"):
            rung = document["isolation"][zone]
            assert (rung["refusal"], rung["canaries"]) == (refusals[case], {}), (case, rung)
        assert f"setup zone: {refusals[case]}" in _row(document, "isolation")["detail"], case

    root_link = top / "root link"
    root_link.symlink_to(roots["root"])
    _code, through_link = _measure(root_link)
    for zone in ("setup", "test"):
        rung = through_link["isolation"][zone]
        assert (rung["refusal"], rung["canaries"]) == (refusals["root"], {}), ("a link", rung)


@needs_nono
def test_a_git_dir_that_is_not_the_roots_own_refuses_both_zones_and_a_linked_one_is_proven(
    tmp_path: Path,
) -> None:
    """The git directory both zones read must belong to the root by a link
    in both directions (#700, the security review of #770). A `.git` file
    naming the git directory of a second valid repository, and a linked
    worktree whose `worktrees/<name>/gitdir` names another path, refuse both
    zones and name the path. So do a `.git` directory and a worktree entry
    outside any git directory whose `commondir` names the second repository.
    A `.git` that is a symlink to a git directory or to a `.git` file, a
    linked worktree whose back link names it through a symlink, a relative
    `gitdir:` and a symlinked `worktrees` directory are proven: `ks`
    resolves the root, so the symlinks that reach the comparison are in the
    git directory, in the `.git` file, in its `gitdir:` and in the back link."""
    top = tmp_path.resolve()
    second = top / "second"
    second.mkdir()
    git_in(second, "init", "-q")
    stolen = _repo(top / "stolen", "", confirm=False)
    shutil.rmtree(stolen / ".git")
    (stolen / ".git").write_text(f"gitdir: {second / '.git'}\n", encoding="utf-8")

    main = _repo(top / "main", "", confirm=False)
    broken, good = top / "broken", top / "good"
    git_in(main, "worktree", "add", "-q", "-b", "broken", str(broken))
    git_in(main, "worktree", "add", "-q", "-b", "good", str(good))
    (main / ".git" / "worktrees" / "broken" / "gitdir").write_text(
        f"{second / '.git'}\n", encoding="utf-8"
    )
    alias = top / "alias"
    alias.symlink_to(good)
    (main / ".git" / "worktrees" / "good" / "gitdir").write_text(
        f"{alias / '.git'}\n", encoding="utf-8"
    )
    kept = _repo(top / "kept", "", confirm=False)
    store = top / "store"
    shutil.move(kept / ".git", store)
    (kept / ".git").symlink_to(store)
    # Git takes the common directory from a `commondir` file in a `.git`
    # directory, and from one in a worktree entry outside any git directory.
    pointed = _repo(top / "pointed", "", confirm=False)
    (pointed / ".git" / "commondir").write_text(f"{second / '.git'}\n", encoding="utf-8")
    planted = _repo(top / "planted", "", confirm=False)
    entry = top / "fake" / "worktrees" / "n"
    shutil.move(planted / ".git", top / "fake" / "old")
    entry.mkdir(parents=True)
    shutil.copy(top / "fake" / "old" / "HEAD", entry / "HEAD")
    (entry / "commondir").write_text(f"{second / '.git'}\n", encoding="utf-8")
    (entry / "gitdir").write_text(f"{planted / '.git'}\n", encoding="utf-8")
    (planted / ".git").write_text(f"gitdir: {entry}\n", encoding="utf-8")
    # Proven: a relative `gitdir:`, a `.git` symlink to the worktree's `.git`
    # file, and a `worktrees` directory that is a symlink.
    relative, linked, files = top / "relative", top / "linked", top / "files"
    git_in(main, "worktree", "add", "-q", "-b", "relative", str(relative))
    gitdir = os.path.relpath(main / ".git" / "worktrees" / "relative", relative)
    (relative / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    git_in(main, "worktree", "add", "-q", "-b", "linked", str(linked))
    files.mkdir()
    shutil.move(linked / ".git", files / "linked.git")
    (linked / ".git").symlink_to(files / "linked.git")
    (main / ".git" / "worktrees" / "linked" / "gitdir").write_text(
        f"{files / 'linked.git'}\n", encoding="utf-8"
    )
    moved, aside = _repo(top / "moved", "", confirm=False), top / "aside"
    git_in(moved, "worktree", "add", "-q", "-b", "aside", str(aside))
    shutil.move(moved / ".git" / "worktrees", top / "entries")
    (moved / ".git" / "worktrees").symlink_to(top / "entries")
    (top / "entries" / "aside" / "commondir").write_text(f"{moved / '.git'}\n", encoding="utf-8")
    not_own = "is not the git directory of the root"
    refused = {
        "a second repository": (stolen, second / ".git"),
        "a back link to another path": (broken, main / ".git"),
        "a .git directory with a commondir": (pointed, second / ".git"),
        "a worktree entry outside the git directory": (planted, second / ".git"),
    }

    readings = {case: _measure(root) for case, (root, _) in refused.items()}
    proven = {
        "a linked worktree": (good, main / ".git"),
        "a symlinked .git": (kept, store),
        "a relative gitdir": (relative, main / ".git"),
        "a .git symlink to a .git file": (linked, main / ".git"),
        "a symlinked worktrees directory": (aside, moved / ".git"),
    }
    accepted = {case: _measure(root) for case, (root, _) in proven.items()}

    for case, (_code, document) in readings.items():
        expected = GIT_DIR_REFUSAL.format(path=refused[case][1], why=not_own)
        for zone in ("setup", "test"):
            rung = document["isolation"][zone]
            assert (rung["refusal"], rung["canaries"]) == (expected, {}), (case, rung)
    for case, (_code, document) in accepted.items():
        for zone in ("setup", "test"):
            rung = document["isolation"][zone]
            assert rung["refusal"] == "", (case, rung)
            assert str(proven[case][1]) in _policy(rung)["filesystem"]["read"], (case, rung)


@needs_nono
def test_a_command_refused_a_path_in_a_proven_zone_says_the_sandbox_can_be_the_cause(
    tmp_path: Path,
) -> None:
    """A setup and a check read a file under the home directory that no
    grant covers, so each fails inside the rung with only the tool's own
    error. The replay of `ks doctor --measure` and the base refusal of
    `ks factory` each say that the zone the command ran in can be the
    cause and name the two [stack] keys. The replay keeps it apart from
    its detail, and the base-gates record does not carry it."""
    unreadable = Path.home() / f".kstrl-hint-{secrets.token_hex(6)}"
    unreadable.mkdir()
    (unreadable / "data.txt").write_text("data\n", encoding="utf-8")
    read = f'cat "{unreadable / "data.txt"}"'
    setup_root = _repo(tmp_path / "setup", _stack({"tests": "true"}, setup=read))
    check_root = _repo(tmp_path / "check", _stack({"read": read}))
    try:
        # The factory first: a failed replay leaves the stack unconfirmed.
        setup_run = _factory(tmp_path / "setup", setup_root)
        check_run = _factory(tmp_path / "check", check_root)
        setup_code, setup_doc = _measure(setup_root)
        check_code, check_doc = _measure(check_root)
    finally:
        shutil.rmtree(unreadable)

    setup_hint, test_hint = (SANDBOX_HINT.format(zone=zone) for zone in ("setup", "test"))
    for code, document, failed, hint in (
        (setup_code, setup_doc, "setup_failed:1", setup_hint),
        (check_code, check_doc, "base_contradiction", test_hint),
    ):
        replay = document["replay"]
        assert code == 1, document
        assert (replay["failed"], replay["sandbox"]) == (failed, hint), replay
        assert "sandbox" not in replay["detail"], replay
        assert _row(document, "replay")["detail"].startswith(
            f"{failed}: {replay['detail']}; {hint}"
        ), document["checks"]
    for run, root, hint, other in (
        (setup_run, setup_root, setup_hint, test_hint),
        (check_run, check_root, test_hint, setup_hint),
    ):
        assert (run.code, run.calls) == (2, 0), run.out
        assert HEADLINE in run.out, run.out
        assert f"  {hint}\n" in run.out, run.out
        assert other not in run.out, run.out
        assert hint not in json.dumps(_record(root)), run.out


def test_with_no_prover_a_failed_command_does_not_name_a_sandbox(tmp_path: Path) -> None:
    """On a platform with no prover no sandbox ran, so a check that fails
    there names none: not the replay, not its row, not the base refusal."""
    root = _repo(tmp_path, _stack({"tests": "exit 1"}))

    run = _factory(tmp_path, root, env=NO_PROVER)
    code, document = _measure(root, NO_PROVER)

    assert code == 1, document
    replay = document["replay"]
    assert (replay["failed"], replay["sandbox"]) == ("base_contradiction", ""), replay
    assert "isolation sandbox" not in _row(document, "replay")["detail"], document["checks"]
    assert (run.code, run.calls) == (2, 0), run.out
    assert HEADLINE in run.out, run.out
    assert "isolation sandbox" not in run.out, run.out


@on_macos
def test_a_rung_that_lets_a_canary_out_refuses_before_the_base_and_the_engineer(
    tmp_path: Path,
) -> None:
    """A nono that ignores its policy lets every canary out. The run exits 2
    naming the canary, before the base gates run and before any engineer,
    and the record it leaves says why."""
    root = _repo(tmp_path, _stack({"tests": "true"}))

    run = _factory(tmp_path, root, env=_ignoring_nono(tmp_path))

    assert run.code == 2, run.out
    assert REFUSAL in run.out, run.out
    assert "write_outside (escaped)" in run.out, run.out
    assert run.calls == 0, run.out
    assert sorted((root / ".kstrl" / "runs").glob("*/base-gates.json")) == [], run.out
    rungs = _isolation_record(root)
    assert "write_outside (escaped)" in rungs["test"]["refusal"], rungs
    assert not Path(rungs["test"]["scratch"]).exists(), rungs


@on_macos
def test_browser_rules_reach_the_test_zone_only_when_the_stack_declares_a_browser(
    tmp_path: Path,
) -> None:
    """Headless Chromium needs two raw Seatbelt allow rules (gap G3). They
    widen the boundary, so only a stack that says ``browser = true`` gets
    them, and only in the test zone. Read off the policy files the run
    recorded; the fake nono refuses the rung, which is after both are
    written."""
    plain = _repo(tmp_path / "plain", _stack({"tests": "true"}))
    browser = _repo(tmp_path / "browser", _stack({"tests": "true"}, rung={"browser": True}))

    _factory(tmp_path / "plain", plain, env=_ignoring_nono(tmp_path / "plain"))
    _factory(tmp_path / "browser", browser, env=_ignoring_nono(tmp_path / "browser"))

    without, with_browser = _isolation_record(plain), _isolation_record(browser)
    for zone in ("setup", "test"):
        assert "unsafe_macos_seatbelt_rules" not in _policy(without[zone]), without[zone]
    assert "unsafe_macos_seatbelt_rules" not in _policy(with_browser["setup"])
    assert _policy(with_browser["test"])["unsafe_macos_seatbelt_rules"] == [
        "(allow mach-register)",
        "(allow iokit-open)",
    ]


def test_with_no_prover_a_stack_run_runs_on_the_host_and_every_record_says_so(
    tmp_path: Path,
) -> None:
    """On a platform with no prover a run under a confirmed stack does not
    refuse: the base gates, Phase 1 and the PR all happen, and the check ran
    on the host, because it wrote a path no grant covers. The terminal, the
    isolation record, the base-gates record, the verification event and the
    PR body each carry the one fallback label, and the isolation record
    claims nothing a rung proves."""
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "ran-on-the-host"
    # The origin first: it names the control directory the confirmation goes in.
    root = _repo(tmp_path, _stack({"tests": f'echo x >> "{marker}"'}), confirm=False)
    origin = tmp_path / "origin.git"
    git_in(tmp_path, "init", "-q", "--bare", str(origin))
    git_in(root, "remote", "add", "origin", str(origin))
    confirm_stack(root)
    git_in(root, "push", "-q", "-u", "origin", "main")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    write_executable(bindir / "gh", FAKE_GH)
    gh_log = tmp_path / "gh.log"
    engineer = write_executable(
        tmp_path / "engineer.sh",
        "#!/bin/sh\ncat > /dev/null\necho hello > hello.txt\n"
        "git add -A && git commit -q -m hello >/dev/null 2>&1\n"
        "echo '<promise>COMPLETE</promise>'\n",
    )

    code, out = _spawn(
        [
            "factory",
            *("--manifest", str(root / "scripts" / "kstrl" / "manifest.json")),
            *("--root", str(root), "--agent-cmd", str(engineer)),
            *("--no-tui", "--yes", "--ui", "plain", "--no-color"),
            *("--max-retries", "0", "--max-parallel", "1"),
            *("--review-mode", "skip", "--security-mode", "skip"),
            *("--contract-check", "skip"),
        ],
        root,
        {
            **NO_PROVER,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "GH_LOG": str(gh_log),
            "GH_HEAD": str(tmp_path / "gh.head"),
        },
    )

    assert code == 0, out
    assert REFUSAL not in out, out
    # The base gates and Phase 1 each ran the check, on the host.
    assert marker.read_text(encoding="utf-8").splitlines() == ["x", "x"], out
    assert out.count(f"Isolation: {FALLBACK_LABEL}") == 1, out
    rungs = _isolation_record(root)
    assert (rungs["setup"], rungs["test"]) == (FALLBACK_RECORD, FALLBACK_RECORD), rungs
    record = _record(root)
    assert (record["refused"], record["isolation"]) == (False, FALLBACK_LABEL), record
    assert _verification_isolation(root) == [FALLBACK_LABEL], out
    assert f"## Isolation\n\n{FALLBACK_LABEL}\n" in gh_log.read_text(encoding="utf-8"), out


def test_with_no_prover_doctor_shows_the_label_runs_no_canary_and_replays_on_the_host(
    tmp_path: Path,
) -> None:
    """`ks doctor --measure` on a platform with no prover: the isolation row
    is the fallback label alone, a warning, with no canary run (the nono on
    PATH is never called), and the replay of the stack runs its check on
    the host under the same label."""
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "ran-on-the-host"
    root = _repo(tmp_path, _stack({"tests": f'echo x >> "{marker}"'}))
    env = {**_fake_nono(tmp_path, "exit 1\n"), "KSTRL_NONO": "", **NO_PROVER}

    code, document = _measure(root, env)

    assert code == 0, document
    row = _row(document, "isolation")
    assert (row["status"], row["detail"]) == ("warn", FALLBACK_LABEL), row
    assert document["isolation"] == {"setup": FALLBACK_RECORD, "test": FALLBACK_RECORD}
    assert not (tmp_path / "nono-calls.log").exists(), "a canary ran through nono"
    replay = document["replay"]
    assert _row(document, "replay")["status"] == "ok", document["checks"]
    assert replay["isolation"] == {"setup": FALLBACK_LABEL, "test": FALLBACK_LABEL}, replay
    # The base gates and the replay each ran the check, on the host.
    assert marker.read_text(encoding="utf-8").splitlines() == ["x", "x"], document


@pytest.mark.skipif(sys.platform == "darwin", reason="nono may run only on macOS")
def test_off_macos_naming_a_prover_platform_still_runs_no_nono(tmp_path: Path) -> None:
    """The seam decides only whether a run falls back. Naming macOS on
    another system reaches the prover, which reads the real platform and
    refuses: nono never runs unmeasured there (gap G7)."""
    root = _repo(tmp_path, _stack({"tests": "true"}))

    run = _factory(tmp_path, root, env={"KSTRL_ISOLATION_PLATFORM": "darwin"})

    assert run.code == 2, run.out
    assert REFUSAL in run.out, run.out
    assert f"no process rung implemented on {sys.platform}" in run.out, run.out
    assert run.calls == 0, run.out


def test_editing_writable_readable_or_browser_changes_the_stack_digest(tmp_path: Path) -> None:
    """The rung keys are part of what the stack is: each edit is a new
    digest, and an empty key is the same stack as an absent one."""
    tables = {
        "base": _stack({"tests": "true"}),
        "empty": _stack({"tests": "true"}, rung={"writable": [], "readable": []}),
        "writable": _stack({"tests": "true"}, rung={"writable": ["~/.cache/demo"]}),
        "readable": _stack({"tests": "true"}, rung={"readable": ["docs"]}),
        "browser": _stack({"tests": "true"}, rung={"browser": True}),
    }
    digests = {}
    for name, table in tables.items():
        root = _repo(tmp_path / name, table)
        _code, document = _measure(root)
        digests[name] = document["base_gates"]["stackDigest"]

    assert digests["empty"] == digests["base"], digests
    edited = [digests[name] for name in ("base", "writable", "readable", "browser")]
    assert len(set(edited)) == 4, digests
