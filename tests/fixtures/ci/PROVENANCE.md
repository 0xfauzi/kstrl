# Provenance of the captured GitHub replies

Captured 2026-09-26 by lane 553 (verify-and-plan) with gh 2.73.0 against
0xfauzi/kstrl, one call per file, all exit 0:

    gh api graphql -F owner='{owner}' -F repo='{repo}' -f oid=<sha> -f query="$CI_QUERY"

where `$CI_QUERY` is `kstrl.ci_state.CI_QUERY`. Each file is gh's stdout
byte for byte plus the trailing newline end-of-file-fixer adds.

| file | commit | what GitHub reported |
|---|---|---|
| passed.json | b057de21b57bb8543db8bb0b2e1569e0a98e84e5 (main) | 7 check runs, all COMPLETED/SUCCESS |
| failed.json | 0aa55d980241dd702f6f56d661ea0fc8bad04a8f (a PR branch) | 5 check runs, `test` COMPLETED/FAILURE, `coverage` SKIPPED |
| running.json | 6cabef5547ce47529addcbde62e6c865e89d5aff (a PR branch) | 4 check runs, `test` IN_PROGRESS with conclusion null |
| no-checks.json | 76a0d8ad2a5107d00eb10c172591705638ba91ef (the root commit) | `statusCheckRollup: null` |
| no-commit.json | 0000000000000000000000000000000000000001 | `object: null` |
| not-a-commit.json | f4241a207d960272f35548ccb1db59fc8cfb381e (the tree of b057de2, not a commit) | `object: {}` |

Measured alongside, not stored as fixtures: a bad oid and a bad token
both exit 1 (`Variable $oid ... invalid value`, `Bad credentials (HTTP
401)`), and REST `repos/{owner}/{repo}/commits/<sha>/status` answers
`"state": "pending", "total_count": 0` for b057de2, a commit with no
commit statuses at all, which is why that endpoint is not read.

## paged.json (#570)

Captured 2026-09-26 by lane 570 (verify-and-plan) with gh 2.73.0 against
0xfauzi/kstrl, exit 0:

    gh api graphql --paginate --slurp -F owner='{owner}' -F repo='{repo}' -f oid=b057de21b57bb8543db8bb0b2e1569e0a98e84e5 -f query="$Q"

where `$Q` is `kstrl.ci_state.CI_QUERY` with `first: 100` replaced by
`first: 3`, so the seven checks of b057de2 come back as three pages
(3, 3, 1 nodes; `hasNextPage` true, true, false). It is gh's stdout byte
for byte plus the trailing newline end-of-file-fixer adds. It proves
that `--paginate` follows the nested `contexts` connection and that
`--slurp` prints the pages as one JSON array.

The six files above were captured before #570 with the one-page query,
which had no `$endCursor` and no `pageInfo`. They are single page
documents; `tests/test_ci_state.py::_answer` wraps each one in the array
`--slurp` prints. The parser reads neither `pageInfo` nor `endCursor`
(gh does), so the wrapped documents are what one page of the new query
looks like to it. They were kept rather than re-captured because
running.json cannot be: re-read on 2026-09-26 with the new query,
6cabef5 answers five COMPLETED check runs, `test` and `coverage`
CANCELLED, so no commit in the repository is still running.
