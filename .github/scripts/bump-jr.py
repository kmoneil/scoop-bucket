#!/usr/bin/env python3
"""Point bucket/jr.json at a jr release, and refuse to do it on anything unverified.

The manifest names two files from one release: the full profile's Windows zip
for each of two architectures. Each has the digest Scoop checks after
downloading it and the directory Scoop extracts from it, and both carry the
version in their names. A bump is the version, those URLs, those digests and
those directories, and nothing else in the file.

Each digest is read from the release's checksums.txt *and* re-derived from the
file downloaded from the URL the manifest will carry. checksums.txt is published
by the same workflow that built the files, so believing it alone is believing
one source twice; hashing the download proves the bytes a user will fetch are
the bytes the manifest names. The downloads are kept, in JR_DOWNLOAD_DIR or
dist/, for verify-provenance.py to ask about.

It writes nothing until every check has passed, so a failure leaves the manifest
alone rather than half-edited. It is bump-hunk.py for jr's release, whose files
are zips named for their version and whose manifest is checksums.txt rather
than SHA256SUMS; one script that worked out which release it was looking at
would be guessing, which is the thing both exist to stop.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Scoop's architecture names and the release's platform for each. The bucket
# ships the full profile only, as the Homebrew tap does: the machine running
# `scoop install` belongs to a person, and the restricted profiles exist for
# containers, which fetch the zip directly.
FILES = {"64bit": "windows_amd64", "arm64": "windows_arm64"}
PROFILE = "jr-full"

URL = re.compile(
    r"https://github\.com/(?P<repo>[^/]+/[^/]+)/releases/download/"
    r"v(?P<tag>[^/]+)/(?P<name>jr-full_(?P<version>[^_/]+)_(?P<platform>[a-z0-9_]+))\.zip"
)


class Refused(Exception):
    """A check failed. The manifest is not written."""


def fetch(url: str, dest: Path | None = None) -> bytes:
    """Read a URL, saving it when asked. A release asset is public: no token."""
    try:
        with urllib.request.urlopen(url) as response:  # noqa: S310 - https, fixed host
            data = response.read()
    except urllib.error.HTTPError as err:
        raise Refused(f"{url} answered {err.code} {err.reason}") from err
    except urllib.error.URLError as err:
        raise Refused(f"{url} could not be reached: {err.reason}") from err
    if dest is not None:
        dest.write_bytes(data)
    return data


def read_checksums(text: str) -> dict[str, str]:
    """Parse `sha256sum` output into file name -> digest."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts[0], parts[1].lstrip("*")
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            out[name] = digest
    if not out:
        raise Refused("checksums.txt held no digests, so nothing could be checked against it")
    return out


def archive(version: str, platform: str) -> str:
    """The release's name for one platform's zip, without the extension."""
    return f"{PROFILE}_{version}_{platform}"


def check_shape(manifest: dict, repo: str) -> None:
    """Refuse a manifest whose shape is not the one this script knows.

    Rewriting a shape nobody has looked at is how a bump silently pairs a URL
    with somebody else's digest, so anything unexpected is for a person to read
    first.
    """
    if not isinstance(manifest.get("version"), str):
        raise Refused("the manifest has no version")
    arch = manifest.get("architecture")
    if not isinstance(arch, dict) or sorted(arch) != sorted(FILES):
        found = sorted(arch) if isinstance(arch, dict) else "none"
        raise Refused(
            f"the manifest's architectures are {found}, and this script knows {sorted(FILES)}"
        )
    for key, platform in FILES.items():
        entry = arch[key]
        url, hash_, extract = entry.get("url"), entry.get("hash"), entry.get("extract_dir")
        if not all(isinstance(v, str) for v in (url, hash_, extract)):
            raise Refused(f"{key} needs a url, a hash and an extract_dir, each a single string")
        found = URL.fullmatch(url)
        if not found:
            raise Refused(f"{key}'s url is not a release download this script can read")
        if found["repo"] != repo:
            raise Refused(f"{key}'s url points at {found['repo']}, not {repo}")
        if found["platform"] != platform:
            raise Refused(f"{key}'s url fetches the {found['platform']} zip, and this script expects {platform}")
        if found["version"] != found["tag"]:
            raise Refused(f"{key}'s url names {found['version']} under the tag v{found['tag']}")
        if extract != found["name"]:
            raise Refused(f"{key} extracts {extract} from {found['name']}.zip, which holds {found['name']}")


def plan(path: Path, tag: str, repo: str) -> tuple[str, list[tuple[str, str]]]:
    """Work out the new manifest text without writing it."""
    manifest = json.loads(path.read_text())
    check_shape(manifest, repo)

    version = tag.removeprefix("v")
    base = f"https://github.com/{repo}/releases/download/{tag}"
    published = read_checksums(fetch(f"{base}/checksums.txt").decode())

    downloads = Path(os.environ.get("JR_DOWNLOAD_DIR", "dist"))
    downloads.mkdir(parents=True, exist_ok=True)

    digests: dict[str, str] = {}
    for key, platform in FILES.items():
        name = archive(version, platform)
        zip_name = f"{name}.zip"
        if zip_name not in published:
            raise Refused(f"{tag} publishes no {zip_name}")
        want = published[zip_name]
        got = hashlib.sha256(fetch(f"{base}/{zip_name}", downloads / zip_name)).hexdigest()
        if got != want:
            # The manifest and the bytes behind the URL disagree. Whichever is
            # wrong, the bucket must carry neither.
            raise Refused(
                f"{zip_name} hashes to {got} and checksums.txt says {want}. "
                "The file and the manifest disagree; nothing is written"
            )
        digests[zip_name] = want
        entry = manifest["architecture"][key]
        entry["url"] = f"{base}/{zip_name}"
        entry["hash"] = want
        entry["extract_dir"] = name

    manifest["version"] = version
    text = json.dumps(manifest, indent=4, ensure_ascii=False) + "\n"
    return text, list(digests.items())


def emit(name: str, value: str) -> None:
    """Report to the workflow, when there is one to report to."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag", required=True, help="the jr tag to point at, e.g. v0.17.2")
    ap.add_argument("--manifest", default="bucket/jr.json", type=Path)
    ap.add_argument("--repo", default="kmoneil/jr")
    ap.add_argument(
        "--check",
        action="store_true",
        help="say what would change and write nothing",
    )
    args = ap.parse_args(argv)

    # vX.Y.Z and nothing else: a prerelease is not something to install.
    if not re.fullmatch(r"v\d+\.\d+\.\d+", args.tag):
        print(f"refused: {args.tag} is not a release tag", file=sys.stderr)
        return 2

    try:
        text, changed = plan(args.manifest, args.tag, args.repo)
    except Refused as err:
        print(f"refused: {err}", file=sys.stderr)
        return 1

    version = args.tag.removeprefix("v")
    emit("version", version)
    name = args.manifest.name

    if text == args.manifest.read_text():
        # Idempotent on purpose: a re-run of a release dispatches again, and the
        # second one has nothing to do rather than something to undo.
        print(f"{name} already points at {version}; nothing to do")
        emit("changed", "false")
        return 0

    for asset, digest in changed:
        print(f"{asset}  {digest}")
    emit("changed", "true")

    if args.check:
        print("--check: the manifest is unchanged on disk")
        return 0

    args.manifest.write_text(text)
    print(f"{name} now points at {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
