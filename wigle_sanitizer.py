#!/usr/bin/env python3

"""
Prepare WiGLE CSVs for upload. Stateless, standard libraries only.

# SPDX-License-Identifier: MIT
# Assisted-by: Claude:claude-sonnet-5-5

Point it at a file or a directory; all CSVs found are pooled, the top-N most-seen networks are listed
and you're asked two questions:

  1. Locations to filter: drop every row within R meters of each entry selected here
  2. Entries to filter: drop only that entry's own rows (travel routers etc.)

Answer with the checklist (arrows or j/k to move, space to check, enter to confirm); without a
terminal it falls back to typing numbers.

This matches and operates on BSSIDs (MACs) only when writing the results, it does not consider ESSIDs.

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

# Conversion factor from degrees to meters, which lets the script do  all its
# distance math without a geo library.
M_PER_DEG = 111320.0

# Column header and row layout shared by the checklist and the typed-number fallback
HEADER = f"{'MAC':<17}  {'SSID':<24} {'seen':>6}  {'spread':>8}"


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


def checklist(title, header, lines):  # noqa: C901
    # Full-screen checkbox list (curses). Returns the set of checked 1-based positions.
    # Keys: up/down or k/j move, space toggles, a = all, n = none, enter confirms, q aborts.
    # The C901 exemption is deliberate: a flat key-dispatch loop is many trivial branches,
    # not hard logic, and splitting it would scatter one small curses screen across functions.
    def run(scr):
        curses.curs_set(0)                          # hide the text cursor
        checked = [False] * len(lines)
        cur = top = 0                               # highlighted row / first visible row

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
            top = max(min(top, cur), cur - page + 1)    # scroll to keep the highlight visible
            scr.erase()
            put(0, 0, title, curses.A_BOLD)
            put(1, 0, "up/down or j/k move | space check | a all | n none | enter confirm | q abort")
            put(2, 0, "     " + header, curses.A_UNDERLINE)
            for row, (text, flag) in enumerate(lines[top:top + page]):
                i = top + row
                base = f" {'[*]' if checked[i] else '[ ]'} {text}"
                attr = checked_attr if checked[i] else 0    # checked rows get the blue bar
                if i == cur:                                # cursor row: reverse video on top
                    attr |= curses.A_REVERSE
                put(3 + row, 0, base.ljust(w - 1), attr)    # pad so the bar spans the full row
                if flag:                                    # "<- moves" in its own yellow badge
                    put(3 + row, len(base) + 2, flag, flag_attr)
            put(h - 1, 0, f" {sum(checked)} of {len(lines)} selected")
            scr.refresh()

            key = scr.getch()
            if key in (curses.KEY_UP, ord("k")):
                cur = max(cur - 1, 0)
            elif key in (curses.KEY_DOWN, ord("j")):
                cur = min(cur + 1, len(lines) - 1)
            elif key == curses.KEY_PPAGE:
                cur = max(cur - page, 0)
            elif key == curses.KEY_NPAGE:
                cur = min(cur + page, len(lines) - 1)
            elif key == curses.KEY_HOME:
                cur = 0
            elif key == curses.KEY_END:
                cur = len(lines) - 1
            elif key == ord(" "):                    # toggle [ ] <-> [*]
                checked[cur] = not checked[cur]
            elif key == ord("a"):
                checked = [True] * len(lines)
            elif key == ord("n"):
                checked = [False] * len(lines)
            elif key in (10, 13, curses.KEY_ENTER):  # confirm
                return {i + 1 for i, c in enumerate(checked) if c}
            elif key == ord("q"):                    # main() turns this into a clean abort
                raise KeyboardInterrupt

    return curses.wrapper(run)                       # wrapper restores the terminal even on errors


def ask(prompt, table):
    # Show the networks and return the 1-based numbers the user picked.
    # Uses the checkbox UI on a real terminal; with piped input, or if curses
    # can't start, it prints the numbered table and asks for typed numbers instead.
    picked = None
    if curses and sys.stdin.isatty() and sys.stdout.isatty():
        try:
            picked = checklist(prompt, HEADER, [fmt_row(t) for t in table])
            # The checklist screen vanishes on exit, so leave a recap in the scrollback
            print(f"{prompt}\n  -> " + (", ".join(table[i - 1][0] for i in sorted(picked)) or "none"))
        except curses.error:
            picked = None
    if picked is not None:
        return picked
    show(table)
    while True:
        try:
            return parse_sel(input(f"{prompt} [e.g. 1,3,5-7 / all / none]: "), len(table))
        except ValueError:
            print("  Use numbers/ranges like 1,3,5-7 or 'none' / 'all'.")


def fmt_row(entry):
    # One network as (display text, flag). The flag marks devices whose sightings spread
    # over >300 m; it's kept separate so the checklist can color it.
    mac, ssid, cnt, spread = entry
    flag = "<- moves" if spread > 300 else ""
    return f"{mac:<17}  {ssid[:24]:<24} {cnt:>6}  {spread:>6.0f} m", flag


def show(table):
    # Print a numbered table of networks (used by the typed-number fallback)
    print(f"\n{'#':>3}  {HEADER}")
    for i, entry in enumerate(table, 1):
        text, flag = fmt_row(entry)
        print(f"{i:>3}  {text}" + (f"  {flag}" if flag else ""))
    print()


def pool_devices(group):
    # Pool every row in the group: sighting count, positions, and SSIDs per MAC
    pts, ssids, counts = defaultdict(list), defaultdict(Counter), Counter()
    for d in group:
        iM, iS = d["ix"]["MAC"], d["ix"].get("SSID")
        for r, p in d["valid"]:
            counts[r[iM]] += 1
            pts[r[iM]].append(p)
            if iS is not None:
                ssids[r[iM]][r[iS]] += 1
    return pts, ssids, counts


def rank_devices(pts, ssids, counts, top):
    # Top-N most-seen devices as (mac, ssid, count, spread) tuples
    table = []
    for mac, cnt in counts.most_common(top):
        p = sorted(pts[mac])
        med = p[len(p) // 2]                            # median position
        dd = sorted(dist_m(med, q) for q in p)
        spread = dd[int(0.9 * (len(dd) - 1))]           # p90, ignores GPS outliers
        ssid = ssids[mac].most_common(1)[0][0] if ssids[mac] else ""
        table.append((mac, ssid or "<hidden>", cnt, spread))
    return table


def ask_scrub(table, radius):
    # Question 1: devices that mark a place to scrub (home, work...).
    # Returns the 1-based table numbers the user checked.
    scrub = ask(f"[1/2] SCRUB: check networks near a place to remove (all data within {radius:.0f} m)", table)

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
    return scrub


def ask_filter(table, scrub):
    # Question 2: of the networks not already scrubbed, devices to drop on their own
    # (travel routers etc.). Returns a set of MACs.
    rest = [t for i, t in enumerate(table, 1) if i not in scrub]
    if not rest:
        return set()
    pick = ask("[2/2] FILTER: check networks to drop (only that device's own rows)", rest)
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
        # Matching is by BSSID (the MAC column) only. SSID appears in the table but is never
        # compared. Checked MACs are dropped by exact string match, so sibling BSSIDs of one
        # network (dual-band, mesh, travel routers) must each be checked on their own.
        # SCRUB also drops any row, from any device, within the radius of where the
        # checked devices were seen.
        if r[iM] in filt or r[iM] in scrub_macs:        # the chosen devices themselves
            n_filt += r[iM] in filt
            n_scrub += r[iM] in scrub_macs
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


def curate(group, a):
    # Curate one group of datasets (the whole batch, or one file with --per-file):
    # rank devices, ask what to scrub/filter, then back up and rewrite each file.
    pts, ssids, counts = pool_devices(group)

    # Name a single file, otherwise just count them (hundreds of names would clutter the output)
    label = group[0]["path"].name if len(group) == 1 else f"{len(group)} files"
    print(f"\n== {label}")
    print(f"{sum(d['nrows'] for d in group)} rows, {len(counts)} unique devices, "
          f"{sum(d['no_gps'] for d in group)} rows with no GPS (will be dropped)")

    table = rank_devices(pts, ssids, counts, a.top)
    if not table:
        print("No usable rows; nothing to do.")
        return

    scrub = ask_scrub(table, a.radius)
    filt = ask_filter(table, scrub)
    scrub_macs = {table[i - 1][0] for i in scrub}
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="a WiGLE CSV, or a directory of them")
    ap.add_argument("-n", "--top", type=int, default=25, help="networks to list (default 25)")
    ap.add_argument("-r", "--radius", type=float, default=300, help="scrub radius in meters")
    ap.add_argument("-R", "--recursive", action="store_true", help="also search subdirectories")
    ap.add_argument("--per-file", action="store_true",
                    help="ask the questions separately for each CSV instead of once for the batch")
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
