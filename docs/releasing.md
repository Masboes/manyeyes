# Releasing

manyeyes uses [semantic versioning](https://semver.org/). The version lives in one place: `src/manyeyes/__init__.py`.

## Cut a release

1. Move the `## [Unreleased]` notes in `CHANGELOG.md` under a new heading `## [X.Y.Z] — YYYY-MM-DD`.
2. Set `__version__ = "X.Y.Z"` in `src/manyeyes/__init__.py`.
3. Commit (`chore: release X.Y.Z`) and merge to `main`.
4. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.

The `release` workflow then:

- checks that the tag equals `__version__`,
- builds and pushes the image `ghcr.io/<owner>/manyeyes:X.Y.Z` and `:latest`,
- creates a GitHub release with the CHANGELOG section and the Python package attached.

Every merge to `main` also pushes `ghcr.io/<owner>/manyeyes:main`, which development versions of the CLI use.

After a release, set `__version__` to the next development version (`X.Y.(Z+1).dev0`).

## How users learn about it

- **Server**: checks GitHub's latest release twice a day (`update.check`, `update.repo`). The web UI shows "Update available" in the header; the Settings page shows the release notes link and the update commands.
- **CLI**: `manyeyes version --check`.
