# Vault rules (wikismith)

These are the general rules for every wikismith vault. The vault's own `CLAUDE.md` adds its personal rules; where they conflict, the vault's rules win. Folder names below are the defaults; `wikismith.yaml` can change them. Only mcpedia writes to the vault: agents in other sessions add captures, and forge and audit jobs do the rest, each on its own branch.

## Layout and flow

```
raw/ ──forge──► wiki/<area>/ ──outdated──► archive/<area>/
```

- `raw/`: everything collected, kept verbatim: exports, clippings, notes captured from chats. **Never edit, move or delete anything here.** Exception: secrets may be redacted, with the owner's OK. The subfolder layout doesn't matter.
- `raw/captures/`: notes added through mcpedia, one file per capture, named `YYYY-MM-DD_slug.md`. Only new files; existing captures are never changed.
- `wiki/<area>/`: the forged knowledge, one folder per area. Each area has an `_index.md` and a reference page of facts about the owner's own setup (`my-*.md`).
- `wiki/_log.md`: one line per forged raw file: `- <raw path> — <YYYY-MM-DD> — <area>[, <area>]` (or `none` if nothing was worth keeping). A raw file not listed here is new and still needs forging.
- `archive/<area>/`: wiki pages that are outdated but worth looking up later. Agents can read them; search skips them unless asked.
- `reports/`: audit reports for the owner. Not part of the wiki.
- `wikismith.yaml`: the vault's configuration. Never change it.

## Areas

- An area is a folder in `wiki/` with an `_index.md`. The index's frontmatter has a one-line `description:` of what the area holds; mcpedia shows it to agents.
- **Never create an area without the owner's OK.** If a file fits no area, or you can't tell which of several it belongs to, leave it unlogged and propose: either the area it should go to, or a new area (name, one-line description, which files it would hold). The owner approves a new area with a capture whose frontmatter lists it under `approves_areas`; only then create its folder and `_index.md`.
- A new vault has no areas: propose a whole set from the material in `raw/`, a handful of broad areas rather than many narrow ones.
- Inside an area, create, merge, split and rename pages freely, and update the area's `_index.md` whenever you do.

## Pages

- **One page per topic.** Before creating a page, check the area's `_index.md` and search the wiki for a page on the same topic, and extend that page instead.
- **No pages by source or batch.** Material from a new source (a mail or chat digest, notes, a capture, one batch of an import) goes into the existing topic pages, not into a page of its own. A separate page is only for a topic that has no page yet.
- **Cross-cutting pages link, they don't copy.** A page that spans topics or projects (a timeline, a list of accomplishments, a decision log) holds one short line per entry with a `[[wikilink]]` to the topic page where the detail lives. The detail stays on the topic page, so a correction there is the only one needed.
- Every page starts with frontmatter:
  ```yaml
  ---
  title: <Title>
  area: <area>
  tags: []
  updated: YYYY-MM-DD
  sources: []
  ---
  ```
  An area's `_index.md` also has `description:`.
- Cite the raw source of each fact inline, e.g. `(source: raw/captures/2026-01-06_pepper-seeds.md)`, and list it in `sources`.
- Link related pages with `[[wikilinks]]`. Images in `raw/` are embedded where they help (`![[<file name>]]`), never copied into the wiki.
- Don't hard-wrap Markdown: one line per paragraph or list item.

## Moving text

mcpedia merges moves without review only when it can verify them, so keep moves and edits apart:

- **Merging or splitting pages:** move every line verbatim. New lines may only be headings, links, frontmatter and index entries. Don't reword, shorten or combine facts in the same forge; propose that as a separate change.
- **Archiving:** move the page unchanged (`git mv wiki/<area>/<page>.md archive/<area>/`), then remove it from the area's `_index.md` and fix links pointing to it.

## Facts

- **Correct facts in place.** When a fact changes or turns out to be wrong, rewrite or remove the old line where it stands; never leave the wrong fact on the page with a separate correction line next to it. A rewrite waits for the owner's review, and that is intended.
- **Provisional text is tagged.** Open questions, plans, tentative dates and the status of ongoing work go in a section whose heading carries a volatile tag (by default `#open`, `#plan`, `#status`, e.g. `## Next steps #plan`); the tag covers the section with its subsections. A single provisional line among facts gets the tag at its end (`- Which hose to buy? #open`). Such text is expected to change: update it in place when newer information arrives.
- **Facts never go in a tagged section.** When a question is decided or a plan is done, write the result as a fact in a normal section and remove or update the tagged line. Tag only what is really provisional, never to avoid a review.
- When sources disagree and neither says which is right, keep both claims, note the conflict, and prefer the more recent one.
- Don't upgrade intentions to facts: research or a plan to buy is "considered", not "bought", unless a source says it happened. Ask the owner when unsure.

## Secrets and personal data

- **Secrets** follow the vault's secret policy (`secrets.policy`, listed under this vault's settings). Under `allow`, keep them as written where they belong. Under `warn`, `redact` or `block`, never put passwords, tokens, keys, account numbers or similar secrets in the wiki; write where they're kept instead ("in the password manager"), and flag the raw file and the kind of secret (never the value) to the owner.
- **Other people's personal details** follow the vault's PII policy (`secrets.pii`). Under `allow`, keep them as written. Under `warn`, `redact` or `block`, refer to people by first name or role ("the neighbor to the north"); never record full names together with addresses, phone numbers, emails or ID numbers such as parcel IDs. The owner's own details are always fine.
- `***REDACTED-…***` in a raw file stands for a masked value. Keep it as it is.
