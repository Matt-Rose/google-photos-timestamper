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
  a *complete* shared album. Only the metadata survives the copy.
* **Filenames do not survive either, reliably.** A shared album populated
  from a Google web download carries Google's names ("IMG_1234.JPG"), a
  private album built from Takeout carries Takeout's ("IMG_1234(1).JPG",
  "image.jpg"); in one 1,400-item pair only 106 names agreed. Capture time
  is the robust identity -- but even that disagrees by up to a second
  (sub-second rounding differs between the two export routes) and, for a
  handful, by exactly one hour. ``match_assets`` pairs items one-to-one by
  capture time with those tolerances, then by filename for what is left.
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
import datetime
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


APPLE_EPOCH = 978307200  # 2001-01-01T00:00:00Z, the zero of ZDATECREATED


def apple_date(when: float | None) -> str:
    if when is None:
        return "undated"
    return datetime.datetime.fromtimestamp(
        when + APPLE_EPOCH, datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")


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


def norm_title(title: str) -> str:
    """Key used to pair a private album with its shared twin.

    Album titles arrive with stray whitespace and inconsistent case -- Google
    exported folders like "Holiday " and "Family album ", and the
    album import carried those names into Photos verbatim, while the shared
    albums built by hand are clean. Exact comparison missed seven of seventeen
    real pairs in one library. Whitespace and case are normalised; punctuation
    is not, because "Party!" and "Party" may genuinely differ.
    """
    return " ".join(title.split()).casefold()


def live_albums(con: sqlite3.Connection) -> dict[str, int]:
    """Newest non-trashed album row per (normalised) title."""
    out: dict[str, int] = {}
    for pk, title in con.execute(
        "select Z_PK, ZTITLE from ZGENERICALBUM where ZKIND=2 and ZTITLE is not null "
        "and (ZTRASHEDSTATE is null or ZTRASHEDSTATE=0) order by Z_PK"
    ):
        out[norm_title(title)] = pk          # later rows win: newest per title
    return out


def shared_albums(con: sqlite3.Connection) -> dict[str, int]:
    return {
        norm_title(title): pk
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


# A private item and a shared item are the same photo if their capture times
# agree to within this many seconds ...
TIME_TOLERANCE = 1
# ... or differ by a whole number of hours (an export route that read the
# zone differently), up to the widest offset in use.
HOUR_SHIFTS = [h * 3600 for h in range(-14, 15) if h]


def match_assets(private: list[tuple[str, float | None]],
                 shared: list[tuple[str, float | None]]) -> list[tuple[str, float | None]]:
    """Items of ``private`` that have no counterpart in ``shared``.

    Each item is ``(original filename, capture time in seconds)``. Matching is
    one-to-one -- a shared item can stand in for at most one private item, so
    a burst of three identical-second frames needs three shared copies -- and
    runs in three passes, strongest evidence first: capture time within
    ``TIME_TOLERANCE``, capture time shifted by a whole number of hours, then
    filename alone (the only evidence for items with no date).

    Takeout exports a photo edited in Google as two files, "IMG_1.jpg" and
    "IMG_1-edited.jpg", with the same capture time; a shared album holds one
    copy of that photo. So an unmatched edited copy whose original matched is
    not missing -- the photo is there. It is dropped from the result.
    """
    by_second: dict[int, list[int]] = collections.defaultdict(list)
    by_name: dict[str, list[int]] = collections.defaultdict(list)
    for i, (name, when) in enumerate(shared):
        if when is not None:
            by_second[int(when)].append(i)
        if name:
            by_name[name].append(i)
    used: set[int] = set()

    def take(candidates: list[int]) -> bool:
        for i in candidates:
            if i not in used:
                used.add(i)
                return True
        return False

    def near(when: float, shift: int) -> list[int]:
        centre = int(when) + shift
        return [i for s in range(centre - TIME_TOLERANCE, centre + TIME_TOLERANCE + 1)
                for i in by_second.get(s, ())]

    unmatched = list(range(len(private)))
    for shifts in ([0], HOUR_SHIFTS):
        still: list[int] = []
        for p in unmatched:
            when = private[p][1]
            if when is None or not any(take(near(when, s)) for s in shifts):
                still.append(p)
        unmatched = still
    unmatched = [p for p in unmatched if not take(by_name.get(private[p][0], []))]
    matched_names = {private[p][0] for p in range(len(private))} - {
        private[p][0] for p in unmatched}
    return [private[p] for p in unmatched
            if edit_original(private[p][0]) not in matched_names]


EDITED = re.compile(r"^(.*)-edited(\.[^.]+)$")


def edit_original(name: str) -> str | None:
    """"IMG_1-edited.jpg" -> "IMG_1.jpg"; None if this is not an edited copy."""
    m = EDITED.match(name)
    return f"{m.group(1)}{m.group(2)}" if m else None


def classify(private_total, in_shared_library, shared_pk, missing_names,
             require_shared_library=True):
    """Which of the four states an album is in.

    ``require_shared_library=False`` skips the first test. That is the right
    setting for a library whose owner never used the Shared Library at all,
    where the only question is whether each private album has a complete
    shared-album twin -- otherwise every album reports as "still has items
    outside the Shared Library" and the comparison you want never runs.
    """
    if require_shared_library and private_total and in_shared_library < private_total:
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
    parser.add_argument("--verbose", action="store_true", help="list the missing items (name and capture time)")
    parser.add_argument("--no-shared-library", action="store_true",
                        help="skip the Shared Library check; compare private albums "
                             "against their shared-album twins only")
    args = parser.parse_args()

    con = connect(args.library)
    join, album_col, asset_col = album_asset_table(con)
    live, shares = live_albums(con), shared_albums(con)
    invitees = share_participants(con)

    if args.albums:
        with open(args.albums) as handle:
            wanted = [r["name"] for r in csv.DictReader(handle, delimiter="\t")]
    else:
        # Display the raw title, but look up by the normalised key.
        wanted = sorted(
            {t for (t,) in con.execute(
                "select ZTITLE from ZGENERICALBUM where ZKIND=2 and ZTITLE is not null "
                "and (ZTRASHEDSTATE is null or ZTRASHEDSTATE=0)")},
            key=norm_title)

    buckets: dict[str, list] = collections.defaultdict(list)
    for title in wanted:
        pk = live.get(norm_title(title))
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

        share_pk = shares.get(norm_title(title))
        missing: list[str] = []
        if share_pk is not None:
            def items(sql, arg):
                return [(name or "", when) for name, when in con.execute(sql, (arg,))]
            private_items = items(
                f"select aa.ZORIGINALFILENAME, x.ZDATECREATED from {join} j "
                f"join ZASSET x on x.Z_PK=j.{asset_col} "
                "join ZADDITIONALASSETATTRIBUTES aa on aa.ZASSET=x.Z_PK "
                f"where j.{album_col}=?", pk)
            shared_items = items(
                "select aa.ZORIGINALFILENAME, x.ZDATECREATED from ZASSET x "
                "join ZADDITIONALASSETATTRIBUTES aa on aa.ZASSET=x.Z_PK "
                "where x.ZCOLLECTIONSHARE=? and x.ZTRASHEDSTATE=0", share_pk)
            missing = sorted(
                f"{name or '(no name)'}  {apple_date(when)}"
                for name, when in match_assets(private_items, shared_items)
            )

        people = invitees.get(share_pk, []) if share_pk is not None else []
        state = classify(total, in_lib, share_pk, missing,
                         require_shared_library=not args.no_shared_library)
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
