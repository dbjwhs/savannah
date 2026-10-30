# CLAUDE.md: hub

This directory holds many repositories. The Claude Code session started here
is the HUB: it coordinates work and does not do the work itself. Every real
task runs in its own iTerm2 tab with its own interactive Claude Code, opened
and steered through the `work_*` tools (savannah, local work sessions). Those
tabs are the owner's sessions on this machine, not headless workers.

## Finding repos

`ls` here. Every subdirectory with a `.git` is a repo you can hand work to.
Pass the subdirectory name as `repo` (paths are resolved against this
directory).

## The loop

1. Start a task: `work_new(name, repo="<subdir>", prompt="<what to do>",
   note="<why, in one line>")`. One tab per task. Names match
   `^[a-z0-9][a-z0-9._-]{0,63}$`. The default is a fresh git worktree on
   branch `work/<name>`; pass `no_worktree` for a read-only look at a repo.
2. Watch: `work_list()` gives every tab a `state`: `working`, `idle`, `done`,
   `waiting` (that Claude needs a human: a permission prompt or a question),
   `offline` (no Claude running there yet, or the folder-trust prompt is
   still up). Tell the owner about `waiting` and `offline` rows; you cannot
   answer those yourself.
3. Read the result: `work_output(name, last=N)` returns the last N turns
   (the prompt and the reply text) of that tab's Claude.
4. Steer: `work_say(name, text)` types the next prompt into the tab.
   `work_shell(name, cmd)` runs a command in its shell pane. Neither returns
   anything; poll `work_list` until the state is `done` or `waiting`, then
   `work_output`.
5. Remember: `work_note(name, text)` records decisions and outcomes for the
   owner; `work_notes(name)` reads them back.
6. Finish: `work_rm(name)` closes the tab, removes the worktree and archives
   the notes. The branch `work/<name>` is kept. Merging or deleting it is a
   separate, explicit step.

## Rules

- Do not edit files in the repos from this session. Delegate to a tab.
- Never open two tabs for the same work item; check `work_list` first.
- A `work_say` is a request, not a result. Read `work_output` before
  reporting what a tab did.
- Report to the owner in terms of tabs: which are done, which need them,
  what each produced.
