# ADR-003: Adopt REPO_STANDARDS.md as Canonical Repository Standards

**Date**: 2026-05-25  
**Status**: Accepted  
**Author**: Werner Vogels (D2.3 Standards Documentation)

## Decision

Adopt `REPO_STANDARDS.md` as the canonical reference for naming conventions, directory layout, file organization, and linting policy in the hermes-agent monorepo. All contributors must follow these standards, and they are cross-referenced from `CONTRIBUTING.md`.

## Context

The hermes-agent monorepo has organically grown naming and layout inconsistencies over time:

- **Naming**: Python files inconsistently mixed `snake_case` and `camelCase`; directories used hyphens and underscores interchangeably.
- **Layout**: New contributors had no single reference for where their changes should go, leading to misplaced files.
- **Completion artifacts**: `*_COMPLETION_SUMMARY` files accumulated at the repo root, cluttering directory listings.
- **Editor configuration**: No `.editorconfig` existed, leading to inconsistent indentation, charset, and trailing whitespace across contributors' editors.
- **Linting**: Only `PLW1514` was enforced in CI; advisory rules were undocumented, and there was no process for promoting them to blocking status.

These inconsistencies increased friction for new contributors and made code review less efficient.

## Decision

We adopt `REPO_STANDARDS.md` (created in D2.1) as the single source of truth for:

1. **Naming conventions** — `snake_case.py` for files, `PascalCase` for classes, `test_` prefix for tests, `ADR-{NNN}-{kebab}` for architecture records.
2. **Directory layout** — Canonical directory tree showing where each type of file belongs.
3. **Editor configuration** — `.editorconfig` rules for charset, indentation, and whitespace.
4. **Linting policy** — Blocking rules (PLW1514 only), advisory rules (I001, F, E/W, N, UP), and the promotion process.
5. **ADR conventions** — Naming pattern and indexing requirements for `docs/adr/`.
6. **Completion file policy** — No `*_COMPLETION_SUMMARY` files at repo root; use `docs/completions/` or delete after merge.

This decision is formalized through three cross-referencing documents:

- **`REPO_STANDARDS.md`** — The canonical reference (descriptive, documents what exists).
- **`CONTRIBUTING.md`** — The contributor guide (practical, "how to contribute" with decision trees and code examples). Sections: Repository Structure, Coding Conventions, Completion File Policy.
- **This ADR** — The architectural decision record (why we chose this approach).

## Consequences

### Positive

- **Onboarding**: New contributors have a single source of truth. The "Where does my change go?" decision tree in `CONTRIBUTING.md` provides instant answers.
- **Code review**: Reviewers can reference objective standards rather than subjective preferences ("use snake_case" → "see REPO_STANDARDS.md §1").
- **Consistency**: `.editorconfig` eliminates editor-specific formatting drift. `ruff` enforces encoding (PLW1514) and can auto-fix import sorting (I001).
- **Repo hygiene**: No more `*_COMPLETION_SUMMARY` files at root. `.gitignore` catches these patterns.
- **Governance**: ADR process for evolving the standards (create a new ADR to propose changes).

### Negative

- **Migration effort**: Existing files that violate the naming conventions are not renamed as part of this decision (that's tracked in D2.2). The standards are descriptive of the target state, not a migration plan.
- **Advisory rules not yet blocking**: Rules like I001 and E/W are documented but not enforced in CI. Promotion requires manual triage first (see REPO_STANDARDS.md §4 promotion process).
- **Documentation maintenance**: Three documents (REPO_STANDARDS.md, CONTRIBUTING.md, this ADR) must stay in sync. We accept this because each serves a different audience: the ADR is for architects, REPO_STANDARDS.md is for the canonical reference, and CONTRIBUTING.md is for day-to-day contributors.

## Cross-references

- [REPO_STANDARDS.md](../../REPO_STANDARDS.md) — Canonical reference for naming, layout, linting, and completion file policy.
- [CONTRIBUTING.md](../../CONTRIBUTING.md) — Contributor guide with practical examples and decision tree.
- [ADR-001](./ADR-001-duckdb-primary-datastore.md) — DuckDB as primary data store (references REPO_STANDARDS for `docs/` layout).
- [ADR-002](./ADR-002-nats-jetstream-events.md) — NATS JetStream for events (references REPO_STANDARDS for service layout).
- [.editorconfig](../../.editorconfig) — Editor configuration that enforces formatting rules described in REPO_STANDARDS.md §3.
- [pyproject.toml](../../pyproject.toml) — Ruff configuration that implements the linting policy described in REPO_STANDARDS.md §4.