"""Import files into Apple Photos without being defeated by a hung Photos.app.

Wraps ``osxphotos import`` with the safeguards that two real migrations showed
to be necessary. See ``docs/apple-photos-import.md`` for why each exists; the
short version:

* Photos hangs *inside* AppleScript event handling. It stays running, so
  "relaunch if the process is gone" never fires. Every batch after the hang
  does nothing at all.
* A polite quit cannot recover it — the quit is itself an Apple Event queued
  behind the blocked one.
* photoscript relaunches with ``launch`` rather than an open event, producing
  a windowless Photos. Always reopen with ``open -a``.
* Photos needs time to index after launch or album adds fail with
  "Invalid photo id" for assets that exist.

Run with ``tools/osxphotos-safe`` on PATH, or set ``OSXPHOTOS_BIN``, so that
photoscript's ``killall Photos`` hook is suppressed.
"""

import argparse
import collections
import os
import subprocess
import sys
import time

# Summed %CPU across these must fall below QUIET_THRESHOLD before a batch.
# They are what starve Photos during import and keep the WAL hot.
ANALYSIS_DAEMONS = ("mediaanalysisd", "photoanalysisd", "cloudphotod")
QUIET_THRESHOLD = 25.0
QUIET_MAXWAIT = 1800

PHOTOS_PATTERN = "Photos.app/Contents/MacOS/Photos"
PROBE_TIMEOUT = 90
INDEX_SETTLE_SECONDS = 90


def say(msg: str) -> None:
    print(msg, flush=True)


def analysis_cpu() -> float:
    """Summed %CPU of the daemons that starve Photos during an import."""
    out = subprocess.run(
        ["ps", "-Ao", "%cpu,comm"], capture_output=True, text=True
    ).stdout
    total = 0.0
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        cpu, comm = parts
        if any(d in comm for d in ANALYSIS_DAEMONS):
            try:
                total += float(cpu)
            except ValueError:
                pass
    return total


def wait_for_quiet(threshold: float = QUIET_THRESHOLD, maxwait: int = QUIET_MAXWAIT) -> None:
    """Block until the analysis daemons are idle, or give up and proceed."""
    waited = 0
    while waited < maxwait:
        cpu = analysis_cpu()
        if cpu < threshold:
            if waited:
                say(f"    daemons quiet ({cpu:.0f}% < {threshold:.0f}%) after {waited}s")
            return
        time.sleep(30)
        waited += 30
    say(f"    still busy ({analysis_cpu():.0f}%) after {maxwait}s - proceeding anyway")


def photos_pid() -> str | None:
    """PID of *this user's* Photos.app, or None.

    The -u filter is essential, not tidiness. On a machine with fast user
    switching, another account's Photos is also running and matches the same
    pattern -- without the filter this returns their PID and restart_photos()
    force-quits an innocent app in someone else's session, mid-import.
    """
    out = subprocess.run(
        ["pgrep", "-u", str(os.getuid()), "-f", PHOTOS_PATTERN],
        capture_output=True,
        text=True,
    ).stdout.split()
    return out[0] if out else None


def photos_answers() -> str | None:
    """Liveness probe.

    Counts *albums*, not media items. Counting ~44k assets takes about 40
    seconds on a real library, so an expensive probe cannot tell slow from
    dead — it once failed a Photos that had just answered correctly. Albums
    number in the hundreds and walk the same AppleScript path.
    """
    proc = subprocess.run(
        [
            "osascript",
            "-e", f"with timeout of {PROBE_TIMEOUT} seconds",
            "-e", 'tell application "Photos" to return (count of albums) as text',
            "-e", "end timeout",
        ],
        capture_output=True,
        text=True,
    )
    answer = proc.stdout.strip()
    return answer or None


def photos_is_hung() -> bool:
    """Two probes 20s apart, so one slow answer cannot condemn a healthy app."""
    if photos_answers():
        return False
    time.sleep(20)
    return photos_answers() is None


def restart_photos() -> bool:
    """Force Photos into a known-good state. True if it ends up healthy."""
    if photos_pid():
        say("    asking Photos to quit (30s grace)...")
        subprocess.Popen(
            [
                "osascript",
                "-e", "with timeout of 25 seconds",
                "-e", 'tell application "Photos" to quit',
                "-e", "end timeout",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(6):
            if not photos_pid():
                break
            time.sleep(5)

        # Expected to be needed whenever it actually matters: a hung Photos
        # cannot process the quit event, because that event is queued behind
        # the one it is stuck on.
        for sig, grace in (("-TERM", 4), ("-KILL", 1)):
            if not photos_pid():
                break
            say(f"    escalating: pkill {sig}")
            subprocess.run(
                ["pkill", sig, "-u", str(os.getuid()), "-f", PHOTOS_PATTERN],
                capture_output=True,
            )
            for _ in range(grace):
                if not photos_pid():
                    break
                time.sleep(5)

        if photos_pid():
            say("    FAILED: cannot terminate Photos")
            return False

    # `open -a` sends a proper open event. photoscript uses `tell application
    # "Photos" to launch`, which starts the app WITHOUT one -- that is how you
    # get "running in the taskbar but no window". Never use `launch` here.
    say("    reopening Photos...")
    subprocess.run(["open", "-a", "Photos"], capture_output=True)
    time.sleep(20)
    if not photos_pid():
        say("    FAILED: Photos did not reopen")
        return False

    # Give it time to index. Importing 80s after launch once produced 192
    # consecutive "Invalid photo id" failures for assets that existed.
    say(f"    letting it index ({INDEX_SETTLE_SECONDS}s)...")
    time.sleep(INDEX_SETTLE_SECONDS)

    good = 0
    for attempt in range(1, 9):
        answer = photos_answers()
        if answer:
            good += 1
            say(f"    probe {attempt} OK ({answer} albums) [{good}/2]")
            if good >= 2:
                say("    Photos is healthy")
                return True
        else:
            good = 0
            say(f"    probe {attempt} no answer, waiting 30s...")
        time.sleep(30)

    say("    FAILED: Photos is up but will not answer reliably")
    return False


def chunk_files(paths: list[str], chunk_size: int) -> list[list[str]]:
    """Group by filename stem, then pack into chunks.

    A Live Photo's still and video share a stem and MUST stay in the same
    chunk, or Photos imports them as two unrelated assets.
    """
    groups: dict[str, list[str]] = collections.defaultdict(list)
    for path in paths:
        groups[os.path.splitext(os.path.basename(path))[0]].append(path)

    chunks: list[list[str]] = []
    current: list[str] = []
    for stem in sorted(groups):
        members = groups[stem]
        if current and len(current) + len(members) > chunk_size:
            chunks.append(current)
            current = []
        current.extend(members)
    if current:
        chunks.append(current)
    return chunks


def import_chunk(files: list[str], album: str, osxphotos: str) -> bool:
    cmd = [
        osxphotos, "import", *files,
        "--album", album,
        "--skip-dups", "--dup-albums", "--auto-live", "--resume", "--verbose",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode == 0


def run(album_dir: str, album: str, chunk_size: int, osxphotos: str, attempts: int) -> int:
    files = [
        os.path.join(album_dir, f)
        for f in sorted(os.listdir(album_dir))
        if not f.startswith(".")
    ]
    chunks = chunk_files(files, chunk_size)
    say(f"{len(files)} files -> {len(chunks)} chunks of <= {chunk_size}")

    failed = 0
    for index, chunk in enumerate(chunks, 1):
        for attempt in range(1, attempts + 1):
            wait_for_quiet()
            if photos_is_hung() and not restart_photos():
                say(f"[{index}/{len(chunks)}] ABORT: Photos unrecoverable")
                return 1
            if import_chunk(chunk, album, osxphotos):
                say(f"[{index}/{len(chunks)}] ok ({len(chunk)} files)")
                break
            say(f"[{index}/{len(chunks)}] attempt {attempt}/{attempts} failed")
            if attempt < attempts:
                # Photos is very likely wedged: restart before retrying rather
                # than firing the same batch at a blocked event loop.
                restart_photos()
                time.sleep(60 * attempt)
        else:
            failed += 1
            say(f"[{index}/{len(chunks)}] GIVING UP after {attempts} attempts")

    say(f"done: {len(chunks) - failed}/{len(chunks)} chunks ok")
    return 0 if failed == 0 else 2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("album_dir")
    parser.add_argument("album")
    parser.add_argument("--chunk-size", type=int, default=20)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--osxphotos", default=os.environ.get("OSXPHOTOS_BIN", "osxphotos"))
    args = parser.parse_args()
    sys.exit(run(args.album_dir, args.album, args.chunk_size, args.osxphotos, args.attempts))


if __name__ == "__main__":
    main()
