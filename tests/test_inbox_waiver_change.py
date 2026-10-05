"""A policy_exception or test_adequacy item records the change the operator is shown (#646).

An item used to record the finding's text and nothing about the change it
was found on. Two attempts that wrote different code with the same finding
text collapsed onto one open item, and the repeat replaced its evidence, so
the operator approved an item describing a change they were never shown.
Each item now carries ``head_sha`` (the commit Phase 1 judged) and
``diff_sha`` (sha256 of the diff Phase 1 judged, against the component's own
base), both dedupe keys include ``diff_sha``, and ``ks inbox approve`` names
the commit.

Every test drives the real ``ks`` CLI in a subprocess, with the harness of
``tests/test_inbox_waivers.py``: a real git repository with a bare origin, a
shell engineer that commits, every verify command ``true``, review and
security skipped, and a stub ``gh``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from kstrl.inbox import Inbox, InboxConfig, ItemKind
from tests.helpers import gitrepo
from tests.test_inbox_waivers import (
    ADEQUACY_TOML,
    BRANCH,
    COMP,
    POLICY_TOML,
    _decide,
    _engineer,
    _env,
    _factory,
    _failed_run,
    _gated,
    _manifest_path,
    _open,
    _repo,
    _retry,
    _retry_regenerated,
    _verification_failures,
)

#: Ten added lines against a cap of five, with no other policy rule in play.
SIZE_TOML = "[policy]\nenabled = true\npaths_deny = []\nmax_lines_changed = 5\n"
TEN_LINES = _engineer("mkdir -p app && seq 1 10 > app/big.txt")
DEP = "a"
DEP_BRANCH = f"kstrl/factory/{DEP}"


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout


def _sha(root: Path, branch: str) -> str:
    return _git(root, "rev-parse", f"refs/heads/{branch}").decode("utf-8").strip()


def _diff_sha(root: Path, base: str, branch: str) -> str:
    return hashlib.sha256(_git(root, "diff", f"{base}...{branch}", "--")).hexdigest()


def test_an_item_records_the_commit_and_diff_it_was_found_on(tmp_path: Path) -> None:
    root, env = _failed_run(tmp_path, SIZE_TOML, TEN_LINES)
    (item,) = _open(root, ItemKind.POLICY_EXCEPTION)
    assert item.evidence["category"] == "policy_max_lines_changed", item.evidence
    head = _sha(root, BRANCH)

    assert item.evidence["head_sha"] == head
    assert item.evidence["diff_sha"] == _diff_sha(root, "main", BRANCH)
    out = _decide(root, env, "approve", item.id)
    assert f"(found on commit {head[:12]})." in out, out


#: Per waivable kind: its config, a change whose finding text is the same on
#: every attempt while its diff is not ($n is the attempt number), and the
#: categories that change files.
REPEATS = {
    ItemKind.POLICY_EXCEPTION: (
        SIZE_TOML,
        'mkdir -p app && seq 1 10 | sed "s/^/$n-/" > app/big.txt',
        {"policy_max_lines_changed"},
    ),
    ItemKind.TEST_ADEQUACY: (
        ADEQUACY_TOML,
        "printf 'def test_one():\\n    assert 1 + 1 == 2\\n' > tests/test_core.py && "
        'echo "$n" > note.txt',
        {"adequacy_test_deleted", "adequacy_assertion_removed"},
    ),
}


@pytest.mark.parametrize("kind", list(REPEATS), ids=str)
def test_a_repeat_on_a_different_change_opens_its_own_item(tmp_path: Path, kind: ItemKind) -> None:
    """Two attempts in one run write different code with the same finding text.
    Before #646 the second attempt refreshed the first item's evidence, so the
    item the operator approves described a change they were never shown."""
    toml, change, categories = REPEATS[kind]
    counter, heads = tmp_path / "counter", tmp_path / "heads"
    engineer = (
        f'n=$(( $(cat "{counter}" 2>/dev/null || echo 0) + 1 )); echo "$n" > "{counter}"; '
        f"{change} && git add -A && git commit -q -m work >/dev/null 2>&1; "
        f"git rev-parse HEAD >> \"{heads}\"; echo '<promise>COMPLETE</promise>'"
    )
    root = _repo(tmp_path, toml)
    env = _env(tmp_path, engineer)

    code, out = _factory(root, env, "--max-retries", "1")

    assert code == 1, out
    recorded = heads.read_text(encoding="utf-8").split()
    assert len(set(recorded)) == 2, recorded
    items = _open(root, kind)
    assert {i.evidence["category"] for i in items} == categories, out
    for category in categories:
        same = [i for i in items if i.evidence["category"] == category]
        assert len(same) == 2, [(i.evidence.get("head_sha"), i.occurrences) for i in same]
        assert len({i.evidence["explanation"] for i in same}) == 1, same
        assert sorted(i.evidence["head_sha"] for i in same) == sorted(recorded)
        assert len({i.evidence["diff_sha"] for i in same}) == 2, same


def test_a_dependent_items_diff_is_its_own_change(tmp_path: Path) -> None:
    """Under --no-prs a completed dependency's code reaches the dependent only
    through its branch, so the dependent is judged from the commit it started
    at. The item's diff_sha is of that diff, not of the diff against main."""
    root = _repo(tmp_path, SIZE_TOML)
    feature = root / "scripts" / "kstrl" / "feature"
    prd = json.loads((feature / COMP / "prd.json").read_text(encoding="utf-8"))
    (feature / DEP).mkdir()
    (feature / DEP / "prd.json").write_text(
        json.dumps({**prd, "branchName": DEP_BRANCH}), encoding="utf-8"
    )
    gitrepo.git_in(root, "add", "-A")
    gitrepo.git_in(root, "commit", "-q", "-m", "dependency prd")
    gitrepo.git_in(root, "push", "-q", "origin", "main")
    manifest = json.loads(_manifest_path(root).read_text(encoding="utf-8"))
    (comp,) = manifest["components"]
    dep = {**comp, "id": DEP, "title": DEP, "branchName": DEP_BRANCH}
    dep["prdPath"] = f"scripts/kstrl/feature/{DEP}/prd.json"
    manifest["components"] = [dep, {**comp, "dependencies": [DEP]}]
    _manifest_path(root).write_text(json.dumps(manifest), encoding="utf-8")
    env = _env(
        tmp_path,
        _engineer(
            'case "$(basename "$PWD")" in a) printf "a\\n" > a.txt;; '
            "*) mkdir -p app && seq 1 10 > app/big.txt;; esac"
        ),
    )

    code, out = _factory(root, env, "--no-prs")

    assert code == 1, out
    (item,) = _open(root, ItemKind.POLICY_EXCEPTION)
    assert item.component == comp["id"], item
    assert item.evidence["head_sha"] == _sha(root, BRANCH)
    # The fixture holds dependency code: the two diffs differ.
    assert _diff_sha(root, "main", BRANCH) != _diff_sha(root, DEP_BRANCH, BRANCH)
    assert item.evidence["diff_sha"] == _diff_sha(root, DEP_BRANCH, BRANCH)


def test_a_repeat_on_the_same_change_stays_one_item(tmp_path: Path) -> None:
    """Attempt 2 commits nothing new (an empty commit), so its head differs
    and its diff does not. The key is the diff, not the commit: one item,
    seen twice, whose evidence names the latest head."""
    heads = tmp_path / "heads"
    engineer = (
        "mkdir -p app && seq 1 10 > app/big.txt && git add -A && "
        "git commit -q --allow-empty -m work >/dev/null 2>&1; "
        f"git rev-parse HEAD >> \"{heads}\"; echo '<promise>COMPLETE</promise>'"
    )
    root = _repo(tmp_path, SIZE_TOML)
    env = _env(tmp_path, engineer)

    code, out = _factory(root, env, "--max-retries", "1")

    assert code == 1, out
    recorded = heads.read_text(encoding="utf-8").split()
    assert len(set(recorded)) == 2, recorded
    (item,) = _open(root, ItemKind.POLICY_EXCEPTION)
    assert item.occurrences == 2, item
    assert item.evidence["head_sha"] == recorded[-1] == _sha(root, BRANCH)
    assert item.evidence["diff_sha"] == _diff_sha(root, "main", BRANCH)


@pytest.mark.parametrize("head", [None, ""], ids=["absent", "empty"])
def test_approving_an_item_with_no_commit_says_so(tmp_path: Path, head: str | None) -> None:
    """An item filed before #646 has no head_sha, and one whose branch git
    could not read has "". Approve still waives it and names no commit."""
    root, env = _failed_run(tmp_path, SIZE_TOML, TEN_LINES)
    (item,) = _open(root, ItemKind.POLICY_EXCEPTION)
    evidence = {k: v for k, v in item.evidence.items() if k != "head_sha"}
    if head is not None:
        evidence["head_sha"] = head
    # A repeat refreshes an open item's evidence (tests/test_inbox_waivers.py does the same).
    Inbox(root, InboxConfig.load(root)).add(
        ItemKind.POLICY_EXCEPTION,
        item.title,
        component=COMP,
        dedupe_key=item.dedupe_key,
        evidence=evidence,
    )

    out = _decide(root, env, "approve", item.id)

    assert "waives this one finding" in out, out
    assert "the item records no commit" in out, out
    assert "found on commit" not in out, out


# --- slice 2: an approval covers only the change it was taken on -------------------

#: Two lines at a denied path: the change the operator approves.
TWO_LINES = _engineer("mkdir -p secrets && printf 'a\\nb\\n' > secrets/key.txt")
#: The regenerated change, per case. other_content: the same path with other
#: content. cr_joined_line and ff_joined_line: the two approved lines joined
#: into ONE line by a carriage return or a form feed (#698: a line with a
#: mid-line CR or FF is one line, so an approval for its fragments does not
#: cover it). external_diff_driver: other content while `diff.external` makes
#: `git diff` print nothing for every change. byte_not_utf_8: other content
#: holding a byte that is not utf-8.
REGENERATED = {
    "other_content": _engineer("mkdir -p secrets && printf 'z\\n' > secrets/key.txt"),
    "cr_joined_line": _engineer("mkdir -p secrets && printf 'a\\r+b\\n' > secrets/key.txt"),
    "ff_joined_line": _engineer("mkdir -p secrets && printf 'a\\f+b\\n' > secrets/key.txt"),
    "external_diff_driver": _engineer("mkdir -p secrets && printf 'z\\n' > secrets/key.txt"),
    "byte_not_utf_8": _engineer("mkdir -p secrets && printf 'z\\351\\n' > secrets/key.txt"),
}


@pytest.mark.parametrize("case", list(REGENERATED))
def test_an_approval_does_not_cover_a_regenerated_change(tmp_path: Path, case: str) -> None:
    """The finding names only the path, so its text is the same for any content
    there. Before slice 2 the approval matched on that text and the retry
    passed with content the operator never saw."""
    root = _repo(tmp_path, POLICY_TOML)
    if case == "external_diff_driver":
        gitrepo.git_in(root, "config", "diff.external", "true")
    env = _env(tmp_path, TWO_LINES)
    code, out = _factory(root, env)
    assert code == 1, out
    (item,) = _open(root, ItemKind.POLICY_EXCEPTION)
    _decide(root, env, "approve", item.id)

    code, out = _retry_regenerated(root, {**env, "AGENT_CMD": REGENERATED[case]})

    assert code == 1, out
    (finding,) = _gated(root, "policy_")
    assert (finding.category, finding.severity) == ("policy_paths_deny", "high")
    assert f"waiver_refused:{item.id}" in finding.tags
    assert any("a regenerated change is asked again" in f for f in _verification_failures(root))


def test_an_approval_filed_before_it_was_bound_to_its_change_is_refused(tmp_path: Path) -> None:
    """An item approved under #613 carries no diff_sha (owner decision 3a)."""
    root, env = _failed_run(tmp_path, POLICY_TOML, TWO_LINES)
    (item,) = _open(root, ItemKind.POLICY_EXCEPTION)
    # A repeat refreshes an open item's evidence (tests/test_inbox_waivers.py does the same).
    Inbox(root, InboxConfig.load(root)).add(
        ItemKind.POLICY_EXCEPTION,
        item.title,
        component=COMP,
        dedupe_key=item.dedupe_key,
        evidence={k: v for k, v in item.evidence.items() if k != "diff_sha"},
    )
    out = _decide(root, env, "approve", item.id)
    assert "records approval only" in out, out

    code, out = _retry(root, env)

    assert code == 1, out
    (finding,) = _gated(root, "policy_")
    assert (finding.severity, f"waiver_refused:{item.id}" in finding.tags) == ("high", True)
    assert any(
        "filed before an approval was bound to its change" in f
        for f in _verification_failures(root)
    )


def test_an_approval_does_not_cover_a_different_finding_on_the_same_change(
    tmp_path: Path,
) -> None:
    """The same diff can raise a finding that reads differently: the operator
    narrows ``paths_deny`` after approving, so the retry's finding names
    another pattern. The approval covers the finding it was shown, not every
    finding of its category on that change."""
    root, env = _failed_run(tmp_path, POLICY_TOML, TWO_LINES)
    (item,) = _open(root, ItemKind.POLICY_EXCEPTION)
    _decide(root, env, "approve", item.id)
    toml = root / "kstrl.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace('"secrets/**"', '"secrets/*"'), encoding="utf-8"
    )

    code, out = _retry(root, env)

    assert code == 1, out
    (finding,) = _gated(root, "policy_")
    assert (finding.severity, f"waiver_refused:{item.id}" in finding.tags) == ("high", True)
    assert "(deny 'secrets/*')" in finding.explanation, finding.explanation
    # The same change: the refusal below is the finding's, not the diff's.
    (refiled,) = _open(root, ItemKind.POLICY_EXCEPTION)
    assert refiled.evidence["diff_sha"] == item.evidence["diff_sha"], refiled.evidence
    assert any(
        "covers a different policy_paths_deny finding" in f for f in _verification_failures(root)
    )
