"""An MCP server, so coding agents can ask wheelgap whether a package has a wheel for a platform.

Run it with `python -m wheelgap.mcp` (or the `wheelgap-mcp` command). It speaks the Model Context Protocol over
stdio: one JSON-RPC 2.0 message per line on stdin and stdout, using the initialize handshake of the 2024-11-05 to
2025-11-25 protocol revisions. Newer clients that first probe with server/discover get "method not found" and fall
back to that handshake. Like the rest of wheelgap it needs only the standard library and packaging.

Tools (all read-only; they look packages up on PyPI, or the index in WHEELGAP_INDEX_URL):
- check_package: one package (latest release or a given version) on each target.
- check_lockfile: every package a lockfile or project directory installs, and which targets lack a wheel.
- list_targets: the platform names the other tools accept.
"""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from . import __version__
from .api import Result, check_lockfile, check_package, make_targets, running_python
from .check import Status
from .pyodide import PyodideLockError
from .sources import SourceError
from .targets import (
    ALIASES,
    DEFAULT_PLATFORMS,
    EVERY_NATIVE_PLATFORM,
    MOBILE_PLATFORMS,
    NATIVE_PLATFORMS,
    PLATFORMS,
    Target,
    TargetError,
)

# The handshake-era revisions, oldest first. A client asking for one of these gets it back; any other version gets the
# newest, and the client decides whether it can use that (as the MCP lifecycle spec says).
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
_STRUCTURED_FROM = "2025-06-18"  # the first revision with structuredContent in tool results

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    "wheelgap tells you whether pip or uv can install a package from a prebuilt wheel on a platform, or will have to "
    "build it from source (or can't install it at all). Use check_lockfile on a project's uv.lock, poetry.lock, "
    "pylock.toml, Pipfile.lock or requirements.txt before changing its deployment platforms or Docker base image, and "
    "check_package before adding a dependency. Pass the project's Python version: results differ between versions."
)

_STATUS_HELP = (
    "Statuses: 'wheel' (a compatible wheel exists), 'source' (no wheel, so installers build the sdist and need a "
    "compiler and headers), 'missing' (no wheel and no sdist: install fails), 'unknown' (the lookup failed), 'n/a' "
    "(environment markers say it isn't installed there), 'bundled' (Pyodide ships its own build)."
)

_PYTHON_PROP = {
    "type": "string",
    "description": "Target Python version(s), comma-separated, e.g. '3.13' or '3.12,3.14t' (t = free-threaded).",
}
_TARGETS_PROP = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Platforms, e.g. ['linux-aarch64', 'musl-x86_64', 'win-arm64', 'pyodide'], Docker platforms such as "
        f"'linux/arm64', or the groups 'all', 'everything' and 'mobile'. Default: {', '.join(DEFAULT_PLATFORMS)}. "
        "list_targets gives every name."
    ),
}
_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "check_package",
        "title": "Check a package's wheels",
        "description": (
            "Check whether one PyPI package has a prebuilt wheel for each target platform and Python. Returns each "
            "target's status, the targets with a gap, and ok (true if every target installs without building). "
            + _STATUS_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The package name, e.g. 'numpy'."},
                "version": {"type": "string", "description": "An exact version, e.g. '2.3.4'. Default: the latest."},
                "python": _PYTHON_PROP | {"description": _PYTHON_PROP["description"] + " Default: the server's."},
                "targets": _TARGETS_PROP,
            },
            "required": ["name"],
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "check_lockfile",
        "title": "Check a lockfile's wheels",
        "description": (
            "Check every package a lockfile installs (uv.lock, poetry.lock, pylock.toml, Pipfile.lock, pdm.lock, "
            "requirements.txt, or a project directory containing one) and report the packages without a wheel on "
            "some target. Environment markers are respected. " + _STATUS_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the lockfile or project directory (relative paths are resolved "
                    "against the server's working directory).",
                },
                "python": _PYTHON_PROP
                | {
                    "description": _PYTHON_PROP["description"] + " Default: .python-version next to the lockfile, "
                    "else the server's."
                },
                "targets": _TARGETS_PROP,
                "offline": {
                    "type": "boolean",
                    "description": "Use only the files recorded in the lockfile; never contact the index.",
                },
            },
            "required": ["path"],
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "list_targets",
        "title": "List target platforms",
        "description": "List the platform names and groups that check_package and check_lockfile accept.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": _READ_ONLY | {"openWorldHint": False},
    },
]


class ToolError(Exception):
    """A problem with the tool's arguments or input, reported to the model as a tool result with isError."""


def _string(args: dict[str, Any], key: str, *, required: bool = False) -> str | None:
    value = args.get(key)
    if value is None:
        if required:
            raise ToolError(f"'{key}' is required")
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise ToolError(f"'{key}' must be a non-empty string" if required else f"'{key}' must be a string")
    return value


def _targets(args: dict[str, Any], default_python: str | list[str] | None) -> list[Target]:
    platforms = args.get("targets")
    if platforms is not None and (not isinstance(platforms, list) or not all(isinstance(p, str) for p in platforms)):
        raise ToolError("'targets' must be an array of strings")
    python = _string(args, "python")
    if python is not None and not python.strip():
        python = None
    try:
        return make_targets(python or default_python, platforms or None)
    except TargetError as e:
        raise ToolError(str(e)) from e


def _result_dict(result: Result) -> dict[str, Any]:
    out: dict[str, Any] = result.to_dict()
    out["ok"] = result.ok
    out["gaps"] = [t.label(True) for t in result.gaps]
    return out


def _check_package(args: dict[str, Any]) -> dict[str, Any]:
    name = _string(args, "name", required=True)
    assert name is not None
    version = _string(args, "version")
    targets = _targets(args, None)
    result = check_package(name.strip(), version.strip() if version and version.strip() else None, targets)
    out = _result_dict(result)
    out["targets"] = [t.label(True) for t in targets]
    return out


def _check_lockfile(args: dict[str, Any]) -> dict[str, Any]:
    from .cli import read_python_version  # the CLI's own .python-version rules

    raw = _string(args, "path", required=True)
    assert raw is not None
    offline = args.get("offline", False)
    if not isinstance(offline, bool):
        raise ToolError("'offline' must be true or false")
    path = Path(raw).expanduser()
    if not path.exists():
        raise ToolError(f"{raw}: no such file or directory (pass an absolute path)")
    folder = path if path.is_dir() else path.parent
    targets = _targets(args, read_python_version(folder) or None)
    try:
        results = check_lockfile(path, targets, offline=offline)
    except (SourceError, PyodideLockError) as e:
        raise ToolError(str(e)) from e
    except OSError as e:
        raise ToolError(f"{raw}: {e.strerror or e}") from e
    gaps = [r for r in results if not r.ok]
    return {
        "path": str(path.resolve()),
        "targets": [t.label(True) for t in targets],
        "packages": len(results),
        "ok": not gaps,
        "unknown": sum(1 for r in gaps if Status.UNKNOWN in r.statuses.values()),
        "gaps": [_result_dict(r) for r in gaps],
    }


def _list_targets(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "platforms": list(PLATFORMS),
        "default": list(DEFAULT_PLATFORMS),
        "groups": {"all": NATIVE_PLATFORMS, "everything": EVERY_NATIVE_PLATFORM, "mobile": MOBILE_PLATFORMS},
        "aliases": dict(ALIASES),
        "server_python": running_python(),
    }


_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "check_package": _check_package,
    "check_lockfile": _check_lockfile,
    "list_targets": _list_targets,
}


class Server:
    """The protocol state for one stdio connection."""

    def __init__(self) -> None:
        self.protocol_version = PROTOCOL_VERSIONS[-1]

    def handle(self, message: object) -> dict[str, Any] | list[dict[str, Any]] | None:
        """The response to one parsed message (or batch), or None for notifications."""
        if isinstance(message, list):  # JSON-RPC batches (only 2025-03-26 allowed them, but they cost nothing)
            if not message:
                return _error(None, INVALID_REQUEST, "empty batch")
            replies = [r for r in (self._one(m) for m in message) if r is not None]
            return replies or None
        return self._one(message)

    def _one(self, message: object) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(_id(message), INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method = message.get("method")
        if "id" not in message:  # a notification (initialized, cancelled, ...) or a response: nothing to send
            return None
        msg_id = message["id"]
        if not isinstance(msg_id, (str, int)) or isinstance(msg_id, bool):
            return _error(None, INVALID_REQUEST, "id must be a string or an integer")
        if not isinstance(method, str):
            return _error(msg_id, INVALID_REQUEST, "method must be a string")
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(msg_id, INVALID_PARAMS, "params must be an object")
        if method == "initialize":
            return _reply(msg_id, self._initialize(params))
        if method == "ping":
            return _reply(msg_id, {})
        if method == "tools/list":
            return _reply(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            if name not in _HANDLERS:
                return _error(msg_id, INVALID_PARAMS, f"unknown tool: {name!r}")
            arguments = params.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                return _error(msg_id, INVALID_PARAMS, "arguments must be an object")
            return _reply(msg_id, self._call(name, arguments))
        return _error(msg_id, METHOD_NOT_FOUND, f"method not found: {method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        asked = params.get("protocolVersion")
        self.protocol_version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1]
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "wheelgap", "version": __version__},
            "instructions": INSTRUCTIONS,
        }

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            # wheelgap's own messages go to stderr already; this keeps any stray print off the protocol stream.
            with contextlib.redirect_stdout(sys.stderr):
                data = _HANDLERS[name](arguments)
        except (ToolError, TargetError, SourceError, ValueError) as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except Exception as e:  # a bug: report it to the model rather than end the connection
            return {"content": [{"type": "text", "text": f"internal error: {type(e).__name__}: {e}"}], "isError": True}
        result: dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(data, indent=1)}], "isError": False}
        if self.protocol_version >= _STRUCTURED_FROM:
            result["structuredContent"] = data
        return result


def _id(message: object) -> Any:
    msg_id = message.get("id") if isinstance(message, dict) else None
    return msg_id if isinstance(msg_id, (str, int)) and not isinstance(msg_id, bool) else None


def _reply(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def serve(stdin: BinaryIO, stdout: BinaryIO) -> None:
    """Answer messages from stdin until it closes."""
    server = Server()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            reply: object = _error(None, PARSE_ERROR, f"parse error: {e}")
        else:
            try:
                reply = server.handle(message)
            except Exception as e:  # never let one bad message end the connection
                reply = _error(_id(message), INTERNAL_ERROR, f"internal error: {type(e).__name__}: {e}")
        if reply is not None:
            # ensure_ascii keeps every message on one line whatever the package metadata contains.
            stdout.write(json.dumps(reply, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args in (["--version"], ["-V"]):
        print(f"wheelgap-mcp {__version__}")
        return 0
    if args:
        print(
            "usage: wheelgap-mcp\n\nAn MCP server for wheelgap over stdio. Add it to your agent's MCP settings with "
            "the command `uvx --from wheelgap wheelgap-mcp`.",
            file=sys.stderr,
        )
        return 0 if args in (["-h"], ["--help"]) else 2
    try:
        serve(sys.stdin.buffer, sys.stdout.buffer)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
