import io
import json

import pytest

from wheelgap import __version__, mcp


def run(*messages: object, raw: bytes = b"") -> list:
    """Send messages to a fresh server over in-memory stdio; return the parsed replies, one per output line."""
    stdin = io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in messages) + raw)
    stdout = io.BytesIO()
    mcp.serve(stdin, stdout)
    lines = stdout.getvalue().splitlines()
    return [json.loads(line) for line in lines]


def init(version: str = "2025-06-18") -> dict:
    return {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": version}}


def call(tool: str, msg_id: int = 1, **arguments: object) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}


@pytest.mark.parametrize(
    ("asked", "agreed"),
    [("2024-11-05", "2024-11-05"), ("2025-11-25", "2025-11-25"), ("2099-01-01", "2025-11-25"), (None, "2025-11-25")],
)
def test_initialize_negotiates_version(asked, agreed):
    (reply,) = run(init(asked))
    result = reply["result"]
    assert reply["id"] == 0
    assert result["protocolVersion"] == agreed
    assert result["serverInfo"] == {"name": "wheelgap", "version": __version__}
    assert result["capabilities"] == {"tools": {"listChanged": False}}


def test_notifications_get_no_reply_and_unknown_methods_are_errors():
    replies = run(
        init(),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 9}},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        # Clients on the 2026 revisions probe with server/discover and fall back to initialize on any error.
        {"jsonrpc": "2.0", "id": 3, "method": "server/discover", "params": {}},
    )
    assert [r["id"] for r in replies] == [0, 2, 3]
    assert replies[1]["result"] == {}
    assert replies[2]["error"]["code"] == mcp.METHOD_NOT_FOUND


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"{not json\n", mcp.PARSE_ERROR),
        (b"\xff\xfe\n", mcp.PARSE_ERROR),
        (b'{"id": 1, "method": "ping"}\n', mcp.INVALID_REQUEST),
        (b'{"jsonrpc": "2.0", "id": true, "method": "ping"}\n', mcp.INVALID_REQUEST),
        (b'{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}\n', mcp.INVALID_PARAMS),
        (b"[]\n", mcp.INVALID_REQUEST),
    ],
)
def test_bad_messages_get_errors_and_the_server_keeps_going(raw, code):
    replies = run(raw=raw + b'\n{"jsonrpc": "2.0", "id": 7, "method": "ping"}\r\n')
    assert replies[0]["error"]["code"] == code
    assert replies[-1] == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_batches_reply_with_a_list():
    (reply,) = run([{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "x"}])
    assert reply == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_tools_list_has_valid_schemas():
    (_, reply) = run(init(), {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in reply["result"]["tools"]}
    assert set(tools) == {"check_package", "check_lockfile", "list_targets"}
    for tool in tools.values():
        assert tool["inputSchema"]["type"] == "object"
        assert tool["annotations"]["readOnlyHint"] is True
        assert set(tool["inputSchema"].get("required", [])) <= set(tool["inputSchema"]["properties"])


def test_check_package(fake_pypi):
    (_, reply) = run(init(), call("check_package", name="winonly", python="3.13", targets=["win-amd64", "linux/arm64"]))
    result = reply["result"]
    data = json.loads(result["content"][0]["text"])
    assert result["isError"] is False
    assert result["structuredContent"] == data
    assert data["results"] == {"3.13 win-amd64": "wheel", "3.13 linux-aarch64": "missing"}
    assert data["ok"] is False
    assert data["gaps"] == ["3.13 linux-aarch64"]
    assert data["version"] == "311"
    assert fake_pypi == ["https://pypi.org/pypi/winonly/json"]


def test_structured_content_only_from_2025_06_18(fake_pypi):
    (_, reply) = run(init("2025-03-26"), call("check_package", name="purewheel", python="3.13"))
    assert "structuredContent" not in reply["result"]
    assert json.loads(reply["result"]["content"][0]["text"])["ok"] is True


def test_check_package_lookup_failure_is_unknown_not_ok(fake_pypi):
    (_, reply) = run(init(), call("check_package", name="no-such-package", python="3.13", targets=["linux-x86_64"]))
    data = reply["result"]["structuredContent"]
    assert data["ok"] is False
    assert data["results"] == {"3.13 linux-x86_64": "unknown"}
    assert data["error"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "'name' is required"),
        ({"name": ""}, "'name' must be a non-empty string"),
        ({"name": "numpy", "targets": "linux-aarch64"}, "'targets' must be an array of strings"),
        ({"name": "numpy", "targets": ["linux-sparc"]}, "linux-sparc"),
        ({"name": "numpy", "python": "three"}, "three"),
    ],
)
def test_bad_arguments_are_tool_errors_without_lookups(fake_pypi, arguments, message):
    (_, reply) = run(init(), call("check_package", **arguments))
    assert reply["result"]["isError"] is True
    assert message in reply["result"]["content"][0]["text"]
    assert not fake_pypi


def test_unknown_tool_is_a_protocol_error():
    (_, reply) = run(init(), call("rm_rf"))
    assert reply["error"]["code"] == mcp.INVALID_PARAMS


def test_check_lockfile_reports_only_gaps(fake_pypi, fixtures):
    (_, reply) = run(
        init(), call("check_lockfile", path=str(fixtures / "uvproj"), python="3.13", targets=["linux-x86_64"])
    )
    data = reply["result"]["structuredContent"]
    assert data["targets"] == ["3.13 linux-x86_64"]
    assert data["ok"] is False
    assert data["packages"] == 5
    assert {g["name"]: g["results"] for g in data["gaps"]} == {
        "nativeext": {"3.13 linux-x86_64": "source"},
        "puresdist": {"3.13 linux-x86_64": "source"},
    }


def test_check_lockfile_uses_python_version_file(fake_pypi, tmp_path, fixtures):
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    (tmp_path / ".python-version").write_text("3.12.4\n", encoding="utf-8")
    (_, reply) = run(init(), call("check_lockfile", path=str(tmp_path), targets=["linux-x86_64"], offline=True))
    assert reply["result"]["structuredContent"]["targets"] == ["3.12 linux-x86_64"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"path": "/no/such/uv.lock"}, "no such file or directory"),
        ({"path": "."}, None),
        ({"path": "x", "offline": "yes"}, "'offline' must be true or false"),
    ],
)
def test_check_lockfile_errors(fake_pypi, tmp_path, monkeypatch, arguments, message):
    monkeypatch.chdir(tmp_path)  # an empty directory: no lockfile to find
    (tmp_path / "x").write_text("", encoding="utf-8")
    (_, reply) = run(init(), call("check_lockfile", **arguments))
    assert reply["result"]["isError"] is True
    if message:
        assert message in reply["result"]["content"][0]["text"]


def test_list_targets():
    (_, reply) = run(init(), call("list_targets"))
    data = reply["result"]["structuredContent"]
    assert "musl-aarch64" in data["platforms"]
    assert data["groups"]["all"] and set(data["groups"]["all"]) <= set(data["platforms"])
    assert set(data["aliases"].values()) <= set(data["platforms"])


def test_stray_prints_stay_off_the_protocol_stream(monkeypatch):
    def noisy(args):
        print("progress...")
        return {"done": True}

    monkeypatch.setitem(mcp._HANDLERS, "list_targets", noisy)
    (_, reply) = run(init(), call("list_targets"))
    assert reply["result"]["structuredContent"] == {"done": True}


def test_crash_in_a_tool_is_reported_not_fatal(monkeypatch):
    def broken(args):
        raise KeyError("boom")

    monkeypatch.setitem(mcp._HANDLERS, "list_targets", broken)
    (_, reply, ping) = run(init(), call("list_targets"), {"jsonrpc": "2.0", "id": 5, "method": "ping"})
    assert reply["result"]["isError"] is True
    assert "KeyError" in reply["result"]["content"][0]["text"]
    assert ping["result"] == {}


def test_non_ascii_output_stays_on_one_line(monkeypatch):
    monkeypatch.setitem(mcp._HANDLERS, "list_targets", lambda args: {"text": "naïve\nline"})
    stdin = io.BytesIO(json.dumps(call("list_targets")).encode() + b"\n")
    stdout = io.BytesIO()
    mcp.serve(stdin, stdout)
    assert stdout.getvalue().count(b"\n") == 1
    assert json.loads(stdout.getvalue())["result"]["structuredContent"]["text"] == "naïve\nline"


@pytest.mark.parametrize(("argv", "code"), [(["--version"], 0), (["--help"], 0), (["--bogus"], 2)])
def test_main_flags(argv, code, capsys):
    assert mcp.main(argv) == code
