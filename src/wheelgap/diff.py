"""Compare two lockfiles: which packages got worse on a target, and which new packages have gaps.

    python -m wheelgap.diff old/uv.lock uv.lock -p 3.13 -t linux-aarch64,musl-aarch64

Both sides are checked with `check_lockfile()` and packages are matched by canonical name. Exit 1 if
anything got worse (a new package with a gap counts), 0 otherwise, and 2 on bad input.

How statuses rank, per target:

- `wheel`, `bundled` and `n/a` are all fine and rank the same; `source` is worse than them, and `missing`
  is worse than `source`.
- `unknown` (the package couldn't be checked) isn't on that scale, because it says nothing about what pip
  would do. So a change is never called "not worse" on a guess: anything → `unknown` is worse, and
  `unknown` → `source` or `missing` is worse too. Only `unknown` → `wheel`, `bundled` or `n/a` is not.
  `unknown` on both sides is unchanged, and a note on stderr names those packages.
- A package listed more than once (a uv.lock fork) takes its worst status on each target, with `unknown`
  beating `missing`, and ignoring `n/a` unless every entry is `n/a`.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

from packaging.utils import canonicalize_name

from .api import Result, check_lockfile, make_targets
from .check import Status
from .cli import CELL
from .targets import Target

# Higher is worse. UNKNOWN only orders entries of the same package on one target (see the module docstring).
RANK = {
    Status.WHEEL: 0,
    Status.BUNDLED: 0,
    Status.NOT_NEEDED: 0,
    Status.SOURCE: 1,
    Status.MISSING: 2,
    Status.UNKNOWN: 3,
}
GAPS = (Status.SOURCE, Status.MISSING, Status.UNKNOWN)


@dataclass
class Package:
    """One package name on one side: its version(s) and its worst status per target."""

    name: str
    version: str
    statuses: dict[Target, Status]


def merge(results: list[Result], targets: list[Target]) -> dict[str, Package]:
    """The results keyed by canonical name, with entries for the same name combined."""
    grouped: dict[str, list[Result]] = {}
    for r in results:
        grouped.setdefault(canonicalize_name(r.name), []).append(r)
    merged = {}
    for key, rs in grouped.items():
        statuses = {}
        for t in targets:
            found = [r.statuses.get(t, Status.NOT_NEEDED) for r in rs]
            needed = [s for s in found if s is not Status.NOT_NEEDED]
            statuses[t] = max(needed, key=RANK.__getitem__) if needed else Status.NOT_NEEDED
        versions = dict.fromkeys(r.version or "?" for r in rs)
        merged[key] = Package(rs[0].name, ", ".join(versions), statuses)
    return merged


def worse(old: Status, new: Status) -> bool:
    if new is Status.UNKNOWN:
        return old is not Status.UNKNOWN
    if old is Status.UNKNOWN:
        return new in GAPS
    return RANK[new] > RANK[old]


def compare(old: list[Result], new: list[Result], targets: list[Target]) -> tuple[list[str], int, int, list[str]]:
    """The report lines, how many packages got worse, how many new packages have gaps, and the names
    that couldn't be checked on either side."""
    before, after = merge(old, targets), merge(new, targets)
    multi_py = len({t.python for t in targets}) > 1
    lines: list[str] = []
    n_worse = n_new = 0
    unchecked = []
    for key, pkg in after.items():
        if key not in before:
            gaps = [t for t in targets if pkg.statuses[t] in GAPS]
            if gaps:
                n_new += 1
                lines.append(f"{pkg.name} {pkg.version} (new)")
                lines += [f"  {t.label(multi_py)}: {CELL[pkg.statuses[t]][0]}" for t in gaps]
            continue
        was = before[key]
        changed = [t for t in targets if worse(was.statuses[t], pkg.statuses[t])]
        if any(was.statuses[t] is Status.UNKNOWN and pkg.statuses[t] is Status.UNKNOWN for t in targets):
            unchecked.append(pkg.name)
        if changed:
            n_worse += 1
            version = pkg.version if pkg.version == was.version else f"{was.version} → {pkg.version}"
            lines.append(f"{pkg.name} {version}")
            lines += [
                f"  {t.label(multi_py)}: {CELL[was.statuses[t]][0]} → {CELL[pkg.statuses[t]][0]}" for t in changed
            ]
    return lines, n_worse, n_new, unchecked


def summary(n_worse: int, n_new: int) -> str:
    if not n_worse and not n_new:
        return "Nothing got worse."
    parts = []
    if n_worse:
        parts.append(f"{n_worse} package{'s' * (n_worse != 1)} got worse")
    if n_new:
        parts.append(f"{n_new} new package{'s' * (n_new != 1)} {'has' if n_new == 1 else 'have'} gaps")
    return ", ".join(parts) + "."


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # As in cli.main: write UTF-8 to a non-UTF-8 pipe (Windows cp1252) instead of crashing on ✓ and →.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            if encoding != "utf8" and not stream.isatty() and not os.environ.get("PYTHONIOENCODING"):
                reconfigure(encoding="utf-8", errors="replace")
            else:
                reconfigure(errors="replace")
    p = argparse.ArgumentParser(prog="python -m wheelgap.diff", description=__doc__.splitlines()[0])
    p.add_argument("old", metavar="OLD", help="the lockfile before the change (or a directory)")
    p.add_argument("new", metavar="NEW", help="the lockfile after the change (or a directory)")
    p.add_argument(
        "-p",
        "--python",
        action="append",
        default=[],
        metavar="VER",
        help="target Python, e.g. 3.13 or 3.14t (repeatable; default: the running Python)",
    )
    p.add_argument(
        "-t", "--target", action="append", default=[], metavar="PLATFORM", help="target platform (repeatable)"
    )
    args = p.parse_args(argv)
    try:
        targets = make_targets(args.python or None, args.target or None)
        old = check_lockfile(args.old, targets)
        new = check_lockfile(args.new, targets)
    except Exception as e:  # unknown targets, unreadable or malformed lockfiles
        print(f"wheelgap: error: {e}", file=sys.stderr)
        return 2

    lines, n_worse, n_new, unchecked = compare(old, new, targets)
    for line in lines:
        print(line)
    print(summary(n_worse, n_new))
    if unchecked:
        print(f"wheelgap: note: couldn't check {', '.join(unchecked)} in either lockfile", file=sys.stderr)
    return 1 if n_worse or n_new else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
