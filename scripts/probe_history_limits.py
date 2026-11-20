"""Onderzoek welke historie generate-chart per metric, resolutie en periode levert (alleen lezen).

Draait in de test-HA-container (gebruikt de tokens van de config entry daar) via de Python-client van de integratie:

    docker exec solaredge-ha-dev python /config/scripts/probe_history_limits.py

Schrijft ``/config/probe_results.json``; ``scripts/history_limits_report.py`` maakt daar het overzicht van.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
import json
import re
import sys
import time
from typing import Any

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

GRANULARITIES = ["FIVE_MINUTES", "QUARTER_HOUR", "HOUR", "DAY", "WEEK", "MONTH"]
PERIODS = {"1d": 1, "14d": 14, "1m": 31, "1y": 365, "3y": 1095}
FINE = ["FIVE_MINUTES", "QUARTER_HOUR", "HOUR"]
AGES = [700, 729, 731, 800]  # dagen terug, venster van 1 dag
PARALLEL = 3


def spacing(points: list[datetime]) -> str | None:
    """De meest voorkomende afstand tussen punten, leesbaar."""
    if len(points) < 2:
        return None
    gaps = Counter(int((b - a).total_seconds()) for a, b in zip(points, points[1:], strict=False))
    seconds = gaps.most_common(1)[0][0]
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


async def probe(
    client: SolarEdgeMonitoringClient,
    site_id: int,
    device_type: str,
    serials: list[str],
    category: str,
    uri: str,
    granularity: str,
    start: datetime,
    end: datetime,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """Eén generate-chart; resultaat of fout."""
    datasource = measurement_datasource(site_id, device_type, serials, [(category, uri)], granularity)
    async with semaphore:
        began = time.monotonic()
        try:
            chart = await client.analysis.generate_chart(site_id, start, end, [datasource])
        except ApiError as err:
            return {
                "ok": False,
                "status": err.status,
                "code": err.code,
                "message": (err.detail or str(err))[:400],
                "seconds": round(time.monotonic() - began, 2),
            }
        except Exception as err:  # noqa: BLE001
            return {"ok": False, "status": None, "code": type(err).__name__, "message": str(err)[:400]}
    seconds = round(time.monotonic() - began, 2)
    timestamps: list[datetime] = []
    non_null = 0
    first_value = None
    for index in range(len(chart.data)):
        for point in chart.points(index):
            timestamps.append(point.timestamp)
            if point.values and point.values[0] is not None:
                non_null += 1
                if first_value is None:
                    first_value = point.timestamp.isoformat()
    timestamps = sorted(set(timestamps))
    return {
        "ok": True,
        "points": len(timestamps),
        "non_null": non_null,
        "spacing": spacing(timestamps),
        "first_value": first_value,
        "seconds": seconds,
    }


async def export_tool(session: aiohttp.ClientSession) -> list[dict[str, str]]:
    """Zoek in de JavaScript van de webapp hoe export-report aangeroepen wordt (statische bestanden, geen API)."""
    found: list[dict[str, str]] = []
    async with session.get("https://monitoring.solaredge.com/one") as response:
        html = await response.text()
    scripts = re.findall(r'src="([^"]+\.js)"', html)
    seen: set[str] = set()
    queue = [s if s.startswith("http") else f"https://monitoring.solaredge.com{s if s.startswith('/') else '/one/' + s}" for s in scripts]
    while queue and len(seen) < 80:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            async with session.get(url) as response:
                if response.status != 200:
                    continue
                text = await response.text()
        except aiohttp.ClientError:
            continue
        base = url.rsplit("/", 1)[0]
        for chunk in re.findall(r'"(\./[\w.\-]+\.js|[\w.\-]+-[\w]{6,}\.js)"', text):
            queue.append(f"{base}/{chunk.removeprefix('./')}")
        for keyword in ("export-report", "exportReport", "EXPORT_LIMIT_REACHED", "730"):
            for match in re.finditer(re.escape(keyword), text):
                context = text[max(0, match.start() - 400) : match.end() + 600]
                if keyword == "730" and "day" not in context.lower():
                    continue
                found.append({"file": url.rsplit("/", 1)[-1], "keyword": keyword, "context": context})
                if len([f for f in found if f["keyword"] == keyword]) > 6:
                    break
    return found


async def main() -> None:
    entries = json.load(open("/config/.storage/core.config_entries"))["data"]["entries"]
    data = next(e for e in entries if e["domain"] == "solaredge_monitoring")["data"]
    site_id = data["site_id"]
    semaphore = asyncio.Semaphore(PARALLEL)
    results: dict[str, Any] = {"site_id": "<site>", "generated": datetime.now(UTC).isoformat()}

    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        manager = TokenManager(
            CognitoAuthenticator(session), data["username"], data.get("password"), AuthTokens.from_dict(data.get("tokens"))
        )
        client = SolarEdgeMonitoringClient(session, manager)

        availability = await client.dashboard.get_data_availability(site_id)
        reports = await client.reports.get_available_reports(site_id)
        results["data_availability"] = {
            "production_from": str(availability.production_data_available_from),
            "production_until": str(availability.production_data_available_until),
        }
        results["available_reports"] = [{"type": r.report_type, "range": r.date_range} for r in reports]

        devices = await client.analysis.get_devices(site_id)
        inverters = [d for d in devices if d.device_type and "INVERTER" in d.device_type]
        layout = await client.layout.get_logical_layout(site_id)
        optimizers = [n.identifier for n in layout.nodes("OPTIMIZER") if n.identifier]
        targets = [(d.device_type, [d.identifier]) for d in inverters] + [("OPTIMIZER", optimizers[:1])]
        results["devices"] = {t: len(s) for t, s in targets} | {"OPTIMIZER (totaal)": len(optimizers)}

        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        start_of_data = datetime.combine(availability.production_data_available_from, datetime.min.time(), UTC)
        tasks: list[tuple[dict[str, Any], asyncio.Task[dict[str, Any]]]] = []
        for device_type, serials in targets:
            supported = await client.analysis.get_device_type_metrics(site_id, device_type)
            for category in supported.supported_measurements:
                for metric in category.supported_metrics:
                    key = {
                        "device_type": device_type,
                        "metric": f"{category.uri}.{metric.uri}",
                        "kind": metric.kind,
                        "unit": metric.units,
                    }
                    periods = dict(PERIODS) | {"max": (now - start_of_data).days}
                    for granularity in GRANULARITIES:
                        for label, days in periods.items():
                            info = key | {"test": "period", "granularity": granularity, "period": label}
                            coroutine = probe(
                                client, site_id, device_type, serials, category.uri, metric.uri, granularity,
                                now - timedelta(days=days), now, semaphore,
                            )
                            tasks.append((info, asyncio.create_task(coroutine)))
                    for granularity in FINE:
                        for age in AGES + [(now - start_of_data).days - 2]:
                            info = key | {"test": "age", "granularity": granularity, "age_days": age}
                            begin = now - timedelta(days=age)
                            coroutine = probe(
                                client, site_id, device_type, serials, category.uri, metric.uri, granularity,
                                begin, begin + timedelta(days=1), semaphore,
                            )
                            tasks.append((info, asyncio.create_task(coroutine)))

        # Meer apparaten tegelijk: telt de grens per request of per apparaat?
        for granularity, days in (("FIVE_MINUTES", 7), ("FIVE_MINUTES", 14), ("FIVE_MINUTES", 15), ("HOUR", 31), ("HOUR", 62)):
            for count in (1, len(optimizers)):
                info = {"test": "devices", "device_type": "OPTIMIZER", "metric": "measurements.output_power",
                        "granularity": granularity, "period": f"{days}d", "devices": count}
                coroutine = probe(
                    client, site_id, "OPTIMIZER", optimizers[:count], "measurements", "output_power", granularity,
                    now - timedelta(days=days), now, semaphore,
                )
                tasks.append((info, asyncio.create_task(coroutine)))

        # Grootste periode per resolutie zonder fout (binair zoeken, één metric)
        async def largest(granularity: str) -> dict[str, Any]:
            low, high = 1, 4000
            best = 0
            while low <= high:
                middle = (low + high) // 2
                result = await probe(
                    client, site_id, inverters[0].device_type, [inverters[0].identifier], "measurements", "ac_voltage",
                    granularity, now - timedelta(days=middle), now, semaphore,
                )
                if result["ok"]:
                    best, low = middle, middle + 1
                else:
                    high = middle - 1
            return {"test": "max_period", "granularity": granularity, "max_days": best}

        print(f"{len(tasks)} requests…", flush=True)
        results["probes"] = []
        for info, task in tasks:
            results["probes"].append(info | await task)
        results["max_period"] = [await largest(g) for g in ("FIVE_MINUTES", "QUARTER_HOUR", "HOUR")]
        results["export_tool"] = await export_tool(session)

    with open("/config/probe_results.json", "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=1, default=str)
    print("klaar:", len(results["probes"]), "probes")


asyncio.run(main())
