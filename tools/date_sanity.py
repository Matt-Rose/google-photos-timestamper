"""Find assets whose capture date is implausible.

Photos stores capture dates as seconds since 2001-01-01. A sidecar with a
malformed timestamp, or a filename-derived date that parsed wrongly, produces
values thousands of years out -- and Photos sorts them to the very start or
end of the library where nobody scrolls, so they go unnoticed for years.

    python3 tools/date_sanity.py "/path/to/Library.photoslibrary" [--min 1900] [--max 2100]

Read-only. Prints each offender with its original filename and UUID, so it
can be found in Photos and corrected. The UUID is what Photos' own search
and osxphotos both key on.
"""

import argparse
import datetime
import os
import sqlite3
import sys

APPLE_EPOCH = datetime.datetime(2001, 1, 1)


def to_iso(seconds: float | None) -> str:
    if seconds is None:
        return "(none)"
    try:
        return (APPLE_EPOCH + datetime.timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, ValueError):
        return f"(unrepresentable: {seconds:.0f}s)"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library")
    parser.add_argument("--min", type=int, default=1900, help="earliest plausible year")
    parser.add_argument("--max", type=int, default=2100, help="latest plausible year")
    args = parser.parse_args()

    db = os.path.join(args.library, "database", "Photos.sqlite")
    if not os.path.exists(db):
        sys.exit(f"no Photos.sqlite under {args.library}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    lo = (datetime.datetime(args.min, 1, 1) - APPLE_EPOCH).total_seconds()
    hi = (datetime.datetime(args.max, 1, 1) - APPLE_EPOCH).total_seconds()

    rows = con.execute(
        "select a.ZUUID, a.ZDATECREATED, x.ZORIGINALFILENAME, a.ZKIND "
        "from ZASSET a left join ZADDITIONALASSETATTRIBUTES x on x.ZASSET = a.Z_PK "
        "where a.ZTRASHEDSTATE = 0 "
        "  and (a.ZDATECREATED is null or a.ZDATECREATED < ? or a.ZDATECREATED >= ?) "
        "order by a.ZDATECREATED", (lo, hi)).fetchall()
    total = con.execute("select count(*) from ZASSET where ZTRASHEDSTATE=0").fetchone()[0]

    print(f"{len(rows)} of {total} assets have a capture date outside {args.min}-{args.max}\n")
    if not rows:
        return
    print(f"{'capture date':<28}{'kind':<6}{'original filename':<40}uuid")
    for uuid, when, name, kind in rows:
        print(f"{to_iso(when):<28}{'video' if kind == 1 else 'photo':<6}{(name or '?')[:38]:<40}{uuid}")


if __name__ == "__main__":
    main()
