# Fact-necessary learning fixture

A two-slice project where slice 2 passes its hidden check only if it follows a
convention that slice 1's spec states and slice 2's spec does not. It exists so
that a factory run can measure whether a distilled fact changes an outcome,
not only whether its text reappears (#217, slice 4; issue #508).

## Layout

- `repo/`: the seed repository, `ledgerlite`, standard library only. A run
  copies it into a fresh directory. Its tests are kept out of kstrl's own
  collection by `conftest.py`.
- `slices/slice-1.md`: builds `src/ledgerlite/accounts.py` and states the
  project-wide rule that every error message ends with ` [LL-417]`.
- `slices/slice-2.md`: builds `src/ledgerlite/amounts.py`. It asks for error
  messages and does not state the suffix.
- `hidden_check.py`: calls slice 2's `parse_amount` with three inputs slice 2's
  spec names as invalid and passes only when every message ends with
  ` [LL-417]`. It is never copied into the target repository.

The scorer is `kstrl/learning_fixture.py`. It reports the hidden check's
verdict, whether a knowledge claim in the engineer's delivered prompt contains
`LL-417`, and the `knowledge_utilization` counts the run journaled. A reading
whose input was not given is `null`, never false or zero. Exit codes: 0 check
passed, 1 check failed, 2 an input could not be read or the check gave no
verdict. `tests/test_learning_fixture.py` runs it against hand-written
worktrees at no cost.

## Paid run P-A (not run by this PR)

P-A spends money and needs the owner's authorisation. Nothing below has been
run against a model. The steps that cost nothing (copy, `git init`,
`ks init`) were run on a scratch copy of the seed, and the prompt extraction
in step 5 was run on transcripts of earlier factory runs.

Run the whole procedure twice, each time in a fresh directory: once with
`$KSTRL` checked out before #217's slice 3 merges, and once after. One run per
arm is one sample and cannot separate an effect from noise.

```sh
KSTRL=/path/to/kstrl/checkout                  # the commit under test
FIX="$KSTRL/tests/learning_fixtures/fact_necessary"
PA="$(mktemp -d /tmp/kstrl-pa-XXXXXX)"
CAP_USD=...                                    # the cap the owner authorised
```

1. Build the target repository.

   ```sh
   cp -R "$FIX/repo/." "$PA/"
   cd "$PA"
   git init -q -b main && git add -A && git commit -qm "seed"
   uv run --project "$KSTRL" ks init --ui plain
   git add -A && git commit -qm "ks init"
   uv sync
   ```

2. Build slice 1 and merge its component branches into `main`.

   ```sh
   uv run --project "$KSTRL" ks factory --root "$PA" --spec "$FIX/slices/slice-1.md" \
     --project-name ledgerlite --no-prs --agent-type claude-code \
     --max-cost-usd "$CAP_USD" --ui plain --no-tui -y
   git -C "$PA" branch --list 'kstrl/factory/*'  # stop here if this lists nothing
   for b in $(git -C "$PA" branch --no-merged main --list 'kstrl/factory/*' --format='%(refname:short)'); do
     git -C "$PA" merge --no-edit "$b"
   done
   ls "$PA/.kstrl/knowledge"                     # slice 1's distilled facts
   ```

3. Mark the start of slice 2, so step 5 reads only slice 2's transcripts.

   ```sh
   touch "$PA.slice2-start"
   ```

4. Build slice 2 and merge its component branches the same way.

   ```sh
   uv run --project "$KSTRL" ks factory --root "$PA" --spec "$FIX/slices/slice-2.md" \
     --project-name ledgerlite --no-prs --agent-type claude-code \
     --max-cost-usd "$CAP_USD" --ui plain --no-tui -y
   for b in $(git -C "$PA" branch --no-merged main --list 'kstrl/factory/*' --format='%(refname:short)'); do
     git -C "$PA" merge --no-edit "$b"
   done
   ```

5. Extract the prompts slice 2's engineer was given. kstrl does not write the
   engineer's prompt to disk; it pipes it to `claude --print`, which keeps
   each session under `~/.claude/projects/<encoded working directory>/`. The
   first user message of an engineer session is the whole delivered prompt,
   knowledge prefix included; reviewer, security and distiller sessions share
   the directory and are skipped because they lack the engineer prompt's
   heading.

   ```sh
   python3 - "$PA" "$PA.slice2-start" > "$PA.slice2-prompts.txt" <<'EOF'
   import json, pathlib, sys
   name, since = pathlib.Path(sys.argv[1]).name, pathlib.Path(sys.argv[2]).stat().st_mtime
   for f in sorted(pathlib.Path.home().glob(f".claude/projects/*{name}*/*.jsonl")):
       if f.stat().st_mtime < since:
           continue
       for line in f.open(encoding="utf-8"):
           row = json.loads(line)
           content = row.get("message", {}).get("content") if row.get("type") == "user" else None
           if isinstance(content, str):
               if "# kstrl Agent Instructions" in content:
                   print(content)
               break
   EOF
   grep -c "# kstrl Agent Instructions" "$PA.slice2-prompts.txt"  # engineer sessions found
   ```

6. Score.

   ```sh
   uv run --project "$KSTRL" python -m kstrl.learning_fixture \
     --check "$FIX/hidden_check.py" --marker LL-417 --worktree "$PA" \
     --prompt "$PA.slice2-prompts.txt" --journal "$PA/.kstrl/evolution.jsonl"
   ```

## Reading the result

- `hidden_check.passed`: whether slice 2 followed the convention.
- `fact_in_prompt` and `claims_with_marker`: whether the fact reached slice 2's
  engineer, and in which tier. Before slice 3 the expected reading is `false`:
  slice 2's manifest names only its own components, so slice 1's facts are not
  retrieved (#453 D5).
- `utilization`: the counts each run journaled, by run id and component id.
  Slice 2's run id is the one whose component ids are not slice 1's.

The fixture has one known confound. Slice 1's code is in the tree, so slice 2's
engineer can learn the suffix by reading `accounts.py` without any fact. That
holds in both arms, which is why P-A compares the arms and reads
`fact_in_prompt` beside the verdict instead of reading the verdict alone.
