---
name: check-wheels
description: Check whether a Python project's dependencies have prebuilt wheels for the platforms it ships to (Linux ARM64, Alpine/musl, Windows on ARM, macOS, free-threaded 3.14t, Pyodide, iOS/Android), using wheelgap. Use when a Docker or CI build is slow or fails compiling a Python package, when moving to ARM, Alpine or a new Python version, or when asked "will our dependencies install on X?"
license: MIT
---

# Check wheels with wheelgap

wheelgap reads a lockfile and reports, per package and target platform, whether pip/uv would install a prebuilt **wheel**, build from **source** (slow, needs a compiler), or find nothing (**missing**). It runs offline from lockfiles in about a second.

## Run it

From the project directory (it finds `uv.lock`, `poetry.lock`, `pdm.lock`, `pylock.toml`, `Pipfile.lock` or `requirements*.txt` on its own). No install needed:

```bash
uvx wheelgap -p 3.13 -t linux-aarch64,musl-aarch64 --json
```

Pick targets from what the project actually ships to:

- **A Dockerfile:** `uvx wheelgap --dockerfile Dockerfile --json` takes the Python and platform from the final stage.
- **A `[tool.wheelgap]` table in `pyproject.toml`:** run with no `-p`/`-t`.
- **Otherwise:** ask, or use `-t all` (the eight common platforms). `-p 3.14t` checks free-threaded Python.

`uvx wheelgap --list-targets` lists every target and alias.

## Read the result

- **Exit code:** `0` no gaps, `1` gaps, `2` bad input (read stderr).
- **In `--json`:** each `packages[]` entry has `results`: target label → `wheel`, `source`, `missing`, `unknown`, `n/a` or `bundled`. Read the label list from the top-level `targets` (labels are just the platform with one Python, `"3.13 linux-x86_64"` with several).
- **`unknown`** means a lookup failed; the package's `error` field says why. Don't report it as fine.
- **Why:** add `--explain` for each rejected wheel and the reason (glibc too new, wrong arch, abi3 on free-threaded, and so on).

## What to tell the user

1. Name the packages with gaps, per platform, with the reason from `--explain`.
2. Offer concrete fixes:
   - pin a version that ships the wheel;
   - use a `-slim` Debian base instead of Alpine;
   - add build dependencies to the Dockerfile on purpose;
   - `--fail-on missing` to accept source builds in CI.
3. To accept a known gap without hiding it, use `--ignore NAME` or `ignore = ["NAME"]` in `[tool.wheelgap]`.

wheelgap checks wheel availability only, not whether a package works without the GIL.

For a quick check of one popular package without running anything, use `https://wheelgap.github.io/tracker/data/packages/<normalized-name>.json`: the top 5,000 PyPI packages, scanned nightly.
