---
name: vezlu
description: Keep alternative text artifacts in local branches, compare actual outputs and combine the user's selected compatible artifacts.
---

# Vezlu workflow

Use when the user wants to keep multiple approaches to text work, compare their actual deliverables and continue with a chosen combination. The configured Vezlu MCP server must be available. This file supplies instructions; it does not install or start the server.

1. Use `projects` and `branches` to discover existing work. Create a named project with `create` when needed. `main` starts empty.
2. Prepare a shared base with `write`. Each `changes` object maps a stable artifact key, such as `brief.md` or `research/sources.md`, to complete text. Use `null` only for an intended deletion.
3. Read the current base head and call `branch` for each alternative using `source` and its exact `expected_head`. Fork before editing so alternatives share real ancestry. Supply the alternatives yourself from the user's brief; Vezlu does not call a model.
4. Write complete alternative artifacts on their own branches, using each current `expected_head`. Keep comparable content under matching keys. Use `read` to inspect the actual saved result.
5. Call `compare` with `left` and `right`. Explain the material text differences and tradeoffs. Do not claim one is better from a diff alone.
6. Obtain the user's artifact choices before combining them. Fork the selected starting branch into a new result branch, keeping both alternatives available.
7. Call `merge` with the selected `paths`, `source`, `target` and current `expected_source`/`expected_target` tokens. An `ok: false` result leaves the target unchanged. Show conflicting base/source/target text and ask the user to choose or revise it; save that explicit choice with a guarded `write`.
8. If any head is stale, reread and compare before retrying. Never silently substitute a newer token for an earlier choice.
9. Read the result branch and present the saved text. Use `history` and `read` with a revision token when the user wants an earlier state.

Merges select whole artifacts, not individual lines. Partial combinations retain source references without claiming that unselected content was integrated; later edits may conservatively conflict. No artifact key grants access to local files. Local storage is plaintext, so avoid credentials and sensitive material unless the user intends to retain it there.
