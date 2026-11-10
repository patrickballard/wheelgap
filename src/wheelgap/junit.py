"""JUnit XML report for CI dashboards (Jenkins, GitLab, Azure Pipelines, CircleCI)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .check import Row, Status
from .targets import Target


def _cell_fails(status: Status, fail_on: str) -> bool:
    """Whether this target cell should fail the run, matching main()'s --fail-on rules."""
    if status in (Status.MISSING, Status.UNKNOWN):
        return True
    if status is Status.SOURCE:
        return fail_on != "missing"
    return False


def _rank(cell: tuple[str | None, str | None]) -> int:
    fail, skip = cell
    return 2 if fail is not None else 0 if skip is not None else 1


def junit_xml(rows: list[Row], targets: list[Target], fail_on: str) -> str:
    """One testsuite per target, one testcase per package.

    A cell is a failure when its status fails the run (`missing`, `unknown`,
    and `source` unless `--fail-on missing`). `n/a` is skipped. Other statuses
    (wheel, bundled, and source with `--fail-on missing`) are passes.

    Dashboards key a test on classname + name, so each name appears once per
    suite: a package locked at several versions gets the version in its name,
    and one that several inputs lock at the same version is one testcase,
    which fails if any of them fails.
    """
    versions: dict[str, set[str | None]] = {}
    for row in rows:
        versions.setdefault(row.package.key, set()).add(row.package.version)
    multi_py = len({t.python for t in targets}) > 1
    # A <testsuites> wrapper is accepted by every dashboard and keeps several
    # suites in one file.
    root = ET.Element("testsuites")
    for target in targets:
        label = target.label(multi_py)
        merged: dict[str, tuple[str | None, str | None]] = {}  # name -> failure, skip
        for row in rows:
            status = row.results[target]
            name = row.package.name
            if len(versions[row.package.key]) > 1:
                name = f"{name} {row.package.version}"
            if status is Status.NOT_NEEDED:
                cell: tuple[str | None, str | None] = (None, "n/a")
            elif _cell_fails(status, fail_on):
                cell = (status.value, None)
            else:
                cell = (None, None)
            # The worst cell wins: a failure, then a pass, then skipped.
            if name not in merged or _rank(cell) > _rank(merged[name]):
                merged[name] = cell
        cases = [(name, fail, skip) for name, (fail, skip) in merged.items()]
        failures = sum(1 for _, fail, _ in cases if fail is not None)
        skipped = sum(1 for _, _, skip in cases if skip is not None)
        suite = ET.SubElement(
            root,
            "testsuite",
            name=label,
            tests=str(len(cases)),
            failures=str(failures),
            skipped=str(skipped),
        )
        for name, fail, skip in cases:
            case = ET.SubElement(suite, "testcase", name=name, classname=label)
            if fail is not None:
                ET.SubElement(case, "failure", message=fail).text = fail
            elif skip is not None:
                ET.SubElement(case, "skipped", message=skip)
    # XML declaration so picky parsers don't have to guess the encoding.
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def write_junit(path: str, rows: list[Row], targets: list[Target], fail_on: str) -> None:
    Path(path).write_text(junit_xml(rows, targets, fail_on), encoding="utf-8")
