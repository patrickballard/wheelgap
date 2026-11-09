from wheelgap.cli import main


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
