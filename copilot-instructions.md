# Copilot Instructions — Provision Gateway

> These instructions apply to ALL development work in this workspace.
> Violating any of them means incomplete work.

---

## 1. Post-Implementation Design Audit

After implementing a feature, you **must** re-read the relevant sections of
`design.md` and `requirements.md` and verify every line item is satisfied.

- Go point-by-point through the design spec
- Check every API endpoint, every UI element, every DB field
- If something is missing, implement it before claiming "done"
- Do NOT rely on memory — re-read the document each time

---

## 2. Documentation Discipline

All feature development goes hand-in-hand with docs. Docs live in `docs/` at
the repo root and serve as the **single source of truth** for how features
evolve over time.

- Create or update a doc in `docs/` for every feature or sub-feature
- Use **Mermaid diagrams** for complex flows, state machines, or architecture
- Docs must include: purpose, design decisions, API changes, test coverage
- Docs are chronological — append, don't overwrite history

---

## 3. Testing Requirements

Every feature **must** have three layers of tests. No exceptions. No skipping.

| Layer | Scope | Tool | Must Pass |
|---|---|---|---|
| **Unit tests** | Function correctness, edge cases | `pytest` | ✅ |
| **E2E tests** | Longer logic chains with mocked deps | `pytest` + mocks | ✅ |
| **Integration tests** | Real dependencies, real containers | `.sh` scripts | ✅ |

Rules:
- Write tests **alongside** the code, not after
- Integration tests must run against the actual deployed system
- No test may be skipped (`@pytest.mark.skip` is forbidden)
- Test files must be executable and self-contained
- Summarize test results in the corresponding doc in `docs/`

---

## 4. Always Stay on Track

Constantly cross-reference the design and docs:

- Before starting a feature: read the design section
- During implementation: check docs for any recorded changes or decisions
- After implementation: verify against both design AND docs
- If you find yourself drifting, stop and realign

---

## 5. Never Make Things Up

- If a requirement is **unclear**, **ask the user** for clarification
- If you start a long development task and encounter unclear parts:
  - Do **NOT** break early or abandon the task
  - Defer unclear items to the end of the batch
  - Implement everything that IS clear first
  - Before leaving any feature behind, **double-check** the design and docs
    to confirm the item is genuinely unclarified (not just missed)
- Do **NOT** implement features that are not specified — ask first
- Do **NOT** guess API keys, credentials, URLs, or user intent

---

## 6. Handling Design Conflicts

If you find a conflict or error in the design that requires a change:

- **Leave a comment** next to the modification in `design.md` explaining why
- **Record the change** in `updated_design.md` in `docs/` with:
  - What the original design said
  - Why it was wrong or conflicting
  - What the resolution was
  - Which files were affected
- Never silently overwrite the design

---

## 7. Never Be Lazy

This is the most important rule.

- Do not skip tests because "it's simple"
- Do not skip docs because "it's obvious"
- Do not implement half a feature and call it done
- Do not leave broken UI elements and move on
- Do not push code without running ALL tests first
- Do not assume — verify, check, test, document
- **Finishing means: implemented + tested + documented + verified against design**
