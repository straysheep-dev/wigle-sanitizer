#!/usr/bin/env python3

"""
Prepare WiGLE CSVs for upload. Standard library only.

# SPDX-License-Identifier: MIT
# Assisted-by: Claude:claude-sonnet-5-5

Point it at a file or a directory; all CSVs found are pooled, every network is listed (most-seen
first) and you choose what to filter out, in two ways:

  1. Filter by zone: drop every row within R meters of each network selected here (home, work...)
  2. Filter by device: drop only that network's own rows (travel routers etc.)

Answer with the checklist (arrows or j/k to move, space to check, / to search, a to check
everything shown, enter to confirm); without a terminal it falls back to typing numbers.

This matches and operates on BSSIDs (MACs) only when writing the results, it does not consider ESSIDs.

Devices you pick are remembered in ignore.txt (current directory, or --ignore-file PATH):
one line per device (action,bssid,ssid) where action is zone or device. On the next run
those entries start checked in the tables, so you keep the final say. The file is
append-only and names your networks (home, work...): keep it out of git and upload folders.

Each CSV is moved to <name>.csv.bak and a cleaned file is written in its place, ready to upload.
The .bak only appears once the cleaned file is complete, so a .bak never sits beside an
unsanitized original. Files that already have a .bak are skipped; re-runs can't clobber your
originals. Everything the tool writes is mode 0600.

Afterwards every sanitized CSV in the folder (any with a .bak beside it, from this or earlier
runs) is merged into one file for upload: wigle.combined.csv in the input folder, or --output
PATH (must end in .combined.csv; --no-combine to skip). Pointing at a single file never merges.
*.combined.csv files are never read as inputs, and the combined file is rebuilt from scratch
each run, so re-running gives the same result. It takes the header line and column order of
the first file.

Last, the combined file is checked against the ignore list. Any row whose BSSID is on it
means that file was sanitized without the device checked (an earlier run, a hand-edited list,
a stray .bak): those rows are reported and the exit status is 1. Don't upload until resolved.
"""

import argparse
import csv
import math
import os
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

# Mode for everything the tool writes (cleaned CSVs, .bak, combined file, ignore list): owner only
PRIVATE = 0o600

# Columns a file needs to count as a WiGLE CSV
REQUIRED_COLS = {"MAC", "CurrentLatitude", "CurrentLongitude"}

# The combined upload file is recognised by this name ending, so it can live in the input
# folder without ever being read as input
COMBINED_SUFFIX = ".combined.csv"
COMBINED_NAME = "wigle" + COMBINED_SUFFIX

# Column header and row layout shared by the checklist and the typed-number fallback
HEADER = f"{'MAC':<17}  {'SSID':<24} {'seen':>6}  {'spread':>8}"

# The two things the ignore list can say about a device, and the header written to a new list
ACTIONS = ("zone", "device")
IGNORE_HEADER = (
    "# wigle-sanitizer ignore list. Holds your network identities (home, work...): never commit or share.\n"
    "# zone   = filter by zone: drop this device AND everything within the radius of where it was seen\n"
    "# device = filter by device: drop only this device's own rows\n"
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
    # Combined output files are never inputs, whatever folder they were dropped in
    return sorted(f for f in dir if f.is_file() and f.suffix.lower() == ".csv"
                  and not f.name.lower().endswith(COMBINED_SUFFIX))


def read_csv(path):
    # Return (preamble, cols, rows): the optional WigleWifi line, the header, then the data rows
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
    return preamble, cols, rows


def load(path):
    """Return a dataset dict, or None (with a note) if the file isn't a usable WiGLE CSV."""
    bak = path.with_name(path.name + ".bak")
    if bak.exists():
        print(f"  skip  {path.name}: {bak.name} already exists (already processed?)")
        return None
    preamble, cols, rows = read_csv(path)
    if not REQUIRED_COLS <= set(cols):
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
    # words used in that question: "filter by zone" or "filter by device").
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
    # MACs go through norm_mac() so ignore-list entries match whatever case the export uses.
    pts, ssids, counts = defaultdict(list), defaultdict(Counter), Counter()
    for d in group:
        iM, iS = d["ix"]["MAC"], d["ix"].get("SSID")
        for r, p in d["valid"]:
            mac = norm_mac(r[iM])
            counts[mac] += 1
            pts[mac].append(p)
            if iS is not None:
                ssids[mac][r[iS]] += 1
    return pts, ssids, counts


def clean_text(text):
    # SSIDs come off the air and any CSV field can be hand-made, so they can hold control
    # characters (terminal escapes, newlines); swap anything unprintable for "?" before it
    # reaches the screen or the ignore list
    return "".join(c if c.isprintable() else "?" for c in text)


def norm_mac(mac):
    # The one way a MAC column value becomes a key: trimmed, cleaned, upper-cased. Used for
    # matching, display and the ignore list alike, so what you see is what is compared. The
    # rows written out keep their original text.
    return clean_text(mac.strip()).upper()


def rank_devices(pts, ssids, counts):
    # Every device as (mac, ssid, count, spread) tuples, most-seen first
    table = []
    for mac, cnt in counts.most_common():
        p = sorted(pts[mac])
        med = p[len(p) // 2]                            # median position
        dd = sorted(dist_m(med, q) for q in p)
        spread = dd[int(0.9 * (len(dd) - 1))]           # p90, ignores GPS outliers
        ssid = ssids[mac].most_common(1)[0][0] if ssids[mac] else ""
        table.append((mac, clean_text(ssid) or "<hidden>", cnt, spread))
    return table


def ask_zone(table, radius, remembered):
    # Question 1: devices that mark a place to filter out (home, work...). Every device is
    # listed, most-seen first; rows remembered in the ignore list start checked. Returns the
    # set of MACs checked.
    pre = {i for i, t in enumerate(table, 1) if t[0] in remembered}
    zone = ask(f"[1/2] FILTER by zone: check networks marking a place to exclude (all data within {radius:.0f} m)",
               table, pre, "filter by zone")

    # A device that moves more than the radius would carve out its whole route; confirm first
    wide = sorted(i for i in zone if table[i - 1][3] > radius)
    if wide:
        print("\n  Warning: these move more than the radius; filtering by zone carves out their whole track:")
        for i in wide:
            mac, ssid, _, spread = table[i - 1]
            print(f"    {mac}  {ssid}  ({spread:.0f} m spread)")
        if input("  Filter them by zone anyway? [y/N] ").strip().lower() != "y":
            zone -= set(wide)
            print("  Dropped them from the zone list; you can filter them by device in the next step.")
    return {table[i - 1][0] for i in zone}


def ask_device(full, zone_macs, remembered):
    # Question 2: every device not already filtered by zone, most-seen first, so anything can
    # be picked (travel routers etc.). Rows remembered in the ignore list start checked.
    # Returns the set of MACs checked.
    rest = [t for t in full if t[0] not in zone_macs]
    if not rest:
        return set()
    pre = {i for i, t in enumerate(rest, 1) if t[0] in remembered}
    pick = ask("[2/2] FILTER by device: check networks to drop (only that device's own rows)",
               rest, pre, "filter by device")
    return {rest[i - 1][0] for i in pick}               # positions in rest -> MACs


def build_zone(zone_macs, pts, group, radius):
    # Returns near_anchor(p): True if point p is within `radius` meters of anywhere
    # a zone device was seen.

    # Anchors: where zone devices were seen, deduped on a ~25 m grid.
    g = 25 / M_PER_DEG
    anchors = {(round(la / g), round(lo / g)): (la, lo)
               for m in zone_macs for la, lo in pts[m]}.values()
    # Bucket anchors into cells at least one radius wide, so a point only has to be
    # compared against anchors in its own and the 8 neighboring cells
    cell_lat = radius * 1.01 / M_PER_DEG
    max_lat = max((abs(p[0]) for d in group for _, p in d["valid"]), default=0)
    cell_lon = radius * 1.01 / (M_PER_DEG * max(math.cos(math.radians(min(max_lat, 89))), 0.01))
    grid = defaultdict(list)
    for pt in anchors:
        grid[(math.floor(pt[0] / cell_lat), math.floor(pt[1] / cell_lon))].append(pt)

    def near_anchor(p):
        if not grid:                                    # nothing was checked for filtering by zone
            return False
        cy, cx = math.floor(p[0] / cell_lat), math.floor(p[1] / cell_lon)
        return any(dist_m(p, q) <= radius
                   for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                   for q in grid.get((cy + dy, cx + dx), ()))

    return near_anchor


def split_rows(d, device_macs, zone_macs, near_anchor):
    # Sort one file's rows into kept vs dropped.
    # Returns (kept rows, number dropped by zone, number dropped by device).
    iM = d["ix"]["MAC"]
    kept, n_zone, n_device = [], 0, 0
    for r, p in d["valid"]:
        # Matching is by BSSID (the MAC column, via norm_mac) only. SSID appears in the table
        # but is never compared. Checked MACs are dropped by exact match, so sibling BSSIDs of
        # one network (dual-band, mesh, travel routers) must each be checked on their own.
        # Filtering by zone also drops any row, from any device, within the radius of where the
        # checked devices were seen.
        mac = norm_mac(r[iM])
        if mac in device_macs or mac in zone_macs:      # the chosen devices themselves
            n_device += mac in device_macs
            n_zone += mac in zone_macs
        elif near_anchor(p):                            # any other device inside a zone
            n_zone += 1
        else:
            kept.append(r)
    return kept, n_zone, n_device


def write_csv(dest, preamble, cols, rows, backup=None):
    # Write to a temp file in the destination folder, then swap it in atomically. This
    # writes filtered data to a tmp file *before* moving the unfiltered data to a .bak
    # file. Finally the tmp file is moved to replace the original csv. This prevents
    # rare cases where crashes or disk failures would leave behind an unfiltered original.
    # Additional checks are in place to catch that either way.
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    try:
        os.chmod(tmp, PRIVATE)                          # mkstemp already does this on POSIX
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            if preamble:
                f.write(preamble)
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)
    except BaseException:
        os.unlink(tmp)                                  # never leave a half-written .tmp behind
        raise
    if backup:
        os.replace(dest, backup)
        os.chmod(backup, PRIVATE)
    os.replace(tmp, dest)


def write_cleaned(d, kept):
    # Replace the file with the cleaned rows, moving the original to its .bak
    write_csv(d["path"], d["preamble"], d["cols"], kept, backup=d["bak"])


def load_sanitized(found):
    # Read every already-sanitized CSV (one with a .bak beside it) as (path, preamble, cols, rows)
    parts = []
    for f in found:
        if f.with_name(f.name + ".bak").exists():
            preamble, cols, rows = read_csv(f)
            if REQUIRED_COLS <= set(cols):
                parts.append((f, preamble, cols, rows))
    return parts


def merge_parts(parts, known):
    # Stack the rows of every part under the first file's columns. Other files are mapped by
    # column name so a different column order still lines up (columns the first lacks are
    # dropped). Also returns hits: {ignore-list MAC: {file name: rows}} for every row whose
    # BSSID is on the ignore list.
    first, _, cols, _ = parts[0]
    mac_at = cols.index("MAC")
    rows, hits = [], defaultdict(Counter)
    for f, _, c, data in parts:
        ix = {name: i for i, name in enumerate(c)}
        pos = [ix.get(name) for name in cols]
        if set(c) != set(cols):
            print(f"  note: {f.name} has different columns than {first.name}; mapped by name")
        for r in data:
            row = [r[i] if i is not None and i < len(r) else "" for i in pos]
            rows.append(row)
            mac = norm_mac(row[mac_at])
            if mac in known:
                hits[mac][f.name] += 1
    if len({p for _, p, _, _ in parts}) > 1:
        print(f"  note: files have different WigleWifi header lines; using the one from {first.name}")
    return rows, hits


def report_ignored(hits, known, out, n_rows):
    # Final check on what is about to be uploaded: say which ignore-list devices still have
    # rows in the combined file, and in which files. Always prints the outcome, so a clean
    # result is confirmed, not just silent. Returns the number of flagged rows.
    total = sum(sum(per_file.values()) for per_file in hits.values())
    if not known:
        print("  ignore check: the ignore list is empty or missing; nothing to check against")
        return 0
    if not total:
        print(f"  ignore check: 0 hits ({len(known)} ignore-list {'entry' if len(known) == 1 else 'entries'} "
              f"checked against {n_rows} rows)")
        return 0
    print(f"\n  WARNING: {total} row(s) in {out.name} are from devices on your ignore list:")
    for mac, per_file in sorted(hits.items()):
        action, ssid = known[mac]
        print(f"    {action:<6} {mac}  {clean_text(ssid)}  "
              f"{sum(per_file.values())} row(s) in {', '.join(sorted(per_file))}")
    print("  Those files were sanitized without these devices checked (an earlier run, an edited\n"
          "  ignore list, or a stray .bak). Don't upload yet: put the original back\n"
          "  (mv <name>.csv.bak <name>.csv), re-run, and check them.")
    return total


def combine_sanitized(found, out, known, dry_run):
    # Merge every already-sanitized CSV into one file for upload. It works from what is on
    # disk, not from this run, so it can follow any number of earlier runs, and the same files
    # always give the same result: `out` is derived data and is replaced each time. The header
    # line and column order come from the first file. The result is then checked against the
    # ignore list `known`; returns how many rows matched it (0 = clean).
    parts = load_sanitized(found)
    if not parts:
        print("  combined: no sanitized CSVs (ones with a .bak beside them) found; nothing written")
        return 0
    rows, hits = merge_parts(parts, known)
    if not rows:
        print("  combined: no rows left after cleaning; nothing written")
        return 0
    if not dry_run:
        write_csv(out, parts[0][1], parts[0][2], rows)
    print(f"  combined: {'would write' if dry_run else 'wrote'} {len(rows)} rows "
          f"from {len(parts)} file(s) to {out}")
    return report_ignored(hits, known, out, len(rows))


def load_ignore(path):
    # Read the ignore list into {BSSID (upper case): (action, ssid)} and count unusable lines.
    # Comment (#) and blank lines are skipped; if a BSSID is listed twice, zone wins.
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
            mac = norm_mac(row[1])
            if entries.get(mac, ("",))[0] != "zone":    # zone is never downgraded to device
                entries[mac] = (action, row[2] if len(row) > 2 else "")
    return entries, bad


def report_ignore(path, known, bad, counts):
    # Say what the ignore list contributed. Entries for devices missing from this batch
    # can't be shown or applied, so they're counted separately.
    if bad:
        print(f"  ignore list: skipped {bad} unusable line(s) in {path}")
    if known:
        n_zone = sum(1 for action, _ in known.values() if action == "zone")
        absent = sum(1 for mac in known if mac not in counts)
        print(f"  ignore list: {len(known)} {'entry' if len(known) == 1 else 'entries'} from {path} ({n_zone} zone, "
              f"{len(known) - n_zone} device, {absent} not seen in this batch); pre-checked below")


def new_entries(chosen, known):
    # Picks not already on the ignore list (a zone pick upgrades an existing device entry)
    return [(act, mac, ssid) for act, mac, ssid in chosen
            if mac not in known or (act == "zone" and known[mac][0] == "device")]


def save_ignore(path, fresh):
    # Append entries to the ignore list. Existing lines are never rewritten, so hand edits
    # and comments survive. A new file gets a header and is created private (PRIVATE).
    size = path.stat().st_size if path.exists() else 0
    lead = ""
    if size:                                            # don't glue onto a last line lacking a newline
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            lead = "" if f.read(1) == b"\n" else "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, PRIVATE)
    with os.fdopen(fd, "a", newline="", encoding="utf-8") as f:
        f.write(lead if size else IGNORE_HEADER)
        csv.writer(f, lineterminator="\n").writerows(fresh)


def remember(path, full, zone_macs, device_macs, known, dry_run):
    # Add this run's picks to the ignore list and say what happened
    names = {mac: ("" if ssid == "<hidden>" else ssid) for mac, ssid, _, _ in full}
    chosen = ([("zone", m, names[m]) for m in sorted(zone_macs)]
              + [("device", m, names[m]) for m in sorted(device_macs)])
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
    # rank devices, ask what to filter by zone and by device, then back up and rewrite each file.
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

    zone_macs = ask_zone(full, a.radius, remembered["zone"])
    device_macs = ask_device(full, zone_macs, remembered["device"])
    near_anchor = build_zone(zone_macs, pts, group, a.radius)

    # Apply to each file: drop the chosen devices and anything inside a zone,
    # then back up the original and swap the cleaned file in. Prints one summary
    # line for the whole group instead of one line per file.
    print()
    action = "dry-run" if a.dry_run else "done"
    totals = Counter()
    for d in group:
        kept, n_zone, n_device = split_rows(d, device_macs, zone_macs, near_anchor)
        if not a.dry_run:
            write_cleaned(d, kept)
        totals.update(kept=len(kept), zone=n_zone, device=n_device, no_gps=d["no_gps"])
    print(f"  {action:<7} {label}: kept {totals['kept']} | by zone {totals['zone']} "
          f"| by device {totals['device']} | no-gps {totals['no_gps']}")
    remember(a.ignore_file, full, zone_macs, device_macs, known, a.dry_run)


def combined_path(path, output):
    # Where the combined file goes: --output, else inside the input folder. A single file is
    # only sanitized, never merged (returns None): a merge of one file would replace the
    # folder-wide combined file with a subset.
    # The name must end in COMBINED_SUFFIX or later runs would read it as an input.
    p = Path(path)
    if not p.is_dir():
        if output:
            sys.exit("--output only applies when the path is a folder; a single file is not merged.")
        return None
    out = output or p / COMBINED_NAME
    if not out.name.lower().endswith(COMBINED_SUFFIX):
        sys.exit(f"--output must end in {COMBINED_SUFFIX} so re-runs never read it as an input: {out}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="a WiGLE CSV, or a directory of them")
    ap.add_argument("-r", "--radius", type=float, default=300, help="zone radius in meters")
    ap.add_argument("-R", "--recursive", action="store_true", help="also search subdirectories")
    ap.add_argument("--per-file", action="store_true",
                    help="ask the questions separately for each CSV instead of once for the batch")
    ap.add_argument("--ignore-file", type=Path, default=Path("ignore.txt"),
                    help="remembered zone/device filters (default: ignore.txt in the current directory)")
    ap.add_argument("-o", "--output", type=Path,
                    help=f"combined upload file, must end in {COMBINED_SUFFIX} "
                         f"(default: {COMBINED_NAME} in the input folder)")
    ap.add_argument("--no-combine", action="store_true", help="don't write the combined file")
    ap.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
    a = ap.parse_args()

    found = find_csvs(a.path, a.recursive)
    out = combined_path(a.path, a.output)
    combining = out is not None and not a.no_combine
    print(f"Found {len(found)} CSV file(s) in {a.path}")
    datasets = [d for d in (load(f) for f in found) if d]
    if not datasets and not combining:
        sys.exit("Nothing to process.")
    if not datasets:
        print("Nothing new to sanitize.")

    try:
        for group in ([[d] for d in datasets] if a.per_file else [datasets] if datasets else []):
            curate(group, a)
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nAborted. Nothing further was modified (files already marked 'done' were processed).")

    flagged = 0
    if combining:
        print()
        known, _ = load_ignore(a.ignore_file)           # re-read: includes this run's new picks
        flagged = combine_sanitized(found, out, known, a.dry_run)

    if not a.dry_run:
        print("\nOriginals are saved as *.csv.bak and contain the UNFILTERED data: "
              "keep them out of any upload or sync folder.")
    if flagged:
        sys.exit(1)


if __name__ == "__main__":
    main()
