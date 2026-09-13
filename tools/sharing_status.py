"""Report how far each private album has got towards being fully shared.

The end state for a migrated album is: every one of its assets lives in the
Shared Library, an iCloud Shared Album of the same name exists, and that
shared album contains everything the private album does. Once all three hold,
the private album is redundant and can be deleted.

This prints each album in exactly one of four states::

    python3 tools/sharing_status.py "/path/to/Library.photoslibrary"
    python3 tools/sharing_status.py "/path/to/Library.photoslibrary" --albums picks.tsv

Read-only: opens the database with mode=ro and never writes.

It also reports who each shared album is shared with and whether they have
accepted, and ends with an AWAITING ACCEPTANCE list -- an album whose
invitation is unaccepted looks finished here but is not yet visible to anyone.

Three traps this works around, each of which produced a wrong answer first:

* **A shared album shares no assets with the private album.** It holds its own
  downscaled copies, so ``ZASSET.ZCOLLECTIONSHARE`` never points at the
  private album's assets and an asset-identity join returns zero overlap for
  a *complete* shared album. Filenames are the only usable join.
* **`ZFILENAME` is Photos' internal name**, not the original. Comparing it
  against anything gives 0% overlap. Use
  ``ZADDITIONALASSETATTRIBUTES.ZORIGINALFILENAME``.
* **Deleted albums linger** with ``ZTRASHEDSTATE=1``, and osxphotos creates a
  *new* album per import rather than adding to an existing one of the same
  name -- so a title can match several rows. Exclude trashed rows and take
  the newest.
"""

import argparse
import collections
import csv
import os
import re
import sqlite3
import sys

# One of four states, in the order an album passes through them.
HAS_PRIVATE = "still has items outside the Shared Library"
NO_SHARED_ALBUM = "fully in Shared Library, but no shared album exists"
SHARED_INCOMPLETE = "shared album exists but is missing items"
DELETABLE = "fully shared -- private album can be deleted"


def connect(library: str) -> sqlite3.Connection:
    db = os.path.join(library, "database", "Photos.sqlite")
    if not os.path.exists(db):
        sys.exit(f"no Photos.sqlite under {library}")
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


ALBUM_COL = re.compile(r"^Z_\d+ALBUMS$")
ASSET_COL = re.compile(r"^Z_\d+ASSETS$")


def album_asset_table(con: sqlite3.Connection) -> tuple[str, str, str]:
    """Find the album<->asset join table; its name varies by Photos version.

    Match the column names exactly, not by substring. ``Z_32KEYASSETS`` (the
    album *thumbnail* table) has a column ``Z_32ALBUMSBEINGKEYASSETS`` which
    contains both "ALBUM" and "ASSET", so a substring test picks it and every
    album then reports a handful of members instead of its real count.
    Where several tables still qualify, take the largest: album membership is
    by far the biggest such table.
    """
    best: tuple[int, str, str, str] | None = None
    for (name,) in con.execute(
        "select name from sqlite_master where type='table' and name like 'Z\\_%ASSETS' escape '\\'"
    ):
        cols = [c[1] for c in con.execute(f"pragma table_info({name})")]
        album = next((c for c in cols if ALBUM_COL.match(c)), None)
        asset = next((c for c in cols if ASSET_COL.match(c)), None)
        if not (album and asset):
            continue
        rows = con.execute(f"select count(*) from {name}").fetchone()[0]
        if best is None or rows > best[0]:
            best = (rows, name, album, asset)
    if best is None:
        sys.exit("could not locate the album/asset join table")
    return best[1], best[2], best[3]


def live_albums(con: sqlite3.Connection) -> dict[str, int]:
    """Newest non-trashed album row per title."""
    out: dict[str, int] = {}
    for pk, title in con.execute(
        "select Z_PK, ZTITLE from ZGENERICALBUM where ZKIND=2 and ZTITLE is not null "
        "and (ZTRASHEDSTATE is null or ZTRASHEDSTATE=0) order by Z_PK"
    ):
        out[title] = pk          # later rows win: newest per title
    return out


def shared_albums(con: sqlite3.Connection) -> dict[str, int]:
    return {
        title: pk
        for pk, title in con.execute(
            "select Z_PK, ZTITLE from ZSHARE where ZSCOPETYPE=0 and ZTITLE is not null"
        )
    }


# ZSHAREPARTICIPANT.ZACCEPTANCESTATUS. Only these two were observed across 22
# shares; every "accepted" row also carried a ZSUBSCRIPTIONDATE and every
# "invited" row had none, which is what pins the mapping. An unknown code is
# printed raw rather than guessed at.
ACCEPTANCE = {1: "invited", 2: "accepted"}


def describe_participant(email: str | None, phone: str | None, status: int | None) -> str:
    """One invitee, as 'who (state)'.

    **Which identity is present tells you how the album was shared**, which
    matters when a recipient has lost access to one of their addresses:

    * email only, or email + phone -> invited by email address
    * phone only                   -> invited by phone number

    Two phone formats occur and they are not interchangeable. A human-formatted
    ``+44 7928 313055`` is what someone typed into the invite field. A bare
    ``447928313055`` is Apple's canonical form, filled in beside the email on
    some shares once the invitation resolves to a real iCloud account -- it does
    not mean the phone number was used. Hence: prefer the email whenever there
    is one, and do not add a ``+`` to a number that already has one.
    """
    if email:
        who = email
    elif phone:
        who = phone if phone.startswith("+") else f"+{phone}"
    else:
        who = "unknown"
    state = ACCEPTANCE.get(status, f"status {status}")
    return f"{who} ({state})"


def share_participants(con: sqlite3.Connection) -> dict[int, list[tuple[str, int | None]]]:
    """Invitees per share, excluding yourself, as {share pk: [(text, status)]}.

    Joins on ``ZSHAREPARTICIPANT.ZSHARE``, not the ``Z<NN>_SHARE`` column beside
    it -- that one is numbered per Photos version (Z66_SHARE here, Z51/Z54/Z61
    elsewhere) and points at a different entity. Returns {} on a library with no
    such table rather than failing, since the rest of the report still works.
    """
    out: dict[int, list[tuple[str, int | None]]] = collections.defaultdict(list)
    try:
        rows = con.execute(
            "select ZSHARE, ZEMAILADDRESS, ZPHONENUMBER, ZACCEPTANCESTATUS "
            "from ZSHAREPARTICIPANT where ZISCURRENTUSER = 0 and ZSHARE is not null"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    for share_pk, email, phone, status in rows:
        out[share_pk].append((describe_participant(email, phone, status), status))
    return dict(out)


def awaiting_acceptance(participants: list[tuple[str, int | None]]) -> list[str]:
    """Invitees who have not accepted. An album with none is fully live."""
    return [text for text, status in participants if status != 2]


def classify(private_total, in_shared_library, shared_pk, missing_names):
    if private_total and in_shared_library < private_total:
        return HAS_PRIVATE
    if shared_pk is None:
        return NO_SHARED_ALBUM
    if missing_names:
        return SHARED_INCOMPLETE
    return DELETABLE


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library")
    parser.add_argument("--albums", help="TSV with a 'name' column; default is every album")
    parser.add_argument("--verbose", action="store_true", help="list the missing filenames")
    args = parser.parse_args()

    con = connect(args.library)
    join, album_col, asset_col = album_asset_table(con)
    live, shares = live_albums(con), shared_albums(con)
    invitees = share_participants(con)

    if args.albums:
        with open(args.albums) as handle:
            wanted = [r["name"] for r in csv.DictReader(handle, delimiter="\t")]
    else:
        wanted = sorted(live)

    buckets: dict[str, list] = collections.defaultdict(list)
    for title in wanted:
        pk = live.get(title)
        if pk is None:
            buckets["private album no longer exists"].append((title, "", [], []))
            continue
        total = con.execute(
            f"select count(*) from {join} where {album_col}=?", (pk,)
        ).fetchone()[0]
        in_lib = con.execute(
            f"select count(*) from {join} j join ZASSET x on x.Z_PK=j.{asset_col} "
            f"where j.{album_col}=? and x.ZLIBRARYSCOPE=1", (pk,)
        ).fetchone()[0]

        share_pk = shares.get(title)
        missing: list[str] = []
        if share_pk is not None:
            def names(sql, arg):
                return collections.Counter(
                    r[0] for r in con.execute(sql, (arg,)) if r[0]
                )
            private_names = names(
                f"select aa.ZORIGINALFILENAME from {join} j "
                f"join ZASSET x on x.Z_PK=j.{asset_col} "
                "join ZADDITIONALASSETATTRIBUTES aa on aa.ZASSET=x.Z_PK "
                f"where j.{album_col}=?", pk)
            shared_names = names(
                "select aa.ZORIGINALFILENAME from ZASSET x "
                "join ZADDITIONALASSETATTRIBUTES aa on aa.ZASSET=x.Z_PK "
                "where x.ZCOLLECTIONSHARE=?", share_pk)
            missing = sorted(
                n for n in private_names if private_names[n] > shared_names.get(n, 0)
            )

        people = invitees.get(share_pk, []) if share_pk is not None else []
        state = classify(total, in_lib, share_pk, missing)
        detail = f"{in_lib}/{total} in Shared Library"
        if state in (SHARED_INCOMPLETE, DELETABLE):
            detail = f"{total} items, shared album short by {len(missing)}"
        buckets[state].append((title, detail, missing, people))

    for state in (HAS_PRIVATE, NO_SHARED_ALBUM, SHARED_INCOMPLETE, DELETABLE,
                  "private album no longer exists"):
        rows = buckets.get(state)
        if not rows:
            continue
        print(f"\n{state.upper()}  ({len(rows)})")
        for title, detail, missing, people in sorted(rows):
            print(f"  {title:<38} {detail}")
            if people:
                print(f"      shared with: {', '.join(t for t, _ in people)}")
            elif state in (SHARED_INCOMPLETE, DELETABLE):
                print("      shared with: nobody -- the album exists but has no invitees")
            if args.verbose and missing:
                for name in missing:
                    print(f"      missing: {name}")

    # A shared album nobody has accepted is not yet visible to anyone, whatever
    # state the rest of the report puts it in. This is NOT a reason to keep the
    # private album -- see the note printed below.
    pending = sorted(
        (title, awaiting_acceptance(people))
        for rows in buckets.values()
        for title, _, _, people in rows
        if awaiting_acceptance(people)
    )
    if pending:
        print(f"\nAWAITING ACCEPTANCE  ({len(pending)})")
        print("  Created and populated, but not yet visible to the recipient. An")
        print("  invitation to an address that is already an Apple ID appears in")
        print("  Photos on their devices, not only by email.")
        for title, who in pending:
            print(f"  {title:<38} {', '.join(who)}")
        print("\n  This does not block deleting the private album: a shared album")
        print("  holds its own copies and survives the private album's deletion")
        print("  intact, accepted or not. What you lose by deleting is the ability")
        print("  to check the shared album is COMPLETE -- the private album is the")
        print("  reference this report compares against. Verify first, then delete.")
    print()


if __name__ == "__main__":
    main()
