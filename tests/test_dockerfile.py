import json

import pytest

from wheelgap.cli import main
from wheelgap.dockerfile import DockerfileError, ImageTarget, last_from, parse_from, read_dockerfile


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("python:3.13-alpine", ImageTarget("3.13", "musl-x86_64", musl="1.2")),
        ("--platform=linux/arm64 python:3.12-slim", ImageTarget("3.12", "linux-aarch64")),
        ("python:3.14-slim-trixie", ImageTarget("3.14", "linux-x86_64", glibc="2.41")),
        ("python:3.13-bookworm", ImageTarget("3.13", "linux-x86_64", glibc="2.36")),
        ("python:3.11-slim-bullseye", ImageTarget("3.11", "linux-x86_64", glibc="2.31")),
        ("python:3.13.1-alpine3.22 AS runtime", ImageTarget("3.13", "musl-x86_64", musl="1.2")),
        ("--platform linux/arm64 python:3.13-alpine", ImageTarget("3.13", "musl-aarch64", musl="1.2")),
        ("docker.io/library/python:3.12", ImageTarget("3.12", "linux-x86_64")),
        ("python:3.13-slim@sha256:" + "0" * 64, ImageTarget("3.13", "linux-x86_64")),
        ("python:3.15.0a1-slim", ImageTarget("3.15", "linux-x86_64")),
        ("python:3.13-windowsservercore-ltsc2022", ImageTarget("3.13", "win-amd64")),
        ("ghcr.io/astral-sh/uv:python3.13-bookworm-slim", ImageTarget("3.13", "linux-x86_64", glibc="2.36")),
        ("ghcr.io/astral-sh/uv:python3.14-trixie-slim", ImageTarget("3.14", "linux-x86_64", glibc="2.41")),
        ("ghcr.io/astral-sh/uv:0.13.7-python3.13-trixie-slim", ImageTarget("3.13", "linux-x86_64", glibc="2.41")),
        ("ghcr.io/astral-sh/uv:0.13-python3.13-trixie-slim", ImageTarget("3.13", "linux-x86_64", glibc="2.41")),
        ("--platform=linux/arm/v7 python:3.13-alpine", ImageTarget("3.13", "musl-armv7l", musl="1.2")),
        ("--platform=linux/ppc64le python:3.13-bookworm", ImageTarget("3.13", "linux-ppc64le", glibc="2.36")),
        ("--platform=linux/riscv64 python:3.14-trixie", ImageTarget("3.14", "linux-riscv64", glibc="2.41")),
        (
            "--platform=linux/arm64 ghcr.io/astral-sh/uv:python3.12-alpine3.23",
            ImageTarget("3.12", "musl-aarch64", musl="1.2"),
        ),
    ],
)
def test_parse_from(line, expected):
    assert parse_from(line) == expected


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("ubuntu:24.04", "only the official python images"),
        ("python", "must start with a Python version"),
        ("python:latest", "must start with a Python version"),
        ("ghcr.io/astral-sh/uv:bookworm-slim", r"must specify a Python version like python3\.13-alpine"),
        ("ghcr.io/astral-sh/uv:3.13-alpine", r"must specify a Python version like python3\.13-alpine"),
        ("ghcr.io/astral-sh/uv:0.13.7-alpine", r"must specify a Python version like python3\.13-alpine"),
        ("python:${PY}-slim", "build arguments"),
        ("--platform=$BUILDPLATFORM python:3.13", "build arguments"),
        ("--platform=linux/386 python:3.13", "only linux/amd64"),
        ("--platform=linux/s390x ghcr.io/astral-sh/uv:python3.13-bookworm-slim", "only published for linux/amd64"),
        ("python:3.13-slim-buster", "unknown image variant 'buster'"),
    ],
)
def test_parse_from_errors(line, message):
    with pytest.raises(DockerfileError, match=message):
        parse_from(line)


def test_last_from_takes_the_final_stage():
    text = (
        "# syntax=docker/dockerfile:1\n"
        "FROM python:3.12-bookworm AS build\n"
        "RUN pip wheel . \\\n"
        "    -w /wheels\n"
        "from --platform=linux/arm64 \\\n"
        "    python:3.13-alpine\n"
        "COPY --from=build /wheels /wheels\n"
    )
    assert last_from(text) == "--platform=linux/arm64     python:3.13-alpine"  # joined as Docker joins
    assert parse_from(last_from(text)) == ImageTarget("3.13", "musl-aarch64", musl="1.2")


def test_no_from(tmp_path):
    path = tmp_path / "Dockerfile"
    path.write_text("RUN echo hi\n")
    with pytest.raises(DockerfileError, match="no FROM line"):
        read_dockerfile(path)


@pytest.mark.parametrize("image", ["python:3.13-alpine", "ghcr.io/astral-sh/uv:python3.13-alpine"])
def test_cli_dockerfile(capsys, fixtures, tmp_path, image):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"FROM {image}\n")
    code = main([str(fixtures / "uvproj"), "--dockerfile", str(dockerfile), "--json"])
    captured = capsys.readouterr()
    assert code in (0, 1), captured.err
    data = json.loads(captured.out)
    assert [(t["python"], t["platform"]) for t in data["targets"]] == [("3.13", "musl-x86_64")]


def test_cli_flags_beat_dockerfile(capsys, fixtures, tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.13-alpine\n")
    main([str(fixtures / "uvproj"), "--dockerfile", str(dockerfile), "-t", "win-amd64", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert [(t["python"], t["platform"]) for t in data["targets"]] == [("3.13", "win-amd64")]


def test_cli_bad_dockerfile_exit_2(capsys, fixtures, tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM node:22\n")
    assert main([str(fixtures / "uvproj"), "--dockerfile", str(dockerfile)]) == 2
    assert "only the official python images" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("line", "musl"),
    [
        ("python:3.9-alpine3.12", "1.1"),
        ("python:3.9-alpine3.10", "1.1"),
        ("python:3.13-alpine3.13", "1.2"),
        ("python:3.13-alpine", "1.2"),
    ],
)
def test_alpine_musl_version(line, musl):
    assert parse_from(line).musl == musl  # Alpine 3.12 and earlier ship musl 1.1


@pytest.mark.parametrize("platform", ["linux/arm64", "linux/arm64,linux/amd64"])
def test_cli_docker_platform_names_keep_the_images_libc(capsys, fixtures, tmp_path, platform):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.13-alpine\n")
    main([str(fixtures / "uvproj"), "--dockerfile", str(dockerfile), "-t", platform, "--json"])
    platforms = [t["platform"] for t in json.loads(capsys.readouterr().out)["targets"]]
    assert all(p.startswith("musl-") for p in platforms)  # used to be linux-aarch64 (glibc)


def test_cli_wheelgap_names_still_win_over_the_image(capsys, fixtures, tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.13-alpine\n")
    main([str(fixtures / "uvproj"), "--dockerfile", str(dockerfile), "-t", "linux-aarch64", "--json"])
    assert [t["platform"] for t in json.loads(capsys.readouterr().out)["targets"]] == ["linux-aarch64"]


def _dockerfile(tmp_path, text):
    path = tmp_path / "Dockerfile"
    path.write_text(text)
    return path


def test_named_stage_is_followed(tmp_path):
    text = "FROM python:3.13-alpine AS base\nRUN apk add --no-cache libpq\n\nFROM base AS runtime\nCOPY . /app\n"
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.13", "musl-x86_64", musl="1.2")


def test_chain_of_stages_and_platforms(tmp_path):
    text = (
        "FROM --platform=linux/arm64 python:3.12-slim-bookworm AS base\n"
        "FROM base AS deps\n"
        "FROM python:3.11 AS unrelated\n"
        "FROM deps\n"
    )
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.12", "linux-aarch64", glibc="2.36")
    # As in Docker, a stage keeps the platform of the stage it builds on; --platform here changes nothing.
    text += "FROM --platform=linux/amd64 deps\n"
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.12", "linux-aarch64", glibc="2.36")
    text = "FROM python:3.13-slim AS base\nFROM --platform=linux/arm64 base\n"
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.13", "linux-x86_64")


def test_stage_names_are_lowercased_like_docker(tmp_path):
    # Docker lowercases the AS name but looks a reference up as written (and image names are lowercase).
    text = "FROM python:3.13 as Base\nFROM base\n"
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.13", "linux-x86_64")
    with pytest.raises(DockerfileError, match="FROM BASE: not an earlier stage"):
        read_dockerfile(_dockerfile(tmp_path, "FROM python:3.13 as Base\nFROM BASE\n"))


def test_unknown_stage_is_a_clear_error(tmp_path):
    text = "FROM python:3.13-alpine AS base\nFROM bse\n"
    with pytest.raises(DockerfileError, match=r"FROM bse: not an earlier stage \(base\) or an official python image"):
        read_dockerfile(_dockerfile(tmp_path, text))


def test_scratch_after_stages_keeps_the_usual_error(tmp_path):
    text = "FROM python:3.13-alpine AS build\nRUN pip wheel . -w /w\nFROM scratch\nCOPY --from=build /w /w\n"
    with pytest.raises(DockerfileError, match="scratch: only the official python images"):
        read_dockerfile(_dockerfile(tmp_path, text))


@pytest.mark.parametrize(
    "line",
    [
        "RUN echo $((1<<SHIFT))",
        "RUN echo $((1 << BITS))",
        'RUN python -c "print(1<<n)"',
        "CMD cat <<EOF",
        "ENV X=<<EOF",
        'LABEL note="a<<b"',
        'RUN ["sh", "-c", "cat <<EOF"]',
        "RUN cat <<<word",
    ],
)
def test_lines_with_heredoc_like_text_never_hide_the_final_from(tmp_path, line):
    # wheelgap doesn't parse heredocs, so whatever these mean to Docker, the final FROM is still found.
    text = f"FROM python:3.13-alpine AS base\n{line}\nFROM python:3.12-slim\n"
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.12", "linux-x86_64")


def test_unknown_stage_lists_only_earlier_stages(tmp_path):
    text = "FROM python:3.13-alpine AS base\nFROM bse AS final\n"
    with pytest.raises(DockerfileError, match=r"not an earlier stage \(base\) or"):
        read_dockerfile(_dockerfile(tmp_path, text))
    # A stage built on a non-python image gets the usual image error, not a stage one.
    with pytest.raises(DockerfileError, match="alpine: only the official python images"):
        read_dockerfile(_dockerfile(tmp_path, "FROM alpine AS base\nFROM base\n"))


def test_python_lines_starting_with_from_are_not_errors(tmp_path):
    text = "FROM python:3.13 AS a\nRUN python - <<EOF\nfromtimestamp = 1\nEOF\nFROM python:3.12\n"
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.12", "linux-x86_64")


def test_python_imports_in_heredocs_are_not_from_lines(tmp_path):
    text = (
        "FROM python:3.12-slim AS app\n"
        "COPY . /app\n"
        "FROM python:3.13-alpine\n"
        "COPY --from=app /app /app\n"
        "RUN python - <<EOF\n"
        "from app import main\n"
        "EOF\n"
    )
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.13", "musl-x86_64", musl="1.2")


@pytest.mark.parametrize(
    "text",
    [
        # A blank line inside a continued instruction is skipped, so FROM base is part of the RUN.
        "FROM python:3.12-slim AS base\nFROM python:3.13-alpine\nRUN rm -rf /tmp/* \\\n\nFROM base\n",
        # The same with a comment line in between.
        "FROM python:3.12-slim AS base\nFROM python:3.13-alpine\nRUN rm -rf /tmp/* \\\n# note\nFROM base\n",
        # With `# escape=` + "`", a trailing backtick continues the line (and a backslash doesn't).
        "# escape=`\nFROM python:3.12-windowsservercore AS base\nFROM python:3.13-alpine\nRUN echo `\nFROM base\n",
    ],
)
def test_continuations_like_docker(tmp_path, text):
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.13", "musl-x86_64", musl="1.2")


@pytest.mark.parametrize(
    "text",
    [
        # A comment ending in a backslash doesn't continue onto the next line.
        "FROM python:3.13-alpine AS app\n# path C:\\\nFROM python:3.12-slim\n",
        # A doubled backslash is an escaped backslash, not a continuation.
        "FROM python:3.13-alpine AS app\nRUN echo \\\\\nFROM python:3.12-slim\n",
        # Under `# escape=` + "`", a trailing backslash is just a character.
        "# escape=`\nFROM python:3.13-alpine AS app\nRUN dir C:\\\nFROM python:3.12-slim\n",
    ],
)
def test_lines_that_do_not_continue(tmp_path, text):
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.12", "linux-x86_64")


@pytest.mark.parametrize(
    "text",
    [
        "FROM python:3.12-alpine AS build\nFROM --platform=linux/arm64 \\\n    python:3.11-slim \\\n# end\n",
        "FROM python:3.12-alpine AS build\nFROM --platform=linux/arm64 python:3.11-slim \\",
        "\ufeffFROM --platform=linux/arm64 python:3.11-slim\n",  # a UTF-8 BOM, which Docker strips
        "FROM --platform=linux/arm64 python:3.1\\\n1-slim\n",  # no space is added at the join
    ],
)
def test_final_line_edge_cases(tmp_path, text):
    path = tmp_path / "Dockerfile"
    path.write_text(text, encoding="utf-8")
    assert read_dockerfile(path) == ImageTarget("3.11", "linux-aarch64")


def test_escape_directive_rules(tmp_path):
    # An unknown directive-looking comment ends the directives, so the later escape= is ignored.
    text = "# version=2\n# escape=`\nFROM python:3.12-alpine\nRUN echo \\\nFROM python:3.11-slim\n"
    assert read_dockerfile(_dockerfile(tmp_path, text)) == ImageTarget("3.12", "musl-x86_64", musl="1.2")
    with pytest.raises(DockerfileError, match="invalid escape directive"):
        read_dockerfile(_dockerfile(tmp_path, "# escape=x\nFROM python:3.12\n"))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("# escape=`\n# escape=\\\nFROM python:3.12\n", "only one escape parser directive"),
        ("# syntax=docker/dockerfile:1\n# syntax=docker/dockerfile:1\nFROM python:3.12\n", "only one syntax"),
        ("FROM python:3.12 AS a\nFROM\\\npython:3.11\n", "unknown instruction 'FROMpython:3.11'"),
        ("FROM python:3.12 AS a\nFROM\n", "FROM with no image"),
    ],
)
def test_dockerfiles_docker_rejects(tmp_path, text, message):
    with pytest.raises(DockerfileError, match=message):
        read_dockerfile(_dockerfile(tmp_path, text))
