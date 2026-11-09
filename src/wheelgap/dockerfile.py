"""Turn the final stage of a Dockerfile into a wheelgap target, following named stages back to an image."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

# Debian release in the image tag -> its glibc version.
DEBIAN_GLIBC = {"bullseye": "2.31", "bookworm": "2.36", "trixie": "2.41"}
ALPINE_MUSL = "1.2"  # Alpine 3.13 and later; 3.12 and earlier ship musl 1.1
_ALPINE_RE = re.compile(r"^alpine(\d+)\.(\d+)")
# docker --platform -> wheelgap arch
ARCHES = {
    "linux/amd64": "x86_64",
    "linux/x86_64": "x86_64",
    "linux/arm64": "aarch64",
    "linux/arm64/v8": "aarch64",
    "linux/aarch64": "aarch64",
}
WINDOWS_VARIANTS = ("windowsservercore", "nanoserver")
IMAGE_NAMES = ("python", "library/python", "docker.io/library/python", "docker.io/python")
UV_IMAGE = "ghcr.io/astral-sh/uv"

_TAG_RE = re.compile(r"^(\d+)\.(\d+)(?:\.\d+)?(?:(?:a|b|rc)\d+)?(?:-(.+))?$")
_UV_TAG_PREFIX_RE = re.compile(r"^(?:\d+\.\d+(?:\.\d+)?-)?python")


class DockerfileError(ValueError):
    pass


@dataclass(frozen=True)
class ImageTarget:
    python: str
    platform: str
    glibc: str | None = None
    musl: str | None = None


# BuildKit's heredoc rule: a shell-form word of RUN, COPY or ADD that starts with `<<` (or `<<-`),
# optionally after a file descriptor number. The rest of the word is the delimiter.
_HEREDOC_WORD_RE = re.compile(r"^\d*<<(-?)([^<]+)$")
_HEREDOC_INSTRUCTIONS = {"RUN", "COPY", "ADD"}


def _heredocs(instruction: str) -> list[tuple[bool, str]]:
    """The heredocs an instruction opens, as (strip leading tabs, delimiter)."""
    words = instruction.split(None, 2)
    if words and words[0].upper() == "ONBUILD":
        words = words[1:2] + (words[2].split(None, 1) if len(words) > 2 else [])
    if len(words) < 2 or words[0].upper() not in _HEREDOC_INSTRUCTIONS or words[1].startswith("["):
        return []  # other instructions and exec (JSON) form have no heredocs
    lexer = shlex.shlex(words[1], posix=False)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:  # an unbalanced quote: not something to guess about
        return []
    found = []
    for token in tokens:
        if m := _HEREDOC_WORD_RE.match(token):
            delimiter = m.group(2)
            if len(delimiter) >= 2 and delimiter[0] == delimiter[-1] and delimiter[0] in "'\"":
                delimiter = delimiter[1:-1]
            found.append((bool(m.group(1)), delimiter))
    return found


def _from_lines(text: str) -> list[str]:
    """The arguments of every FROM instruction in order, with line continuations joined.

    Heredoc bodies (`RUN <<EOF ... EOF`) are skipped, so a Python `from x import y` in one isn't
    read as a FROM. Bodies are read line by line as written, as Docker does.
    """
    found = []
    pending: list[tuple[bool, str]] = []  # heredocs still open
    parts: list[str] = []  # an instruction continued with a trailing backslash
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if pending:
            strip_tabs, delimiter = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                pending.pop(0)
            continue
        if parts and line.lstrip().startswith("#"):
            continue  # comments inside a continued instruction are dropped
        if line.rstrip().endswith("\\"):
            parts.append(line.rstrip()[:-1])
            continue
        instruction = " ".join([*parts, line]).strip()
        parts = []
        words = instruction.split(None, 1)
        if len(words) == 2 and words[0].upper() == "FROM":
            found.append(words[1])
        elif words and not words[0].startswith("#"):
            pending = _heredocs(instruction)
    if pending:
        raise DockerfileError(f"heredoc <<{pending[0][1]} is never closed")
    return found


def last_from(text: str) -> str:
    """The arguments of the last FROM instruction, with line continuations joined."""
    found = _from_lines(text)
    if not found:
        raise DockerfileError("no FROM line found")
    return found[-1]


def _split_from(args: str) -> tuple[str, str | None, str | None]:
    """`[--platform=P] image [AS name]` -> (image, platform, stage name)."""
    words = args.split()
    platform = None
    while words and words[0].startswith("--"):
        flag = words.pop(0)
        if flag.startswith("--platform="):
            platform = flag.split("=", 1)[1]
        elif flag == "--platform" and words:
            platform = words.pop(0)
    if not words:
        raise DockerfileError(f"FROM {args}: no image")
    stage = words[2].lower() if len(words) >= 3 and words[1].upper() == "AS" else None
    return words[0], platform, stage


def parse_from(args: str) -> ImageTarget:
    """Parse `[--platform=P] image[:tag][@digest] [AS name]`."""
    image, platform, _ = _split_from(args)
    return parse_image(image, platform)


def final_image(text: str) -> tuple[str, str | None]:
    """The image the last stage is built on, and its platform.

    `FROM base AS runtime` builds on an earlier stage called `base`; that is followed back (through
    any chain of stages) to a real image. Like Docker, a stage keeps the platform of the stage it
    builds on: a `--platform` on `FROM base` doesn't change what `base` was built for.
    """
    stages: dict[str, tuple[str, str | None]] = {}
    lines = _from_lines(text)
    if not lines:
        raise DockerfileError("no FROM line found")
    for args in lines[:-1]:
        image, platform, stage = _split_from(args)
        if (base := stages.get(image)) is not None:  # AS names are lowercased, references aren't
            image, platform = base
        if stage:
            stages[stage] = (image, platform)
    image, platform, _ = _split_from(lines[-1])
    if (base := stages.get(image)) is not None:
        return base
    if stages and not any(c in image for c in ":/@$") and image.lower() not in (*IMAGE_NAMES, "scratch"):
        known = ", ".join(stages)
        raise DockerfileError(
            f"FROM {image}: not an earlier stage ({known}) or an official python image, pass -p/-t instead"
        )
    return image, platform


def parse_image(image: str, platform: str | None = None) -> ImageTarget:
    if "$" in image or (platform and "$" in platform):
        raise DockerfileError(f"{image}: build arguments are not supported, pass -p/-t instead")
    image = image.split("@", 1)[0]
    name, _, tag = image.rpartition(":") if ":" in image.rsplit("/", 1)[-1] else (image, "", "")
    name = name.lower()
    if name not in IMAGE_NAMES and name != UV_IMAGE:
        raise DockerfileError(
            f"{image}: only the official python images and Python-tagged Astral uv images are supported, "
            "pass -p/-t instead"
        )
    if name == UV_IMAGE:
        prefix = _UV_TAG_PREFIX_RE.match(tag)
        tag = tag[prefix.end() :] if prefix else ""
    m = _TAG_RE.match(tag)
    if not m:
        if name == UV_IMAGE:
            raise DockerfileError(f"{image}: the tag must specify a Python version like python3.13-alpine")
        raise DockerfileError(f"{image}: the tag must start with a Python version like 3.13")
    python = f"{m.group(1)}.{m.group(2)}"
    variant = (m.group(3) or "").lower().split("-") if m.group(3) else []

    if alpine := next((part for part in variant if part.startswith("alpine")), None):
        family, glibc, musl = "musl", None, ALPINE_MUSL
        if (m := _ALPINE_RE.match(alpine)) and (int(m.group(1)), int(m.group(2))) <= (3, 12):
            musl = "1.1"
    elif any(part in WINDOWS_VARIANTS for part in variant):
        if platform:
            raise DockerfileError(f"{image}: --platform is not supported for Windows images")
        return ImageTarget(python, "win-amd64")
    else:
        family, musl = "linux", None
        unknown = [part for part in variant if part not in DEBIAN_GLIBC and part != "slim"]
        if unknown:
            raise DockerfileError(f"{image}: unknown image variant '{'-'.join(unknown)}'")
        glibc = next((DEBIAN_GLIBC[part] for part in variant if part in DEBIAN_GLIBC), None)

    arch = "x86_64"
    if platform:
        if platform.lower() not in ARCHES:
            raise DockerfileError(f"--platform={platform}: only {', '.join(ARCHES)} are supported")
        arch = ARCHES[platform.lower()]
    return ImageTarget(python, f"{family}-{arch}", glibc, musl)


def read_dockerfile(path: Path) -> ImageTarget:
    try:
        return parse_image(*final_image(path.read_text(encoding="utf-8")))
    except DockerfileError as e:
        raise DockerfileError(f"{path}: {e}") from None
