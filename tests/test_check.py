from wheelgap.check import Status, check, marker_holds, status_for
from wheelgap.pypi import fill_all
from wheelgap.sources import Package, Source, load_inputs
from wheelgap.targets import build_targets


def results(rows):
    return {r.package.name: [s.value for s in r.results.values()] for r in rows}


def test_status_for():
    (lin,) = build_targets(["3.13"], ["linux-x86_64"])
    assert status_for(["a-1-cp313-cp313-manylinux_2_17_x86_64.whl"], lin) is Status.WHEEL
    assert status_for(["a-1-cp313-cp313-win_amd64.whl", "a-1.tar.gz"], lin) is Status.SOURCE
    assert status_for(["a-1-cp313-cp313-win_amd64.whl"], lin) is Status.MISSING
    assert status_for([], lin) is Status.MISSING
    assert status_for(None, lin) is Status.UNKNOWN
    assert status_for(["garbage.whl", "a-1.zip"], lin) is Status.SOURCE


def test_uv_lock_graph(fixtures):
    sources = load_inputs([str(fixtures / "uvproj")])
    targets = build_targets(["3.13"], ["linux-x86_64", "musl-aarch64", "win-amd64"])
    got = results(check(sources, targets))
    assert got["fastlib"] == ["wheel", "source", "wheel"]
    assert got["purewheel"] == ["wheel", "wheel", "wheel"]
    assert got["puresdist"] == ["source", "source", "source"]
    assert got["winonly"] == ["n/a", "n/a", "wheel"]  # only pulled in on Windows
    assert got["oldpy"] == ["n/a", "n/a", "n/a"]  # only on Python < 3.13
    assert got["devtool"] == ["wheel", "wheel", "wheel"]  # dev group is included
    assert got["orphan"] == ["n/a", "n/a", "n/a"]  # nothing depends on it
    assert "gitdep" not in got and "demo" not in got


def test_uv_lock_graph_python_312(fixtures):
    sources = load_inputs([str(fixtures / "uvproj")])
    (t,) = build_targets(["3.12"], ["linux-x86_64"])
    got = results(check(sources, [t]))
    assert got["oldpy"] == ["source"]
    assert got["fastlib"] == ["source"]  # only cp313 wheels


def test_uv_version_disambiguation():
    src = Source(
        "uv.lock",
        packages=[
            Package("root", "0", skip="editable"),
            Package("np", "1.0", files=["np-1.0.tar.gz"]),
            Package("np", "2.0", files=["np-2.0-py3-none-any.whl"]),
        ],
        edges={0: [("np", "1.0", "python_version < '3.10'"), ("np", "2.0", "python_version >= '3.10'")], 1: [], 2: []},
        roots=[0],
    )
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    rows = check([src], [t])
    assert [(r.package.version, r.results[t].value) for r in rows] == [("1.0", "n/a"), ("2.0", "wheel")]


def test_markers_on_packages(fixtures):
    sources = load_inputs([str(fixtures / "pylockproj")])
    targets = build_targets(["3.13"], ["linux-aarch64", "macos-arm64"])
    got = results(check(sources, targets))
    assert got["fastlib"] == ["wheel", "missing"]
    assert got["macpkg"] == ["n/a", "source"]


def test_bad_marker_counts_as_needed():
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    assert marker_holds("this is not a marker", t)
    assert marker_holds(None, t)
    assert not marker_holds("sys_platform == 'win32'", t)


def test_requirements_against_fake_index(fixtures, fake_pypi):
    sources = load_inputs([str(fixtures / "reqproj")])
    packages = [p for s in sources for p in s.packages]
    fill_all(packages)
    targets = build_targets(["3.13"], ["linux-x86_64", "win-amd64"])
    got = results(check(sources, targets))
    assert got == {
        "fastlib": ["wheel", "source"],
        "unpinned": ["wheel", "wheel"],
        "winonly": ["n/a", "wheel"],
        "purewheel": ["wheel", "wheel"],
    }
    assert next(p for p in packages if p.name == "unpinned").version == "4.2"
    assert not any("localpkg" in c for c in fake_pypi)


def test_lookup_errors_are_unknown(fake_pypi):
    pkgs = [Package("doesnotexist", "1.0"), Package("fastlib", "9.9")]
    fill_all(pkgs)
    assert [p.error for p in pkgs] == ["not on index", "not on index"]
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    rows = check([Source("x", pkgs)], [t])
    assert [r.results[t] for r in rows] == [Status.UNKNOWN, Status.UNKNOWN]


def test_online_refresh_overrides_lockfile(fixtures, fake_pypi):
    sources = load_inputs([str(fixtures / "uvproj")])
    packages = [p for s in sources for p in s.packages]
    fill_all(packages, refresh=True)
    fastlib = next(p for p in packages if p.name == "fastlib")
    assert fastlib.files == ["fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", "fastlib-2.0.0.tar.gz"]


def test_pyodide_sdist_only_is_missing():
    (py,) = build_targets(["3.13"], ["pyodide"])
    (lin,) = build_targets(["3.13"], ["linux-x86_64"])
    assert status_for(["a-1.tar.gz"], lin) is Status.SOURCE
    assert status_for(["a-1.tar.gz"], py) is Status.MISSING  # no compiler in the browser
    assert status_for(["a-1-cp313-cp313-pyodide_2025_0_wasm32.whl", "a-1.tar.gz"], py) is Status.WHEEL
