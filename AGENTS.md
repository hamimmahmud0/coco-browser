# AGENTS.md

## Project

- Workspace: `/root/coco-browser`
- Application: COCO instance annotation browser named TRAVAT.
- Main server: `server.py`
- Frontend: `static/index.html`, `static/app.js`, `static/style.css`
- Authentication: `auth_store.py` with SQLite-backed users and sessions.
- Project configuration: `config.yaml`
- Documentation: `README.md`

## Runtime

- Run the server on port `8888` only:
  `python3 server.py --host 0.0.0.0 --port 8888 --workers 0 --no-prompt`
- The server may take several seconds to load the local COCO dataset at startup.
- Check health with:
  `curl -fsS http://127.0.0.1:8888/api/health`
- Do not commit changes unless explicitly requested.
- Do not expose credentials, tokens, or secret files.

## Architecture

- The complete source COCO dataset is loaded into the server process and indexed in memory.
- Project workspace is under `~/.cache/coco-browser/<project-name>/`.
- Important project files:
  - `project.json`
  - `collaboration.json`
  - `analysis-cache.json`
  - `dataset/`
- Collaboration state contains frame workflow states, assignments, workspaces, account metadata, and `island_cleans`.
- `analysis-cache.json` stores latest cached results for Island Frequency, Shape Descriptors, Excess Islands, and Island Cleanup.
- All API routes require authentication. Manager-only routes must remain manager-protected.
- Mutating API requests require the CSRF token returned by login/auth-me.

## Authentication and Roles

- Roles are `manager` and `annotator`.
- Manager-only functionality includes project configuration, account management, frame assignment, Review, and Analysis tools.
- Annotators see only assigned frames and use frame-scoped workspace persistence.
- Frame states are `waiting`, `annotated`, and `reviewed`.
- Account credentials are stored locally in `creds/accounts.txt`; do not print or commit them.

## Analysis Tools

### Island Frequency

- Recalculation must analyze the working project when Island Cleanup drops have been applied.
- `working_project_clean_indices(data)` selects only entries in `collaboration.json` with `applied: true`.
- `count_island_range(bounds, clean_indices)` applies those component drops in worker processes before counting.
- Do not revert frequency to reading only the original source masks.
- A full live scan currently contains approximately 157,921 annotations and can be CPU-intensive. Use progress/cancellation when possible.

### Island Cleanup

- Manager-only tool at `Analysis → Cleanup`.
- Dry-run criteria:
  - Minimum island count `N`.
  - Minimum largest-to-other area ratio `T`; candidates qualify when the ratio is at least `T`.
  - Optional same-image/class evidence from `k` random one-island samples.
  - Evidence sample area ratio must be within `[1-a, 1+a]`.
- Dry-run is cached and does not mutate data.
- Confirmation stores validated component indices in `collaboration.json` under `island_cleans`.
- `apply-island-cleanup/apply` supports:
  - `scope: "current"`: materialize cleaned masks in the active working view.
  - `scope: "all"`: mark all confirmed entries applied in the working project and return cleaned current-image annotations.
- Source annotations must remain unchanged. Export is the authoritative safe application path.
- Export applies cleanup drops before manual mask strokes, recalculates area and bbox, and omits fully removed annotations.
- Current-image applied masks are stored in the client workspace as `state.appliedIslandCleans` and are included in history/project workspace state.
- Apply actions must show an immediate inline busy/status message, disable both Apply buttons while active, and report success or failure.
- Current asset version is `v70`; bump the CSS/JS query version after frontend edits when browser caching matters.

## Frontend Conventions

- Match existing vanilla JavaScript and CSS style; do not add comments.
- Analysis ribbon is manager-only and currently contains Frequency, Descriptors, Excess, and Cleanup.
- Shape Descriptor uses a four-column class grid and per-class random sampling.
- After every UI edit, perform a full UI artifact check:
  - No duplicate HTML IDs.
  - Every `querySelector("#...")` reference exists.
  - Ribbon tabs map to existing panels.
  - No duplicate filter controls.
  - Toggle/checkbox elements are valid and accessible.
  - `node --check static/app.js` passes.
  - Ruff and Python compilation pass.
  - Served HTML contains the new controls and expected asset version.
- Keep progress/status updates visible; users need clear feedback for long-running operations.

## Verification Commands

Run before completing work:

```bash
ruff check server.py auth_store.py
python3 -m py_compile server.py auth_store.py
node --check static/app.js
git diff --check
```

Also run targeted synthetic mask/export tests for mask changes. Do not leave generated `__pycache__` changes in the working tree; restore tracked bytecode and remove untracked bytecode after tests.

## Working Tree

- Do not commit automatically.
- Inspect `git status --short` and the intended diff before reporting completion.
- Preserve unrelated user changes.
- If a live server is needed, restart it only after code changes and verify `/api/health`.
