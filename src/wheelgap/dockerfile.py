"""Turn the final stage of a Dockerfile into a wheelgap target, following named stages back to an image."""

from __future__ import annotations

import re
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


# A heredoc start like `RUN <<EOF`, `<<-'EOF'` or `COPY <<"conf" /etc/x` (but not bash's `<<<`).
_HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)(-?)\s*([\"']?)([A-Za-z_][A-Za-z0-9_]*)\2")


def _from_lines(text: str) -> list[str]:
    """The arguments of every FROM instruction in order, with line continuations joined.

    Heredoc bodies are skipped, so a Python `from x import y` inside `RUN <<EOF` isn't read as a FROM.
    """
    joined = re.sub(r"\\\r?\n", " ", text)
    found = []
    pending: list[tuple[bool, str]] = []  # heredocs still open: (tabs stripped, delimiter)
    for line in joined.splitlines():
        if pending:
            strip_tabs, delimiter = pending[0]
            if (line.lstrip("\t") if strip_tabs else line).rstrip("\r") == delimiter:
                pending.pop(0)
            continue
        words = line.strip().split(None, 1)
        if len(words) == 2 and words[0].upper() == "FROM":
            found.append(words[1])
        elif words and not words[0].startswith("#"):
            pending = [(bool(m.group(1)), m.group(3)) for m in _HEREDOC_RE.finditer(line)]
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
    image, platform = "", None
    lines = _from_lines(text)
    if not lines:
        raise DockerfileError("no FROM line found")
    for args in lines:
        image, platform, stage = _split_from(args)
        if (base := stages.get(image.lower())) is not None:
            image, platform = base
        if stage:
            stages[stage] = (image, platform)
    bare = not any(c in image for c in ":/@$") and image.lower() not in (*IMAGE_NAMES, "scratch")
    if stages and bare:
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
