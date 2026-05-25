# Repository Standards

This document records naming conventions, directory layout, and linting policy
for the hermes-agent monorepo.  It is descriptive (documents what exists) rather
than aspirational — any proposed reorganisation belongs in a separate ADR.

---

## 1  Naming Conventions

| Asset | Convention | Examples |
|---|---|---|
| Python source files | `snake_case.py` | `vcg_dispatcher.py`, `run_agent.py`, `model_tools.py` |
| Python classes | `PascalCase` | `VCGDispatcher`, `AIAgent`, `HermesCLI` |
| Test files | `test_{module_name}.py` under `tests/` | `test_vcg_dispatcher.py`, `test_rate_limiter_concurrent.py` |
| Directory names | Lowercase, underscores preferred (no hyphens for Python packages) | `agent/`, `hermes_cli/`, `tui_gateway/` |
| Config / dotfiles | Lowercase, hyphens where needed | `.editorconfig`, `pyproject.toml`, `cli-config.yaml.example` |
| ADR documents | `ADR-{NNN}-{kebab-case-title}.md` in `docs/adr/` | `ADR-001-duckdb-primary-datastore.md` |
| Completion / summary files | **Do NOT land in repo root.** Use `docs/completions/` or delete after merge. | ~~`O7-06_COMPLETION_SUMMARY.md`~~ → `docs/completions/O7-06_completion_summary.md` |
| Skills | `kebab-case` skill directories under `skills/` or `optional-skills/` | `skills/code-review/`, `optional-skills/media/youtube-content/` |

### Rationale

- **snake_case for Python files**: Standard PEP 8 convention; makes imports predictable (`from gateway.vcg_dispatcher import VCGDispatcher` matches filename).
- **PascalCase for classes**: Universal Python convention.
- **Underscores for directories**: Avoids import-path ambiguity (`import hermes_cli` works directly; `hermes-cli` would require `importlib` hacks or a package-name mismatch).
- **Hyphens for config/dotfiles**: Follows EditorConfig, GitHub, and UNIX conventions (`.gitignore`, `.editorconfig`, `.mailmap`).
- **Completion files out of root**: Root clutter makes directory listings noisy. Completion summaries are documentation — they belong under `docs/`.

---

## 2  Standard Directory Layout

```
hermes-agent/                   # Monorepo root
├── run_agent.py                # Core agent loop (AIAgent class)
├── model_tools.py              # Tool orchestration, discover_builtin_tools()
├── toolsets.py                 # Toolset definitions, _HERMES_CORE_TOOLS
├── cli.py                      # CLI orchestrator (HermesCLI)
├── hermes_state.py             # SessionDB — SQLite session store
├── hermes_constants.py         # get_hermes_home(), display_hermes_home()
├── hermes_logging.py           # setup_logging() — profile-aware log paths
├── hermes_time.py              # Timezone / timestamp helpers
├── hermes_bootstrap.py         # First-run wizard
├── batch_runner.py             # Parallel batch processing
├── trajectory_compressor.py    # Conversation trajectory compression
├── toolset_distributions.py    # Toolset packaging / distribution metadata
├── utils.py                    # Shared utilities
├── rl_cli.py                   # RL training CLI
├── mcp_serve.py                # MCP server entry point
├── demo_help_system.py         # Help-system demo script
│
├── agent/                      # Agent internals
│   ├── memory_manager.py       # Persistent memory (LTM, STM, TemporalTrace)
│   ├── prompt_builder.py       # System prompt construction & injection
│   ├── display.py              # KawaiiSpinner, Rich output formatting
│   ├── skill_commands.py       # Slash-command registry for skills
│   ├── skill_utils.py          # Skill discovery & loading
│   ├── credential_pool.py      # API key rotation & budgeting
│   ├── context_compressor.py   # Conversation context compression
│   └── ...                     # (providers, adapters, security, i18n, etc.)
│
├── tools/                      # Tool implementations (auto-discovered)
│   ├── registry.py              # Tool registration — imported by all tools
│   ├── terminal_tool.py        # Shell execution tool
│   ├── browser_tool.py         # Browser automation
│   ├── file_tools.py           # File read/write/patch
│   ├── web_tools.py            # Web search & extraction
│   ├── environments/           # Terminal backends (local, docker, ssh, modal, …)
│   └── ...                     # (kanban, cronjob, memory, skills, vision, etc.)
│
├── gateway/                    # Messaging gateway + platform adapters
│   ├── run.py                  # Gateway entry point
│   ├── session.py              # Per-platform session management
│   ├── config.py               # Gateway configuration loader
│   ├── vcg_dispatcher.py       # VCG game-theoretic task allocation
│   ├── vcg_gateway.py          # VCG integration with gateway
│   ├── instance_orchestrator.py # Multi-instance orchestration
│   ├── platforms/              # Per-platform adapters (telegram, discord, …)
│   └── builtin_hooks/          # Hook extension point
│
├── hermes_cli/                 # CLI subcommands & setup wizard
│   ├── main.py                 # Entry point (hermes command)
│   ├── commands.py             # Slash-command registry
│   └── skin_engine.py          # Data-driven CLI theming
│
├── plugins/                    # Plugin system (memory, model-providers, kanban, …)
├── skills/                     # Built-in skills (active by default)
├── optional-skills/            # Heavier/niche skills (not active by default)
├── tui_gateway/                # Python JSON-RPC backend for the TUI
├── ui-tui/                     # Ink (React) terminal UI — hermes --tui
│
├── acp_adapter/                # ACP server (VS Code / Zed / JetBrains integration)
├── acp_registry/               # ACP client registry
├── cron/                       # Scheduler — jobs.py, scheduler.py
├── providers/                  # Model provider adapters
├── environments/               # RL training environments (Atropos)
├── scripts/                    # run_tests.sh, release.py, utility scripts
├── docker/                     # Dockerfile + docker-compose.yml
├── packaging/                  # Package distribution config
├── assets/                     # Static assets (images, sounds, etc.)
├── locales/                    # i18n translation files
├── nix/                        # Nix flake / build support
├── website/                    # Docusaurus documentation site
├── web/                        # FastAPI web dashboard
├── projects/                   # Project-specific configs
├── src/                        # Build artifacts (src layout)
│
├── tests/                      # ~17k tests across ~900 files
├── docs/
│   ├── adr/                    # Architecture Decision Records
│   │   ├── ADR-001-duckdb-primary-datastore.md
│   │   ├── ADR-002-nats-jetstream-events.md
│   │   └── INDEX.md            # ADR index
│   ├── design/                 # Design documents
│   ├── plans/                  # Planning documents
│   └── completions/            # (Proposed) Completion summaries — NOT repo root
│
├── .editorconfig               # Editor settings (see §3)
├── pyproject.toml              # Build config, ruff config, pytest config
├── CONTRIBUTING.md              # Contribution guide
├── AGENTS.md                   # AI coding assistant instructions
├── CHANGELOG.md                # Release history
├── README.md                   # Project overview
├── SECURITY.md                 # Security policy
└── LICENSE                     # MIT license
```

---

## 3  Editor Configuration

The root `.editorconfig` enforces consistent formatting across all editors:

| Setting | Global | Python | YAML/JSON/TOML | Markdown | Shell | JS/TS/CSS |
|---|---|---|---|---|---|---|
| `charset` | utf-8 | — | — | — | — | — |
| `end_of_line` | lf | — | — | — | — | — |
| `insert_final_newline` | true | — | — | — | — | — |
| `trim_trailing_whitespace` | true | — | — | **false** | — | — |
| `indent_size` | — | 4 | 2 | 2 | 2 | 2 |
| `indent_style` | — | space | space | space | space | space |

Markdown trailing whitespace is preserved because two trailing spaces = hard line break in CommonMark.

---

## 4  Linting Policy

### Blocking (enforced by CI)

Only **PLW1514** (unspecified-encoding) is in the blocking `select` list in
`pyproject.toml`.  This catches `open("file")` without `encoding=` which causes
silent data corruption on Windows.

### Advisory (run manually)

The following rule categories are documented but not yet in the blocking select:

| Code | Category | `ruff check --select` |
|---|---|---|
| I001 | import sorting (isort) | `--select I001` |
| F | pyflakes | `--select F` |
| E, W | pycodestyle | `--select E,W` |
| N | pep8-naming | `--select N` |
| UP | pyupgrade | `--select UP` |

Run all advisory rules:

```bash
ruff check --select I001,F,E,W,N,UP --statistics
```

### Promotion Process

To promote an advisory rule to blocking:

1. Run `ruff check --select <CODE> --statistics` to assess violation count.
2. If violations are few (< 50), fix them in a focused PR and add the code to
   the blocking `select` list in `pyproject.toml`.
3. If violations are many, create a dedicated cleanup task (D2.x) and defer
   promotion until the backlog is cleared.

### Formatting

`ruff format` is configured with:

- `line-length = 100`
- `quote-style = "double"`

These apply automatically — no manual enforcement needed.

---

## 5  ADR Conventions

Architecture Decision Records live in `docs/adr/` and follow the naming pattern
`ADR-{NNN}-{kebab-case-title}.md`.  An `INDEX.md` lists all ADRs with status
(proposed / accepted / deprecated / superseded).

When creating a new ADR:

1. Increment the sequence number (check `INDEX.md` for the latest).
2. Use kebab-case for the title suffix: `ADR-003-my-decision.md`.
3. Add an entry to `INDEX.md`.

---

## 6  Completion / Summary Files

**Do not land completion summaries or handoff documents at the repository root.**

The root currently contains stale files like `O7-06_COMPLETION_SUMMARY.md`,
`P1-005_COMPLETION_SUMMARY.txt`, etc. — these are documentation artifacts that
should live under `docs/completions/` (to be created in D2.2) or be deleted
after merge.

This is a documentation-only standard; no files are moved or renamed as part of
D2.1.  Cleanup is tracked separately (D2.2).