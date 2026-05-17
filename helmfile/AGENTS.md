# AGENTS.md

## Scope
This workspace is a Helmfile-centered repo.
Everything under this folder should be treated as Helmfile/charts operational work.

- Workspace root: `helmfile/`
- Helmfile entry: `helmfile.yaml`
- Charts root: `chart/`
- Primary app chart: `chart/android-tv`

## Workspace Reality Snapshot (2026-06-20)
Current managed charts in this workspace:

- `chart/android-tv`
- `chart/cluster-issuer`
- `chart/internal-registry`
- `chart/local-llm`
- `chart/local-llm-ui`
- `chart/local-llm-proxy`
- `chart/vscode-server`

Template policy status:

- Chart templates are now standardized to a single active scenario path.
- Runtime behavior switching with Helm `if`/`else`/`with` branches is removed from chart templates.
- Chart templates must not read from `.Values` unless the user explicitly requests an exception.
- All managed chart `values.yaml` files are placeholder-only (`{}`) unless the user explicitly requests configurability.

Current `helmfile.yaml` releases include:

- `cert-manager`
- `cluster-issuer`
- `internal-registry`
- `android-tv`
- `local-llm`
- `local-llm-ui`
- `local-llm-proxy`
- `vscode-server`

Current managed secret/template coupling facts:

- `chart/cluster-issuer` now renders both the `letsencrypt-cloudflare` `ClusterIssuer` and the `cloudflare-api-token` `Secret` consumed by its Cloudflare DNS01 solver.
- `chart/cluster-issuer/secrets.sops.yaml` is the SOPS-backed source for the `cloudflare-api-token` Secret data via Helm secrets integration.
- Project-local SOPS bootstrap and execution helpers are in `scripts/init-local-sops.sh` and `scripts/with-local-sops.sh`.
- `chart/vscode-server` terminates external TLS at Ingress via cert-manager (`letsencrypt-cloudflare`), and no longer generates/uses an in-container self-signed certificate for `code-server`.
- `chart/vscode-server` Continue config now splits model roles (`tinyllama-chat` for chat, `tinyllama` for edit/apply/summarize), uses high chat token limits (`maxTokens: 8192`), and enables `context` providers including `web` for chat retrieval workflows.

## Android-TV Runtime Architecture (Authoritative)
RPI runtime is split into 3 Python modules and 1 HTML template:

- `chart/android-tv/src/rxpy_adb_stream.py`
  - Orchestrator only.
  - Subscribes to source frames and forwards to web frame store.
- `chart/android-tv/src/adb_source.py`
  - ADB + ffmpeg source pipeline.
  - Uses `adb exec-out screenrecord --size ... --output-format=h264 -`.
  - Default size is `640x360` via `ADB_RECORD_SIZE` env var.
- `chart/android-tv/src/web_stream.py`
  - HTTP server: `/`, `/healthz`, `/stream.mjpg`.
  - Event-driven frame push with Rx `exclusive()` (exhaust-like behavior).
- `chart/android-tv/src/web_stream.html`
  - HTML template loaded by web server.

## Active Templates (android-tv)
Keep changes on RPI path unless explicitly requested.

- `chart/android-tv/templates/BuildJob.yaml`
- `chart/android-tv/templates/ConfigMap.yaml`
- `chart/android-tv/templates/Deployment_rpi.yaml`
- `chart/android-tv/templates/PersistentVolumeClaim.yaml`
- `chart/android-tv/templates/Service_rpi.yaml`

## Operational Commands
Run from `helmfile/`.

- Render one chart:
  - `helm template android-tv chart/android-tv -n android-tv`
- Render cluster-issuer with decrypted secrets:
  - `helm secrets template cluster-issuer chart/cluster-issuer -n cert-manager -f chart/cluster-issuer/values.yaml -f chart/cluster-issuer/secrets.sops.yaml`
- Run commands with project-local SOPS key/config:
  - `./scripts/with-local-sops.sh <command> [args...]`
- Full sync:
  - `helmfile sync`
- Apply:
  - `helmfile apply`
- Android-TV rollout check:
  - `kubectl -n android-tv rollout status deployment/android-rpi --timeout=180s`
- Android-TV health check:
  - `curl -sS -D - http://192.168.1.141:30081/healthz`

Run from `chart/android-tv/src/`.

- Python compile check:
  - `python3 -m py_compile rxpy_adb_stream.py adb_source.py web_stream.py`

## Hard Rules for AI Agents
These rules are mandatory unless user explicitly overrides.

1. Helmfile-first workflow
- Prefer source edits + `helmfile sync`.
- Do not patch Helm-managed resources with `kubectl patch` unless truly unavoidable.

2. Templates-only customization policy (global no-`.Values` rule)
- When AI customizes any chart, **do not introduce or move new knobs into `values.yaml`**.
- Default behavior: write concrete values directly in template manifests.
- Do not add new Helm values indirections unless user explicitly asks for configurability.
- All files under `chart/*/templates/*.yaml` must not read from `.Values`.
- If AI touches any file in a chart, migrate that chart's templates to remove all `.Values` reads in the same task.
- Do not preserve existing `.Values` compatibility by default. A remaining `.Values` read is allowed only when the user explicitly asks for that exception in the same task.
- Acceptance check for completion: `rg '\.Values' chart -g '*/templates/*.yaml'` must return zero matches (unless explicit user-approved exceptions are documented in task notes).

3. Post-task documentation sync (required)
- After every completed AI task, check whether folder/release/template/runtime facts changed.
- If changed, update this `AGENTS.md` in the same task before finishing.
- Minimum sections to refresh when relevant:
  - `Workspace Reality Snapshot`
  - `Active Templates`
  - `Operational Commands`
  - `Hard Rules for AI Agents`

4. Android-TV code split must be preserved
- Source logic stays in `adb_source.py`.
- Web serving logic stays in `web_stream.py`.
- Orchestration stays in `rxpy_adb_stream.py`.
- Keep HTML in `web_stream.html`, not inline Python strings.

5. Single-scenario chart policy (no if/else)
- For chart implementation, do not use Helm conditional branches such as `if`, `else`, or `with` for behavior switching.
- This environment has only one runtime scenario; encode that scenario directly in templates.
- Do not introduce enable/disable toggles for mutually exclusive paths.
- If old templates contain conditional branches from historical reasons, simplify them to the single active path when touched.

6. Values hygiene for single-scenario charts
- Do not keep dead toggles in `values.yaml` after branch removal.
- If a behavior is fixed in templates, remove its `enabled`/`type` switching keys from values in the same task.
- Since templates must not read `.Values` by default, remove corresponding keys from `values.yaml` in the same task. An empty `values.yaml` is preferred over stale compatibility knobs.

## Post-Task Update Checklist (Run Before Finishing)
Use this checklist at the end of every AI task.

1. Workspace facts changed?
- Did chart folders, release names, key runtime files, or active templates change?
- If yes, update `Workspace Reality Snapshot` and/or `Active Templates`.

2. Commands still valid?
- If deployment or validation flow changed, update `Operational Commands`.

3. Rules still aligned?
- If user gave new constraints, reflect them in `Hard Rules for AI Agents`.
- Keep Templates-only customization policy unless user explicitly overrides.
- Confirm chart templates do not add new `if`/`else` branching for runtime behavior.
- Run `rg '\.Values' chart -g '*/templates/*.yaml'` and confirm zero matches, or document explicit user-approved exceptions.

4. Android-TV architecture preserved?
- Confirm source/web/orchestrator split is still intact.
- Confirm HTML remains in `web_stream.html`.

5. Final consistency pass
- Ensure this document reflects the current folder and Helmfile reality.
- Do not finish the task if relevant AGENTS.md updates are pending.

## Recovery Notes
- If Helm upgrade fails with ConfigMap field-manager conflict on `src`:
  - `kubectl -n android-tv delete configmap src && helmfile sync`
- Avoid manual patch drift for Helm-managed objects.

## Known Environment Details
- Service endpoint: `http://192.168.1.141:30081/`
- Namespace: `android-tv`
- Deployment: `android-rpi`
- ConfigMap-mounted source directory: `/src`
- Internal registry image domain target: `registry.lan:30500`
