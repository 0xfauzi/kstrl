"""Measurement harness for the TUI spike.

Subcommands:

  latency   - headless tailer latency/CPU matrix: for each (poll, rate)
              cell, spawn fake_run.py at that rate, tail events.jsonl +
              engineer.jsonl with JsonlTailer at that poll interval, and
              report p50/p95/max of (t_seen - t_emit) plus tailer CPU
              (getrusage deltas) and records seen vs written.
  monitor   - sample %cpu/rss of a pid via ps until it exits (for the
              Textual app measured externally).
  pty-app   - run app.py inside a pseudo-terminal, drive it with a fake
              run, then deliver SIGINT/SIGTERM/crash and verify the
              terminal restore sequences appear in the captured output.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

SPIKE_DIR = Path(__file__).parent


def _percentiles(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {"n": 0, "p50": -1.0, "p95": -1.0, "max": -1.0}
    xs = sorted(xs)
    return {
        "n": len(xs),
        "p50": round(statistics.median(xs) * 1000, 1),
        "p95": round(xs[min(len(xs) - 1, int(0.95 * len(xs)))] * 1000, 1),
        "max": round(xs[-1] * 1000, 1),
    }


def run_latency_cell(poll: float, rate: float, duration: float, out_root: Path,
                     components: int = 4) -> dict[str, object]:
    sys.path.insert(0, str(SPIKE_DIR))
    from tailer import RunTailer  # noqa: E402

    cell_dir = out_root / f"cell-p{poll}-r{rate}"
    cell_dir.mkdir(parents=True, exist_ok=True)
    gen_cmd = [sys.executable, str(SPIKE_DIR / "fake_run.py"), "--out", str(cell_dir),
               "--components", str(components), "--rate", str(rate),
               "--duration", str(duration), "--torn-tail-every", "50"]
    if rate > 1:
        gen_cmd.append("--churn")  # sustained storm: components never finish
    gen = subprocess.Popen(gen_cmd, stdout=subprocess.PIPE, text=True)
    runs_root = cell_dir / ".ralph" / "runs"
    deadline = time.monotonic() + 10
    run_dir: Path | None = None
    while time.monotonic() < deadline and run_dir is None:
        if runs_root.is_dir():
            candidates = sorted(runs_root.iterdir())
            if candidates:
                run_dir = candidates[-1]
                break
        time.sleep(0.05)
    if run_dir is None:
        gen.kill()
        raise RuntimeError("generator never created a run dir")

    tailer = RunTailer(run_dir)
    latencies: list[float] = []
    seen = 0
    ru0 = resource.getrusage(resource.RUSAGE_SELF)
    wall0 = time.monotonic()
    polls = 0
    while True:
        records = tailer.poll_events()
        now = time.time()
        polls += 1
        for rec in records:
            seen += 1
            t_emit = rec.get("t_emit")
            if isinstance(t_emit, (int, float)):
                latencies.append(now - t_emit)
        if gen.poll() is not None:
            # generator finished: drain twice more, then stop
            for _ in range(2):
                time.sleep(poll)
                for rec in tailer.poll_events():
                    seen += 1
                    t_emit = rec.get("t_emit")
                    if isinstance(t_emit, (int, float)):
                        latencies.append(time.time() - t_emit)
            break
        time.sleep(poll)
    ru1 = resource.getrusage(resource.RUSAGE_SELF)
    wall = time.monotonic() - wall0
    cpu = (ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime)
    written = 0
    gen_out = (gen.stdout.read() if gen.stdout else "") or ""
    for part in gen_out.split():
        if part.startswith("events="):
            written = int(part.split("=")[1])
    # Latency of records that existed before the first poll is not
    # meaningful for steady-state; keep them, they only appear in max.
    return {
        "poll_s": poll, "rate": rate, "duration_s": duration,
        "records_written": written, "records_seen": seen,
        "latency_ms": _percentiles(latencies),
        "tailer_cpu_s": round(cpu, 3), "wall_s": round(wall, 1),
        "tailer_cpu_pct": round(100 * cpu / wall, 2), "polls": polls,
    }


def cmd_latency(args: argparse.Namespace) -> None:
    out_root = Path(args.out)
    results = []
    polls = [float(x) for x in args.polls.split(",")]
    rates = [float(x) for x in args.rates.split(",")]
    for rate in rates:
        for poll in polls:
            r = run_latency_cell(poll, rate, args.duration, out_root)
            results.append(r)
            print(json.dumps(r), flush=True)
    summary_path = out_root / "latency-results.json"
    summary_path.write_text(json.dumps(results, indent=2))
    print(f"wrote {summary_path}", file=sys.stderr)


def cmd_monitor(args: argparse.Namespace) -> None:
    samples: list[tuple[float, float]] = []
    while True:
        p = subprocess.run(
            ["ps", "-o", "%cpu=,rss=", "-p", str(args.pid)],
            capture_output=True, text=True,
        )
        if p.returncode != 0:
            break
        parts = p.stdout.split()
        if len(parts) >= 2:
            samples.append((float(parts[0]), float(parts[1])))
        time.sleep(args.interval)
        if args.duration and len(samples) * args.interval >= args.duration:
            break
    if samples:
        cpus = sorted(s[0] for s in samples)
        print(json.dumps({
            "samples": len(samples),
            "cpu_pct_p50": cpus[len(cpus) // 2],
            "cpu_pct_p95": cpus[min(len(cpus) - 1, int(0.95 * len(cpus)))],
            "cpu_pct_max": cpus[-1],
            "rss_kb_max": max(s[1] for s in samples),
        }))
    else:
        print(json.dumps({"samples": 0}))


ALT_SCREEN_EXIT = b"\x1b[?1049l"
CURSOR_SHOW = b"\x1b[?25h"


def cmd_pty_app(args: argparse.Namespace) -> None:
    """Run app.py in a pty; send a signal or key; verify restore output."""
    import pty

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.no_gen:
        gen = subprocess.Popen(["sleep", "0"])
    else:
        gen_cmd = [sys.executable, str(SPIKE_DIR / "fake_run.py"), "--out", str(out_dir),
                   "--components", "3", "--rate", str(args.rate), "--duration", "600"]
        if args.rate > 1:
            gen_cmd.append("--churn")
        gen = subprocess.Popen(gen_cmd, stdout=subprocess.DEVNULL)
    time.sleep(1.0)
    master, slave = pty.openpty()
    env = dict(os.environ, TERM="xterm-256color", COLUMNS="120", LINES="40")
    cmd = [sys.executable, str(SPIKE_DIR / "app.py"), "--root", str(out_dir),
           "--poll", "0.2"]
    if args.chatter:
        cmd.append("--chatter")
    if args.subproc_chatter:
        cmd.append("--subproc-chatter")
    if args.prompt_demo:
        cmd.append("--prompt-demo")
    if args.no_transcript:
        cmd.append("--no-transcript")
    if args.crash_after:
        cmd += ["--crash-after", str(args.crash_after)]
    app = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, env=env,
                           start_new_session=True)
    os.close(slave)
    captured = b""
    end = time.monotonic() + args.run_for
    os.set_blocking(master, False)
    cpu_samples: list[float] = []
    last_sample = 0.0
    while time.monotonic() < end and app.poll() is None:
        try:
            captured += os.read(master, 65536)
        except BlockingIOError:
            pass
        except OSError:
            break
        if time.monotonic() - last_sample >= 1.0:
            last_sample = time.monotonic()
            ps = subprocess.run(["ps", "-o", "%cpu=", "-p", str(app.pid)],
                                capture_output=True, text=True)
            if ps.returncode == 0 and ps.stdout.strip():
                cpu_samples.append(float(ps.stdout.strip()))
        time.sleep(0.05)

    verdicts: dict[str, object] = {"mode": args.action}
    if app.poll() is None:
        if args.action == "sigint":
            os.kill(app.pid, signal.SIGINT)
        elif args.action == "sigterm":
            os.kill(app.pid, signal.SIGTERM)
        elif args.action == "key-q":
            os.write(master, b"q")
        elif args.action == "ctrl-c-key":
            os.write(master, b"\x03")
    deadline = time.monotonic() + 10
    while app.poll() is None and time.monotonic() < deadline:
        try:
            captured += os.read(master, 65536)
        except (BlockingIOError, OSError):
            pass
        time.sleep(0.05)
    # final drain
    for _ in range(20):
        try:
            captured += os.read(master, 65536)
        except (BlockingIOError, OSError):
            break
        time.sleep(0.02)
    if app.poll() is None:
        app.kill()
        verdicts["exited"] = False
    else:
        verdicts["exited"] = True
        verdicts["returncode"] = app.returncode
    gen.terminate()
    if cpu_samples:
        cs = sorted(cpu_samples)
        verdicts["app_cpu_pct_p50"] = cs[len(cs) // 2]
        verdicts["app_cpu_pct_p95"] = cs[min(len(cs) - 1, int(0.95 * len(cs)))]
        verdicts["app_cpu_pct_max"] = cs[-1]
    verdicts["alt_screen_entered"] = b"\x1b[?1049h" in captured
    verdicts["alt_screen_restored"] = ALT_SCREEN_EXIT in captured
    verdicts["cursor_restored"] = CURSOR_SHOW in captured
    verdicts["bytes_captured"] = len(captured)
    (Path(args.out) / f"pty-{args.action}.raw").write_bytes(captured)
    print(json.dumps(verdicts))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("latency")
    pl.add_argument("--out", required=True)
    pl.add_argument("--polls", default="0.05,0.1,0.25,0.5,1.0")
    pl.add_argument("--rates", default="1,10")
    pl.add_argument("--duration", type=float, default=60.0)
    pl.set_defaults(func=cmd_latency)

    pm = sub.add_parser("monitor")
    pm.add_argument("--pid", type=int, required=True)
    pm.add_argument("--interval", type=float, default=1.0)
    pm.add_argument("--duration", type=float, default=0.0)
    pm.set_defaults(func=cmd_monitor)

    pp = sub.add_parser("pty-app")
    pp.add_argument("--out", required=True)
    pp.add_argument("--action", required=True,
                    choices=["sigint", "sigterm", "key-q", "ctrl-c-key", "crash"])
    pp.add_argument("--rate", type=float, default=1.0)
    pp.add_argument("--run-for", type=float, default=8.0)
    pp.add_argument("--chatter", action="store_true")
    pp.add_argument("--subproc-chatter", action="store_true")
    pp.add_argument("--prompt-demo", action="store_true")
    pp.add_argument("--crash-after", type=float, default=0.0)
    pp.add_argument("--no-gen", action="store_true",
                    help="tail an existing (finished) run - the idle case")
    pp.add_argument("--no-transcript", action="store_true")
    pp.set_defaults(func=cmd_pty_app)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
