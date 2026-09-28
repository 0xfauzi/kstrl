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
    _decide,
    _engineer,
    _env,
    _factory,
    _failed_run,
    _manifest_path,
    _open,
    _repo,
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
