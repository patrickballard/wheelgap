import json
from pathlib import Path

import pytest

from wheelgap import cache, check_packages, pypi
from wheelgap.cache import cache_dir as real_cache_dir
from wheelgap.cli import main
from wheelgap.sources import Package

FASTLIB = "https://pypi.org/pypi/fastlib/2.0.0/json"
FASTLIB_FILES = ["fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", "fastlib-2.0.0.tar.gz"]


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def pinned(tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("fastlib==2.0.0\n", encoding="utf-8")
    return str(path)


def lookup(name, version):
    return pypi.fill(Package(name, version), use_cache=True)


def test_repeat_run_reads_pinned_lookups_from_the_cache(capsys, fake_pypi, pinned, lookup_cache):
    first = run(capsys, pinned, "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert fake_pypi == [FASTLIB]
    second = run(capsys, pinned, "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert fake_pypi == [FASTLIB]  # no second lookup
    assert second == first and first[0] == 0
    assert len(list(lookup_cache.glob("*.json"))) == 1


@pytest.mark.parametrize("flag", ["--online", "--no-cache"])
def test_online_and_no_cache_look_up_again(capsys, fake_pypi, pinned, lookup_cache, flag):
    run(capsys, pinned, "-p", "3.13", "-t", "linux-x86_64")
    code, _, _ = run(capsys, pinned, "-p", "3.13", "-t", "linux-x86_64", flag)
    assert code == 0
    assert fake_pypi == [FASTLIB, FASTLIB]


def test_no_cache_neither_reads_nor_writes(capsys, fake_pypi, pinned, lookup_cache):
    run(capsys, pinned, "-p", "3.13", "-t", "linux-x86_64", "--no-cache")
    assert not list(lookup_cache.iterdir())


def test_an_expired_entry_is_looked_up_again(fake_pypi, monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr(cache, "_now", lambda: now)
    lookup("fastlib", "2.0.0")
    now += cache.MAX_AGE - 1
    assert lookup("fastlib", "2.0.0").files
    assert fake_pypi == [FASTLIB]
    now += 1  # 24 hours old
    assert lookup("fastlib", "2.0.0").files
    assert fake_pypi == [FASTLIB, FASTLIB]
    now += 1  # the fresh answer was saved again
    lookup("fastlib", "2.0.0")
    assert fake_pypi == [FASTLIB, FASTLIB]


def test_an_entry_from_the_future_is_a_miss(fake_pypi, monkeypatch):
    monkeypatch.setattr(cache, "_now", lambda: 2_000_000_000.0)
    lookup("fastlib", "2.0.0")
    monkeypatch.setattr(cache, "_now", lambda: 1_000_000_000.0)  # the clock went back
    lookup("fastlib", "2.0.0")
    assert fake_pypi == [FASTLIB, FASTLIB]


@pytest.mark.parametrize("name,version", [("nosuchpkg", "1.0"), ("fastlib", "9.9")])
def test_not_found_is_not_cached(fake_pypi, lookup_cache, name, version):
    for _ in range(2):
        assert "not on index" in lookup(name, version).error
    assert fake_pypi.count(f"https://pypi.org/pypi/{name}/{version}/json") == 2
    assert not list(lookup_cache.iterdir())


@pytest.mark.parametrize("response", [None, {"urls": "x"}, {"urls": [{"name": "no-filename.whl"}]}])
def test_an_odd_response_is_not_cached(monkeypatch, lookup_cache, response):
    monkeypatch.setattr(pypi, "fetch_json", lambda url, timeout=20.0: response)
    assert lookup("fastlib", "2.0.0").error == "unexpected response from the index"
    assert not list(lookup_cache.iterdir())


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not json",
        b"\xff\xfe",
        b"[]",
        b"{}",
        b'{"saved": "today", "files": []}',
        b'{"saved": true, "files": []}',
        b'{"saved": 1800000000, "files": "x"}',
        b'{"saved": 1800000000, "files": [{"name": "no-filename.whl"}]}',
    ],
)
def test_a_corrupt_entry_is_a_miss(fake_pypi, monkeypatch, lookup_cache, content):
    monkeypatch.setattr(cache, "_now", lambda: 1_800_000_000.0)
    lookup("fastlib", "2.0.0")
    (entry,) = lookup_cache.glob("*.json")
    entry.write_bytes(content)
    pkg = lookup("fastlib", "2.0.0")
    assert pkg.error is None and pkg.files == FASTLIB_FILES
    assert fake_pypi == [FASTLIB, FASTLIB]
    lookup("fastlib", "2.0.0")  # and the fresh answer replaced it
    assert fake_pypi == [FASTLIB, FASTLIB]


def test_an_unwritable_cache_never_fails_a_run(capsys, fake_pypi, pinned, monkeypatch, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(cache, "cache_dir", lambda: blocker / "wheelgap")  # mkdir fails on every OS
    for _ in range(2):
        code, out, err = run(capsys, pinned, "-p", "3.13", "-t", "linux-x86_64", "--json")
        assert code == 0 and not err
        assert json.loads(out)["packages"][0]["results"] == {"linux-x86_64": "wheel"}
    assert fake_pypi == [FASTLIB, FASTLIB]


def test_yanked_files_stay_marked_when_cached(fake_pypi):
    first = lookup("halfyanked", "2.0")
    second = lookup("halfyanked", "2.0")
    assert len(fake_pypi) == 1
    assert second.files == first.files
    assert second.yanked == first.yanked == frozenset({"halfyanked-2.0-py3-none-any.whl"})


def test_unpinned_and_range_lookups_are_not_cached(fake_pypi, lookup_cache):
    for _ in range(2):
        pypi.fill_all([Package("unpinned", None), Package("ranged", None, spec=">=1,<2")], use_cache=True)
    assert len(fake_pypi) == 4
    assert not list(lookup_cache.iterdir())


def test_simple_index_lookups_are_not_cached(fake_simple, lookup_cache):
    for _ in range(2):
        pkg = Package("fastlib", "2.0.0")
        pypi.fill_all([pkg], simple_index="https://user:secret@index.example/simple", use_cache=True)
        assert pkg.error is None and pkg.files
    assert len(fake_simple.calls) == 2
    assert not list(lookup_cache.iterdir())


def test_the_python_api_does_not_cache(fake_pypi, lookup_cache):
    for _ in range(2):
        check_packages([("fastlib", "2.0.0")])
    assert fake_pypi == [FASTLIB, FASTLIB]
    assert not list(lookup_cache.iterdir())


def test_cache_entries_hold_no_urls_or_credentials(monkeypatch, lookup_cache):
    base = "https://user:s3cret-token@mirror.example/pypi"
    monkeypatch.setenv("WHEELGAP_INDEX_URL", base)
    calls = []

    def fetch_json(url, timeout=20.0):
        calls.append(url)
        signed = "https://files.mirror.example/fastlib-2.0.0.tar.gz?sig=s3cret-signature"
        return {"info": {"version": "2.0.0"}, "urls": [{"filename": "fastlib-2.0.0.tar.gz", "url": signed}]}

    monkeypatch.setattr(pypi, "fetch_json", fetch_json)
    lookup("fastlib", "2.0.0")
    assert lookup("fastlib", "2.0.0").files == ["fastlib-2.0.0.tar.gz"]
    assert calls == [f"{base}/fastlib/2.0.0/json"]
    (entry,) = lookup_cache.iterdir()
    text = entry.read_text(encoding="utf-8")
    assert "s3cret" not in entry.name and "s3cret" not in text and "mirror.example" not in text
    assert json.loads(text)["files"] == [{"filename": "fastlib-2.0.0.tar.gz"}]


def test_the_index_is_part_of_the_key(fake_pypi, monkeypatch):
    lookup("fastlib", "2.0.0")
    monkeypatch.setattr(pypi, "index_url", lambda: "https://mirror.example/pypi")
    lookup("fastlib", "2.0.0")  # not on the fake mirror, but asked: another index's answer isn't reused
    assert fake_pypi[:2] == [FASTLIB, "https://mirror.example/pypi/fastlib/2.0.0/json"]


@pytest.mark.parametrize(
    "platform,env,expected",
    [
        ("linux", {"XDG_CACHE_HOME": str(Path("/xdg").absolute())}, Path("/xdg").absolute()),
        ("linux", {"XDG_CACHE_HOME": "relative/dir"}, Path.home() / ".cache"),
        ("linux", {"XDG_CACHE_HOME": ""}, Path.home() / ".cache"),
        ("darwin", {"XDG_CACHE_HOME": str(Path("/xdg").absolute())}, Path.home() / "Library" / "Caches"),
        ("win32", {"LOCALAPPDATA": str(Path("/local").absolute())}, Path("/local").absolute()),
        ("win32", {"LOCALAPPDATA": ""}, Path.home() / "AppData" / "Local"),
    ],
)
def test_cache_dir_per_platform(monkeypatch, platform, env, expected):
    monkeypatch.setattr(cache.sys, "platform", platform)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert real_cache_dir() == expected / "wheelgap" / "pinned-v1"
