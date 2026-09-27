# Attention

The operator is on the loop, not in it. Everything below intake runs faster than a person can usefully watch; everything above it changes slower than a person would check. The operator's own loop runs at minutes to hours, and drops into a faster loop only when that loop is stuck.

| loop | what the operator wants to see | what they may do | real signal |
|---|---|---|---|
| implement | is the agent alive; is the tree changing | nothing, unless stuck: read its output, stop the run | heartbeat every 15 s; the TUI calls an agent stale at 45 s without a heartbeat and 60 s without output. These are display thresholds only: nothing is killed when they pass. |
| accept | did the failing count fall this attempt | answer the checkpoint, retry | attempt n of 4; the failing gate's count per attempt |
| integrate | which parts are ready, running, landed; what it has cost; is main green | stop, retry a part, steer with guidance | 4 slots; merge poll 10 s; CI state per merge commit |
| intake | what runs next and why not yet; the day's spend; open PRs | add, pause, resume, reset a poisoned item | 60 s between cycles; the admission gates in order |
| trust, learn | the level and the evidence for the next one; what the factory now knows | promote, demote | 8 decisive runs; the placeholder thresholds |

Asks from fast loops are frequent and cheap to reverse (retry a part). Asks from slow loops are rare and hard to reverse (raise the autonomy level). They should not look the same.

## For design

- Most of the time the fast loops have nothing to say. Give them one line when healthy and room only when something needs a person.
- Liveness is an age, not a colour: `last event 8s ago`, `no output for 2m`. A stale label must not imply that kstrl acted on it; it did not.
