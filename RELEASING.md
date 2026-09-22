# Releasing Theseus

Every consuming project must pin an immutable `theseus` release. Changes are
made in this repository, verified here, and released centrally rather than
patched inside an application.

## First release

The working version is `0.1.0.dev0`; no release tag is part of this repository's
initial state. Choose the first release version when its scope is approved.
Development snapshots should be consumed by exact commit.

## Version policy

While the package is below `1.0.0`:

- Increment the patch version for a backward-compatible bug fix.
- Increment the minor version for a backward-compatible feature.
- Increment the minor version for a breaking public API or behavior change
  and document its integration requirements.

At `1.0.0` and later, use normal Semantic Versioning: patch for fixes, minor for
backward-compatible features, and major for breaking changes.

Public behavior includes exported Python APIs, documented configuration,
persisted-state compatibility, emitted event handling, and supported runtime
requirements. Repository-only documentation and test changes do not require a
release unless they accompany a distributable change.

## Change workflow

1. Decide whether the change belongs in the shared library. Project-specific
   prompts, tools, business rules, storage, and UI remain in the application.
2. For a bug, add a regression test that fails for the reported behavior.
3. Implement the smallest general fix or extension point.
4. Update the `Unreleased` section of `CHANGELOG.md`.
5. Run the core suite and any relevant live or integration tests.
6. Have at least one consuming project exercise a feature or risky fix before
   release when practical.

## Release checklist

1. Choose the version from the policy above.
2. Set `theseus/_version.py` to that version.
3. Move the changelog's `Unreleased` entries under a heading containing the
   version and release date, then create a fresh empty `Unreleased` section.
4. Run the full approved test suite.
5. Build both source and wheel distributions:

   ```bash
   python -m build
   ```

6. Inspect the generated distributions and install the wheel in a clean
   environment for a smoke check.
7. Commit the release as `Release X.Y.Z`.
8. Create an annotated, signed tag when signing is configured:

   ```bash
   git tag -s vX.Y.Z -m "Theseus X.Y.Z"
   ```

   Otherwise, create an annotated tag with `git tag -a`.
9. Push the commit and tag only after explicit approval.
10. Publish to the chosen package registry, if one is configured, without
    replacing an existing artifact.

Release tags and published artifacts are immutable. If a release is wrong,
fix it in a new version rather than moving the tag or overwriting the package.

## Consuming a release

Applications should pin the release tag or exact registry version and commit
their dependency lockfile. Upgrades are explicit: update the pin, review the
changelog, run the application's tests, and deploy through its normal rollout
process.
