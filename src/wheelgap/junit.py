"""JUnit XML report for CI dashboards (Jenkins, GitLab, Azure Pipelines, CircleCI)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

from wheelgap.check import Row, Status, Target

if TYPE_CHECKING:
    pass


def _cell_fails(status: Status, fail_on: str) -> bool:
    """Whether this target cell should fail the run, matching main()'s --fail-on rules."""
    if status in (Status.MISSING, Status.UNKNOWN):
        return True
    if status is Status.SOURCE:
        return fail_on != "missing"
    return False


def junit_xml(rows: list[Row], targets: list[Target], fail_on: str) -> str:
    """One testsuite per target, one testcase per package.

    A cell is a failure when its status fails the run (`missing`, `unknown`,
    and `source` unless `--fail-on missing`). `n/a` is skipped. Other statuses
    (wheel, bundled, and source with `--fail-on missing`) are passes.
    """
    multi_py = len({t.python for t in targets}) > 1
    # A <testsuites> wrapper is accepted by every dashboard and keeps several
    # suites in one file.
    root = ET.Element("testsuites")
    for target in targets:
        label = target.label(multi_py)
        cases: list[tuple[str, str | None, str | None]] = []  # name, failure, skip
        for row in rows:
            status = row.results[target]
            name = row.package.name
            if status is Status.NOT_NEEDED:
                cases.append((name, None, "n/a"))
            elif _cell_fails(status, fail_on):
                cases.append((name, status.value, None))
            else:
                cases.append((name, None, None))
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
    from pathlib import Path

    Path(path).write_text(junit_xml(rows, targets, fail_on), encoding="utf-8")
