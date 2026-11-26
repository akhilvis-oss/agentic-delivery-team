# Publish a complete kit release

Release tags and notes are owner-approved and owner-published. Do not publish from an
agent branch. Start with passing checks and independent review on the intended commit.

1. Update the kit version in `templates/agentic-kit.example.json`, the repository's
   `.github/agentic-kit.json`, the matching documentation example and pinned version
   tests as **release work**, not as part of an unrelated feature story. Keep all six
   feature switches explicit. Add the dated changelog entry.
2. Check `release-files.json` against every tracked file. Every carried source must
   exist, and all remaining files must match `kit_only`; every hosted sparse path must
   be covered by a destination. Keep hashes out of this committed manifest.
3. Approve notes with a single line `Release commit: <40 lowercase hexadecimal
   characters>`, naming exactly the commit the tag resolves to. Include both runbooks
   and the checker command. Obtain that SHA from Git, never from a request to the owner
   to supply it manually.
4. For every adopter-owned change, list the destination, JSON path or Markdown section,
   and exact added/changed value. Say explicitly when there are none. Include a migration
   of `features.lifecycle` where needed; a missing kit file previously kept guards on.
5. List every private-install action, App permission and GitHub setting needed, and
   whether services need an owner-authorized restart. Keep private values out of notes.
6. The owner creates the approved tag. From a fresh checkout at that tag, generate:

   ```sh
   python -I scripts/release_files.py --repo . --tag "$TAG" --output release-hashes.json
   ```

   The generator reads bytes from `git archive <tag>`, not the working tree. The asset
   records the tag, commit and SHA-256 for each carried source. Never commit the asset.
7. Publish the tag, its notes and `release-hashes.json`. Confirm the published tag resolves
   to the notes' commit. Run the checker from a fresh export using this asset against a
   clean scratch adopter before calling the release ready.

The first upgrade from v0.5.x has no old manifest: delete no old framework files, and
list any unmatched lifecycle files for the owner. Follow `UPGRADE.md` exactly. A cold
outside-developer trial follows `INSTALL.md`; any failure becomes a story, not a silent
manual exception. Public repositories may need the trusted-workflow Actions policy;
required checks on private repositories require a paid plan.
