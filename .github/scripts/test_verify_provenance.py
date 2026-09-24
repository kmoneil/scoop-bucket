#!/usr/bin/env python3
"""Tests for verify-provenance.py: what it asks gh, and what it refuses.

gh does the cryptography, and kmoneil/homebrew-tap checked its answers against a
real jr release by hand. What these hold is the part that is this script's own:
it asks about every file the manifest names, asks all four questions of each,
never asks about nothing, and takes any no as a refusal.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("verify_provenance", HERE / "verify-provenance.py")
prov = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prov)

REPO = "kmoneil/jr"
WORKFLOW = "kmoneil/jr/.github/workflows/release.yml"
TAG = "v2.0.0"
BASE = f"https://github.com/{REPO}/releases/download/{TAG}"
NAMES = ["jr-full_2.0.0_windows_amd64.zip", "jr-full_2.0.0_windows_arm64.zip"]
MANIFEST = {
    "version": "2.0.0",
    "architecture": {
        "64bit": {"url": f"{BASE}/{NAMES[0]}", "hash": "0" * 64, "extract_dir": NAMES[0][:-4]},
        "arm64": {"url": f"{BASE}/{NAMES[1]}", "hash": "0" * 64, "extract_dir": NAMES[1][:-4]},
    },
    "bin": "jr.exe",
}
REPORT = json.dumps(
    [
        {
            "verificationResult": {
                "signature": {
                    "certificate": {
                        "buildSignerURI": f"https://github.com/{WORKFLOW}@refs/tags/{TAG}",
                        "sourceRepositoryDigest": "85eea024de018b2ec8745585b2467d8842e4eb2e",
                        "runInvocationURI": "https://github.com/kmoneil/jr/actions/runs/1/attempts/1",
                    }
                }
            }
        }
    ]
)


class Gh:
    """Stands in for gh. Says yes, except about the files it is told to refuse."""

    def __init__(self, refuse=(), report=REPORT):
        self.refuse, self.report, self.calls = set(refuse), report, []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if Path(cmd[3]).name in self.refuse:
            return subprocess.CompletedProcess(cmd, 1, "", "Error: HTTP 404: Not Found\n")
        return subprocess.CompletedProcess(cmd, 0, self.report, "")


class Release(unittest.TestCase):
    """A manifest naming two zips, and the two downloaded beside it."""

    def setUp(self):
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.manifest = scratch / "jr.json"
        self.manifest.write_text(json.dumps(MANIFEST, indent=4) + "\n")
        self.downloads = scratch / "dist"
        self.downloads.mkdir()
        for name in NAMES:
            (self.downloads / name).write_bytes(b"archive")


class VerifyTest(Release):
    def verify(self, gh):
        return prov.verify(self.manifest, self.downloads, REPO, WORKFLOW, TAG, run=gh)

    def refusal(self, gh):
        with self.assertRaises(prov.Refused) as caught:
            self.verify(gh)
        return str(caught.exception)

    def test_every_file_is_asked_all_four_questions(self):
        """A real release passes with any one of these dropped, so only this sees it go."""
        gh = Gh()
        self.verify(gh)
        self.assertEqual(
            gh.calls,
            [
                [
                    "gh", "attestation", "verify", str(self.downloads / name),
                    "--repo", REPO,
                    "--signer-workflow", WORKFLOW,
                    "--source-ref", f"refs/tags/{TAG}",
                    "--deny-self-hosted-runners",
                    "--format", "json",
                ]
                for name in NAMES
            ],
        )

    def test_one_file_without_provenance_refuses_the_bump(self):
        message = self.refusal(Gh(refuse={NAMES[1]}))
        self.assertIn(f"{NAMES[1]} has no build provenance from {WORKFLOW} at {TAG}", message)
        self.assertIn("HTTP 404", message, "gh's reason did not reach the log")

    def test_a_file_that_was_not_downloaded_is_refused_before_gh_is_asked(self):
        (self.downloads / NAMES[0]).unlink()
        gh = Gh()
        self.assertIn(f"{self.downloads / NAMES[0]} does not exist", self.refusal(gh))
        self.assertEqual(gh.calls, [], "gh was asked before every file was known to be there")

    def test_a_manifest_naming_no_release_file_is_refused(self):
        self.manifest.write_text(json.dumps({"version": "2.0.0", "bin": "jr.exe"}))
        gh = Gh()
        self.assertIn("names no release file", self.refusal(gh))
        self.assertEqual(gh.calls, [])

    def test_a_renamed_download_is_asked_about_by_its_release_name(self):
        """hunk's manifest saves files under #/ names; the release knows the real one."""
        renamed = json.loads(self.manifest.read_text())
        renamed["architecture"]["64bit"]["url"] += "#/jr.zip"
        self.manifest.write_text(json.dumps(renamed))
        gh = Gh()
        self.verify(gh)
        self.assertEqual(Path(gh.calls[0][3]).name, NAMES[0])

    def test_it_says_who_built_each_file(self):
        verified = self.verify(Gh())
        self.assertEqual([name for name, _ in verified], NAMES)
        self.assertEqual(
            verified[0][1],
            f"built by https://github.com/{WORKFLOW}@refs/tags/{TAG} at 85eea024de01, "
            "https://github.com/kmoneil/jr/actions/runs/1/attempts/1",
        )

    def test_an_unreadable_report_does_not_undo_a_verification(self):
        """gh's exit status is the verdict; its JSON is only for the log."""
        verified = self.verify(Gh(report="not json"))
        self.assertEqual(len(verified), len(NAMES))
        self.assertIn("could not be read", verified[0][1])


class MainTest(Release):
    def run_main(self, gh):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = prov.main(
                [
                    "--manifest", str(self.manifest),
                    "--downloads", str(self.downloads),
                    "--repo", REPO,
                    "--workflow", WORKFLOW,
                    "--tag", TAG,
                ],
                run=gh,
            )
        return code, out.getvalue(), err.getvalue()

    def test_a_refusal_exits_1_and_says_why(self):
        code, _, err = self.run_main(Gh(refuse={NAMES[0]}))
        self.assertEqual(code, 1)
        self.assertIn(f"refused: {NAMES[0]} has no build provenance", err)

    def test_success_names_every_file(self):
        code, out, _ = self.run_main(Gh())
        self.assertEqual(code, 0)
        self.assertIn(f"build provenance verified on 2 files, from {WORKFLOW} at {TAG}:", out)
        for name in NAMES:
            self.assertIn(f"  {name}: built by", out)


if __name__ == "__main__":
    unittest.main()
