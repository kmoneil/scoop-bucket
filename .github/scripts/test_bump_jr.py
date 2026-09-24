#!/usr/bin/env python3
"""Tests for bump-jr.py, and mostly for the refusals.

The happy path is checked every release by the workflow that runs the script:
Scoop installs the manifest it wrote, on Windows, and runs the binary. What that
run cannot show is a refusal, because a healthy release does not produce one.
These do.

Every file is served different bytes, so every digest differs. That is what
lets a test see a digest land beside the wrong URL: with one body for both zips,
a rewrite that gave 64bit the arm64 digest would pass.
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
SPEC = importlib.util.spec_from_file_location("bump_jr", HERE / "bump-jr.py")
bump = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bump)

OLD = "https://github.com/kmoneil/jr/releases/download/v1.0.0"
BASE = "https://github.com/kmoneil/jr/releases/download/v2.0.0"
ZERO = "0" * 64

MANIFEST = {
    "version": "1.0.0",
    "description": "Jira client whose output is a versioned contract, for scripts and agents",
    "homepage": "https://github.com/kmoneil/jr",
    "license": "Apache-2.0",
    "architecture": {
        "64bit": {
            "url": f"{OLD}/jr-full_1.0.0_windows_amd64.zip",
            "hash": ZERO,
            "extract_dir": "jr-full_1.0.0_windows_amd64",
        },
        "arm64": {
            "url": f"{OLD}/jr-full_1.0.0_windows_arm64.zip",
            "hash": ZERO,
            "extract_dir": "jr-full_1.0.0_windows_arm64",
        },
    },
    "bin": "jr.exe",
    "post_install": ["$skill = Join-Path $env:USERPROFILE '.claude\\skills\\jr'"],
    "notes": ["jr's agent skill is in ~\\.claude\\skills\\jr"],
}
TEXT = json.dumps(MANIFEST, indent=4) + "\n"

ZIPS = ["jr-full_2.0.0_windows_amd64.zip", "jr-full_2.0.0_windows_arm64.zip"]
# What a release publishes, the files no manifest here names included.
PUBLISHED = ZIPS + [
    f"jr-{profile}_2.0.0_{platform}"
    for profile in ("full", "agent", "reader", "ci")
    for platform in ("linux_amd64.tar.gz", "darwin_arm64.tar.gz", "windows_amd64.zip")
    if not (profile == "full" and platform == "windows_amd64.zip")
]


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

    def __call__(self, url: str, dest: Path | None = None) -> bytes:
        self.urls.append(url)
        name = url.rsplit("/", 1)[1]
        if name == "checksums.txt":
            return "".join(f"{d}  {n}\n" for n, d in self.sums.items()).encode()
        data = self.bodies.get(name, body(name))
        if dest is not None:
            dest.write_bytes(data)
        return data


class PlanTest(unittest.TestCase):
    def setUp(self):
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = scratch / "jr.json"
        self.path.write_text(TEXT)
        self.downloads = scratch / "dist"
        self.enterContext(unittest.mock.patch.dict(os.environ, {"JR_DOWNLOAD_DIR": str(self.downloads)}))
        self.addCleanup(setattr, bump, "fetch", bump.fetch)

    def use(self, sums, bodies=None):
        bump.fetch = Stub(sums, bodies)
        return bump.fetch

    def refusal(self, text=None):
        if text is not None:
            self.assertNotEqual(text, TEXT, "the test's own edit did not happen")
            self.path.write_text(text)
        with self.assertRaises(bump.Refused) as caught:
            bump.plan(self.path, "v2.0.0", "kmoneil/jr")
        return str(caught.exception)

    def test_a_manifest_the_file_contradicts_is_refused(self):
        """The one that matters: checksums.txt says one thing, the bytes another."""
        self.use(honest(), {"jr-full_2.0.0_windows_arm64.zip": b"not what the manifest hashed"})
        self.assertIn("jr-full_2.0.0_windows_arm64.zip hashes to", self.refusal())
        self.assertEqual(self.path.read_text(), TEXT, "the manifest was written anyway")

    def test_a_release_without_windows_zips_is_refused(self):
        """Every release before Windows shipped: the tarballs are there and no zip is."""
        sums = {n: d for n, d in honest().items() if not n.endswith(".zip")}
        self.use(sums)
        self.assertIn("v2.0.0 publishes no jr-full_2.0.0_windows_amd64.zip", self.refusal())

    def test_an_empty_manifest_is_refused(self):
        self.use({})
        self.assertIn("checksums.txt held no digests", self.refusal())

    def test_a_missing_architecture_is_refused(self):
        self.use(honest())
        text = edited(lambda m: m["architecture"].pop("arm64"))
        self.assertIn("architectures are ['64bit'], and this script knows ['64bit', 'arm64']", self.refusal(text))

    def test_a_url_in_another_repository_is_refused(self):
        self.use(honest())

        def move(m):
            m["architecture"]["arm64"]["url"] = m["architecture"]["arm64"]["url"].replace("kmoneil/jr", "someone/jr")

        self.assertIn("arm64's url points at someone/jr, not kmoneil/jr", self.refusal(edited(move)))

    def test_the_zips_cannot_be_crossed(self):
        self.use(honest())

        def cross(m):
            m["architecture"]["64bit"]["url"] = f"{OLD}/jr-full_1.0.0_windows_arm64.zip"
            m["architecture"]["64bit"]["extract_dir"] = "jr-full_1.0.0_windows_arm64"

        self.assertIn(
            "64bit's url fetches the windows_arm64 zip, and this script expects windows_amd64",
            self.refusal(edited(cross)),
        )

    def test_another_profile_is_refused(self):
        """The bucket installs the full profile; a reader zip is a different product."""
        self.use(honest())

        def reader(m):
            m["architecture"]["64bit"]["url"] = f"{OLD}/jr-reader_1.0.0_windows_amd64.zip"

        self.assertIn("64bit's url is not a release download this script can read", self.refusal(edited(reader)))

    def test_an_extract_dir_that_is_not_the_zips_is_refused(self):
        """Scoop would extract a directory the zip does not hold, and install nothing."""
        self.use(honest())

        def elsewhere(m):
            m["architecture"]["arm64"]["extract_dir"] = "jr"

        self.assertIn(
            "arm64 extracts jr from jr-full_1.0.0_windows_arm64.zip, which holds jr-full_1.0.0_windows_arm64",
            self.refusal(edited(elsewhere)),
        )

    def test_a_url_whose_name_disagrees_with_its_tag_is_refused(self):
        self.use(honest())

        def mixed(m):
            m["architecture"]["64bit"]["url"] = f"{OLD}/jr-full_0.9.0_windows_amd64.zip"
            m["architecture"]["64bit"]["extract_dir"] = "jr-full_0.9.0_windows_amd64"

        self.assertIn("64bit's url names 0.9.0 under the tag v1.0.0", self.refusal(edited(mixed)))

    def test_a_list_where_a_string_belongs_is_refused(self):
        """hunk's manifest carries lists; a jr entry is one file and one hash."""
        self.use(honest())

        def listed(m):
            m["architecture"]["64bit"]["url"] = [m["architecture"]["64bit"]["url"]]

        self.assertIn("64bit needs a url, a hash and an extract_dir, each a single string", self.refusal(edited(listed)))

    def test_a_manifest_without_a_version_is_refused(self):
        self.use(honest())
        self.assertIn("the manifest has no version", self.refusal(edited(lambda m: m.pop("version"))))

    def test_it_rewrites_the_version_urls_hashes_and_directories_and_nothing_else(self):
        self.use(honest())
        text, _ = bump.plan(self.path, "v2.0.0", "kmoneil/jr")
        after = json.loads(text)
        self.assertEqual(after["version"], "2.0.0")
        self.assertNotIn("1.0.0", text)
        self.assertNotIn(ZERO, text)
        for key in ("64bit", "arm64"):
            after["architecture"][key] = MANIFEST["architecture"][key]
        after["version"] = MANIFEST["version"]
        self.assertEqual(after, MANIFEST, "something other than the version, urls, hashes and directories changed")

    def test_every_url_keeps_its_own_digest(self):
        """A 64bit zip with the arm64 digest would fail every install."""
        self.use(honest())
        after = json.loads(bump.plan(self.path, "v2.0.0", "kmoneil/jr")[0])
        for key, platform in bump.FILES.items():
            entry = after["architecture"][key]
            name = f"jr-full_2.0.0_{platform}"
            self.assertEqual(entry["url"], f"{BASE}/{name}.zip")
            self.assertEqual(entry["hash"], digest(f"{name}.zip"))
            self.assertEqual(entry["extract_dir"], name)

    def test_the_downloads_are_kept_for_provenance(self):
        """verify-provenance.py asks about the bytes this hashed, so they have to be there."""
        self.use(honest())
        bump.plan(self.path, "v2.0.0", "kmoneil/jr")
        for name in ZIPS:
            self.assertEqual((self.downloads / name).read_bytes(), body(name))

    def test_the_manifest_is_read_first_and_each_zip_once(self):
        stub = self.use(honest())
        bump.plan(self.path, "v2.0.0", "kmoneil/jr")
        self.assertEqual(stub.urls[0], f"{BASE}/checksums.txt", "the manifest was not read first")
        self.assertEqual(sorted(stub.urls[1:]), sorted(f"{BASE}/{z}" for z in ZIPS))

    def test_the_manifest_keeps_its_layout(self):
        """Four-space indent, keys in their order, and a final newline, as Scoop's own are."""
        self.use(honest())
        text, _ = bump.plan(self.path, "v2.0.0", "kmoneil/jr")
        self.assertTrue(text.endswith("}\n"))
        self.assertTrue(text.startswith('{\n    "version": "2.0.0",\n    "description"'))
        self.assertEqual(list(json.loads(text)), list(MANIFEST))


class MainTest(unittest.TestCase):
    """The command line: what it refuses, what it writes, and what a re-run does."""

    def setUp(self):
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = scratch / "jr.json"
        self.path.write_text(TEXT)
        self.outputs = scratch / "outputs"
        self.addCleanup(setattr, bump, "fetch", bump.fetch)
        bump.fetch = Stub(honest())
        self.enterContext(unittest.mock.patch.dict(os.environ, {
            "GITHUB_OUTPUT": str(self.outputs),
            "JR_DOWNLOAD_DIR": str(scratch / "dist"),
        }))

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
        self.assertIn("refused: checksums.txt held no digests", err)
        self.assertEqual(self.path.read_text(), TEXT)

    def test_it_writes_the_manifest_and_says_so(self):
        code, out, _ = self.run_main("--tag", "v2.0.0")
        self.assertEqual(code, 0)
        self.assertIn("jr.json now points at 2.0.0", out)
        self.assertIn(f"{BASE}/jr-full_2.0.0_windows_arm64.zip", self.path.read_text())
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
        self.assertIn("jr.json already points at 2.0.0; nothing to do", out)
        self.assertEqual(self.path.read_text(), written)
        self.assertEqual(self.emitted(), {"version": "2.0.0", "changed": "false"})


class FetchTest(unittest.TestCase):
    """A network failure is a refusal that names the URL, not a traceback."""

    def refusal(self, error):
        with unittest.mock.patch.object(bump.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(bump.Refused) as caught:
                bump.fetch(f"{BASE}/checksums.txt")
        return str(caught.exception)

    def test_a_missing_file_is_refused(self):
        error = urllib.error.HTTPError(f"{BASE}/checksums.txt", 404, "Not Found", None, None)
        self.assertIn("checksums.txt answered 404 Not Found", self.refusal(error))

    def test_an_unreachable_host_is_refused(self):
        self.assertIn("could not be reached: no route", self.refusal(urllib.error.URLError("no route")))


if __name__ == "__main__":
    unittest.main()
