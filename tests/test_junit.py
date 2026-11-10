"""Tests for --junit XML output."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from wheelgap.cli import main


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def _suites(path: Path) -> dict[str, ET.Element]:
    root = ET.parse(path).getroot()
    # ET may wrap a single suite or a testsuites element.
    if root.tag == "testsuite":
        return {root.attrib["name"]: root}
    return {el.attrib["name"]: el for el in root.findall("testsuite")}


def _case(suite: ET.Element, name: str) -> ET.Element:
    matches = [el for el in suite.findall("testcase") if el.attrib["name"] == name]
    assert len(matches) == 1, [el.attrib["name"] for el in suite.findall("testcase")]
    return matches[0]


def test_junit_armgap_fails_only_on_musl(capsys, fixtures, tmp_path, fake_pypi):
    report = tmp_path / "report.xml"
    args = [
        str(fixtures / "armgap"),
        "-p",
        "3.13",
        "-t",
        "linux-aarch64,musl-aarch64",
        "--junit",
        str(report),
    ]
    code, out, err = run(capsys, *args)
    assert code == 1, err
    assert report.is_file()
    suites = _suites(report)
    assert set(suites) == {"linux-aarch64", "musl-aarch64"}
    # armlib has a linux-aarch64 wheel and only an sdist on musl, so it fails
    # the default --fail-on source on musl only.
    glibc = _case(suites["linux-aarch64"], "armlib")
    assert glibc.find("failure") is None
    assert glibc.find("skipped") is None
    musl = _case(suites["musl-aarch64"], "armlib")
    fail = musl.find("failure")
    assert fail is not None
    assert "source" in (fail.get("message") or fail.text or "")
    # With --fail-on missing, armlib's source build is accepted on every target.
    report2 = tmp_path / "missing.xml"
    code, out, err = run(capsys, *args[:-1], str(report2), "--fail-on", "missing")
    assert code in (0, 1), err
    suites = _suites(report2)
    assert _case(suites["linux-aarch64"], "armlib").find("failure") is None
    assert _case(suites["musl-aarch64"], "armlib").find("failure") is None


def test_junit_skips_packages_not_needed_on_a_target(capsys, fixtures, tmp_path, fake_pypi):
    report = tmp_path / "report.xml"
    code, out, err = run(
        capsys,
        str(fixtures / "uvproj"),
        "-p",
        "3.13",
        "-t",
        "linux-x86_64,win-arm64",
        "--junit",
        str(report),
    )
    assert code in (0, 1), err
    suites = _suites(report)
    winonly_linux = _case(suites["linux-x86_64"], "winonly")
    skip = winonly_linux.find("skipped")
    assert skip is not None
    assert (skip.get("message") or "n/a") == "n/a"
    # It is installed on Windows ARM, so that cell is not skipped (it may
    # still fail if the locked wheel is only for win-amd64).
    assert _case(suites["win-arm64"], "winonly").find("skipped") is None
