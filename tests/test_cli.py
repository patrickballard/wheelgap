import json

from wheelgap.cli import main


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_gaps_exit_1(capsys, fixtures):
    code, out, _ = run(capsys, str(fixtures / "uvproj"), "-p", "3.13", "-t", "linux-x86_64,musl-aarch64")
    assert code == 1
    assert "fastlib" in out and "● source" in out
    assert "purewheel" not in out  # fine everywhere, so hidden
    assert "have gaps" in out
    assert "skipped 2 non-index packages" in out
    assert "\033[" not in out  # no colour when not a TTY


def test_exit_zero_and_all(capsys, fixtures):
    code, out, _ = run(capsys, str(fixtures / "uvproj"), "-p", "3.13", "--all", "--exit-zero")
    assert code == 0
    assert "purewheel" in out and "–" in out


def test_clean_exit_0(capsys, fixtures):
    code, out, _ = run(capsys, str(fixtures / "pylockproj"), "-p", "3.13", "-t", "linux-aarch64")
    assert code == 0
    assert "The 1 package has a wheel for 1 target." in out


def test_json(capsys, fixtures):
    code, out, _ = run(capsys, str(fixtures / "uvproj"), "-p", "3.13,3.14t", "-t", "win-amd64", "--json")
    assert code == 1
    data = json.loads(out)
    assert [t["label"] for t in data["targets"]] == ["3.13 win-amd64", "3.14t win-amd64"]
    fastlib = next(p for p in data["packages"] if p["name"] == "fastlib")
    assert fastlib["results"] == {"3.13 win-amd64": "wheel", "3.14t win-amd64": "source"}
    assert {"name": "gitdep", "reason": "git"} in data["skipped"]
    assert data["summary"]["with_gaps"] >= 1


def test_color_always(capsys, fixtures):
    _, out, _ = run(capsys, str(fixtures / "uvproj"), "-p", "3.13", "--color", "always")
    assert "\033[33m" in out


def test_adhoc_packages(capsys, fake_pypi):
    code, out, _ = run(capsys, "nowheels", "purewheel", "-p", "3.13", "-t", "linux")
    assert code == 1
    assert "nowheels" in out and "0.9" in out


def test_errors_exit_2(capsys, fixtures):
    code, _, err = run(capsys, str(fixtures / "empty"))
    assert code == 2 and "no lockfile found" in err
    code, _, err = run(capsys, str(fixtures / "uvproj"), "-t", "solaris")
    assert code == 2 and "unknown target" in err
    code, _, err = run(capsys, str(fixtures / "uvproj"), "-p", "four")
    assert code == 2


def test_list_targets(capsys):
    code, out, _ = run(capsys, "--list-targets")
    assert code == 0 and "musl-aarch64" in out


def test_defaults_from_pyproject(capsys, fixtures):
    code, out, _ = run(capsys, str(fixtures / "configproj"), "--json")
    assert code == 1
    assert [t["label"] for t in json.loads(out)["targets"]] == ["3.13 win-amd64", "3.14t win-amd64"]


def test_flags_beat_pyproject(capsys, fixtures):
    _, out, _ = run(capsys, str(fixtures / "configproj"), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert [t["label"] for t in json.loads(out)["targets"]] == ["linux-x86_64"]


def test_bad_pyproject_exit_2(capsys, fixtures, tmp_path):
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nglibc = 2.31\n")
    code, _, err = run(capsys, str(tmp_path))
    assert code == 2 and "glibc must be a string" in err


def test_fail_on_missing_still_fails_when_a_package_cant_install(fixtures):
    armgap = str(fixtures / "armgap")
    assert main([armgap, "-p", "3.13", "-t", "musl-aarch64"]) == 1
    assert main([armgap, "-p", "3.13", "-t", "musl-aarch64", "--fail-on", "missing"]) == 1  # newglibc


def test_fail_on_missing_accepts_source_builds(capsys, fake_pypi):
    assert main(["nowheels", "-p", "3.13", "-t", "linux"]) == 1
    capsys.readouterr()
    assert main(["nowheels", "-p", "3.13", "-t", "linux", "--fail-on", "missing"]) == 0
    out = capsys.readouterr().out
    assert "● source" in out and "--fail-on missing" in out
    assert main(["nowheels", "-p", "3.13", "-t", "linux", "--fail-on", "missing", "--json"]) == 0


def test_fail_on_from_pyproject(capsys, fake_pypi, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nfail-on = "missing"\n')
    assert main(["nowheels", "-p", "3.13", "-t", "linux"]) == 0
    assert main(["nowheels", "-p", "3.13", "-t", "linux", "--fail-on", "source"]) == 1  # flag wins
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nfail-on = "never"\n')
    assert main(["nowheels", "-p", "3.13", "-t", "linux"]) == 2
    assert "fail-on must be one of" in capsys.readouterr().err
