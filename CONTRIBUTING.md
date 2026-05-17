# Contributing to Idlegit

Thanks for the interest. Idlegit is a lightweight, opinionated TUI — the bar
for accepting changes is high in one direction (correctness, safety)
and low in another (cosmetic polish, default config values). If you're
unsure whether a change fits, open an issue first.

## The cardinal rule

**Idlegit must never perform destructive git operations on the user's
repos.** No `reset --hard`, no `push --force`, no `branch -D`, no
`clean -fd`, no `checkout --`, no `rebase`, no `filter-branch`, no
anything else that can destroy uncommitted work, rewrite history, or
delete refs. If a feature seems to need one of these, stop and open
an issue — there's almost always a safe alternative (stash, new
branch, soft reset, revert commit). PRs introducing any of these
will be rejected on sight.

This rule applies to the shipped product; it does not restrict your
own dev-workflow git usage in this repo.

## Dev setup

Requirements: Python 3.9+, `git`, and (optionally) `gh` for the GitHub
Actions panel and `git-lfs` for the LFS toggle.

```sh
git clone https://github.com/jtelwar/idlegit.git
cd idlegit
python -m pip install -e .
python -m pip install ruff
```

Run from the source tree without installing:

```sh
./idlegit
```

## Running tests and lint

```sh
python -m unittest discover -s tests        # full suite
python -m unittest tests.test_workspaces    # one module
ruff check core/ ui/ tests/                 # lint
```

Tests must pass and `ruff` must be clean before a PR is merged — CI
([tests.yml](.github/workflows/tests.yml)) enforces both, and the
release workflow blocks tag-based releases when tests fail.

## Pull request workflow

1. **Open an issue first** for non-trivial changes so we can agree on
   the approach before you write code. Bug fixes and small polish are
   fine to PR directly.
2. **Branch from `master`** and push to a fork (or a branch on this
   repo if you have write access).
3. **Keep PRs focused** — one logical change per PR. Refactors should
   land separately from feature work.
4. **Update [VERSION](VERSION)** with a short changelog entry under a
   new sub-heading. Bump per [the version rules](#versioning).
5. **Add or update tests** when changing behaviour. The `tests/`
   directory has integration tests for git pipelines and unit tests
   for pure helpers — follow the closest existing pattern.
6. **Pass `ruff check` locally** before pushing.

## Versioning

The first line of [VERSION](VERSION) is the current semver; everything
below is the changelog. When you change code, bump the version:

- **Patch** (`0.x.Y` → `0.x.Y+1`): bug fixes, refactors with no
  user-facing change, label-only UX tweaks.
- **Minor** (`0.X.y` → `0.X+1.0`): new features, refactors with
  user-facing changes, significant UX/design changes.
- **Major** (`X.y.z` → `X+1.0.0`): breaking changes. Only humans bump
  this — open an issue first if you think a change warrants it.

Add a `### x.y.z` heading + one-line dated entry to the changelog
when bumping.

## Code style

- Python 3.9+ idioms (`from __future__ import annotations` is fine).
- `pathlib.Path` over `os.path` for new code.
- Subprocess invocations use argv lists, never `shell=True`.
- Avoid comments that restate what the code does. Comment the *why*
  (a hidden constraint, a subtle invariant, a workaround for a
  specific bug, a non-obvious surprise).
- No emojis in source unless explicitly part of UI text already
  using them.

## Reporting bugs

Open an issue with: idlegit version (`idlegit-update --check` prints
it), OS + terminal, exact reproduction steps, and what you expected
to happen. For security issues, see [SECURITY.md](SECURITY.md) —
don't file a public issue.

## Code of conduct

Be kind. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
