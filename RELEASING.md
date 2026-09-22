# Releasing

The working version is `0.1.0.dev0`. Until the first release, consumers should
pin a commit.

Before 1.0, use patch versions for compatible fixes and minor versions for
features or breaking changes. From 1.0 onward, breaking changes require a major
version. Document any changed integration requirements.

With the owner's approval:

1. Set `theseus/_version.py` and move the changelog entries into a dated release.
2. Run the local suite and any authorized live checks the change requires.
3. Build the packages:

   ```bash
   python -m pip install build
   python -m build
   ```

4. Check the archives and smoke-install the wheel in a clean environment.
5. Commit as `Release X.Y.Z`, create an annotated tag (signed when configured),
   and push the commit and tag.
6. Publish to the chosen registry if that step is approved.

Never replace a published artifact or move a release tag. Consumers pin the
version or tag and test updates in their own application.
