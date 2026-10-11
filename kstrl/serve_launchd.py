"""The launchd LaunchAgent that runs ks serve: label, log dir, calendar schedule, plist."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from kstrl.serve_config import ServeError
from kstrl.statedir import state_dir

#: Reverse-DNS label prefix. The per-root suffix keeps two checkouts from
#: fighting over one launchd job.
LAUNCHD_LABEL_PREFIX = "com.kstrl.serve"


#: Minimum seconds launchd waits before RELAUNCHING an exited job.
#: launchd's own default is 10s, which for a crash-looping daemon means
#: six restarts a minute; at a measured $1.70-2.60 per engineer iteration
#: the throttle is a spend control. Note what it is NOT: it does not bound
#: how long a running job may take (review #189 F2).
LAUNCHD_THROTTLE_SECONDS = 60


#: Set by a scheduled (interval-mode) LaunchAgent. Its presence means the
#: job was installed on the promise of a bounded cycle, so `ks serve`
#: refuses to start without one (#189 N3).
REQUIRE_TIMEOUT_ENV = "KSTRL_SERVE_REQUIRE_TIMEOUT"


def launchd_label(root_dir: Path) -> str:
    """A launchd label unique to this checkout.

    Derived from the path rather than the directory name because two
    checkouts of the same repo (a worktree and its parent) would
    otherwise collide on one job, and launchd keeps only the last one
    loaded - silently.
    """
    import hashlib

    digest = hashlib.sha256(str(root_dir.resolve()).encode()).hexdigest()[:10]
    return f"{LAUNCHD_LABEL_PREFIX}.{digest}"


def launchd_log_dir(root_dir: Path) -> Path:
    """Where the LaunchAgent writes stdout/stderr.

    Its own function because the CLI must CREATE it: launchd creates the
    log file but not its parent, and a missing parent makes the job fail
    to spawn with nothing in the log explaining why.
    """
    return state_dir(root_dir) / "logs"


def calendar_schedule(interval_minutes: int) -> list[dict[str, int]]:
    """A ``StartCalendarInterval`` array firing every N minutes.

    ``StartCalendarInterval`` rather than ``StartInterval`` because only
    the former catches up after sleep. ``launchd.plist(5)``:

        StartInterval - "If the system is asleep during the time of the
        next scheduled interval firing, that interval will be missed due
        to shortcomings in kqueue(3)."

        StartCalendarInterval - "Unlike cron which skips job invocations
        when the computer is asleep, launchd will start the job the next
        time the computer wakes up."

    Review #189 F3: the guide previously claimed the opposite, and the
    R8.6 plan had recorded the correct behaviour all along.
    """
    if interval_minutes < 1:
        raise ServeError(f"launchd interval must be >= 1 minute, got {interval_minutes}")
    if interval_minutes <= 60:
        if 60 % interval_minutes:
            raise ServeError(
                f"a {interval_minutes}-minute interval does not divide an "
                "hour evenly, so it cannot be expressed as a calendar "
                "schedule; use a divisor of 60 (1, 2, 3, 4, 5, 6, 10, 12, "
                "15, 20, 30, 60)"
            )
        return [{"Minute": m} for m in range(0, 60, interval_minutes)]
    if interval_minutes % 60 or interval_minutes > 24 * 60:
        raise ServeError(
            f"a {interval_minutes}-minute interval must be a whole number "
            "of hours (and at most 24h) to be expressed as a calendar "
            "schedule"
        )
    step = interval_minutes // 60
    if 24 % step:
        # Review #189 N6: range(0, 24, 5) gives hours 0,5,10,15,20 - gaps
        # of 5,5,5,5,4 across midnight. That is not "every five hours",
        # and a job silently firing on an uneven cadence is worse than one
        # that refuses to be created.
        raise ServeError(
            f"a {step}-hour interval does not divide a day evenly, so the "
            "schedule would be uneven across midnight; use an hour count "
            "that divides 24 (1, 2, 3, 4, 6, 8, 12, 24)"
        )
    return [{"Hour": h, "Minute": 0} for h in range(0, 24, step)]


#: The two LaunchAgent shapes :func:`launchd_plist_dict` builds, and the
#: ``ks serve --plist-mode`` choices (#565).
LAUNCHD_MODES: tuple[str, ...] = ("keepalive", "interval")


def launchd_plist_dict(
    root_dir: Path,
    *,
    mode: str = "keepalive",
    interval_minutes: int = 5,
    python: str = "",
    extra_path: str = "",
    factory_timeout_seconds: float = 0.0,
) -> dict[str, Any]:
    """Build the LaunchAgent as a plist DICT.

    A dict fed to :mod:`plistlib` rather than a formatted XML string:
    hand-escaping covered ``&``, ``<`` and ``>`` but not control
    characters, which XML 1.0 cannot represent at all - a checkout path
    containing one produced a plist that failed to parse (review #189
    F4). ``plistlib`` rejects those values outright, which is turned into
    a clear ``ServeError`` by :func:`render_launchd_plist`.

    Two modes, and neither gets a guarantee launchd does not provide:

    - ``keepalive`` - one long-lived ``ks serve`` pacing itself from
      ``[serve] poll_interval_seconds``, relaunched by launchd when it
      EXITS. Sleep is survived because the process simply resumes.
    - ``interval`` - ``ks serve --once`` on a calendar schedule, which
      catches up after wake.

    **launchd bounds neither runtime nor overlap.** ``launchd.plist(5)``:
    "If the job is running during an interval firing, that interval
    firing will likewise be missed." So a wedged cycle is not killed and
    not replaced - it silently blocks every later firing. The only real
    bound is ``[serve] factory_timeout_seconds``, which is why interval
    mode refuses to generate without one (review #189 F2).
    """
    if mode not in LAUNCHD_MODES:
        raise ServeError(f"launchd mode must be 'keepalive' or 'interval', got {mode!r}")
    if mode == "interval" and factory_timeout_seconds <= 0:
        raise ServeError(
            "interval mode needs [serve] factory_timeout_seconds > 0. "
            "launchd does not bound how long a job runs and does not "
            "replace a job still running at the next firing - it just "
            "skips it - so without a timeout one wedged cycle silently "
            "stops every later one."
        )

    root = root_dir.resolve()
    interpreter = python or sys.executable
    log_dir = launchd_log_dir(root)
    args = [interpreter, "-m", "kstrl", "serve", "--root", str(root)]
    if mode == "interval":
        args.append("--once")

    # A LaunchAgent inherits no shell environment; gh and git must be
    # findable or every poll fails silently.
    path_parts = [
        str(Path(interpreter).parent),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    if extra_path:
        path_parts.insert(0, extra_path)
    deduped: list[str] = []
    for part in path_parts:
        if part and part not in deduped:
            deduped.append(part)

    plist: dict[str, Any] = {
        "Label": launchd_label(root),
        "ProgramArguments": args,
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "PATH": ":".join(deduped),
            "KSTRL_NO_TUI": "1",
        },
        "RunAtLoad": True,
        "ThrottleInterval": LAUNCHD_THROTTLE_SECONDS,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "serve.out.log"),
        "StandardErrorPath": str(log_dir / "serve.err.log"),
    }
    if mode == "keepalive":
        plist["KeepAlive"] = True
    else:
        plist["StartCalendarInterval"] = calendar_schedule(interval_minutes)
        # Review #189 N3: checking the timeout at GENERATION only binds the
        # config as it was that day. An operator who later clears
        # factory_timeout_seconds leaves an installed job running
        # unbounded, and launchd will not notice. The marker travels WITH
        # the job, so every scheduled invocation re-checks and fails
        # closed.
        env = plist["EnvironmentVariables"]
        assert isinstance(env, dict)
        env[REQUIRE_TIMEOUT_ENV] = "1"
    return plist


def render_launchd_plist(
    root_dir: Path,
    *,
    mode: str = "keepalive",
    interval_minutes: int = 5,
    python: str = "",
    extra_path: str = "",
    factory_timeout_seconds: float = 0.0,
) -> str:
    """Serialize the LaunchAgent plist, refusing values XML cannot carry."""
    import plistlib

    payload = launchd_plist_dict(
        root_dir,
        mode=mode,
        interval_minutes=interval_minutes,
        python=python,
        extra_path=extra_path,
        factory_timeout_seconds=factory_timeout_seconds,
    )
    try:
        return plistlib.dumps(payload).decode("utf-8")
    except (ValueError, TypeError) as exc:
        # plistlib rejects control characters outright, which is the
        # honest place to fail: a path XML cannot represent has no valid
        # plist, and emitting one that fails to parse later is worse.
        raise ServeError(
            f"cannot express this checkout as a launchd plist ({exc}). "
            "The path most likely contains a control character; move the "
            "checkout somewhere with a plain path."
        ) from exc
