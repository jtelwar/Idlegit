# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public
GitHub issue.

Email: **joel@elwar.co.uk**

Include:

- A description of the issue and the impact you believe it has.
- Steps to reproduce (ideally a minimal proof-of-concept).
- The version of Idlegit affected (`idlegit-update --check` prints
  the installed version).
- Your OS and terminal.

You'll get an acknowledgement within a reasonable time. Idlegit is
maintained as a side project, not a commercial product, so response
times are best-effort — but every report will be read and triaged.

## Scope

Idlegit's [cardinal rule](CONTRIBUTING.md#the-cardinal-rule) is that
it must never perform destructive git operations on the user's repos.
Issues that demonstrate Idlegit triggering — directly or transitively —
any of:

- `git reset --hard`, `push --force`, `branch -D`, `clean -fd`,
  `checkout --`, `rebase`, `filter-branch`
- Loss of uncommitted work, rewriting of history, deletion of refs
- Command injection via repo paths, branch names, remote URLs, or
  user-controlled config values
- Path traversal or symlink escapes in the installer / updater /
  task-log writer

…are treated as priority security bugs.

Out-of-scope (these are bugs, but report them as regular issues):

- UI rendering glitches, layout drift, off-by-one cursor positions.
- Performance issues, slow refreshes, large-workspace responsiveness.
- Feature requests.

## Supported versions

Only the latest released version is supported. There are no backport
branches — fixes ship on `master` and roll into the next tag.

## Disclosure

Coordinated disclosure preferred: I'll work with you on a fix and
release timeline, then credit you in the changelog (unless you'd
rather not be named).
