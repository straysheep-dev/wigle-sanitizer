# WiGLE Sanitizer

![flake8 workflow](https://github.com/straysheep-dev/wigle-sanitizer/actions/workflows/flake8.yml/badge.svg) ![bandit workflow](https://github.com/straysheep-dev/wigle-sanitizer/actions/workflows/bandit.yml/badge.svg)

Prepare WiGLE CSVs for upload. Standard library only; the only state is the optional [ignore list](#ignore-list).

Point it at a file or a directory; all CSVs found are pooled, every network is listed (most-seen first) and you choose what to filter out, in two ways:

1. **Filter by zone**: drop every row within R meters of each network selected here (home, work...), from any device
2. **Filter by device**: drop only that network's own rows (travel routers etc.) These are highlighted with `<- moves`

Answer by selecting which networks to filter by zone or by device; without a terminal it falls back to typing numbers.

| Key | Action |
| --- | --- |
| up / down, `j` / `k` | move |
| space | check / uncheck |
| `/` | search (only matching rows are shown), use with `a` or `n` |
| `a` / `n` | check / uncheck everything shown (`a` asks first when no search is active) |
| enter | confirm |
| `q` | abort |

Only BSSID (MAC) is used to match. SSIDs are displayed to help you recognize networks, but are never compared. A network with several BSSIDs (dual-band, mesh, travel routers) needs each BSSID checked on its own.

## Output

Each CSV is moved to `<name>.csv.bak` and a cleaned file is written in its place with the unwanted rows removed. The `.bak` only appears once the cleaned file is complete, so a `.bak` never sits beside an unsanitized original. Files that already have a `.bak` are skipped, so re-runs can't clobber your originals. Rows without a usable GPS fix are dropped too. Everything the tool writes (cleaned CSVs, `.bak`, combined file, ignore list) is mode `0600`.

After the run, every sanitized CSV in the folder is merged into a single file for upload: `wigle.combined.csv` in the input folder (or `--output PATH`; `--no-combine` skips this step). WiGLE's web upload takes one file at a time, so this is the file to upload.

Last, the combined file is checked against your [ignore list](#ignore-list). A `.bak` means "processed", not "filtered for everything on your list", so a file handled in an earlier run, before you added a device can still carry that device's rows. Any row whose BSSID is on the list is reported with the file it came from, and the run exits with status `1`.

> [!WARNING]
> The `.bak` files contain the **unfiltered** data. Keep them out of any upload or sync folder.

## Ignore list

Networks you pick are remembered in `ignore.txt` (current directory, or `--ignore-file PATH`), one `action,bssid,ssid` line per device, where action is `zone` or `device`. On the next run those networks start checked, so you keep the final say.
- The file is append-only, created with mode `0600`, and edited freely by hand (lines starting with `#` are ignored).
- `.gitignore` prevents it from being committed.

## Usage

```
usage: wigle_sanitizer.py [-h] [-r RADIUS] [-R] [--per-file] [--ignore-file IGNORE_FILE]
                          [-o OUTPUT] [--no-combine] [--dry-run]
                          path

positional arguments:
  path                  a WiGLE CSV, or a directory of them

options:
  -h, --help            show this help message and exit
  -r, --radius RADIUS   zone radius in meters
  -R, --recursive       also search subdirectories
  --per-file            ask the questions separately for each CSV instead of once for the batch
  --ignore-file IGNORE_FILE
                        remembered zone/device filters (default: ignore.txt in the current
                        directory)
  -o, --output OUTPUT   combined upload file, must end in .combined.csv (default:
                        wigle.combined.csv in the input folder)
  --no-combine          don't write the combined file
  --dry-run             report counts, write nothing
```

## Limitations

- This is a best-effort tool: distances use rough math rather than a geo library, and deciding what to filter by zone or by device is a judgment call based on your own threat model.
- Filtering removes what you select. A nearby network you didn't check is only caught if it falls within the zone radius.
- The default radius is 300 m. A zone network that moves more than the radius (a phone hotspot, say) carves out its whole route, so the tool asks before doing that.

## License

[MIT](./LICENSE)

> [!NOTE]
> **AI-assisted Authorship**
>
> The following models were used to draft the majority of the code and comments. Everything has been reviewed and tested by an operator.
>
> `Claude:claude-sonnet-5-5`
