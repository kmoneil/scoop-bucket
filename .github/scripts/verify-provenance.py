#!/usr/bin/env python3
"""Check that every file a manifest names was built by the release it claims.

A bump script proves the bytes behind each URL are the bytes the release's
manifest names. The manifest is published by the same workflow as the files, so
that proves they agree, not where they came from. A release that attests its
files can say where, and `gh attestation verify` checks what it says: a
Sigstore-signed statement that a digest was built by a named workflow in a
named repository, from a named ref, on a named kind of runner.

Every file the manifest names is asked all four, and each must answer yes:

- it is attested, in the release's repository;
- by the workflow the caller named, not by any workflow in that repository;
- built from the tag being bumped to, not from a branch or an earlier tag;
- on a runner GitHub hosts.

What is verified is exactly what a user will download, since the names come
from the manifest, and the copies asked about are the ones the bump script
downloaded and hashed. A manifest naming no release file, or a file that was not
downloaded, is refused rather than passed: a check over nothing says nothing.

It is kmoneil/homebrew-tap's verify-provenance.py, reading a Scoop manifest
instead of a formula.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path


class Refused(Exception):
    """A check failed. Nothing is committed."""


def files_named(manifest: Path) -> list[str]:
    """The release files a manifest points at, in the order it names them."""
    data = json.loads(manifest.read_text())
    names: list[str] = []
    for entry in (data.get("architecture") or {}).values():
        urls = entry.get("url", [])
        for url in [urls] if isinstance(urls, str) else urls:
            name = url.split("#", 1)[0].rsplit("/", 1)[-1]
            if "/releases/download/" in url and name not in names:
                names.append(name)
    if not names:
        raise Refused(f"{manifest} names no release file, so there is nothing to verify")
    return names


def describe(report: str) -> str:
    """One line for the log, from gh's JSON. gh's exit status is the verdict."""
    try:
        cert = json.loads(report)[0]["verificationResult"]["signature"]["certificate"]
        return (
            f"built by {cert['buildSignerURI']} at {cert['sourceRepositoryDigest'][:12]}, "
            f"{cert['runInvocationURI']}"
        )
    except (ValueError, LookupError, TypeError):
        return "verified, though gh's report of it could not be read"


def verify(
    manifest: Path,
    downloads: Path,
    repo: str,
    workflow: str,
    tag: str,
    run: Callable[..., subprocess.CompletedProcess] | None = None,
) -> list[tuple[str, str]]:
    """Ask gh about every file the manifest names, and refuse on any no."""
    run = run or subprocess.run
    names = files_named(manifest)

    # Every file is there before gh is asked about any, so a missing one is
    # refused for what it is rather than after half the answers are in.
    for name in names:
        if not (downloads / name).is_file():
            raise Refused(
                f"{downloads / name} does not exist: the bump did not download it, "
                "so its provenance cannot be checked"
            )

    verified = []
    for name in names:
        result = run(
            [
                "gh", "attestation", "verify", str(downloads / name),
                "--repo", repo,
                "--signer-workflow", workflow,
                "--source-ref", f"refs/tags/{tag}",
                "--deny-self-hosted-runners",
                "--format", "json",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            why = (result.stderr.strip().splitlines() or ["gh gave no reason"])[-1]
            raise Refused(f"{name} has no build provenance from {workflow} at {tag}: {why}")
        verified.append((name, describe(result.stdout)))
    return verified


def main(argv: list[str] | None = None, run=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--manifest", required=True, type=Path, help="the manifest, e.g. bucket/jr.json")
    ap.add_argument(
        "--downloads",
        default=Path("dist"),
        type=Path,
        help="where the bump script saved what it downloaded (default: dist)",
    )
    ap.add_argument("--repo", required=True, help="the release's repository, e.g. kmoneil/jr")
    ap.add_argument(
        "--workflow",
        required=True,
        help="the workflow that attests it, e.g. kmoneil/jr/.github/workflows/release.yml",
    )
    ap.add_argument("--tag", required=True, help="the tag the files were built from, e.g. v0.17.2")
    args = ap.parse_args(argv)

    try:
        verified = verify(args.manifest, args.downloads, args.repo, args.workflow, args.tag, run)
    except Refused as err:
        print(f"refused: {err}", file=sys.stderr)
        return 1

    print(f"build provenance verified on {len(verified)} files, from {args.workflow} at {args.tag}:")
    for name, how in verified:
        print(f"  {name}: {how}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
