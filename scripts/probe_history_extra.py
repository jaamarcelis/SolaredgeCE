"""Aanvullende metingen: stille terugval naar dagresolutie, dag-leeftijdsgrenzen, en de exporttool in de webapp-JS."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
import json
import re
import sys

import aiohttp

sys.path.insert(0, "/config")
from custom_components.solaredge_monitoring.api import (  # noqa: E402
    ApiError,
    AuthTokens,
    CognitoAuthenticator,
    SolarEdgeMonitoringClient,
    TokenManager,
    measurement_datasource,
)

INV = "ONE_PHASE_INVERTER"


def spacing(chart) -> str:
    stamps = sorted({p.timestamp for i in range(len(chart.data)) for p in chart.points(i)})
    if len(stamps) < 2:
        return f"{len(stamps)} punt"
    gap = Counter(int((b - a).total_seconds()) for a, b in zip(stamps, stamps[1:], strict=False)).most_common(1)[0][0]
    return f"{gap // 60}min" if gap < 86400 else f"{gap // 86400}d"


async def main() -> None:
    entries = json.load(open("/config/.storage/core.config_entries"))["data"]["entries"]
    data = next(e for e in entries if e["domain"] == "solaredge_monitoring")["data"]
    site = data["site_id"]
    out: dict = {"downsample": [], "day_age": [], "export_tool": []}
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        client = SolarEdgeMonitoringClient(
            session,
            TokenManager(CognitoAuthenticator(session), data["username"], data.get("password"), AuthTokens.from_dict(data.get("tokens"))),
        )
        devices = await client.analysis.get_devices(site)
        inverter = next(d.identifier for d in devices if d.device_type == INV)
        layout = await client.layout.get_logical_layout(site)
        optimizer = next(n.identifier for n in layout.nodes("OPTIMIZER") if n.identifier)
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)

        async def run(device_type, serial, category, uri, gran, start, end):
            ds = [measurement_datasource(site, device_type, [serial], [(category, uri)], gran)]
            try:
                chart = await client.analysis.generate_chart(site, start, end, ds)
                values = [p for i in range(len(chart.data)) for p in chart.points(i) if p.values and p.values[0] is not None]
                return f"ok {spacing(chart)} ({len(values)} waarden)"
            except ApiError as err:
                return f"{err.code}: {(err.detail or str(err))[:150]}"

        # 1. Vanaf hoeveel dagen valt fijne resolutie stil terug naar dagen?
        for uri in ("lifetime_positive_ac_energy", "temperature", "positive_ac_energy_since_last_telemetry"):
            for gran in ("FIVE_MINUTES", "QUARTER_HOUR", "HOUR"):
                for days in (31, 32, 33, 35, 40, 45, 60, 90):
                    out["downsample"].append([uri, gran, days, await run(INV, inverter, "measurements", uri, gran, now - timedelta(days=days), now)])
        # 2. Dagresolutie op oudere data (1 dag en 32 dagen venster)
        for device_type, serial, uri in (
            (INV, inverter, "ac_voltage"), (INV, inverter, "power_factor"), (INV, inverter, "lifetime_positive_ac_energy"),
            (INV, inverter, "temperature"), ("OPTIMIZER", optimizer, "output_power"), ("OPTIMIZER", optimizer, "panel_voltage"),
        ):
            for age in (60, 89, 91, 200, 364, 366, 731, 1500, 2350):
                for window in (1, 7, 32):
                    begin = now - timedelta(days=age)
                    out["day_age"].append([device_type, uri, age, window, await run(device_type, serial, "measurements", uri, "DAY", begin, begin + timedelta(days=window))])
            for age in (300, 364, 366):
                begin = now - timedelta(days=age)
                out["day_age"].append([device_type, uri, age, "5min/1d", await run(device_type, serial, "measurements", uri, "FIVE_MINUTES", begin, begin + timedelta(days=1))])

        # 3. De exporttool in de JavaScript van de webapp
        async with session.get("https://monitoring.solaredge.com/one", headers={"User-Agent": "Mozilla/5.0"}) as response:
            html = await response.text()
        out["html_scripts"] = re.findall(r"<script[^>]*>", html)[:20]
        urls = {u if u.startswith("http") else "https://monitoring.solaredge.com" + (u if u.startswith("/") else "/one/" + u)
                for u in re.findall(r'(?:src|href)="([^"]+\.js)"', html)}
        seen: set[str] = set()
        queue = sorted(urls)
        while queue and len(seen) < 150:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as response:
                    text = await response.text() if response.status == 200 else ""
            except aiohttp.ClientError:
                continue
            base = url.rsplit("/", 1)[0]
            for chunk in set(re.findall(r'["\'](\.{0,2}/?[\w./\-]*?[\w\-]+\.[0-9a-f]{6,}\.js|\.{0,2}/?[\w./\-]*?[\w\-]+-[\w]{8}\.js)["\']', text)):
                full = chunk if chunk.startswith("http") else (base + "/" + chunk.removeprefix("./")) if not chunk.startswith("/") else "https://monitoring.solaredge.com" + chunk
                if full not in seen:
                    queue.append(full)
            for kw in ("export-report", "exportReport", "export_report", "DATASET_TOO_LARGE"):
                for m in list(re.finditer(re.escape(kw), text))[:4]:
                    out["export_tool"].append({"file": url.rsplit("/", 1)[-1], "kw": kw, "ctx": text[max(0, m.start() - 600): m.end() + 900]})
        out["js_files_scanned"] = len(seen)
    json.dump(out, open("/config/probe_extra.json", "w"), indent=1)
    print("klaar", len(out["downsample"]), len(out["day_age"]), len(out["export_tool"]), out["js_files_scanned"])


asyncio.run(main())
