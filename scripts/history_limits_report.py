"""Maak docs/historie-limieten.md van de uitkomst van probe_history_limits.py.

    python scripts/history_limits_report.py ha-dev/config/probe_results.json docs/historie-limieten.md
"""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re
import sys
from typing import Any

GRANULARITIES = ["FIVE_MINUTES", "QUARTER_HOUR", "HOUR", "DAY", "WEEK", "MONTH"]
PERIODS = ["1d", "14d", "1m", "1y", "3y", "max"]
SHORT = {"FIVE_MINUTES": "5 min", "QUARTER_HOUR": "15 min", "HOUR": "uur", "DAY": "dag", "WEEK": "week", "MONTH": "maand"}


def cell(probe: dict[str, Any] | None) -> str:
    """✓ met de werkelijke resolutie, of de foutcode."""
    if probe is None:
        return "–"
    if probe["ok"]:
        if not probe["non_null"]:
            return f"✓ leeg ({probe['spacing'] or '–'})"
        return f"✓ {probe['spacing'] or '1 punt'}"
    return f"✗ {probe.get('code') or probe.get('status')}"


def main(source: str, target: str) -> None:
    results = json.load(open(source, encoding="utf-8"))
    probes = results["probes"]
    period = defaultdict(dict)
    age = defaultdict(dict)
    metrics: dict[tuple[str, str], dict[str, Any]] = {}
    errors: dict[tuple[str, str], set[str]] = defaultdict(set)
    for p in probes:
        if p["test"] in ("period", "age"):
            key = (p["device_type"], p["metric"])
            metrics.setdefault(key, {"kind": p.get("kind"), "unit": p.get("unit")})
            if p["test"] == "period":
                period[key][(p["granularity"], p["period"])] = p
            else:
                age[key][(p["granularity"], p["age_days"])] = p
        if not p["ok"]:
            message = re.sub(r"site/\d+", "site/<site>", re.sub(r'"timestamp":"[^"]+",?', "", p["message"]))
            errors[(p.get("code") or str(p.get("status")), message)].add(f"{p.get('granularity')}/{p.get('period', p.get('age_days'))}")

    intro = open(Path(target).with_name("historie-limieten-samenvatting.md"), encoding="utf-8").read()
    lines = [
        intro.rstrip(),
        "",
        "# Bijlage: meting per metric, resolutie en periode",
        "",
        f"Gemeten op {results['generated'][:10]} met `scripts/probe_history_limits.py` (Python-client van de integratie), "
        "voor één installatie (1 eenfase-omvormer, optimizers). Elke cel is één request van *periode* tot nu.",
        "",
        f"- Productiedata beschikbaar vanaf: **{results['data_availability']['production_from']}** "
        "(`dashboard/data-availability`); *max* in de tabellen is de periode vanaf die datum.",
        f"- Beschikbare rapporten: {', '.join(r['type'] for r in results['available_reports'])}",
        "",
        "Legenda: `✓ 5min` = gelukt, met de werkelijk teruggegeven afstand tussen punten; `✓ leeg` = gelukt maar zonder "
        "waarden; `✗ CODE` = foutmelding (zie *Foutmeldingen*).",
        "",
    ]
    for (device_type, metric), meta in sorted(metrics.items()):
        lines += [f"### {device_type} · `{metric}` ({meta['kind']}, {meta['unit'] or '–'})", ""]
        lines.append("| Resolutie | " + " | ".join(PERIODS) + " |")
        lines.append("|---|" + "---|" * len(PERIODS))
        for g in GRANULARITIES:
            lines.append(f"| {SHORT[g]} | " + " | ".join(cell(period[(device_type, metric)].get((g, p))) for p in PERIODS) + " |")
        ages = sorted({a for (_, a) in age[(device_type, metric)]})
        if ages:
            lines += ["", "Venster van 1 dag, *n* dagen geleden:", ""]
            lines.append("| Resolutie | " + " | ".join(f"{a} d" for a in ages) + " |")
            lines.append("|---|" + "---|" * len(ages))
            for g in ("FIVE_MINUTES", "QUARTER_HOUR", "HOUR"):
                lines.append(f"| {SHORT[g]} | " + " | ".join(cell(age[(device_type, metric)].get((g, a))) for a in ages) + " |")
        lines.append("")

    lines += ["## Grootste periode per request", "", "| Resolutie | Max. dagen (één metric, één apparaat) |", "|---|---|"]
    lines += [f"| {SHORT[m['granularity']]} | {m['max_days']} |" for m in results.get("max_period", [])]
    lines += ["", "## Meer apparaten in één request", "", "| Resolutie | Periode | Apparaten | Resultaat |", "|---|---|---|---|"]
    lines += [
        f"| {SHORT[p['granularity']]} | {p['period']} | {p['devices']} | {cell(p)} |" for p in probes if p["test"] == "devices"
    ]
    lines += ["", "## Foutmeldingen", "", "| Code | Melding | Voorkomen (resolutie/periode) |", "|---|---|---|"]
    for (code, message), where in sorted(errors.items()):
        sample = ", ".join(sorted(where)[:6]) + (" …" if len(where) > 6 else "")
        lines.append(f"| {code} | {message.replace('|', '/')} | {sample} |")
    lines.append("")
    open(target, "w", encoding="utf-8", newline="\n").write("\n".join(lines))
    print("geschreven:", target)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
