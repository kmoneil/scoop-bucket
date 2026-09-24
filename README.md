# kmoneil/scoop-bucket

A [Scoop](https://scoop.sh) bucket for the tools in this account.

```powershell
scoop bucket add kmoneil https://github.com/kmoneil/scoop-bucket
```

## `jr`

A Jira client whose output is a versioned contract, built for scripts and agents
first and humans second. [kmoneil/jr](https://github.com/kmoneil/jr).

```powershell
scoop bucket add kmoneil https://github.com/kmoneil/scoop-bucket
scoop install kmoneil/jr
jr version
```

On amd64 and arm64. It installs two things: the **full** profile's `jr.exe`, on
your `PATH` through Scoop's shims, and jr's agent skill, in
`~\.agents\skills\jr` and `~\.claude\skills\jr`. Nothing else comes with it,
not even 7-Zip: the release ships a zip, which Scoop extracts on its own.

The next step is `jr auth login`, which
[getting started](https://github.com/kmoneil/jr/blob/main/docs/getting-started.md)
walks through, token and all. Everything it writes lives under your profile:
contexts in `%USERPROFILE%\.config\jr`, the stored credential in
`%USERPROFILE%\.local\state\jr`, readable by your account alone.

**The skill** goes where agents look: `~\.agents\skills\jr`, the cross-agent
folder read by Codex, Cursor, Gemini CLI and most other loaders, and
`~\.claude\skills\jr`, where Claude Code reads it. The binary writes both with
`jr skill --dir`, so they always describe the binary you have. `scoop update jr`
rewrites both at the new version. jr refuses to write into a directory holding
a file the skill does not include, and the install fails with the refusal,
which names the file: usually a reference an older release carried, to delete.

**Tab completion.** jr prints a PowerShell completion script; this line in your
`$PROFILE` loads it in every session:

```powershell
jr completion powershell | Out-String | Invoke-Expression
```

**Updating and removing.** `scoop update jr` moves the binary and both skill
copies together. `scoop uninstall jr` removes the binary and leaves the skills,
your contexts and your credential where they are.

**The other profiles.** Every release also carries `jr-agent`, `jr-reader` and
`jr-ci` for Windows, the same tool with capabilities compiled out rather than
switched off, and those are fetched from
[the releases page](https://github.com/kmoneil/jr/releases) rather than through
this bucket. `jr-reader` is the one to hand an agent: it cannot change anything
in Jira, because it does not contain the code that could.

## `hunk`

A transactional multi-file text editor for coding agents: every edit must match
exactly as many times as it claims, or nothing is written.
[kmoneil/hunk](https://github.com/kmoneil/hunk).

```powershell
scoop install kmoneil/hunk
hunk --version
```

It installs hunk's agent skill as well, into `~\.claude\skills\hunk`, where
Claude Code finds it, and `scoop update hunk` keeps the skill at the binary's
version. `scoop uninstall hunk` leaves the skill where it is.

`--verify` and `--try` run their command with `sh`, which Windows does not
have. `scoop install git` provides one.

## How it stays current

Each hunk and jr release tells this repository that it exists, and
`bump-hunk.yml` or `bump-jr.yml` does the rest: it rewrites the manifest's
version, URLs and digests, reading each digest from the release's own manifest
(`SHA256SUMS` for hunk, `checksums.txt` for jr) and re-deriving it from the
download; for jr, verifies that every zip carries build provenance from jr's
release workflow, built from the tag; installs the result with Scoop on a
Windows runner; and only then moves `main`. `scoop.yml` installs each manifest
the same way on every change.

The skill is downloaded as `skill.download` rather than under its own name, on
purpose: Scoop extracts a `.tar.gz` only after installing 7-Zip, and the
manifest extracts it with Windows' own `tar` instead.
