# WiGLE Sanitizer

![flake8 workflow](https://github.com/straysheep-dev/wigle-sanitizer/actions/workflows/flake8.yml/badge.svg) ![bandit workflow](https://github.com/straysheep-dev/wigle-sanitizer/actions/workflows/bandit.yml/badge.svg)

Prepare WiGLE CSVs for upload. Stateless, standard libraries only.

Point it at a file or a directory; all CSVs found are pooled, all networks are listed and you're asked two questions:

1. Locations to filter: drop every row within R meters of each entry selected here
2. Entries to filter: drop only that entry's own rows (travel routers etc.)

Answer with the checklist (arrows or j/k to move, space to check, enter to confirm); without a terminal it falls back to typing numbers.

This matches and operates on BSSIDs (MACs) only when writing the results, it does not consider ESSIDs.

Each CSV is backed up to <name>.csv.bak and rewritten in place, ready to upload. Files that already have a .bak are skipped; re-runs can't clobber your originals.

```
usage: wigle_sanitizer.py [-h] [-r RADIUS] [-R] [--per-file] [--ignore-file IGNORE_FILE] [--dry-run] path

positional arguments:
  path                  a WiGLE CSV, or a directory of them

options:
  -h, --help            show this help message and exit
  -r, --radius RADIUS   scrub radius in meters
  -R, --recursive       also search subdirectories
  --per-file            ask the questions separately for each CSV instead of once for the batch
  --ignore-file IGNORE_FILE
                        remembered scrub/filter devices (default: ignore.txt in the current directory)
  --dry-run             report counts, write nothing
```

## License

[MIT](./LICENSE)

> [!NOTE]
> **AI-assisted Authorship**
>
> The following models were used to draft the majority of the code and comments. Everything has been reviewed and tested by an operator.
>
> `Claude:claude-sonnet-5-5`