import json

import pytest

from wheelgap.check import Status
from wheelgap.diff import main, worse

TARGET = ["-p", "3.13", "-t", "linux-x86_64"]


def write_pair(tmp_path, old, new):
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    old_path, new_path = tmp_path / "old" / "requirements.txt", tmp_path / "new" / "requirements.txt"
    old_path.write_text(old)
    new_path.write_text(new)
    return str(old_path), str(new_path)


def test_diff_reports_a_package_that_lost_its_wheel(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "ranged==1.5\npurewheel==3.1\n", "ranged==1.9\npurewheel==3.1\n")
    assert main([old, new, *TARGET]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "ranged 1.5 → 1.9",
        "  linux-x86_64: ✓ → ● source",
        "1 package got worse.",
    ]


def test_diff_reports_a_new_package_with_gaps(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "purewheel==3.1\n", "purewheel==3.1\nNoWheels==0.9\n")
    assert main([old, new, *TARGET]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "NoWheels 0.9 (new)",
        "  linux-x86_64: ● source",
        "1 new package has gaps.",
    ]


def test_diff_ignores_new_packages_with_wheels_and_improvements(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "ranged==1.9\n", "ranged==2.0\npurewheel==3.1\n")
    assert main([old, new, *TARGET]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]


def test_diff_matches_packages_by_canonical_name(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "nowheels==0.9\n", "NoWheels==0.9\n")
    assert main([old, new, *TARGET]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]


def test_diff_of_identical_files_exits_0(fake_pypi, tmp_path, capsys):
    text = "ranged==1.9\nnowheels==0.9\npurewheel==3.1\n"
    old, new = write_pair(tmp_path, text, text)
    assert main([old, new, *TARGET]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]


def test_diff_labels_targets_with_python_when_there_are_several(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "ranged==1.5\n", "ranged==1.9\n")
    assert main([old, new, "-p", "3.13", "-p", "3.14", "-t", "linux-x86_64"]) == 1
    assert capsys.readouterr().out.splitlines()[1:3] == [
        "  3.13 linux-x86_64: ✓ → ● source",
        "  3.14 linux-x86_64: ✓ → ● source",
    ]


def test_diff_counts_a_package_it_can_no_longer_check_as_worse(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "ranged==1.5\n", "ranged==9.9\n")  # 9.9 isn't on the index
    assert main([old, new, *TARGET]) == 1
    assert capsys.readouterr().out.splitlines()[:2] == ["ranged 1.5 → 9.9", "  linux-x86_64: ✓ → ? unknown"]


def test_diff_notes_packages_unknown_on_both_sides(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "does-not-exist==1.0\n", "does-not-exist==1.0\n")
    assert main([old, new, *TARGET]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == ["Nothing got worse."]
    assert "couldn't check does-not-exist in either lockfile" in err


def test_diff_counts_unknown_at_a_new_version_as_worse(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "does-not-exist==1.0\n", "does-not-exist==2.0\n")
    assert main([old, new, *TARGET]) == 1
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        "does-not-exist 1.0 → 2.0",
        "  linux-x86_64: ? unknown → ? unknown",
        "1 package got worse.",
    ]
    assert "couldn't check" not in err


def test_diff_counts_a_package_now_installed_from_git_as_worse(fake_pypi, tmp_path, capsys):
    old, new = write_pair(tmp_path, "ranged==1.5\n", "ranged @ git+https://github.com/example/ranged@v1.9\n")
    assert main([old, new, *TARGET]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "ranged 1.5 → (url)",
        "  linux-x86_64: ✓ → skipped (url)",
        "1 package got worse.",
    ]


def test_diff_counts_a_package_now_from_a_private_index_as_worse(fake_pypi, tmp_path, capsys):
    old, _ = write_pair(tmp_path, "ranged==1.5\n", "")
    new = tmp_path / "new" / "Pipfile.lock"
    lock = {
        "_meta": {"sources": [{"name": "internal", "url": "https://pkgs.example.com/simple"}]},
        "default": {"ranged": {"version": "==1.9", "index": "internal"}},
    }
    new.write_text(json.dumps(lock), encoding="utf-8")
    assert main([old, str(new), *TARGET]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "ranged 1.5 → (private index)",
        "  linux-x86_64: ✓ → skipped (private index)",
        "1 package got worse.",
    ]


@pytest.mark.parametrize(
    "new",
    [
        "purewheel==3.1\n",  # removed
        'ranged @ git+https://github.com/example/ranged@v1.9 ; sys_platform == "win32"\n',  # not on this target
    ],
)
def test_diff_ignores_a_package_new_no_longer_installs(fake_pypi, tmp_path, capsys, new):
    old, new = write_pair(tmp_path, "ranged==1.5\npurewheel==3.1\n", new)
    assert main([old, new, *TARGET]) == 0
    assert capsys.readouterr().out.splitlines() == ["Nothing got worse."]


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (Status.WHEEL, Status.SOURCE, True),
        (Status.SOURCE, Status.MISSING, True),
        (Status.NOT_NEEDED, Status.SOURCE, True),
        (Status.BUNDLED, Status.WHEEL, False),
        (Status.WHEEL, Status.NOT_NEEDED, False),
        (Status.MISSING, Status.SOURCE, False),
        (Status.SOURCE, Status.SOURCE, False),
        # unknown isn't ranked against the others: only a known-good result after it is "not worse".
        (Status.WHEEL, Status.UNKNOWN, True),
        (Status.MISSING, Status.UNKNOWN, True),
        (Status.UNKNOWN, Status.SOURCE, True),
        (Status.UNKNOWN, Status.MISSING, True),
        (Status.UNKNOWN, Status.WHEEL, False),
        (Status.UNKNOWN, Status.NOT_NEEDED, False),
        (Status.UNKNOWN, Status.UNKNOWN, False),
    ],
)
def test_worse(old, new, expected):
    assert worse(old, new) is expected


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["{tmp}/missing.txt", "{new}"], "missing.txt"),
        (["{tmp}/empty", "{new}"], "no lockfile found"),
        (["{tmp}/notes.md", "{new}"], "SourceError: don't know how to read"),
        (["{new}", "{new}", "-t", "linux-aarch46"], "TargetError: "),
        (["{new}", "{new}", "-p", "three"], "can't parse Python version"),
    ],
)
def test_diff_bad_input_exits_2(fake_pypi, tmp_path, capsys, args, message):
    (tmp_path / "notes.md").write_text("hi\n")
    (tmp_path / "empty").mkdir()
    new = tmp_path / "requirements.txt"
    new.write_text("purewheel==3.1\n")
    argv = [a.format(tmp=tmp_path, new=new) for a in args]
    if "-p" not in argv:
        argv += ["-p", "3.13"]
    assert main(argv) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("wheelgap: error: ") and message in err
