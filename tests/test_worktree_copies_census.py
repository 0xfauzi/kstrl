"""Every place ``kstrl/`` copies or links a file, pinned (#569).

``_run_component`` used to copy ``prompt.md``, ``CLAUDE.md`` and
``AGENTS.md`` from the checkout kstrl ran from into each component
worktree. A copy there is an untracked file, the engineer's
``git add -A`` commits it, and when the same file is uncommitted in the
root checkout, ``git merge`` of the component branch refuses on it. The
behavioural layer is the census in ``tests/test_root_checkout_merge.py``,
which compares the files the branch commits with the files left untracked
in the root checkout and names none of them. It sees only the files its
scenario makes kstrl write.

This is the static layer. It counts every node in ``kstrl/`` that spells
the name of a call which duplicates a file, whether or not the path is
reachable in that scenario, so a new copy into a worktree shows up as a
row that changed. It enumerates no node types: ``spells`` asks every
field of every node, so an import alias, an attribute and a bare name
all count. It flags, so it may over-match; ``copy`` also counts the
``copy`` module that ``copy.deepcopy`` comes from, which is the whole of
the ``feature_cmd.py`` and ``retry_plan.py`` rows.
"""

from __future__ import annotations

import ast

import pytest

from tests.helpers import astwalk

#: Every name a stdlib call that duplicates a file is reached by:
#: ``shutil``'s copy family and ``move``, ``os.link`` / ``os.symlink``,
#: and ``pathlib.Path``'s link and copy methods.
FILE_DUPLICATING_NAMES = (
    "copy",
    "copy2",
    "copyfile",
    "copytree",
    "copyfileobj",
    "move",
    "symlink",
    "link",
    "symlink_to",
    "hardlink_to",
    "link_to",
    "copy_into",
)

#: One site in ``factory.py``: the component PRD seed in ``_run_component``,
#: which the branch commits on purpose. ``statedir.py`` moves a control
#: file across devices, ``init_cmd.py`` links AGENTS.md to CLAUDE.md in
#: the root checkout ``ks init`` runs in, and the other two rows are
#: ``copy.deepcopy``. None of them writes into a component worktree.
EXPECTED_FILE_DUPLICATING_SITES = {
    "factory.py": 1,
    "feature_cmd.py": 4,
    "init_cmd.py": 1,
    "retry_plan.py": 2,
    "statedir.py": 1,
}


def _duplicates_a_file(node: ast.AST) -> bool:
    return any(astwalk.spells(name)(node) for name in FILE_DUPLICATING_NAMES)


def _seen_in(source: str) -> bool:
    return any(_duplicates_a_file(node) for node in astwalk.all_nodes(astwalk.parse(source)))


def test_every_file_copy_or_link_in_kstrl_is_pinned() -> None:
    astwalk.assert_census(
        sources=astwalk.package_sources(),
        sees=_duplicates_a_file,
        expected=EXPECTED_FILE_DUPLICATING_SITES,
        # One control per name, so a name dropped from the tuple fails
        # here naming the control that went quiet.
        control=[f"x.{name}(a, b)\n" for name in FILE_DUPLICATING_NAMES],
        message=(
            "A file copy or link in kstrl/ was added or removed. A copy into "
            "a component worktree is an untracked file the engineer's commit "
            "tracks, and the merge into the root checkout then refuses on it "
            "(#569): read the file from root_dir instead. If the new site "
            "writes nothing into a worktree, update its row and say why in "
            "the comment above EXPECTED_FILE_DUPLICATING_SITES."
        ),
    )


@pytest.mark.parametrize(
    "source",
    [
        "import shutil as sh\nsh.copyfile(a, b)\n",
        "from shutil import copyfile as cp\ncp(a, b)\n",
        "dest.symlink_to('CLAUDE.md')\n",
        "os.link(a, b)\n",
    ],
)
def test_the_census_sees_aliased_and_method_spellings(source: str) -> None:
    assert _seen_in(source), source


@pytest.mark.xfail(strict=True, raises=AssertionError)
def test_a_file_written_from_another_files_bytes_is_not_seen() -> None:
    """Disclosed limit: a file produced by writing the bytes of another is
    not a copy call, so this layer does not see it. The behavioural census
    in ``tests/test_root_checkout_merge.py`` is the layer that does, for
    the paths its scenario reaches."""
    astwalk.blind_spot(_seen_in, "dest.write_bytes(src.read_bytes())\n")
