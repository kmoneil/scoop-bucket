#!/usr/bin/env python3
"""Point bucket/hunk.json at a hunk release, and refuse to do it on anything unverified.

The manifest names three files from one release: the Windows binary for each of
two architectures, and the agent skill as a tarball, which both install. Each
has the digest Scoop checks after downloading it. A bump is the version, those
URLs and those digests, and nothing else in the file.

Each digest is read from the release's SHA256SUMS *and* re-derived from the file
downloaded from the URL the manifest will carry. SHA256SUMS is published by the
same workflow that built the files, so believing it alone is believing one
source twice; hashing the download proves the bytes a user will fetch are the
bytes the manifest names.

It writes nothing until every check has passed, so a failure leaves the manifest
alone rather than half-edited. It is kmoneil/homebrew-tap's bump-hunk.py, for a
JSON manifest instead of a Ruby formula.
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

# What each architecture installs, in the order its url and hash lists hold
# them: the release's file, and the name Scoop saves it under. The binary
# becomes hunk.exe. The skill becomes skill.download, which Scoop neither
# extracts nor installs 7-Zip to extract; post_install extracts it with
# Windows' own tar.
FILES = {
    "64bit": [("hunk-windows-amd64.exe", "hunk.exe"), ("hunk-skill.tar.gz", "skill.download")],
    "arm64": [("hunk-windows-arm64.exe", "hunk.exe"), ("hunk-skill.tar.gz", "skill.download")],
}

URL = re.compile(
    r"https://github\.com/(?P<repo>[^/]+/[^/]+)/releases/download/"
    r"(?P<tag>v[^/]+)/(?P<asset>[^/#]+)#/(?P<name>[^/]+)"
)


class Refused(Exception):
    """A check failed. The manifest is not written."""


def fetch(url: str) -> bytes:
    """Read a URL. A release asset is public: no token."""
    try:
        with urllib.request.urlopen(url) as response:  # noqa: S310 - https, fixed host
            return response.read()
    except urllib.error.HTTPError as err:
        raise Refused(f"{url} answered {err.code} {err.reason}") from err
    except urllib.error.URLError as err:
        raise Refused(f"{url} could not be reached: {err.reason}") from err


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
        raise Refused("SHA256SUMS held no digests, so nothing could be checked against it")
    return out


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
    for key, files in FILES.items():
        urls, hashes = arch[key].get("url"), arch[key].get("hash")
        if not isinstance(urls, list) or not isinstance(hashes, list):
            raise Refused(f"{key} needs a list of urls and a list of hashes")
        if len(urls) != len(files) or len(hashes) != len(files):
            raise Refused(
                f"{key} names {len(urls)} urls and {len(hashes)} hashes, "
                f"and this script knows {len(files)} of each"
            )
        for i, (url, (asset, name)) in enumerate(zip(urls, files)):
            found = URL.fullmatch(url) if isinstance(url, str) else None
            if not found:
                raise Refused(f"{key} url {i + 1} is not a release download this script can read")
            if found["repo"] != repo:
                raise Refused(f"{key} url {i + 1} points at {found['repo']}, not {repo}")
            if (found["asset"], found["name"]) != (asset, name):
                raise Refused(
                    f"{key} url {i + 1} fetches {found['asset']} as {found['name']}, "
                    f"and this script expects {asset} as {name}"
                )


def plan(path: Path, tag: str, repo: str) -> tuple[str, list[tuple[str, str]]]:
    """Work out the new manifest text without writing it."""
    manifest = json.loads(path.read_text())
    check_shape(manifest, repo)

    base = f"https://github.com/{repo}/releases/download/{tag}"
    published = read_checksums(fetch(f"{base}/SHA256SUMS").decode())

    # Each file once, though the skill is named by both architectures.
    digests: dict[str, str] = {}
    for files in FILES.values():
        for asset, _ in files:
            if asset in digests:
                continue
            if asset not in published:
                raise Refused(f"{tag} publishes no {asset}")
            want = published[asset]
            got = hashlib.sha256(fetch(f"{base}/{asset}")).hexdigest()
            if got != want:
                # The manifest and the bytes behind the URL disagree. Whichever
                # is wrong, the manifest must carry neither.
                raise Refused(
                    f"{asset} hashes to {got} and SHA256SUMS says {want}. "
                    "The file and the manifest disagree; nothing is written"
                )
            digests[asset] = want

    manifest["version"] = tag.removeprefix("v")
    for key, files in FILES.items():
        manifest["architecture"][key]["url"] = [f"{base}/{asset}#/{name}" for asset, name in files]
        manifest["architecture"][key]["hash"] = [digests[asset] for asset, _ in files]

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
    ap.add_argument("--tag", required=True, help="the hunk tag to point at, e.g. v0.2.8")
    ap.add_argument("--manifest", default="bucket/hunk.json", type=Path)
    ap.add_argument("--repo", default="kmoneil/hunk")
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
