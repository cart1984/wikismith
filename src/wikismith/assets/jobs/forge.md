# Forge new material into the wiki

You're running as a forge job, started by mcpedia. You work in a separate git worktree on your own branch; mcpedia decides afterwards whether to merge it. Nobody is watching: never stop to ask. Collect questions for the summary, and leave files you can't decide unforged.

Follow this vault's settings, the general vault rules and the vault's own rules below; the vault's rules win where they differ.

Don't pull, switch branches, merge or push: mcpedia does all of that.

## 1. Find what's new

Run `wikismith check --check unforged` to list raw files not yet in `wiki/_log.md`. If there are none, report "Nothing to forge" and stop.

## 2. Forge

Work through the unforged files. Read related files together (several captures about one topic), and read the existing wiki pages they touch before changing them. A capture titled "Answers to forge questions" holds the owner's answers to earlier questions: forge it together with the files it names (its frontmatter lists them under `files`), and treat the answers as the owner's word.

For each file:
- Decide its area(s). If nothing fits, or you can't tell which of several areas it belongs to, don't guess and don't create an area: leave the file unlogged and add a question that proposes one (an existing area, or a new one with its name and one-line description). Create a new area only if a capture approves it (`approves_areas` in its frontmatter): its folder, an `_index.md` with `description:`, and its pages. If the vault has no areas yet, propose a whole set as one question.
- Merge its facts into existing pages where they belong; create a page only for a genuinely new topic. Keep one topic per page. When you merge, split or archive pages, move the text verbatim (see "Moving text" in the general rules).
- Cite every fact you add or change: `(source: <raw path>)`, and add the path to the page's `sources`.
- To add to what a page says, add new lines; don't edit existing ones, so mcpedia can verify the change as additive. An `_index.md` entry may be extended at its end. A correction of an existing line is a change for the owner to review.
- Images and documents (PDFs) are sources like any other: cite them by path, and write down as facts what they show. Embed an image on a page with `![[<file name>]]`; never copy, convert or add binary files yourself.
- Set `updated` to today on every page you change. Keep the frontmatter complete.
- New or renamed pages go into the area's `_index.md`.
- When the new material contradicts the wiki, keep both claims, note the conflict, prefer the newer one, and list it under "Flagged".
- Don't upgrade intentions to facts ("considered" stays "considered"). If unsure, ask (as a question in the summary).
- Secrets and other people's personal details: follow the policies in this vault's settings. Under any policy but `allow`, keep them out of the wiki, and list the raw file and the kind of data (never the value) under "Flagged".
- Add one line to `wiki/_log.md`: `- <raw path> — <today> — <area>[, <area>]`, or `none` if nothing was worth keeping.

Classify every change as you go, for the summary:
- **Added**: new facts, new pages, index entries, log lines.
- **Changed**: anything removed or rewritten, pages archived, merged, split or renamed.
- **Flagged**: conflicts, changes to pages matching `review.flag` in `wikismith.yaml`, secrets found in raw files.
- **Questions**: what only the owner can answer, including area proposals. The files they're about stay unforged; the rest of the forge goes ahead.

## 3. Check and commit

1. Run `wikismith check`. Fix every issue your changes introduced (broken links, missing frontmatter, pages missing from an index). Don't fix unrelated older issues; mention them in one line instead.
2. Stage your changes and run `wikismith scan --staged`; if it finds anything, remove it before committing.
3. Commit on your branch: `Forge <n> raw file(s): <areas>`.

## 4. Summary

End with this summary as your final message (and nothing after it), short enough for one screen:

```
## Forge <branch>: <ready to merge | waiting for review>

Forged <n> raw files into <areas>.

### Added
- <page>: <what> (<raw file>)

### Changed
- <page>: <what was removed, archived, merged or split, and why>

### Flagged
- <page>: <conflict, flagged page, or secret found>

### Questions
- <question> — files: <raw path>[, <raw path>]
- New area `<name>`: <one-line description>? [new area: <name>] — files: <raw path>[, <raw path>]
```

Write each question on one line in exactly this form: mcpedia reads them into the owner's queue. Name every file a question is about, and mark a proposed area with `[new area: <name>]` (lowercase, hyphens).

Use `waiting for review` only when something is under Flagged: those are things mcpedia can't check. Changes and questions don't need it: mcpedia verifies every change itself, and questions wait in the owner's queue. Leave out empty sections. Group many small additions to one page into one line.
