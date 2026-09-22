# Instructions for consuming projects

If coding agents work on your application, add this to its `AGENTS.md`.
Adjust the package path if needed.

```markdown
## Theseus

Theseus is a shared dependency at `packages/theseus` (a separate Git repository).

- Fix shared backend behavior there; don't patch site-packages or copy the library.
- Commit and push package changes first, then update this repo's submodule pointer.
- Keep application prompts, tools, databases, and UI in this repo.
- Pin an intentional package commit and test dependency updates.
- Keep `.theseus/` private. Never commit it, copy it into fixtures, or inspect or
  delete it without approval.
- Save thread IDs and selected capabilities. Re-register Python tools before
  resuming after restart. Use distinct agents for concurrent runs.
- Run live model tests only when needed and authorized.
```
