# Audit the vault

You're running as an audit job, started by mcpedia. You work in a separate git worktree on your own branch; mcpedia merges your report afterwards. Nobody is watching: don't stop to ask anything.

Follow this vault's settings, the general vault rules and the vault's own rules below. An audit **reports**; it doesn't change the wiki. The owner picks what to fix, and fixes go through a forge.

Don't pull, switch branches, merge or push: mcpedia does all of that.

## 1. Mechanical checks

Run `wikismith check --limit 200` and keep its output for the report.

## 2. Judgment checks

Read each area's `_index.md` and skim its pages. Look for:

- **Duplicates**: two pages on the same topic that should be merged (`wikismith check --check similar` lists pairs that share much of their text; judge each, since a shared quote doesn't make a duplicate), including pages built from one source or batch (a digest page) whose sections belong on existing topic pages, and cross-cutting pages (timelines, decision logs) that copy detail from topic pages instead of linking to them.
- **Misfiled pages**: a page that belongs in another area.
- **Outdated pages**: finished events, replaced devices, superseded plans; candidates for `archive/`.
- **Archive pages to delete**: archived pages nothing is worth looking up in any more (superseded by a wiki page, unlinked, no history the owner would ask about). Deletions only ever happen with the owner's OK; git history keeps them.
- **Contradictions**: two pages (or one page and its sources) that disagree without noting the conflict, and pages that keep a superseded fact next to a "Correction" or "Update" line instead of rewriting it.
- **Unclear status**: purchases or decisions stated as facts without a source that says so.
- **Thin or overgrown pages**: a page that's only a stub, or so long it covers several topics.
- **Personal data**: other people's full names with addresses, phone numbers, emails or ID numbers, where the PII policy isn't `allow`. Name the page and the kind of data, never the data itself.
- **Areas**: an area whose `_index.md` has no `description:`, or a description that no longer fits its pages.
- **Large files**: files over the file limits in this vault's settings, committed without the hook or kept on purpose. `wikismith check --check large` lists them with their sizes; report them, and the owner decides.

## 3. Direct edits

Only mcpedia should write to the wiki. List the commits on the main branch since the last audit that changed `wiki/` or `archive/` without coming from a forge merge (the owner's own edits count too: this is for visibility, not blame):

```
since=$(git log -1 --first-parent --format=%H -- 'reports/audit-*.md')
git log --first-parent --no-merges --date=short --format='%h %ad %an: %s' -n 30 ${since:+$since..}HEAD -- wiki archive
```

## 4. Spot-check facts

Pick 10 cited facts at random, spread across areas, and favor the `my-*` reference pages. For each, open the cited raw file and check that it really says that. Record: page, fact, source, and whether it's confirmed, wrong, or not found in the source.

## 5. Report

Write `reports/audit-YYYY-MM-DD.md`:

```
# Audit YYYY-MM-DD

## Summary
<3-5 lines: overall state, the most important fixes>

## Mechanical checks
<wikismith check output, or "all OK">

## Suggested fixes
- [ ] <page>: <problem> → <suggested fix>

## Archive pages to delete
- [ ] <page>: <why nothing in it is worth keeping>

## Direct edits
<the commits, or "none">

## Fact spot-check
| Page | Fact | Source | Result |

## Notes
<anything else worth knowing>
```

Commit only the report: `git add reports/audit-YYYY-MM-DD.md && git commit -m "Audit YYYY-MM-DD"`. End with the report's Summary section as your final message, under the heading `## Audit YYYY-MM-DD: ready to merge`.
