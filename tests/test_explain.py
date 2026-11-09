import json

from wheelgap.check import check, explain, rejection
from wheelgap.cli import main
from wheelgap.sources import load_inputs
from wheelgap.targets import build_targets

GLIBC_ARM = "armlib-1.4.0-cp313-cp313-manylinux_2_17_aarch64.manylinux2014_aarch64.whl"


def target(py="3.13", plat="linux-x86_64"):
    (t,) = build_targets([py], [plat])
    return t


def test_glibc_arm_wheel_does_not_count_for_alpine_arm(fixtures):
    (src,) = load_inputs([str(fixtures / "armgap")])
    targets = build_targets(["3.13"], ["linux-aarch64", "musl-aarch64", "musl-x86_64"])
    row = next(r for r in check([src], targets) if r.package.name == "armlib")
    assert [s.value for s in row.results.values()] == ["wheel", "source", "wheel"]


def test_rejection_reasons():
    musl_arm = target(plat="musl-aarch64")
    assert rejection(GLIBC_ARM, musl_arm) == "manylinux_2_17_aarch64 is a glibc wheel; musl (Alpine) needs musllinux"
    assert rejection("x-1-cp313-cp313-musllinux_1_2_aarch64.whl", target(plat="linux-aarch64")) == (
        "musllinux_1_2_aarch64 is a musl wheel; glibc Linux needs manylinux"
    )
    assert rejection("x-1-cp313-cp313-manylinux_2_39_aarch64.whl", target(plat="linux-aarch64")) == (
        "manylinux_2_39_aarch64 needs glibc 2.39, newer than 2.36 (--glibc)"
    )
    assert rejection("x-1-cp313-cp313-macosx_15_0_arm64.whl", target(plat="macos-arm64")) == (
        "macosx_15_0_arm64 needs macOS 15.0, newer than 14.0 (--macos)"
    )
    assert rejection("x-1-cp312-cp312-win_amd64.whl", target(plat="win-amd64")) == (
        "built for cp312-cp312, not CPython 3.13"
    )
    assert rejection("x-1-cp312-abi3-manylinux_2_17_x86_64.whl", target("3.14t")) == (
        "abi3 wheels can't be used on free-threaded Python"
    )
    assert rejection("x-1-cp313-cp313-win_amd64.whl", target()) == "win_amd64 doesn't match linux-x86_64"
    assert rejection("x-1-py3-none-any.whl", target()) is None


def test_explain_puts_closest_misses_first(fixtures):
    (src,) = load_inputs([str(fixtures / "armgap")])
    armlib = next(p for p in src.packages if p.name == "armlib")
    got = [f for f, _ in explain(armlib.files, target(plat="musl-aarch64"))]
    assert got[0] == GLIBC_ARM  # same arch, wrong libc: the one a bug reporter cares about
    assert not any(f.endswith(".tar.gz") for f in got)


def test_cli_explain(capsys, fixtures):
    code = main([str(fixtures / "armgap"), "-p", "3.13", "-t", "musl-aarch64", "--explain"])
    out = capsys.readouterr().out
    assert code == 1
    assert "armlib 1.4.0 on musl-aarch64: ● source" in out
    assert GLIBC_ARM in out and "musl (Alpine) needs musllinux" in out
    assert "newglibc 2.0 on musl-aarch64: ✗ missing" in out
    assert "Run with --explain" not in out


def test_cli_hints_at_explain(capsys, fixtures):
    main([str(fixtures / "armgap"), "-p", "3.13", "-t", "musl-aarch64"])
    assert "Run with --explain" in capsys.readouterr().out


def test_json_explain(capsys, fixtures):
    main([str(fixtures / "armgap"), "-p", "3.13", "-t", "linux-aarch64,musl-aarch64", "--json", "--explain"])
    data = json.loads(capsys.readouterr().out)
    armlib = next(p for p in data["packages"] if p["name"] == "armlib")
    assert list(armlib["rejected"]) == ["musl-aarch64"]  # only targets with a gap
    assert armlib["rejected"]["musl-aarch64"][0]["file"] == GLIBC_ARM
    newglibc = next(p for p in data["packages"] if p["name"] == "newglibc")
    assert "needs glibc 2.39" in newglibc["rejected"]["linux-aarch64"][0]["reason"]


def test_json_without_explain_has_no_rejected(capsys, fixtures):
    main([str(fixtures / "armgap"), "-p", "3.13", "-t", "musl-aarch64", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert all("rejected" not in p for p in data["packages"])


def test_explain_pyodide(capsys, fake_pypi):
    main(["nowheels==0.9", "fastlib==2.0.0", "-p", "3.13", "-t", "pyodide", "--explain"])
    out = capsys.readouterr().out
    assert "nowheels 0.9 on pyodide: ✗ missing" in out
    assert "pyodide can't build packages from source" in out
    assert rejection("x-1-cp314-cp314-pyemscripten_2026_0_wasm32.whl", target(plat="pyodide")) == (
        "pyemscripten_2026_0_wasm32 is for a different Pyodide ABI than Python 3.13"
    )
