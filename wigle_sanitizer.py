#!/usr/bin/env python3

"""
Prepare WiGLE CSVs for upload. Stateless, standard libraries only.

# SPDX-License-Identifier: MIT
# Assisted-by: Claude:claude-sonnet-5-5

Point it at a file or a directory; all CSVs found are pooled, every network is listed (most-seen
first) and you're asked two questions:

  1. Locations to filter: drop every row within R meters of each entry selected here
  2. Entries to filter: drop only that entry's own rows (travel routers etc.)

Answer with the checklist (arrows or j/k to move, space to check, / to search, a to check
everything shown, enter to confirm); without a terminal it falls back to typing numbers.

This matches and operates on BSSIDs (MACs) only when writing the results, it does not consider ESSIDs.

Devices you pick are remembered in ignore.txt (current directory, or --ignore-file PATH):
one line per device (action,bssid,ssid) where action is scrub or filter. On the next run
those entries start checked in the tables, so you keep the final say. The file is
append-only and names your networks (home, work...): keep it out of git and upload folders.

Each CSV is backed up to <name>.csv.bak and rewritten in place, ready to upload.
Files that already have a .bak are skipped; re-runs can't clobber your originals.
"""

import argparse
import csv
import math
import os
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

try:
    import curses
except ImportError:  # e.g. Windows without the windows-curses package
    curses = None

# Key codes curses reports from getch(). Terminals disagree on what Enter and Backspace
# send, so each of those is a tuple of every code that should count. (curses can be None.)
ENTER_KEYS = (10, 13, curses.KEY_ENTER) if curses else (10, 13)         # LF, CR, keypad Enter
BACKSPACE_KEYS = (8, 127, curses.KEY_BACKSPACE) if curses else (8, 127)  # BS, DEL, named
ESC_KEY = 27
PRINTABLE_ASCII = range(32, 127)                                         # space through ~

# Conversion factor from degrees to meters, which lets the script do  all its
# distance math without a geo library.
M_PER_DEG = 111320.0

# Column header and row layout shared by the checklist and the typed-number fallback
HEADER = f"{'MAC':<17}  {'SSID':<24} {'seen':>6}  {'spread':>8}"

# The two things the ignore list can say about a device, and the header written to a new list
ACTIONS = ("scrub", "filter")
IGNORE_HEADER = (
    "# wigle-sanitizer ignore list. Holds your network identities (home, work...): never commit or share.\n"
    "# scrub  = drop this device AND everything within the radius of where it was seen\n"
    "# filter = drop only this device's own rows\n"
    "# SSID is for humans only; matching is by BSSID. New entries are appended; edit freely.\n"
    "# Lines starting with a '#' are ignored.\n"
    "action,bssid,ssid\n"
)


def dist_m(a, b):
    # a, b are (lat, lon) in degrees; returns approx. distance in meters
    # North-south gap: 1 degree of latitude is ~111.3 km everywhere
    dy = (b[0] - a[0]) * M_PER_DEG
    # East-west gap: degrees of longitude shrink toward the poles,
    # so scale by cos(latitude), using the midpoint latitude (converted to radians)
    dx = (b[1] - a[1]) * M_PER_DEG * math.cos(math.radians((a[0] + b[0]) / 2))
    # Treat the small area as flat and apply Pythagoras: sqrt(dx**2 + dy**2)
    return math.hypot(dx, dy)


def find_csvs(path, recursive):
    p = Path(path)
    if p.is_file():
        return [p]
    if not p.is_dir():
        sys.exit(f"Not a file or directory: {p}")
    dir = p.rglob("*") if recursive else p.iterdir()
    return sorted(f for f in dir if f.is_file() and f.suffix.lower() == ".csv")


def load(path):
    """Return a dataset dict, or None (with a note) if the file isn't a usable WiGLE CSV."""
    bak = path.with_name(path.name + ".bak")
    if bak.exists():
        print(f"  skip  {path.name}: {bak.name} already exists (already processed?)")
        return None
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        first = f.readline()
        if first.startswith("WigleWifi"):
            preamble, header = first, f.readline()
        else:
            preamble, header = None, first
        try:
            cols = next(csv.reader([header]))
        except StopIteration:
            cols = []
        rows = list(csv.reader(f))
    if not {"MAC", "CurrentLatitude", "CurrentLongitude"} <= set(cols):
        print(f"  skip  {path.name}: not a WiGLE CSV (missing MAC/lat/lon columns)")
        return None
    # Maps column name to index position, e.g. {"MAC": 0, "SSID": 1, ...}
    ix = {c: i for i, c in enumerate(cols)}
    valid, no_gps = [], 0            # rows with usable GPS / count of rows without
    for r in rows:                  # r = one row, a list of strings
        try:
            # p = (lat, lon) as floats, looked up by column name
            p = (float(r[ix["CurrentLatitude"]]), float(r[ix["CurrentLongitude"]]))
        except (ValueError, IndexError):   # blank, garbage, or truncated row
            no_gps += 1
            continue
        if p == (0.0, 0.0):         # "no GPS" placeholder some apps write
            no_gps += 1
            continue
        valid.append((r, p))        # keep the row paired with its parsed point
    # Bundle everything about this file; ix is kept so callers can find columns by name
    return dict(path=path, bak=bak, preamble=preamble, cols=cols, ix=ix,
                valid=valid, no_gps=no_gps, nrows=len(rows))


def parse_sel(text, n):
    # Turn typed input like "1,3,5-7", "all" or "none" into a set of 1-based numbers
    # within 1..n. Raises ValueError on anything malformed or out of range.
    text = text.strip().lower()
    if text in ("", "none", "n"):                   # Enter or "none": select nothing
        return set()
    if text in ("all", "a"):                        # select everything
        return set(range(1, n + 1))
    out = set()
    for part in text.replace(" ", "").split(","):   # "1,3,5-7" -> "1", "3", "5-7"
        lo, _, hi = part.partition("-")             # "5-7" -> lo "5", hi "7"; "3" -> hi ""
        lo, hi = int(lo), int(hi or lo)             # single number: hi = lo (int() rejects junk)
        if not (1 <= lo <= hi <= n):                # reject 0, backwards, or too-big ranges
            raise ValueError(part)
        out.update(range(lo, hi + 1))               # ranges are inclusive
    return out


def read_query(scr, y, width):
    # Tiny line editor for the search box on screen row y. Returns the typed text on
    # Enter, or None if cancelled with Esc.
    text = ""
    while True:
        try:
            scr.addnstr(y, 0, ("/" + text).ljust(width), width, curses.A_BOLD)
        except curses.error:
            pass
        scr.refresh()
        key = scr.getch()
        if key in ENTER_KEYS:
            return text
        if key == ESC_KEY:
            return None
        if key in BACKSPACE_KEYS:
            text = text[:-1]
        elif key in PRINTABLE_ASCII:
            text += chr(key)


def confirm(scr, y, width, message):
    # One-key yes/no prompt on screen row y; anything other than y/Y counts as no
    try:
        scr.addnstr(y, 0, message.ljust(width), width, curses.A_BOLD)
    except curses.error:
        pass
    scr.refresh()
    return scr.getch() in (ord("y"), ord("Y"))


def checklist(title, header, lines, preselected=(), verb="select"):  # noqa: C901
    # Full-screen checkbox list (curses). `lines` holds (text, flag, search_key) per row;
    # rows listed in `preselected` (1-based) start checked. Returns the set of checked
    # 1-based positions.
    # Keys: up/down or k/j move, space toggles, / searches (only matching rows are shown),
    # a / n check or uncheck every row currently shown, enter confirms, q aborts.
    # With no search active, `a` would check every network, so it asks first (`verb` is the
    # word used in that question: "scrub" or "filter").
    # The C901 exemption is deliberate: a flat key-dispatch loop is many trivial branches,
    # not hard logic, and splitting it would scatter one small curses screen across functions.
    os.environ.setdefault("ESCDELAY", "25")         # don't make Esc feel laggy in the search box

    def run(scr):
        curses.curs_set(0)                          # hide the text cursor
        checked = [(i + 1) in preselected for i in range(len(lines))]
        view = list(range(len(lines)))              # indexes of the rows currently shown
        query = ""                                  # active search text ("" = show everything)
        cur = top = 0                               # highlighted row / first visible row (within view)

        # Only two highlights get explicit colors (checked rows: white on blue; the "moves"
        # flag: bold black on yellow). Everything else keeps the terminal's own colors.
        # Monochrome terminals fall back to bold / reverse video.
        checked_attr, flag_attr = curses.A_BOLD, curses.A_BOLD | curses.A_REVERSE
        if curses.has_colors():
            curses.use_default_colors()             # unstyled text = the terminal's own fg/bg, not black
            curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLUE)
            curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_YELLOW)
            checked_attr = curses.color_pair(1)
            flag_attr = curses.color_pair(2) | curses.A_BOLD

        def put(y, x, text, attr=0):
            # Draw text at row y, column x, clipped to the window (writing the last cell would raise)
            try:
                scr.addnstr(y, x, text, max(scr.getmaxyx()[1] - 1 - x, 0), attr)
            except curses.error:
                pass

        while True:
            h, w = scr.getmaxyx()
            page = max(1, h - 5)                    # rows left after title, help, header, footer
            last = max(len(view) - 1, 0)
            top = max(min(top, cur), cur - page + 1)    # scroll to keep the highlight visible
            scr.erase()
            put(0, 0, title, curses.A_BOLD)
            put(1, 0, "up/down j/k move | space check | / search | a/n all/none shown | enter confirm | q abort")
            put(2, 0, "     " + header, curses.A_UNDERLINE)
            for row, idx in enumerate(view[top:top + page]):
                text, flag, _ = lines[idx]
                i = top + row
                base = f" {'[*]' if checked[idx] else '[ ]'} {text}"
                attr = checked_attr if checked[idx] else 0  # checked rows get the blue bar
                if i == cur:                                # cursor row: reverse video on top
                    attr |= curses.A_REVERSE
                put(3 + row, 0, base.ljust(w - 1), attr)    # pad so the bar spans the full row
                if flag:                                    # "<- moves" in its own yellow badge
                    put(3 + row, len(base) + 2, flag, flag_attr)
            found = f" | search '{query}': {len(view)} shown" if query else ""
            put(h - 1, 0, f" {sum(checked)} of {len(lines)} selected{found}")
            scr.refresh()

            key = scr.getch()
            if key in (curses.KEY_UP, ord("k")):
                cur = max(cur - 1, 0)
            elif key in (curses.KEY_DOWN, ord("j")):
                cur = min(cur + 1, last)
            elif key == curses.KEY_PPAGE:
                cur = max(cur - page, 0)
            elif key == curses.KEY_NPAGE:
                cur = min(cur + page, last)
            elif key == curses.KEY_HOME:
                cur = 0
            elif key == curses.KEY_END:
                cur = last
            elif key == ord(" "):                    # toggle [ ] <-> [*]
                if view:
                    checked[view[cur]] = not checked[view[cur]]
            elif key == ord("a"):                    # check every row currently shown
                everything = len(view) == len(lines)         # no search active: that is every network
                if not everything or confirm(scr, h - 1, w - 1, f"Check all {len(view)} networks to {verb}? [y/N] "):
                    for idx in view:
                        checked[idx] = True
            elif key == ord("n"):                    # uncheck every row currently shown
                for idx in view:
                    checked[idx] = False
            elif key == ord("/"):                    # search: show only rows containing the text
                typed = read_query(scr, h - 1, w - 1)
                if typed is not None:
                    query = typed if typed.strip() else ""
                    view = [i for i, (_, _, skey) in enumerate(lines) if query.lower() in skey.lower()]
                    cur = top = 0
            elif key in (10, 13, curses.KEY_ENTER):  # confirm
                return {i + 1 for i, c in enumerate(checked) if c}
            elif key == ord("q"):                    # main() turns this into a clean abort
                raise KeyboardInterrupt

    return curses.wrapper(run)                       # wrapper restores the terminal even on errors


def recap(table, picked, limit=8):
    # One-line summary of the picked MACs for the scrollback (the checklist screen vanishes)
    macs = [table[i - 1][0] for i in sorted(picked)]
    if not macs:
        return "none"
    more = f" (+{len(macs) - limit} more)" if len(macs) > limit else ""
    return ", ".join(macs[:limit]) + more


def ask(prompt, table, preselected=(), verb="select"):
    # Show the networks and return the 1-based numbers the user picked. Rows in
    # `preselected` start checked (remembered from the ignore list) and can be unchecked.
    # Uses the checkbox UI on a real terminal; with piped input, or if curses
    # can't start, it prints the numbered table and asks for typed numbers instead.
    if curses and sys.stdin.isatty() and sys.stdout.isatty():
        lines = [fmt_row(t) + (f"{t[0]} {t[1]}",) for t in table]
        try:
            picked = checklist(prompt, HEADER, lines, preselected, verb)
        except curses.error:
            picked = None
        if picked is not None:
            # The checklist screen vanishes on exit, so leave a recap in the scrollback
            print(f"{prompt}\n  -> {recap(table, picked)}")
            return picked
    return ask_typed(prompt, table, preselected)


def ask_typed(prompt, table, preselected):
    # Typed-number fallback. Rows marked * came from the ignore list; Enter alone keeps them.
    show(table, preselected)
    hint = "Enter = keep *, or " if preselected else ""
    while True:
        text = input(f"{prompt} [{hint}e.g. 1,3,5-7 / all / none]: ")
        if preselected and not text.strip():
            return set(preselected)
        try:
            return parse_sel(text, len(table))
        except ValueError:
            print("  Use numbers/ranges like 1,3,5-7 or 'none' / 'all'.")


def fmt_row(entry):
    # One network as (display text, flag). The flag marks devices whose sightings spread
    # over >300 m; it's kept separate so the checklist can color it.
    mac, ssid, cnt, spread = entry
    flag = "<- moves" if spread > 300 else ""
    return f"{mac:<17}  {ssid[:24]:<24} {cnt:>6}  {spread:>6.0f} m", flag


def show(table, preselected=()):
    # Print a numbered table of networks (used by the typed-number fallback);
    # a * after the number marks rows pre-checked from the ignore list
    print(f"\n{'#':>3}  {HEADER}")
    for i, entry in enumerate(table, 1):
        text, flag = fmt_row(entry)
        mark = "*" if i in preselected else " "
        print(f"{i:>3}{mark} {text}" + (f"  {flag}" if flag else ""))
    print()


def pool_devices(group):
    # Pool every row in the group: sighting count, positions, and SSIDs per MAC.
    # MACs are upper-cased so ignore-list entries match whatever case the export uses.
    pts, ssids, counts = defaultdict(list), defaultdict(Counter), Counter()
    for d in group:
        iM, iS = d["ix"]["MAC"], d["ix"].get("SSID")
        for r, p in d["valid"]:
            mac = r[iM].upper()
            counts[mac] += 1
            pts[mac].append(p)
            if iS is not None:
                ssids[mac][r[iS]] += 1
    return pts, ssids, counts


def clean_ssid(ssid):
    # SSIDs come off the air and can hold control characters (terminal escapes, newlines);
    # swap anything unprintable for "?" before it reaches the screen or the ignore list
    return "".join(c if c.isprintable() else "?" for c in ssid)


def rank_devices(pts, ssids, counts):
    # Every device as (mac, ssid, count, spread) tuples, most-seen first
    table = []
    for mac, cnt in counts.most_common():
        p = sorted(pts[mac])
        med = p[len(p) // 2]                            # median position
        dd = sorted(dist_m(med, q) for q in p)
        spread = dd[int(0.9 * (len(dd) - 1))]           # p90, ignores GPS outliers
        ssid = ssids[mac].most_common(1)[0][0] if ssids[mac] else ""
        table.append((mac, clean_ssid(ssid) or "<hidden>", cnt, spread))
    return table


def ask_scrub(table, radius, remembered):
    # Question 1: devices that mark a place to scrub (home, work...). Every device is listed,
    # most-seen first; rows remembered in the ignore list start checked. Returns the set of
    # MACs checked.
    pre = {i for i, t in enumerate(table, 1) if t[0] in remembered}
    scrub = ask(f"[1/2] SCRUB: check networks near a place to remove (all data within {radius:.0f} m)",
                table, pre, "scrub")

    # A device that moves more than the radius would carve out its whole route; confirm first
    wide = sorted(i for i in scrub if table[i - 1][3] > radius)
    if wide:
        print("\n  Warning: these move more than the radius; scrubbing carves out their whole track:")
        for i in wide:
            mac, ssid, _, spread = table[i - 1]
            print(f"    {mac}  {ssid}  ({spread:.0f} m spread)")
        if input("  Scrub them anyway? [y/N] ").strip().lower() != "y":
            scrub -= set(wide)
            print("  Dropped them from the scrub list; you can filter them in the next step.")
    return {table[i - 1][0] for i in scrub}


def ask_filter(full, scrub_macs, remembered):
    # Question 2: every device not already scrubbed, most-seen first, so anything can be
    # picked (travel routers etc.). Rows remembered in the ignore list start checked.
    # Returns the set of MACs checked.
    rest = [t for t in full if t[0] not in scrub_macs]
    if not rest:
        return set()
    pre = {i for i, t in enumerate(rest, 1) if t[0] in remembered}
    pick = ask("[2/2] FILTER: check networks to drop (only that device's own rows)", rest, pre, "filter")
    return {rest[i - 1][0] for i in pick}               # positions in rest -> MACs


def build_scrub_zone(scrub_macs, pts, group, radius):
    # Returns near_anchor(p): True if point p is within `radius` meters of anywhere
    # a scrub device was seen.

    # Anchors: where scrub devices were seen, deduped on a ~25 m grid.
    g = 25 / M_PER_DEG
    anchors = {(round(la / g), round(lo / g)): (la, lo)
               for m in scrub_macs for la, lo in pts[m]}.values()
    # Bucket anchors into cells at least one radius wide, so a point only has to be
    # compared against anchors in its own and the 8 neighboring cells
    cell_lat = radius * 1.01 / M_PER_DEG
    max_lat = max((abs(p[0]) for d in group for _, p in d["valid"]), default=0)
    cell_lon = radius * 1.01 / (M_PER_DEG * max(math.cos(math.radians(min(max_lat, 89))), 0.01))
    grid = defaultdict(list)
    for pt in anchors:
        grid[(math.floor(pt[0] / cell_lat), math.floor(pt[1] / cell_lon))].append(pt)

    def near_anchor(p):
        if not grid:                                    # nothing was checked for scrubbing
            return False
        cy, cx = math.floor(p[0] / cell_lat), math.floor(p[1] / cell_lon)
        return any(dist_m(p, q) <= radius
                   for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                   for q in grid.get((cy + dy, cx + dx), ()))

    return near_anchor


def split_rows(d, filt, scrub_macs, near_anchor):
    # Sort one file's rows into kept vs dropped.
    # Returns (kept rows, number scrubbed, number filtered).
    iM = d["ix"]["MAC"]
    kept, n_scrub, n_filt = [], 0, 0
    for r, p in d["valid"]:
        # Matching is by BSSID (the MAC column, upper-cased) only. SSID appears in the table
        # but is never compared. Checked MACs are dropped by exact match, so sibling BSSIDs of
        # one network (dual-band, mesh, travel routers) must each be checked on their own.
        # SCRUB also drops any row, from any device, within the radius of where the
        # checked devices were seen.
        mac = r[iM].upper()
        if mac in filt or mac in scrub_macs:            # the chosen devices themselves
            n_filt += mac in filt
            n_scrub += mac in scrub_macs
        elif near_anchor(p):                            # any other device near a scrub spot
            n_scrub += 1
        else:
            kept.append(r)
    return kept, n_scrub, n_filt


def write_cleaned(d, kept):
    # Back up the original, then replace it with the cleaned rows
    shutil.copy2(d["path"], d["bak"])  # original preserved before anything is touched
    # Write to a temp file in the same folder, then replace atomically
    fd, tmp = tempfile.mkstemp(dir=d["path"].parent, suffix=".tmp")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        if d["preamble"]:
            f.write(d["preamble"])
        w = csv.writer(f)
        w.writerow(d["cols"])
        w.writerows(kept)
    os.replace(tmp, d["path"])


def load_ignore(path):
    # Read the ignore list into {BSSID (upper case): (action, ssid)} and count unusable lines.
    # Comment (#) and blank lines are skipped; if a BSSID is listed twice, scrub wins.
    entries, bad = {}, 0
    if not path.is_file():
        return entries, bad
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        lines = (ln for ln in f if ln.strip() and not ln.lstrip().startswith("#"))  # this is what ignores comments in the ignore.txt file
        for row in csv.reader(lines):
            action = row[0].strip().lower()
            if action == "action":                      # the header row
                continue
            if len(row) < 2 or not row[1].strip() or action not in ACTIONS:
                bad += 1
                continue
            mac = row[1].strip().upper()
            if entries.get(mac, ("",))[0] != "scrub":   # scrub is never downgraded to filter
                entries[mac] = (action, row[2] if len(row) > 2 else "")
    return entries, bad


def report_ignore(path, known, bad, counts):
    # Say what the ignore list contributed. Entries for devices missing from this batch
    # can't be shown or applied, so they're counted separately.
    if bad:
        print(f"  ignore list: skipped {bad} unusable line(s) in {path}")
    if known:
        n_scrub = sum(1 for action, _ in known.values() if action == "scrub")
        absent = sum(1 for mac in known if mac not in counts)
        print(f"  ignore list: {len(known)} {'entry' if len(known) == 1 else 'entries'} from {path} ({n_scrub} scrub, "
              f"{len(known) - n_scrub} filter, {absent} not seen in this batch); pre-checked below")


def new_entries(chosen, known):
    # Picks not already on the ignore list (a scrub pick upgrades an existing filter entry)
    return [(act, mac, ssid) for act, mac, ssid in chosen
            if mac not in known or (act == "scrub" and known[mac][0] == "filter")]


def save_ignore(path, fresh):
    # Append entries to the ignore list. Existing lines are never rewritten, so hand edits
    # and comments survive. A new file gets a header and is created private (0600).
    size = path.stat().st_size if path.exists() else 0
    lead = ""
    if size:                                            # don't glue onto a last line lacking a newline
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            lead = "" if f.read(1) == b"\n" else "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", newline="", encoding="utf-8") as f:
        f.write(lead if size else IGNORE_HEADER)
        csv.writer(f, lineterminator="\n").writerows(fresh)


def remember(path, full, scrub_macs, filt, known, dry_run):
    # Add this run's picks to the ignore list and say what happened
    names = {mac: ("" if ssid == "<hidden>" else ssid) for mac, ssid, _, _ in full}
    chosen = ([("scrub", m, names[m]) for m in sorted(scrub_macs)]
              + [("filter", m, names[m]) for m in sorted(filt)])
    fresh = new_entries(chosen, known)
    if not fresh:
        return
    if dry_run:
        print(f"  dry-run: would add {len(fresh)} new {'entry' if len(fresh) == 1 else 'entries'} to {path}")
        return
    save_ignore(path, fresh)
    print(f"  ignore list: added {len(fresh)} new {'entry' if len(fresh) == 1 else 'entries'} to {path}")


def curate(group, a):
    # Curate one group of datasets (the whole batch, or one file with --per-file):
    # rank devices, ask what to scrub/filter, then back up and rewrite each file.
    pts, ssids, counts = pool_devices(group)

    # Name a single file, otherwise just count them (hundreds of names would clutter the output)
    label = group[0]["path"].name if len(group) == 1 else f"{len(group)} files"
    print(f"\n== {label}")
    print(f"{sum(d['nrows'] for d in group)} rows, {len(counts)} unique devices, "
          f"{sum(d['no_gps'] for d in group)} rows with no GPS (will be dropped)")

    full = rank_devices(pts, ssids, counts)
    if not full:
        print("No usable rows; nothing to do.")
        return

    # Devices remembered from earlier runs start checked in the tables below
    known, bad = load_ignore(a.ignore_file)
    report_ignore(a.ignore_file, known, bad, counts)
    remembered = {kind: {m for m, (act, _) in known.items() if act == kind} for kind in ACTIONS}

    scrub_macs = ask_scrub(full, a.radius, remembered["scrub"])
    filt = ask_filter(full, scrub_macs, remembered["filter"])
    near_anchor = build_scrub_zone(scrub_macs, pts, group, a.radius)

    # Apply to each file: drop filtered/scrub devices and anything near a scrub spot,
    # then back up the original and swap the cleaned file in. Prints one summary
    # line for the whole group instead of one line per file.
    print()
    action = "dry-run" if a.dry_run else "done"
    totals = Counter()
    for d in group:
        kept, n_scrub, n_filt = split_rows(d, filt, scrub_macs, near_anchor)
        if not a.dry_run:
            write_cleaned(d, kept)
        totals.update(kept=len(kept), scrubbed=n_scrub, filtered=n_filt, no_gps=d["no_gps"])
    print(f"  {action:<7} {label}: kept {totals['kept']} | scrubbed {totals['scrubbed']} "
          f"| filtered {totals['filtered']} | no-gps {totals['no_gps']}")
    remember(a.ignore_file, full, scrub_macs, filt, known, a.dry_run)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="a WiGLE CSV, or a directory of them")
    ap.add_argument("-r", "--radius", type=float, default=300, help="scrub radius in meters")
    ap.add_argument("-R", "--recursive", action="store_true", help="also search subdirectories")
    ap.add_argument("--per-file", action="store_true",
                    help="ask the questions separately for each CSV instead of once for the batch")
    ap.add_argument("--ignore-file", type=Path, default=Path("ignore.txt"),
                    help="remembered scrub/filter devices (default: ignore.txt in the current directory)")
    ap.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    a = ap.parse_args()

    found = find_csvs(a.path, a.recursive)
    print(f"Found {len(found)} CSV file(s) in {a.path}")
    datasets = [d for d in (load(f) for f in found) if d]
    if not datasets:
        sys.exit("Nothing to process.")

    try:
        for group in ([[d] for d in datasets] if a.per_file else [datasets]):
            curate(group, a)
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nAborted. Nothing further was modified (files already marked 'done' were processed).")

    if not a.dry_run:
        print("\nOriginals are saved as *.csv.bak and contain the UNSCRUBBED data: "
              "keep them out of any upload or sync folder.")


if __name__ == "__main__":
    main()
