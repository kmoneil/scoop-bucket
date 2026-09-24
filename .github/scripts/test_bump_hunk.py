#!/usr/bin/env python3
"""Tests for bump-hunk.py, and mostly for the refusals.

The happy path is checked every release by the workflow that runs the script:
Scoop installs the manifest it wrote, on Windows, and runs the binary. What that
run cannot show is a refusal, because a healthy release does not produce one.
These do.

Every file is served different bytes, so every digest differs. That is what
lets a test see a digest land beside the wrong URL: with one body for all
three, a rewrite that gave 64bit the arm64 digest would pass.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

HERE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("bump_hunk", HERE / "bump-hunk.py")
bump = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bump)

OLD = "https://github.com/kmoneil/hunk/releases/download/v1.0.0"
BASE = "https://github.com/kmoneil/hunk/releases/download/v2.0.0"
ZERO = "0" * 64

MANIFEST = {
    "version": "1.0.0",
    "description": "Transactional multi-file text editor for coding agents",
    "homepage": "https://github.com/kmoneil/hunk",
    "license": "Apache-2.0",
    "architecture": {
        "64bit": {
            "url": [f"{OLD}/hunk-windows-amd64.exe#/hunk.exe", f"{OLD}/hunk-skill.tar.gz#/skill.download"],
            "hash": [ZERO, ZERO],
        },
        "arm64": {
            "url": [f"{OLD}/hunk-windows-arm64.exe#/hunk.exe", f"{OLD}/hunk-skill.tar.gz#/skill.download"],
            "hash": [ZERO, ZERO],
        },
    },
    "bin": "hunk.exe",
    "post_install": ["$skills = Join-Path $env:USERPROFILE '.claude\\skills'"],
    "notes": ["hunk's agent skill is in ~\\.claude\\skills\\hunk"],
}
TEXT = json.dumps(MANIFEST, indent=4) + "\n"

ASSETS = ["hunk-windows-amd64.exe", "hunk-windows-arm64.exe", "hunk-skill.tar.gz"]
# What a release publishes, the files no manifest here names included.
PUBLISHED = ASSETS + ["hunk-darwin-arm64", "hunk-darwin-amd64", "hunk-linux-arm64", "hunk-linux-amd64"]


def body(name: str) -> bytes:
    return f"the bytes of {name}".encode()


def digest(name: str) -> str:
    return hashlib.sha256(body(name)).hexdigest()


def honest() -> dict[str, str]:
    return {n: digest(n) for n in PUBLISHED}


def edited(change) -> str:
    """The manifest with one thing changed, as text."""
    manifest = copy.deepcopy(MANIFEST)
    change(manifest)
    return json.dumps(manifest, indent=4) + "\n"


class Stub:
    """Stands in for the network: a manifest, and different bytes per file."""

    def __init__(self, sums: dict[str, str], bodies: dict[str, bytes] | None = None):
        self.sums, self.bodies = sums, bodies or {}
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        name = url.rsplit("/", 1)[1]
        if name == "SHA256SUMS":
            return "".join(f"{d}  {n}\n" for n, d in self.sums.items()).encode()
        return self.bodies.get(name, body(name))


class PlanTest(unittest.TestCase):
    def setUp(self):
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = scratch / "hunk.json"
        self.path.write_text(TEXT)
        self.addCleanup(setattr, bump, "fetch", bump.fetch)

    def use(self, sums, bodies=None):
        bump.fetch = Stub(sums, bodies)
        return bump.fetch

    def refusal(self, text=None):
        if text is not None:
            self.assertNotEqual(text, TEXT, "the test's own edit did not happen")
            self.path.write_text(text)
        with self.assertRaises(bump.Refused) as caught:
            bump.plan(self.path, "v2.0.0", "kmoneil/hunk")
        return str(caught.exception)

    def test_a_manifest_the_file_contradicts_is_refused(self):
        """The one that matters: SHA256SUMS says one thing, the bytes another."""
        self.use(honest(), {"hunk-windows-arm64.exe": b"not what the manifest hashed"})
        self.assertIn("hunk-windows-arm64.exe hashes to", self.refusal())
        self.assertEqual(self.path.read_text(), TEXT, "the manifest was written anyway")

    def test_a_release_missing_a_binary_is_refused(self):
        sums = honest()
        del sums["hunk-windows-arm64.exe"]
        self.use(sums)
        self.assertIn("v2.0.0 publishes no hunk-windows-arm64.exe", self.refusal())

    def test_a_release_without_the_skill_is_refused(self):
        """Every release before v0.2.8: the binaries are there and the skill is not."""
        sums = honest()
        del sums["hunk-skill.tar.gz"]
        self.use(sums)
        self.assertIn("v2.0.0 publishes no hunk-skill.tar.gz", self.refusal())

    def test_an_empty_manifest_is_refused(self):
        self.use({})
        self.assertIn("SHA256SUMS held no digests", self.refusal())

    def test_a_missing_architecture_is_refused(self):
        self.use(honest())
        text = edited(lambda m: m["architecture"].pop("arm64"))
        self.assertIn("architectures are ['64bit'], and this script knows ['64bit', 'arm64']", self.refusal(text))

    def test_an_architecture_this_script_does_not_know_is_refused(self):
        self.use(honest())
        text = edited(lambda m: m["architecture"].update({"32bit": copy.deepcopy(m["architecture"]["64bit"])}))
        self.assertIn("architectures are ['32bit', '64bit', 'arm64']", self.refusal(text))

    def test_a_list_of_the_wrong_length_is_refused(self):
        self.use(honest())

        def drop_the_skill(m):
            m["architecture"]["64bit"]["url"].pop()
            m["architecture"]["64bit"]["hash"].pop()

        self.assertIn("64bit names 1 urls and 1 hashes", self.refusal(edited(drop_the_skill)))

    def test_a_url_in_another_repository_is_refused(self):
        self.use(honest())

        def move(m):
            m["architecture"]["arm64"]["url"][0] = m["architecture"]["arm64"]["url"][0].replace("kmoneil/hunk", "someone/hunk")

        self.assertIn("arm64 url 1 points at someone/hunk, not kmoneil/hunk", self.refusal(edited(move)))

    def test_a_skill_scoop_would_extract_is_refused(self):
        """Saved as .tar.gz, Scoop would extract it itself, and install 7-Zip to do it."""
        self.use(honest())

        def unrename(m):
            m["architecture"]["64bit"]["url"][1] = f"{OLD}/hunk-skill.tar.gz#/hunk-skill.tar.gz"

        self.assertIn(
            "64bit url 2 fetches hunk-skill.tar.gz as hunk-skill.tar.gz, "
            "and this script expects hunk-skill.tar.gz as skill.download",
            self.refusal(edited(unrename)),
        )

    def test_the_binaries_cannot_be_crossed(self):
        self.use(honest())

        def cross(m):
            m["architecture"]["64bit"]["url"][0] = f"{OLD}/hunk-windows-arm64.exe#/hunk.exe"

        self.assertIn(
            "64bit url 1 fetches hunk-windows-arm64.exe as hunk.exe, "
            "and this script expects hunk-windows-amd64.exe as hunk.exe",
            self.refusal(edited(cross)),
        )

    def test_a_url_this_script_cannot_read_is_refused(self):
        self.use(honest())

        def unnamed(m):
            m["architecture"]["64bit"]["url"][0] = f"{OLD}/hunk-windows-amd64.exe"

        self.assertIn("64bit url 1 is not a release download this script can read", self.refusal(edited(unnamed)))

    def test_a_manifest_without_a_version_is_refused(self):
        self.use(honest())
        self.assertIn("the manifest has no version", self.refusal(edited(lambda m: m.pop("version"))))

    def test_it_rewrites_the_version_urls_and_hashes_and_nothing_else(self):
        self.use(honest())
        text, _ = bump.plan(self.path, "v2.0.0", "kmoneil/hunk")
        after = json.loads(text)
        self.assertEqual(after["version"], "2.0.0")
        self.assertNotIn("v1.0.0", text)
        self.assertNotIn(ZERO, text)
        for key in ("64bit", "arm64"):
            after["architecture"][key]["url"] = MANIFEST["architecture"][key]["url"]
            after["architecture"][key]["hash"] = MANIFEST["architecture"][key]["hash"]
        after["version"] = MANIFEST["version"]
        self.assertEqual(after, MANIFEST, "something other than the version, urls and hashes changed")

    def test_every_url_keeps_its_own_digest(self):
        """A 64bit binary with the arm64 digest would fail every install."""
        self.use(honest())
        after = json.loads(bump.plan(self.path, "v2.0.0", "kmoneil/hunk")[0])
        for key, files in bump.FILES.items():
            urls, hashes = after["architecture"][key]["url"], after["architecture"][key]["hash"]
            for url, hash_, (asset, name) in zip(urls, hashes, files):
                self.assertEqual(url, f"{BASE}/{asset}#/{name}")
                self.assertEqual(hash_, digest(asset))

    def test_each_file_is_fetched_once(self):
        """The skill is named by both architectures and downloaded one time."""
        stub = self.use(honest())
        bump.plan(self.path, "v2.0.0", "kmoneil/hunk")
        self.assertEqual(stub.urls[0], f"{BASE}/SHA256SUMS", "the manifest was not read first")
        self.assertEqual(sorted(stub.urls[1:]), sorted(f"{BASE}/{a}" for a in ASSETS))

    def test_the_manifest_keeps_its_layout(self):
        """Four-space indent, keys in their order, and a final newline, as Scoop's own are."""
        self.use(honest())
        text, _ = bump.plan(self.path, "v2.0.0", "kmoneil/hunk")
        self.assertTrue(text.endswith("}\n"))
        self.assertTrue(text.startswith('{\n    "version": "2.0.0",\n    "description"'))
        self.assertEqual(list(json.loads(text)), list(MANIFEST))


class MainTest(unittest.TestCase):
    """The command line: what it refuses, what it writes, and what a re-run does."""

    def setUp(self):
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = scratch / "hunk.json"
        self.path.write_text(TEXT)
        self.outputs = scratch / "outputs"
        self.addCleanup(setattr, bump, "fetch", bump.fetch)
        bump.fetch = Stub(honest())
        self.enterContext(unittest.mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.outputs)}))

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bump.main(["--manifest", str(self.path), *args])
        return code, out.getvalue(), err.getvalue()

    def emitted(self):
        return dict(line.split("=", 1) for line in self.outputs.read_text().splitlines())

    def test_a_prerelease_is_refused(self):
        code, _, err = self.run_main("--tag", "v2.0.0-rc.1")
        self.assertEqual(code, 2)
        self.assertIn("v2.0.0-rc.1 is not a release tag", err)
        self.assertEqual(self.path.read_text(), TEXT)

    def test_a_refusal_exits_1_and_writes_nothing(self):
        bump.fetch = Stub({})
        code, _, err = self.run_main("--tag", "v2.0.0")
        self.assertEqual(code, 1)
        self.assertIn("refused: SHA256SUMS held no digests", err)
        self.assertEqual(self.path.read_text(), TEXT)

    def test_it_writes_the_manifest_and_says_so(self):
        code, out, _ = self.run_main("--tag", "v2.0.0")
        self.assertEqual(code, 0)
        self.assertIn("hunk.json now points at 2.0.0", out)
        self.assertIn(f"{BASE}/hunk-skill.tar.gz#/skill.download", self.path.read_text())
        self.assertEqual(self.emitted(), {"version": "2.0.0", "changed": "true"})

    def test_check_says_and_writes_nothing(self):
        code, out, _ = self.run_main("--tag", "v2.0.0", "--check")
        self.assertEqual(code, 0)
        self.assertIn("--check: the manifest is unchanged on disk", out)
        self.assertEqual(self.path.read_text(), TEXT)
        self.assertEqual(self.emitted()["changed"], "true")

    def test_a_second_run_has_nothing_to_do(self):
        self.run_main("--tag", "v2.0.0")
        written = self.path.read_text()
        self.outputs.unlink()
        code, out, _ = self.run_main("--tag", "v2.0.0")
        self.assertEqual(code, 0)
        self.assertIn("hunk.json already points at 2.0.0; nothing to do", out)
        self.assertEqual(self.path.read_text(), written)
        self.assertEqual(self.emitted(), {"version": "2.0.0", "changed": "false"})


class FetchTest(unittest.TestCase):
    """A network failure is a refusal that names the URL, not a traceback."""

    def refusal(self, error):
        with unittest.mock.patch.object(bump.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(bump.Refused) as caught:
                bump.fetch(f"{BASE}/SHA256SUMS")
        return str(caught.exception)

    def test_a_missing_file_is_refused(self):
        error = urllib.error.HTTPError(f"{BASE}/SHA256SUMS", 404, "Not Found", None, None)
        self.assertIn("SHA256SUMS answered 404 Not Found", self.refusal(error))

    def test_an_unreachable_host_is_refused(self):
        self.assertIn("could not be reached: no route", self.refusal(urllib.error.URLError("no route")))


if __name__ == "__main__":
    unittest.main()
