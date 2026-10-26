# Home Assistant Dashboard Handoff Prompt

Continue work on the Solplanet dashboard in this repository. Read `AGENTS.md`
first. The user wants the live dashboard to display inverter data correctly and
wants SSH access configured through the installed Home Assistant app. Use an
agent session with permission to control the user's Brave browser and reach the
local network.

## Workspace and target

- Repository: `https://github.com/hieuhoangtrung/home-assistant-solplanet`
- Local checkout: `~/projects/home-assistant-solplanet`
- Branch used for this snapshot: `main`. Check the current Git state before work.
- Home Assistant runs on a Raspberry Pi on the user's Wi-Fi network.
- Dashboard: `http://homeassistant.local:8123/solplanet-dashboard`
- The dashboard embeds `/solplanet_panel/index.html`.
- Resolve the Pi's current address if mDNS fails. Do not confuse the Pi with the
  inverter address in `dashboard/config.json`.
- This repository snapshot has not been verified as deployed to the Pi.

## Current implementation

- `custom_components/solplanet/panel.py` registers the panel and its APIs.
- The HA frontend is `custom_components/solplanet/www/index.html`.
- The standalone frontend is `dashboard/static/index.html`. The two files were
  byte-identical at handoff; compare them before making shared edits.
- `dashboard/server.py` is a local development server that calls the inverter
  directly. Its dependencies are in `dashboard/requirements.txt`.
- The dashboard has Overview, Analytics, and Controls views. The requested design
  is a compact technical energy dashboard with a Tesla-inspired energy flow view.
- HA API routes use `/api/solplanet_panel/`; standalone routes use `/api/`.
- HA data, history, work mode, SOC, schedule, context, and service endpoints require
  authentication. Keep those checks enabled.
- `GET /api/solplanet_panel/ha_context` discovers Solplanet devices and entities,
  plus script and automation entities.
- `POST /api/solplanet_panel/ha_service` dispatches allowlisted services. Its target
  validation still needs verification against a real HA instance and focused tests.
- The proxy currently permits selected `select`, `number`, `switch`, and `button`
  services; Solplanet schedule and meter-limit services; `script.turn_on`; and
  `automation.trigger`. Do not add arbitrary service execution.
- Control dispatch is manual, through the dashboard. No autonomous control loop or
  automatic optimization engine has been implemented.
- HA history uses `custom_components/solplanet/history.db` and a background recorder
  with a 300-second interval.
- Standalone history uses `dashboard/history.db`. It records during successful
  live-data requests, with a 300-second minimum interval. It does not collect
  continuously when nobody requests live data.
- The forecast uses recent readings, averages, and SOC trends. It is a heuristic,
  with no model training or persistent learned model.
- Static design images are in `dashboard/static/assets/`. The current energy scene
  uses CSS. If an image is added to the HA panel, package it under
  `custom_components/solplanet/www/assets/`, which is where its asset route looks.

## Evidence and limits

- In the earlier authenticated Brave session, the HA page and weather section
  rendered, but all inverter values showed `--` and the status showed `Offline`.
- On 2026-10-26, a direct command-line request outside the restricted environment
  returned HTTP 200 for the dashboard and the Pi's web root.
- An unauthenticated request to `/api/` returned HTTP 401. This is expected for a
  protected HA API and does not prove a dashboard authentication defect.
- HTTP reachability did not establish interactive browser access. Later browser
  tools could not attach to the user's session or reach the server. The Brave
  connector was not exposed in the final session.
- An earlier direct browser visit to the panel data API returned
  `ERR_BLOCKED_BY_CLIENT`. Do not treat that as proof that the inverter is offline.
- An SSH port probe was rejected by the execution environment. There was no
  confirmed SSH login, connection refusal from the Pi, or server configuration
  change.
- The live-data root cause remains unconfirmed. Inspect the authenticated panel's
  actual requests and HA logs before deciding on a fix.
- The user requested that this snapshot be committed and pushed for handoff after
  the browser access limits were explained. This is not a production validation
  or a completed repair of the live dashboard.

## SSH preparation

The user installed **Advanced SSH & Web Terminal**, not the official Terminal &
SSH app. Its settings are nested under `ssh`, including `username`, `password`,
and `authorized_keys`.

A dedicated Ed25519 key was created on the user's Mac:

- Private key: `~/.ssh/homeassistant`, permissions `0600`.
- Public key: `~/.ssh/homeassistant.pub`.
- Public-key fingerprint:
  `SHA256:uMz5kZl2uqXBJhtyAwMKHC3pnMQKytlRHriM4XFulzU`.
- The public key was supplied to the user. Its installation in HA is unconfirmed.
- The configured SSH username and port have not been confirmed.

Read only the public key when preparing the app configuration. Keep the private
key on this Mac and out of chat, logs, Git, and uploads. If running on a different
machine, arrange a separate public key rather than transferring this private key.

App documentation:
`https://github.com/hassio-addons/app-ssh/blob/main/ssh/DOCS.md`

## Work to complete

1. Open the dashboard through the authorized Brave connection. Verify the visible
   page and authentication state. Do not report browser access based only on an
   HTTP status from a command-line request.
2. Open Advanced SSH & Web Terminal in HA. Inspect the current username, authorized
   keys, and network port. Append the prepared public key if it is absent. Preserve
   existing keys, other settings, and the current username. Enable a local SSH port
   if it is disabled, then save and restart only the SSH app. Do not enable router
   port forwarding or disable protection mode for this task.
3. Test SSH using the configured username and port and the local key. Verify the
   server host key before accepting it. Start with read-only commands. For this
   app, HA CLI commands run over SSH may need a login shell, for example
   `bash -lc 'ha core info'`. Never print supervisor tokens or credential files.
4. Inspect the dashboard's authenticated requests to `/api/solplanet_panel/data`,
   `/ha_context`, and `/history`. Record response status and relevant error details,
   with credentials removed. Inspect HA logs, integration health, and coordinator
   data. Compare the deployed files with this checkout; the deployed UI may be older.
5. Investigate authentication as one hypothesis: the current frontend uses bare
   `fetch()` calls, while its HA views require authentication. Determine whether
   those requests include valid HA authentication. If missing, use a supported HA
   authenticated request mechanism or an authenticated panel wrapper. Keep
   `requires_auth = True`; do not expose data or controls to solve a login problem.
6. Fix the observed data-path issue with the smallest supported change. Verify
   payload normalization, units, battery and grid sign conventions, and real zero
   versus missing values. Do not guess device state or label missing data as zero.
7. Check the service allowlist and target validation. Include mixed-target requests
   with unrelated entity, device, area, or label selectors. A valid Solplanet entity
   must not authorize other targets supplied in the same request. Use mocks or a
   harmless test entity; do not test by changing the inverter, battery, export limit,
   schedules, or arbitrary household scripts.
8. Preserve direct standalone endpoints, schedule encoding, and stored history.
   Confirm that any shared frontend fix works in both runtimes.
9. Before deployment, back up the current integration files and preserve runtime
   databases and credentials. Deploy the reviewed files, including any required
   assets. Use the smallest reload or HA restart required and check startup logs.
10. Verify the real HA dashboard in desktop and mobile layouts, both themes, and
    offline or empty states. Save current screenshots or accessibility evidence.
    Confirm that live readings update and that history and entity discovery work.
11. Commit and push the completed fix. Report the root cause, changes, deployment
    state, evidence paths, commit IDs, and any remaining limits.

## Checks and publication notes

The handoff snapshot passed Python compilation for `panel.py` and `server.py`,
JavaScript syntax parsing for both frontend files, the frontend byte comparison,
and `git diff --check`.

Ruff was unavailable. A previous standalone launch failed because the selected
Python environment lacked `aiohttp`. Real-browser verification of the current
checkout, authenticated HA API checks, and live control tests remain incomplete.
Recheck the current environment rather than repeating those failures unchanged.

Run the appropriate Python checks, `ruff check custom_components dashboard` when
available, frontend syntax checks, and focused tests for changed behavior. Install
standalone dependencies into a suitable local environment if needed. Use the
project's requirements rather than adding a new package manager or framework.

Local `.playwright-mcp/`, `.lavish/`, old `dashboard/screenshots/`, and `mock-up.jpeg`
were left out of the source snapshot. They are preserved in the original checkout
and may contain unrelated pages, location details, or old dashboard readings.
Neither SSH keys nor runtime SQLite databases belong in the repository.
