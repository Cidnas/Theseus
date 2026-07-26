# Contributing to codeAgent

`codeAgent` is shared infrastructure used by multiple application repositories.
A change here can affect every project that upgrades to it, so contributions
must protect the library boundary and remain intentionally versioned.

## Decide where a change belongs

A change belongs in this package when it improves reusable infrastructure such
as:

- backend process and protocol lifecycle;
- thread creation, resumption, and concurrency;
- tool or skill registration and isolation;
- event routing, structured responses, timeouts, and errors;
- authentication and project-local state handling; or
- a general extension point needed by more than one application.

Keep the following in the consuming project:

- application prompts and agent personalities;
- business-specific tools and workflows;
- application databases and domain models;
- product APIs, command-line interfaces, and user interfaces; and
- application-specific orchestration or evaluation logic.

If one project discovers a missing capability, extract the smallest general
interface rather than moving that project's business logic into this package.

## Development workflow

1. Work from a dedicated branch.
2. Install this checkout as an editable dependency in the application that
   exposed the need when application-level reproduction is useful.
3. For a bug, add a regression test that fails for the observed public
   behavior.
4. Implement the smallest compatible fix or reusable feature.
5. Update the public documentation and the changelog when behavior changes.
6. Run the deterministic suite:

   ```bash
   python -m unittest discover -s tests -v
   ```

7. Run an applicable live test only when real app-server behavior must be
   verified and real model use has been authorized.
8. Follow `RELEASING.md` when preparing a version.

## Testing principles

Every test should answer a practical failure question: what promise to a
consuming project would break if this test disappeared?

- Prefer one focused assertion path over broad scenario simulations.
- Test the public result, error, safety boundary, or integration contract.
- Keep normal tests independent of network access, real authentication, and
  model behavior.
- Do not copy a consuming application's workflows into this suite.
- Keep live tests few, explicit, and disabled by default.
- A packaging change is verified by building and smoke-installing the wheel,
  not by adding a test that merely repeats a version constant.

## State and security

The `.codex-agent/` directory is generated separately for each project. It may
contain authentication, conversations, databases, skills, and caches. It is
not source code and must remain ignored. Do not inspect, copy, publish, or
delete another project's state without explicit approval.

Never place real credentials or conversation data in tests, documentation,
logs, distributions, or commits.

## Review checklist

Before asking for review, confirm that:

- the change belongs in shared infrastructure;
- public compatibility and state compatibility were considered;
- relevant deterministic tests pass;
- tests do not encode one application's business behavior;
- documentation and changelog entries match the behavior; and
- no generated state, credentials, build output, or caches are included.
