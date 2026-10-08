"""R10.7: what counts as a kstrl PR when the daemon bounds open PRs.

`count_open_kstrl_prs` turns one gh invocation into a number the daemon
gates on. What remains here is the end-to-end path through a real
subprocess (PATH lookup, process, stdout, decode, filter) and the
static guards on the footer marker the counter matches: the marker is
spelled once in ``kstrl/``, its literal is pinned, and both writers in
``kstrl/pr_body.py`` append it last so the ``endswith`` anchor holds.
`tests/test_flow_control.py` holds what the daemon does with the
answer, including the counter's refusal shapes through `serve_cycle`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kstrl.pr_body
from kstrl.pr_body import PR_FOOTER_MARKER
from kstrl.serve import count_open_kstrl_prs
from tests.helpers.astwalk import assert_census, folds_to, package_sources
from tests.helpers.fakegh import install_fake_gh as _install_fake_gh
from tests.helpers.fakegh import marked as _marked
from tests.helpers.fakegh import unmarked as _unmarked

# ---------------------------------------------------------------------------
# The counter
# ---------------------------------------------------------------------------


class TestCountOpenKstrlPrs:
    def test_counts_through_a_real_subprocess(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End to end: PATH lookup, process, stdout, decode, filter."""
        _install_fake_gh(tmp_path, monkeypatch, [_marked(1), _unmarked(2)])
        assert count_open_kstrl_prs(tmp_path).count == 1


# ---------------------------------------------------------------------------
# What "carries the marker" means
# ---------------------------------------------------------------------------


class TestMarkerIsAnchoredAtTheEnd:
    """The match is `endswith`, not `in`, and that is load-bearing.

    A substring match made any pull request that MENTIONS the footer
    count as one kstrl opened. Measured live against 0xfauzi/kstrl while
    this was under review: 8 open PRs, exactly one matched, and it was
    PR #354 itself, because its own Summary quoted the constant. The
    daemon can wedge itself on prose that way, with `max_open_prs = 0`
    as the only exit.
    """

    def test_the_other_writer_joins_immediately_after_the_footer(self) -> None:
        """`create_single_pr` pushes a branch, so it is read statically.

        Both marker appends in `kstrl/pr_body.py` must be the LAST line added
        before the join that produces the body. A source check rather
        than a behavioural one because the only way to reach that writer
        is a real `git push`.
        """
        lines = Path(kstrl.pr_body.__file__).read_text(encoding="utf-8").splitlines()
        followers = [
            next(follower for follower in lines[index + 1 :] if follower.strip())
            for index, line in enumerate(lines)
            if line.strip() == "lines.append(PR_FOOTER_MARKER)"
        ]
        assert len(followers) == 2, "expected two footer sites in kstrl/pr_body.py"
        assert all('"\\n".join(lines)' in follower for follower in followers), (
            "A kstrl PR body must END with PR_FOOTER_MARKER: the open-PR "
            "bound matches the footer with endswith, so anything appended "
            f"after it makes every PR kstrl opens uncountable. Saw: {followers}"
        )


# ---------------------------------------------------------------------------
# The marker constant
# ---------------------------------------------------------------------------


#: The marker is spelled ONCE in ``kstrl/``: the constant's own
#: definition. Anything else is a second spelling, which is the drift the
#: hoist exists to prevent - a reader in another module reaches for the
#: nearest spelling, and a footer reword then makes the bound count zero
#: while every test stays green.
EXPECTED_MARKER_SPELLINGS: dict[str, int] = {"pr_body.py": 1}


class TestFooterMarker:
    def test_the_marker_is_spelled_once_in_the_package(self) -> None:
        """Layer 1, the net: every expression in ``kstrl/`` that folds to
        the marker, counted per module, whatever it does with the string
        afterwards. Package-wide rather than scoped to ``pr_body.py``, because
        the modules that will grow a second spelling are the READERS -
        this bound, the baseline, the polled steering channel (#231) -
        and a guard that only reads ``pr_body.py`` cannot see them."""
        assert_census(
            sources=package_sources(),
            sees=folds_to(PR_FOOTER_MARKER),
            expected=EXPECTED_MARKER_SPELLINGS,
            control=f'footer = "{PR_FOOTER_MARKER}"\n',
            message=(
                "The set of places spelling the kstrl PR footer changed. A "
                "reader identifying a kstrl-authored PR must import "
                "PR_FOOTER_MARKER from kstrl.pr_body, not repeat the literal: the "
                "open-PR bound counts bodies containing it, so a second "
                "spelling that drifts makes the count silently zero."
            ),
        )

    def test_the_marker_literal_is_pinned(self) -> None:
        """Layer 3, the value. The census folds AGAINST the constant, so
        it moves with any reword and stays green. A measured mutation
        changing the URL to `kstrl-loop` left all 308 tests in the three
        serve suites green.

        Rewording this line silently un-counts every pull request open at
        the moment of the reword - the same class as the ralph rename,
        which already did it once. Changing the constant is allowed;
        changing it without reading this is not."""
        assert PR_FOOTER_MARKER == "Generated by [kstrl](https://github.com/0xfauzi/kstrl)"

    def test_both_footer_sites_use_the_constant(self) -> None:
        """Layer 2, the message: ``pr_body.py``'s two writers still go through
        the constant. The census above cannot say this - deleting a
        footer site leaves the spelling count at 1 - and "you wrote the
        footer without the constant" is the wrong message for it."""
        source = Path(kstrl.pr_body.__file__).read_text(encoding="utf-8")
        assert source.count("lines.append(PR_FOOTER_MARKER)") == 2
