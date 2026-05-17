
# Idlegit

## Idlegit is a TUI app for managing git repo workspaces.

## Design Goals

   Safe - Frictionless - Opinionated - Configurable

## Rules for Agents

1. **Idlegit must NEVER perform destructive git operations on the user's repos.** This is the Cardinal Rule. Idlegit code must not invoke (directly or transitively) `reset --hard`, `push --force`, `branch -D`, `clean -fd`, `checkout --`, `rebase`, `filter-branch`, or anything else that can destroy uncommitted work, rewrite history, or delete refs. If a feature seems to need one, stop and ask a human — there is almost always a safe alternative (stash, new branch, soft reset, revert commit). This rule applies to the shipped product; it does not restrict your own dev-workflow git usage in this repo.

2. Confirm any breaking changes in detail with a human - omit nothing, but always be concise.

3. Check your work. Run tests or builds appropriately. Ask sub agents to review and audit after large chunks of work are completed.

4. Be opinionated, then obedient. Push back where appropriate — e.g. if you spot a simpler approach, a bug in the brief, or a violation of these rules, raise it once before proceeding. Once the human has decided, follow the decision without re-litigating.

5. The [text](VERSION) file serves two purposes: line 1 is the current semver, and the rest is a changelog. Maintain the version number on the first line according to these rules:
 - Bug fixes, refactors with no user-facing changes, user-facing changes with no behavioural impact (such as a label change or minor design change) - bump the patch version.
 - New features, refactors that have user-facing changes, or significant design/UX changes - bump the minor version.
 - Major version should only ever be incremented by a human - advise if you think there have been breaking changes to warrant a major version increase.

6. Maintain log of recent work in [text](VERSION) upon completing a task. Add a sub-heading for each version number if it doesn't exist, then an entry with CURRENT date in short form - and **short** description of assigned task that has been completed. Only describe changes - do not give explanations or reasons unless absolutely necessary for clarity. Do not fill with technical details or explanations. Three lines maximum per entry, aim for one.

7. When given a new task, if your current task is complete and [text](VERSION) is updated according to rules 5 and 6, overwrite [text](./AGENTS/CURRENT_TASK.md) with a concise description of your task, then maintain a log of progress in that file.
If the task is not complete yet and you are given a new task, you should make a CURRENT_TASK_2{3,4,etc}, and leave the existing in-progress task file.

8. On completing a task, summarise the work in [text](VERSION) according to rule 6. Then tidy the current-task file: if it is a numbered CURRENT_TASK_{2,3,...}.md, delete it; if it is the primary CURRENT_TASK.md and nothing else is in flight, wipe its contents. Always keep at least one CURRENT_TASK file in existence — never delete the last one.

9. Write new filenames for documentation/audits/reviews etc that you are asked to write in the ./AGENTS directory in SCREAMING_SNAKE_CASE. Use markdown for formatting where appropriate. This is your working directory for documentation and project managements. Use subfolders where appropriate to keep the agents directory root mainly for task tracking.

10. Regularly refer to these rules so you do not violate them.