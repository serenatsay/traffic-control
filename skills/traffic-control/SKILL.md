---
name: traffic-control
description: Start, resume, preview, hand off, and merge coding tasks across Codex and Claude Code using named Git worktrees. Use when a user wants a separate coding conversation, a local test link, or wants to land its changes.
---

# Traffic Control

Keep each active coding task in a known folder and branch. The user supplies a plain-English task name; infer the current agent from the host app. Never ask the user to choose a branch name or specify Codex while already in Codex (likewise for Claude). Use the bundled `scripts/traffic_control.py` for Git state and mutations. Run it from within the user's project, using its absolute path if needed. It needs Python 3 and Git.

## Start or resume

- Before starting, run `status` and check for the same feature under a slightly different name. Reuse that task with `resume` rather than creating a duplicate. For a new task, run `python3 <skill>/scripts/traffic_control.py --json start "<task name>" --agent codex` in Codex, or `--agent claude` in Claude Code. The script reuses an existing task with the same name. In an unregistered linked worktree, it adopts that worktree and its branch; it does not make another copy. It creates a new branch and worktree from the project's main branch when run in the original checkout. It fetches `origin` first when available.
- If the user explicitly wants two agents editing the same feature simultaneously or two alternative implementations, run `start "<task name>" --parallel`. Each active writer needs its own worktree. Sequential conversations on one feature reuse the same worktree.
- For an existing task, use `resume "<task name or ID>"`. If there are multiple attempts, ask which one the user means using the IDs returned by `status`.
- Return the task's folder, branch, and suggested conversation title in plain language. If the current conversation is already in that folder, work there. If the script created or found a different folder, open a new conversation in that exact folder when the app supports it; otherwise give the user the folder to select. The current conversation does not move just because a worktree was created.
- In the ChatGPT desktop app, if the Worktree control is unavailable, open the returned folder as the new Codex chat's local workspace. On macOS, `codex app <absolute-folder>` can open that workspace when the Codex CLI is installed. In Claude Code Desktop, when opening a worktree folder created by Traffic Control, leave its Worktree switch **off** so Claude edits that existing folder. If Claude Desktop already created a worktree with its switch on, invoke `start` from that session to register the current folder.
- Rename the current task conversation with an available app tool when supported. Otherwise tell the user the suggested title; do not claim the chat was renamed. Do not rename an unrelated coordinator or planning conversation.

## Handoff between conversations

When the user is moving a feature to another conversation, summarize completed work, remaining work, and any important test result in a short note. Save it with `handoff "<task>" --note "<summary>"`. In the next conversation, call `resume` and use its handoff note. Files carry across conversations only when they open the same folder; chat history does not.

## Preview the task

When the user asks to test a task in the browser, run `preview "<task name or ID>"` with the bundled helper and give them its local URL. The helper runs in the recorded worktree even if this conversation is in another folder. It installs npm dependencies in that worktree if missing, starts its Vite dev server on a free localhost port, and reuses an already running preview. Ask for a specific task ID if multiple attempts share a name. Use `preview "<task>" --status` to check it and `preview "<task>" --stop` when the user is done. Do not start a server automatically for every new task; several open servers consume resources. If startup fails, read the log path in the helper's error and explain the cause in plain language. If the helper reports an unsupported project, explain that this version only manages npm/Vite previews and help the user use the project's existing dev command or the app's preview feature. Do not claim a URL is ready until the helper confirms it.

## Merge

When the user asks to merge a task, inspect its diff and run the relevant project checks in its worktree. Commit only the intended task changes, leaving unrelated work untouched. Run `merge "<task>"` for a read-only preflight. It uses the last fetched remote state and refuses dirty task or destination folders, a local destination behind its known remote, and a detected conflict. If checks pass and the user has asked for a local merge, run `merge "<task>" --apply`; this fetches `origin` again immediately before applying and rechecks readiness. Report the target branch and result. If the work was already merged outside Traffic Control, `--apply` records it as complete without making another merge commit. The script never pushes or deletes the task worktree. If this project uses pull requests or merge queues, use that workflow and do not run a separate local merge.

For a read-only question, a follow-up in the same conversation, or a code review without editing, no new worktree is needed. Never force a merge, reset a checkout, discard changes, or delete a worktree to make a command pass. Explain what needs attention in ordinary language.
