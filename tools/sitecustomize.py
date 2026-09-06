"""Suppress photoscript's ``killall Photos`` retry hook.

MUST be loaded via ``PYTHONPATH``, never by copying into a venv's
``site-packages``. Homebrew ships its own ``sitecustomize.py`` inside the
stdlib directory, and the stdlib dir precedes ``site-packages`` on
``sys.path``, so a copy there never loads. A ``PYTHONPATH`` entry precedes
both.

Because Homebrew's copy does real work (``sys.path`` reshuffling, prefix
fixups), shadowing it without chaining would break the interpreter's paths.
So step 1 executes Homebrew's by explicit file path, then step 2 applies the
patch.

Homebrew's file warns "Don't print from here, or else python command line
scripts may fail!", so this writes to a log file instead of stdout.

What it fixes: photoscript wraps every ``run_script()`` call in a tenacity
retry whose ``before_sleep`` hook runs ``killall Photos``. Any AppleScript
error containing "timed out" therefore kills the app. Because photoscript
relaunches with ``tell application "Photos" to launch`` (no open event),
Photos comes back windowless and every later call fails with
``run_script 'albumAdd' failed: User cancelled. (-128)`` or
``ValueError: Invalid photo id: <uuid>``. The retry is kept; only the kill
is removed.

Usage::

    PYTHONPATH=<repo>/tools osxphotos import ...

See ``tools/osxphotos-safe`` for a wrapper that sets this up, and
``docs/apple-photos-import.md`` for the wider context.
"""

import glob
import os
import sys

_LOG = os.environ.get(
    "PHOTOS_SITECUSTOMIZE_LOG",
    os.path.expanduser("~/photos-sitecustomize.log"),
)


class _SkipChain(Exception):
    """Not a Homebrew 3.14 interpreter: skip chaining and patching entirely."""


def _note(msg: str) -> None:
    try:
        with open(_LOG, "a") as fh:
            fh.write(f"{msg}\n")
    except Exception:
        pass


# --- 1. chain to Homebrew's sitecustomize, or the interpreter paths break ---
#
# ONLY when the running interpreter really is a Homebrew python 3.14.
# Homebrew's sitecustomize starts with a version check and calls exit() if
# PYTHONPATH points at a 3.14 site-packages while running a different Python.
# That is correct for its own purposes but fatal when chained into an
# unrelated interpreter: exporting PYTHONPATH globally once turned a harmless
# warning into "Fatal Python error: init_import_site", killing a script that
# never needed osxphotos at all. Guard, do not propagate.
_is_brew_314 = sys.version_info[:2] == (3, 14) and "/opt/homebrew/" in (
    getattr(sys, "base_prefix", "") or ""
)
try:
    if not _is_brew_314:
        _note(
            f"not a Homebrew 3.14 interpreter "
            f"({sys.version_info[0]}.{sys.version_info[1]}, "
            f"base_prefix={getattr(sys, 'base_prefix', '?')}) - skipping"
        )
        raise _SkipChain
    _pattern = (
        "/opt/homebrew/Cellar/python@3.14/*/Frameworks/Python.framework"
        "/Versions/3.14/lib/python3.14/sitecustomize.py"
    )
    _brew = sorted(glob.glob(_pattern))
    if _brew:
        with open(_brew[-1]) as _fh:
            _src = _fh.read()
        exec(compile(_src, _brew[-1], "exec"), {"__name__": "sitecustomize"})
        _note(f"chained Homebrew sitecustomize: {_brew[-1]}")
    else:
        _note("WARNING: no Homebrew sitecustomize found to chain to")
except _SkipChain:
    pass
except SystemExit as exc:
    # Never let a chained exit() kill the host interpreter.
    _note(f"chained sitecustomize tried to exit ({exc}) - ignored")
except Exception as exc:
    _note(f"WARNING: could not chain Homebrew sitecustomize: {exc!r}")


# --- 2. neuter photoscript's killall ---------------------------------------
try:
    from photoscript import script_loader

    def _no_kill(retry_state: object) -> None:
        _note("AppleScript timed out; retrying WITHOUT killing Photos.app")
        return None

    # run_script() rebuilds its tenacity decorator on every call and resolves
    # kill_photos_app from module globals at that moment, so patching the
    # module attribute takes effect for every subsequent call.
    script_loader.kill_photos_app = _no_kill
    _note(f"photoscript killall suppression ACTIVE (pid {os.getpid()})")
except ImportError:
    pass  # photoscript not installed in this interpreter; nothing to patch
except Exception as exc:
    _note(f"could NOT suppress photoscript killall: {exc!r}")
