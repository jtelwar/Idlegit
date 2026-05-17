# Smart-sync

*Ctrl+S* aligns every checkout of every submodule across the
workspace, then optionally bumps each parent that depends on those
submodules.

## How alignment works

Smart-sync walks every *canonical* repo (a top-level repo that also
appears as a submodule somewhere else) and:

1. Probes every checkout — HEAD, branch, dirty state, ahead/behind.
2. Picks a *winner* — the checkout that's ahead of upstream, or the
   one with the most recent dirty changes, or the latest commit
   when nothing is dirty.
3. Stages + commits + pushes the winner.
4. Fast-forwards every other checkout (loser) onto the winner's
   tip.

## Auto-push submodule parent

When the canonical's `siblings` include a top-level repo that holds
the canonical as a submodule, smart-sync also bumps the parent's
gitlink — *if* the parent's only dirty change is that gitlink.

Cascades upward: if the parent is itself a submodule of a
grandparent, the grandparent's gitlink is bumped too once the
parent push lands.

Off by default? No — on by default. Flip
`default_auto_push_submodule_parent` off per workspace if you'd
prefer the parent commit stay manual.

## Safety

Smart-sync is non-destructive:

- No `reset --hard`, `push --force`, `clean -fd`, `rebase`, or
  anything that could rewrite history or destroy uncommitted work.
- Losers are aligned via `merge --ff-only` (or `merge --no-edit`
  when `prevent_smart_sync_silent_merge` is off — *but never*
  through a rebase).
- Detached HEAD with unique commits → smart-sync warn-skips that
  checkout rather than risk orphaning a commit.

If smart-sync can't safely align a checkout, the row is left alone
and a *warn* task explains why.
