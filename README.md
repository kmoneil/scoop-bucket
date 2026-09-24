# kmoneil/scoop-bucket

A [Scoop](https://scoop.sh) bucket for the tools in this account.

```powershell
scoop bucket add kmoneil https://github.com/kmoneil/scoop-bucket
```

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

Each hunk release tells this repository that it exists, and `bump-hunk.yml`
does the rest: it rewrites the manifest's version, URLs and digests, reading
each digest from the release's `SHA256SUMS` and re-deriving it from the
download; installs the result with Scoop on a Windows runner; and only then
moves `main`. `scoop.yml` installs the manifest the same way on every change.

The skill is downloaded as `skill.download` rather than under its own name, on
purpose: Scoop extracts a `.tar.gz` only after installing 7-Zip, and the
manifest extracts it with Windows' own `tar` instead.
