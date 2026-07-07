"""Solplanet Battery Dashboard - HA custom panel API view."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .client import BatteryWorkModes, ScheduleSlot
from .const import (
    BATTERY_IDENTIFIER,
    DOMAIN,
    INVERTER_IDENTIFIER,
    METER_IDENTIFIER,
)

_LOGGER = logging.getLogger(__name__)

PANEL_DIR = Path(__file__).parent / "www"
DB_PATH = Path(__file__).parent / "history.db"
HISTORY_INTERVAL = 300  # seconds between recordings

SCHEDULE_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------------------------------------------------------------------------
# History DB helpers
# ---------------------------------------------------------------------------
def _db_init(db_path: Path) -> sqlite3.Connection:
    """Open (or create) the history database and ensure schema exists."""
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            ts      INTEGER PRIMARY KEY,
            ppv     REAL,
            pac     REAL,
            pbat    REAL,
            pgrid   REAL,
            soc     REAL,
            tb      REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON readings(ts)")
    conn.commit()
    return conn


def _db_insert(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO readings (ts,ppv,pac,pbat,pgrid,soc,tb) VALUES (?,?,?,?,?,?,?)",
        (
            row["ts"],
            row.get("ppv"),
            row.get("pac"),
            row.get("pbat"),
            row.get("pgrid"),
            row.get("soc"),
            row.get("tb"),
        ),
    )
    conn.commit()


def _db_query(conn: sqlite3.Connection, since_ts: int) -> list[dict]:
    cur = conn.execute(
        "SELECT ts,ppv,pac,pbat,pgrid,soc,tb FROM readings WHERE ts>=? ORDER BY ts",
        (since_ts,),
    )
    cols = ["ts", "ppv", "pac", "pbat", "pgrid", "soc", "tb"]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _serialise(obj: Any) -> Any:
    """Recursively convert dataclasses / HA response objects to plain dicts."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _serialise(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _serialise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialise(i) for i in obj]
    return obj


def _get_coordinator(hass: HomeAssistant):
    """Return first available coordinator or None."""
    entries = hass.data.get(DOMAIN, {})
    for entry_data in entries.values():
        coord = entry_data.get("coordinator")
        if coord and coord.data:
            return coord
    return None


class SolplanetDataView(HomeAssistantView):
    """GET /api/solplanet_panel/data - returns all live data."""

    url = "/api/solplanet_panel/data"
    name = "api:solplanet_panel:data"
    requires_auth = False

    async def get(self, request):
        try:
            return await self._get_data(request)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("Solplanet panel data error: %s", exc)
            return self.json({"error": str(exc)}, status_code=500)

    async def _get_data(self, request):
        hass: HomeAssistant = request.app["hass"]
        coord = _get_coordinator(hass)
        if not coord:
            return self.json({"error": "No Solplanet coordinator found"}, status_code=503)

        data = coord.data
        inv_id = INVERTER_IDENTIFIER
        bat_id = BATTERY_IDENTIFIER
        met_id = METER_IDENTIFIER

        # Build inverter list
        inverters = []
        for isn, entry in data.get(inv_id, {}).items():
            info = entry.get("info")
            idata = entry.get("data")
            inverters.append({
                "sn": isn,
                "model": getattr(info, "model", None),
                "status": getattr(idata, "flg", None),
                "pac": getattr(idata, "pac", None),
                "ppv": getattr(idata, "ppv", None),
                "ppv1": getattr(idata, "ppv1", None),
                "ppv2": getattr(idata, "ppv2", None),
                "ppv3": getattr(idata, "ppv3", None),
                "ppv4": getattr(idata, "ppv4", None),
                "etd": getattr(idata, "etd", None),
                "eto": getattr(idata, "eto", None),
                "tmp": getattr(idata, "tmp", None),
                "fac": getattr(idata, "fac", None),
                "hto": getattr(idata, "hto", None),
                "vac": _serialise(getattr(idata, "vac", None)),
                "iac": _serialise(getattr(idata, "iac", None)),
                "pf":  getattr(idata, "pf", None),
            })

        # Build battery list
        batteries = []
        for isn, entry in data.get(bat_id, {}).items():
            bdata = entry.get("data")
            binfo = entry.get("info")
            work_modes = entry.get("work_modes", {})
            schedule_raw = entry.get("schedule", {})
            schedule = _decode_schedule(schedule_raw) if schedule_raw else {}
            batteries.append({
                "sn": isn,
                "soc":    getattr(bdata, "soc",  None),
                "soh":    getattr(bdata, "soh",  None),
                "pb":     getattr(bdata, "pb",   None),   # battery power (+discharge/-charge)
                "vb":     getattr(bdata, "vb",   None),   # battery voltage *100
                "cb":     getattr(bdata, "cb",   None),   # battery current *10
                "tb":     getattr(bdata, "tb",   None),   # temperature *10
                "bst":    getattr(bdata, "bst",  None),   # battery status
                "ppv":    getattr(bdata, "ppv",  None),   # pv power from battery data
                "etdpv":  getattr(bdata, "etdpv",None),   # today pv
                "ebi":    getattr(bdata, "ebi",  None),   # today bat in
                "ebo":    getattr(bdata, "ebo",  None),   # today bat out
                "eaci":   getattr(bdata, "eaci", None),
                "eaco":   getattr(bdata, "eaco", None),
                "charge_max":    getattr(binfo, "charge_max",    None),
                "discharge_max": getattr(binfo, "discharge_max", None),
                "work_mode_name": getattr(work_modes.get("selected"), "name", None),
                "work_modes_all": [
                    {"name": m.name, "type": getattr(m, "battery_type", 0), "mod_r": getattr(m, "mod_r", i)}
                    for i, m in enumerate(work_modes.get("all") or [])
                ],
                "schedule": schedule,
            })

        # Build meter list
        meters = []
        for sn, entry in data.get(met_id, {}).items():
            mdata = entry.get("data")
            app_data = entry.get("app_data", {}) or {}
            # V2: pac in app_data is negative=export, positive=import (Watts)
            # V1: pac is on mdata directly
            pac = getattr(mdata, "pac", None)
            if pac is None:
                raw = app_data.get("activePower") or app_data.get("pac")
                pac = raw
            itd = getattr(mdata, "itd", None) or app_data.get("importToday") or app_data.get("itd")
            otd = getattr(mdata, "otd", None) or app_data.get("exportToday") or app_data.get("otd")
            iet = getattr(mdata, "iet", None) or app_data.get("importTotal") or app_data.get("iet")
            oet = getattr(mdata, "oet", None) or app_data.get("exportTotal") or app_data.get("oet")
            meters.append({"sn": sn, "pac": pac, "itd": itd, "otd": otd, "iet": iet, "oet": oet})

        return self.json({
            "inverters": inverters,
            "batteries": batteries,
            "meters": meters,
        })


class SolplanetWorkModeView(HomeAssistantView):
    """POST /api/solplanet_panel/work_mode."""

    url = "/api/solplanet_panel/work_mode"
    name = "api:solplanet_panel:work_mode"
    requires_auth = False

    async def post(self, request):
        hass: HomeAssistant = request.app["hass"]
        coord = _get_coordinator(hass)
        if not coord:
            return self.json({"error": "No coordinator"}, status_code=503)
        body = await request.json()
        sn = body.get("sn")
        battery_type = int(body.get("type", 0))
        mod_r = int(body.get("mod_r", 0))
        mode = BatteryWorkModes().get_mode(battery_type, mod_r)
        if not mode:
            return self.json({"error": f"Unknown mode type={battery_type} mod_r={mod_r}"}, status_code=400)
        await coord.set_battery_work_mode(sn, mode)
        return self.json({"ok": True})


class SolplanetSocLimitsView(HomeAssistantView):
    """POST /api/solplanet_panel/soc_limits."""

    url = "/api/solplanet_panel/soc_limits"
    name = "api:solplanet_panel:soc_limits"
    requires_auth = False

    async def post(self, request):
        hass: HomeAssistant = request.app["hass"]
        coord = _get_coordinator(hass)
        if not coord:
            return self.json({"error": "No coordinator"}, status_code=503)
        body = await request.json()
        sn = body.get("sn")
        soc_min = body.get("soc_min")
        soc_max = body.get("soc_max")
        if soc_min is not None:
            await coord.set_battery_soc_min(sn, int(soc_min))
        if soc_max is not None:
            await coord.set_battery_soc_max(sn, int(soc_max))
        return self.json({"ok": True})


class SolplanetScheduleView(HomeAssistantView):
    """GET/POST /api/solplanet_panel/schedule."""

    url = "/api/solplanet_panel/schedule"
    name = "api:solplanet_panel:schedule"
    requires_auth = False

    async def get(self, request):
        hass: HomeAssistant = request.app["hass"]
        coord = _get_coordinator(hass)
        if not coord:
            return self.json({"error": "No coordinator"}, status_code=503)
        # Pull from coordinator data (already fetched)
        bat_data = coord.data.get(BATTERY_IDENTIFIER, {})
        schedule_raw = {}
        for entry in bat_data.values():
            schedule_raw = entry.get("schedule") or {}
            break
        return self.json(_decode_schedule(schedule_raw))

    async def post(self, request):
        hass: HomeAssistant = request.app["hass"]
        coord = _get_coordinator(hass)
        if not coord:
            return self.json({"error": "No coordinator"}, status_code=503)
        body = await request.json()
        pin = int(body.get("pin", 0))
        pout = int(body.get("pout", 0))

        # Build ScheduleSlot objects
        days_slots: dict[str, list[ScheduleSlot]] = {}
        for day, slots in body.get("days", {}).items():
            day_list = []
            for s in slots:
                slot = ScheduleSlot.from_time(s["start"], s["duration"], s["mode"])
                day_list.append(slot)
            days_slots[day] = day_list

        # Get first battery sn
        bat_data = coord.data.get(BATTERY_IDENTIFIER, {})
        if not bat_data:
            return self.json({"error": "No battery found"}, status_code=503)
        sn = next(iter(bat_data))

        await coord.set_battery_schedule_slots(sn, days_slots)
        await coord.set_battery_schedule_power(pin=pin, pout=pout)
        return self.json({"ok": True})


# ---------------------------------------------------------------------------
# Schedule decode helpers
# ---------------------------------------------------------------------------
def _decode_slot(code: int) -> dict | None:
    if code == 0:
        return None
    discharge_bit = code & 0x1
    duration_bits = (code >> 14) & 0x3
    half_hour_bit = (code >> 17) & 0x1
    hour_bits = code >> 24
    start_h = hour_bits
    start_m = 30 if half_hour_bit else 0
    duration = duration_bits + 1
    end_h = (start_h + duration) % 24
    return {
        "start": f"{start_h:02d}:{start_m:02d}",
        "end":   f"{end_h:02d}:{start_m:02d}",
        "duration": duration,
        "mode": "discharge" if discharge_bit else "charge",
    }


def _decode_schedule(raw: dict) -> dict:
    if not raw:
        return {"Pin": 0, "Pout": 0, "days": {d: [] for d in SCHEDULE_DAYS}}
    # raw may be the already-decoded coordinator structure
    if "raw" in raw:
        raw = raw["raw"]
    result: dict = {"Pin": raw.get("Pin", 0), "Pout": raw.get("Pout", 0), "days": {}}
    for day in SCHEDULE_DAYS:
        slots = []
        for code in raw.get(day, [])[:6]:
            if isinstance(code, int):
                slot = _decode_slot(code)
                if slot:
                    slots.append(slot)
            elif isinstance(code, dict):
                slots.append(code)
        result["days"][day] = slots
    return result


class SolplanetPanelView(HomeAssistantView):
    """Serve the dashboard SPA at /solplanet_panel/index.html."""

    url = "/solplanet_panel/index.html"
    name = "solplanet_panel:index"
    requires_auth = False

    async def get(self, request):
        from aiohttp.web import Response
        html = PANEL_DIR / "index.html"
        return Response(
            body=html.read_bytes(),
            content_type="text/html",
            charset="utf-8",
        )


class SolplanetPanelAssetView(HomeAssistantView):
    """Serve dashboard assets at /solplanet_panel/assets/{filename}."""

    url = "/solplanet_panel/assets/{filename}"
    name = "solplanet_panel:asset"
    requires_auth = False

    async def get(self, request):
        from aiohttp.web import FileResponse, Response

        filename = request.match_info.get("filename", "")
        if "/" in filename or "\\" in filename:
            return Response(status=404)
        asset = PANEL_DIR / "assets" / filename
        if not asset.is_file():
            return Response(status=404)
        return FileResponse(asset)


class SolplanetHistoryView(HomeAssistantView):
    """GET /api/solplanet_panel/history?hours=24 - returns recorded readings."""

    url = "/api/solplanet_panel/history"
    name = "api:solplanet_panel:history"
    requires_auth = False

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    async def get(self, request):
        try:
            hours = float(request.rel_url.query.get("hours", 24))
        except (ValueError, TypeError):
            hours = 24
        since_ts = int(time.time()) - int(hours * 3600)
        rows = await asyncio.get_event_loop().run_in_executor(
            None, _db_query, self._conn, since_ts
        )
        return self.json(rows)


async def _history_recorder(hass: HomeAssistant, conn: sqlite3.Connection) -> None:
    """Background task: record inverter snapshot every HISTORY_INTERVAL seconds."""
    while True:
        try:
            coord = _get_coordinator(hass)
            if coord and coord.data:
                data = coord.data
                inv_data = next(iter(data.get(INVERTER_IDENTIFIER, {}).values()), {})
                bat_data_entry = next(iter(data.get(BATTERY_IDENTIFIER, {}).values()), {})
                met_data_entry = next(iter(data.get(METER_IDENTIFIER, {}).values()), {})
                idata = inv_data.get("data") if inv_data else None
                bdata = bat_data_entry.get("data") if bat_data_entry else None
                mdata = met_data_entry.get("data") if met_data_entry else None
                row = {
                    "ts":    int(time.time()),
                    "ppv":   getattr(idata, "ppv",  None) or getattr(bdata, "ppv", None),
                    "pac":   getattr(idata, "pac",  None),
                    "pbat":  getattr(bdata, "pb",   None),
                    "pgrid": getattr(mdata, "pac",  None),
                    "soc":   getattr(bdata, "soc",  None),
                    "tb":    (getattr(bdata, "tb",  None) or 0) / 10,
                }
                await asyncio.get_event_loop().run_in_executor(None, _db_insert, conn, row)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("History recorder error: %s", exc)
        await asyncio.sleep(HISTORY_INTERVAL)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
async def async_register_panel(hass: HomeAssistant) -> None:
    """Register the Solplanet dashboard as an HA frontend panel and API views."""
    # Initialise history DB
    conn = await asyncio.get_event_loop().run_in_executor(None, _db_init, DB_PATH)

    # Register API views + panel HTML view
    hass.http.register_view(SolplanetDataView)
    hass.http.register_view(SolplanetWorkModeView)
    hass.http.register_view(SolplanetSocLimitsView)
    hass.http.register_view(SolplanetScheduleView)
    hass.http.register_view(SolplanetHistoryView(conn))
    hass.http.register_view(SolplanetPanelView)
    hass.http.register_view(SolplanetPanelAssetView)

    # Start background history recorder
    hass.async_create_background_task(
        _history_recorder(hass, conn),
        "solplanet_history_recorder",
    )

    # Register as a sidebar panel
    from homeassistant.components import frontend
    frontend.async_register_built_in_panel(
        hass,
        component_name="iframe",
        sidebar_title="Solar Dashboard",
        sidebar_icon="mdi:solar-panel",
        frontend_url_path="solplanet-dashboard",
        config={"url": "/solplanet_panel/index.html"},
        require_admin=False,
    )

    _LOGGER.info("Solplanet Dashboard panel registered at /solplanet_panel/index.html")
