# Agent Ergonomics: `edit_article` and `search_and_read` Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development) to implement this plan task-by-task.

**Goal:** Cut the tool calls, model turns and bytes an agent spends working in the wiki. `edit_article` changes part of an article without resending it. `search_and_read` returns the best hits' content in the same call as the search.

**Architecture:** Two new tools. Neither adds a new permission path.
- `edit_article` uses the same sequence as `update_article`: grant check, WRITE credential for the exact key, read, compare `if_version`, `put_if_match`, then refresh the listing. It only computes the new body and frontmatter from edits instead of taking them whole.
- `search_and_read` calls the existing `search` handler, then the existing `read_article` handler for each top hit. A batch therefore can never return what individual reads would refuse, and failures are per item.

**Tech stack:** Python 3.13, pytest + moto, the existing `app/mcp/tools/*` pattern (`TOOL = Tool(...)`, registry in `app/mcp/tools/__init__.py`).

**Evidence (session of 2026-09-24):**
- Three roadmap updates each resent 19–23 KB to change a few lines.
- Two decision-log updates each resent about 9 KB.
- Every lookup was search, then read: two sequential model turns.

**Out of scope (YAGNI):** `read_articles`, recursive `list_folder`, backlinks, changes-since, outlines, `insert_table_row` (a `str_replace` covers it).

---

### Task 1: Share the write tail of `update_article`

**Files:** Modify `app/mcp/tools/update_article.py`. Tests: existing `tests/unit/tools/test_update_article.py` must stay green.

Extract the steps after the version check into a module-level helper that `edit_article` will reuse:

```python
def write_current(
    ctx: ToolContext, st: ArticleStore, s3: Any, path: str,
    current: StoredObject, frontmatter: dict[str, Any], content: str,
) -> dict[str, Any]:
    """check_link → seq+1 → serialize_article → put_if_match (412 → _stale of the live
    object) → refresh_parent → {"path", "version", "seq"}."""
```

Also make `_live` and `_stale` importable (rename them to `live_for_update` / `stale_error`, or export them as they are, but be consistent). `update_article.handle` calls the helper. There is no behaviour change. Commit: `refactor(update): share the write tail for patch edits`.

---

### Task 2: `edit_article`

**Files:** Create `app/mcp/tools/edit_article.py` and `tests/unit/tools/test_edit_article.py`. Register it in `app/mcp/tools/__init__.py` after `update_article`.

**Input:**
```
path          article path (required)
if_version    required
edits         array, 1–50 items, each exactly one of:
                {"old": str, "new": str, "replace_all"?: bool}
                {"append": str, "section"?: str}
frontmatter   optional object merged into the stored block at top level;
              a key set to null is removed. `seq` is dropped silently (as update does).
```
At least one of `edits` or `frontmatter` is required.

**Semantics:**
- Edits apply **in order** to the current body. Each sees the result of the ones before it.
- `old`/`new` is an exact substring match.
  - `old` must be non-empty.
  - Zero matches → 400 `bad_request`: "edits[i].old not found".
  - More than one match without `replace_all` → 400: "edits[i].old matches N times — add context or set replace_all".
- `append` without `section` adds the text at the end of the body. If the body doesn't end with a newline, insert one first.
- `append` with `section` inserts at the end of that section, meaning just before the next heading of the same or higher level, or at the end of the body.
  - Headings are matched case-insensitively. Headings inside fenced code are ignored.
  - Reuse `read_article.extract_section`'s rules: refactor it to also expose the span `(start, end)` of the section, and don't write a second parser.
  - Unknown section → 400 naming the section.
- If any edit fails, **nothing is written**. The 400 names the index of the failing edit.
- Frontmatter: merge into `revalidate_stored(stored)`, delete null-valued keys, then `validate_frontmatter(merged, drop_seq=True)`. `check_link` runs inside `write_current`.
- The result body goes through `serialize_article`, which enforces the 1 MiB cap.
- Pointer, tombstone or missing path → 404. Permission: `write` on the path, exactly as for `update_article`.
- **Stale `if_version` (409 `conflict`) is lean.** It returns no `current_body`. Instead it carries:
  - `current_version`;
  - `edits_apply: bool`, true when every edit would succeed against the current body (computed with the same function, no write);
  - a message: "Changed since you read it. If edits_apply is true, retry with if_version = current_version; otherwise read the article and redo the edits."
  - Losing the race at S3 (412) reports the same way.
- **Success** returns the `WRITE_RESULT_SCHEMA` fields (`path`, `version`, `seq`) plus `total_bytes`, the body's byte length. Never the body.
- `DESCRIPTION` states intent so tool search finds it: "Edit part of an article — replace exact text, append to the end or to a section, or change individual frontmatter fields — without resending the whole body. Prefer this over update_article for small changes."

**Tests (TDD; write each failing first):**
1. A single replace. The body changes only there, `seq` increments, the listing refreshes, and the result has no body.
2. Several edits apply in order, the second depending on the first.
3. `old` not found → 400 naming the edit index, nothing written (the version is unchanged).
4. An ambiguous `old` → 400. With `replace_all` every occurrence is replaced.
5. Append at the end, with and without a trailing newline.
6. Append to a section in the middle, before the next same-level heading. A heading inside a code fence is ignored. An unknown section → 400.
7. A frontmatter merge sets a key and removes a key via null. `type` can't become `pointer`. `link_to` added to a doc → 400 (from `check_link`).
8. Stale `if_version` whose edits still apply → 409 with `edits_apply: true` and no `current_body`. A retry with `current_version` succeeds.
9. Stale `if_version` whose edits no longer apply → 409 with `edits_apply: false`.
10. No `write` grant → 404 or 403, matching `update_article`'s behaviour exactly (look at its tests). A zero-grant caller mints nothing.
11. Editing a link article's frontmatter `link_to` to an invalid target → 400.
12. The descriptor is self-contained (no `$ref`), and the scope is `SCOPE_WRITE`.

Commit: `feat(tools): edit_article — patch edits without resending the body`.

---

### Task 3: `search_and_read`

**Files:** Create `app/mcp/tools/search_and_read.py` and `tests/unit/tools/test_search_and_read.py`. Register it after `search`.

**Input:** `query` (required), `prefix?`, `k?` (1–10, default 3), `section?` (applied to every read), `max_bytes?` (default 60,000, maximum 100,000; keeps the result inside the 25k-token tool budget).

**Behaviour:**
- Call `search.handle(ctx, {"query", "prefix", "limit": k})`.
- For each hit in order, call `read_article.handle(ctx, {"path": hit.path, "section"?})`.
  - Catch `ToolError` per item and record `{"path", "error": {"status", "code"}}`. A missing section → 404 for that item only.
- Each result item is the hit's summary fields plus `score` plus one of:
  - `content`, `version`, `total_bytes`, `returned_bytes`, `truncated`;
  - `reference` (the read returned a link or forward reference: pass it through; do **not** follow it);
  - `error`.
- **Budget.** Keep a running byte count of `content`.
  - When the next item's content would exceed `max_bytes`, cut it to the remaining budget (whole UTF-8 characters) and set `truncated: true`.
  - Items after that get no content, only `"skipped": "budget"` and their summary, so the agent can read them individually.
- Top level: `{"results": [...], "search_truncated": <search's truncated>}`.
- Audit: each inner handler already notes its grants. Don't add another note.
- `DESCRIPTION`: "Search, then return the top hits' content in the same call — use when you'd otherwise search and then read the best result. Each read is checked exactly as read_article; links and moved pages come back as references, not followed."

**Tests:**
1. Three matching docs: `k=2` returns two items with content, in search order.
2. `section` returns only that section. A hit without the section gets a per-item 404, and the other items are unaffected.
3. The budget: a small `max_bytes` truncates the first item and marks the rest `skipped`.
4. A link hit returns `reference.kind == "link"`, and no credential is minted for the target (assert on `minter.calls`).
5. **Never more than individual reads.** A caller with read on `/a` only, and matching docs in `/a` and `/b`, gets results only from `/a`. `minter.calls` never touches `/b`.
6. A zero-grant caller → empty results, nothing minted.
7. The descriptor is self-contained, and the scope is `SCOPE_READ`.

Commit: `feat(tools): search_and_read — search and read top hits in one call`.

---

### Task 4: Docs

- HANDOFF.md §10: add both tools in the style of their neighbours. Change the tool count from twelve to fourteen everywhere it appears: HANDOFF, README, PRD (including line 35), ARCHITECTURE (reads/writes split: 8 reads, 6 writes; mermaid label). Add rows to the ARCHITECTURE and PRD tool tables.
- `app/mcp/tools/__init__.py` docstring: "fourteen".
- Grep check: `grep -rn -i "twelve\|12 tools" README.md docs HANDOFF.md app`, excluding docs/plans.

Commit: `docs: edit_article and search_and_read in the spec`.

---

### Task 5: Verify

`make lint && make typecheck && uv run pytest -q -o addopts=""`. All green, and report the summary line.
