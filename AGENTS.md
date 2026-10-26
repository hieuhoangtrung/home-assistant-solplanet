# Solplanet Project Instructions

## Project layout

- `custom_components/solplanet/` contains the Home Assistant integration.
- `custom_components/solplanet/panel.py` serves the HA panel APIs, records HA history, and registers the sidebar panel.
- `custom_components/solplanet/www/index.html` is the HA panel frontend.
- `dashboard/server.py` and `dashboard/static/index.html` provide the standalone local dashboard.
- The integration focuses on V2 devices. Do not add V1 features unless the user asks.

## Dashboard and API rules

- The frontend selects its API root from its URL. The HA panel uses `/api/solplanet_panel/*`; standalone mode uses `/api/*` and talks directly to the inverter.
- Before changing either frontend, compare `custom_components/solplanet/www/index.html` and `dashboard/static/index.html` with `cmp -s`. They can share code, but check first because their API routes and available controls differ.
- HA panel data, history, and control APIs require authentication. Preserve this requirement.
- Route HA controls through `ha_service` when available. Keep its service allowlist and validate target entities against the Solplanet integration registry or the discovered automation and script entities.
- Never add an arbitrary Home Assistant service proxy or permit controls for unrelated entities.
- Keep direct inverter endpoints for standalone compatibility. Do not make the standalone server depend on Home Assistant.
- The UI shows `Offline` when its live data request fails. Check the authenticated request in the loaded HA panel and the coordinator state before changing the API. A direct browser visit to an API URL may not reproduce the panel request.

## History and forecasts

- HA history is stored in `custom_components/solplanet/history.db`; standalone history is stored in `dashboard/history.db`.
- The history recorder samples every 300 seconds. Keep runtime database files out of Git.
- The current forecast is a heuristic based on recent readings. It is not a trained machine-learning model. Do not describe it as learned forecasting.
- Preserve existing history fields and endpoint behavior when changing the dashboard.

## Validation

- Run `python3 -m py_compile custom_components/solplanet/panel.py dashboard/server.py` for Python changes.
- Run `ruff check custom_components dashboard` when Ruff is available. Report when it is unavailable; do not install tools without a project need.
- Extract the inline JavaScript from the changed dashboard page and run `node --check` on the extracted file.
- Compare the two frontend files when the change should apply to both. Do not copy one over the other without checking their current contents.
- For UI changes, use a real browser and save a screenshot or accessibility snapshot as evidence before commit. Prefer `chrome-devtools-axi`, then an available Playwright or browser tool. If browser checks are blocked, report the blocker and do not commit the UI change.
- The standalone server needs the packages in `dashboard/requirements.txt`. It may show `Offline` when the local inverter cannot be reached; this does not verify the HA API.
- Run `git diff --check` and review `git status` and the full diff before commit or push. Keep user changes intact.

## Data and generated files

- Do not commit tokens, passwords, or other credentials.
- Treat `dashboard/config.json` as local configuration. Review changes to inverter network settings and weather credentials before publishing them.
- Review screenshots before publishing them. They may show household energy data.
- Do not commit browser capture folders such as `.playwright-mcp/` or generated review files unless the user asks for them. Browser captures can contain unrelated website content.
- Preserve untracked assets. Add only assets that the dashboard uses or that the user asks to publish.
