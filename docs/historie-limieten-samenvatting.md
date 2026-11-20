# Historie via generate-chart: samenvatting

Gemeten op 2026-11-20 op één installatie: één eenfase-omvormer (`ONE_PHASE_INVERTER`) en 12 optimizers, met productiedata vanaf 2020-04-08. Daarvoor zijn ruim 1.400 leesrequests gedaan via de Python-client van de integratie:
- `scripts/probe_history_limits.py`: elke metric × 6 resoluties × 6 perioden, plus leeftijdsgrenzen;
- `scripts/probe_history_extra.py`: stille terugval naar dagwaarden en leeftijdsgrenzen bij dagresolutie.

De ruwe uitkomsten staan in `docs/data/`; de volledige tabel per metric staat hieronder.

## Limieten per soort metric

| Apparaat · metrics | Fijn (5 min / 15 min / uur) | Dag | Maand | Week |
|---|---|---|---|---|
| **Omvormer** · energie en tellers (`lifetime_positive_ac_energy`, `*_energy_since_last_telemetry`), `temperature`, `ground_fault_resistance`, `status` | ≤ **31 dagen** per request; daarboven **stilletjes dagwaarden**. Alleen de laatste **730 dagen**. | geen grens; terug tot de eerste productiedag (6,6 jaar in één request) | ✓ (maandwaarden) | ✗ `400 Bad Request` |
| **Omvormer** · momentwaarden (`power`, `ac_voltage`, `ac_current`, `ac_frequency`, `dc_voltage`, `active_power`, `apparent_power`, `negative_active_power`, `ac_current_dc_component`) | ≤ 31 dagen; alleen de laatste 730 dagen | ≤ **32 dagen** per request (`DATASET_TOO_LARGE`), geen leeftijdsgrens | ✗ niet per maand | ✗ |
| **Omvormer** · `power_factor`, `reactive_power` | ≤ **7 dagen** per request; ouder dan ~1 jaar leeg | ≤ 7 dagen per request | ✗ | ✗ |
| **Optimizer** · `output_power`, `panel_voltage`, `output_voltage`, `panel_current`, `energy_since_last_telem` | ≤ **14 dagen** per request | ✓ | (alleen energie) | ✗ |
| … leeftijd (optimizer) | Grafiekweergave: alleen de laatste **90 dagen**. 91–365 dagen: `DATASET_TOO_LARGE` ("use the export tool"). Ouder dan 365 dagen: `EXPORT_LIMIT_REACHED`. | idem | idem | |

### Verdere bijzonderheden
- **Dagwaarde van een teller is een daggemiddelde.** Gebruik daarom per dag de delta-metric: die is gelijk aan de toename van de teller over de dag.
- **5-minutendelta's tellen ~3% minder op dan de teller zelf.** Voor tellers gebruikt de import daarom de stand.
- **`status` (tekst)** geeft bij dagresolutie over 3 jaar een `500 Internal Server Error`; de integratie importeert tekstsensoren niet.
- **`WEEK`** wordt niet ondersteund: altijd `400 Bad Request`.
- **Meer apparaten in één request** (12 optimizers) telt niet zwaarder: de grens geldt per request, niet per apparaat.

## Foutmeldingen

| Code | Melding (sjabloon) | Betekenis |
|---|---|---|
| `EXPORT_LIMIT_REACHED` | *… data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution.* | leeftijdsgrens voor fijne resolutie |
| `EXPORT_LIMIT_REACHED` | *… data is only available for the last 365 days.* | leeftijdsgrens (optimizers) |
| `EXPORT_LIMIT_REACHED` | *… data at hourly or finer resolution is limited to 14 days. Please select a shorter date range or switch to daily resolution.* | periode per request (optimizers) |
| `EXPORT_LIMIT_REACHED` | *… data cannot be viewed at monthly or yearly resolution. Please select a daily or finer resolution.* | geen maand/jaar voor momentwaarden |
| `DATASET_TOO_LARGE` | *… charts are limited to 32 days* (of *7 days*). *For larger ranges, please use the export tool.* | periode per request |
| `DATASET_TOO_LARGE` | *… data older than 90 days is not available in the chart view. For older data, please use the export tool.* | leeftijdsgrens van de grafiekweergave (optimizers) |
| `400 Bad Request` | Spring-fout zonder omschrijving | resolutie `WEEK` |
| `500 Internal Server Error` | – | `status` per dag/maand over 3 jaar |
| *(geen fout)* | fijne resolutie over > 31 dagen geeft **dagwaarden** terug | stille terugval |

## Hoe de import hiermee omgaat

- **Vastgelegde limieten.** `history_limits.py` bevat de limieten hierboven, per apparaattype en metric. Nieuwe of andere limieten (bijvoorbeeld van een driefase-omvormer) worden uit de foutmelding gelezen: *limited to N days*, *only available for the last N days*, *older than N days*. Ze worden per installatie bewaard in `.storage/solaredge_monitoring.history_limits.<entry>`, en diagnostics toont ze.
- **Blokken binnen de limieten.** Elk request is zo groot als toegestaan: 31 dagen fijn, 32 of 366 dagen per dag, 14 dagen voor optimizers. Een limietfout leidt tot een kleiner blok. Stille dagwaarden bij een fijne aanvraag ook: het blok wordt gehalveerd en die grens onthouden.
- **Leeftijd.** Wat ouder is dan de grens wordt niet gevraagd, maar overgeslagen met een melding in het resultaat.
- **Interval *Maximaal*.** Eerst alles wat per 5 minuten kan (uurrijen, plus 5-minutenrijen binnen `keep_days`). Daarvóór per dag tot de eerste productiedag uit `dashboard/data-availability`. Energie per dag komt uit de delta-metric. Momentwaarden per dag krijgen alleen een gemiddelde.

Resultaat op de testinstallatie: ruim 6,5 jaar historie.

| Sensor | Requests |
|---|---|
| Omvormer-lifetime | 5 per dag + 24 per 5 minuten |
| Temperatuur | 5 per dag + 24 per 5 minuten |
| AC-spanning | 53 per dag (32-dagenblokken) + 24 per 5 minuten |
| Optimizervermogen | 7 per 5 minuten (14-dagenblokken), alleen de laatste 90 dagen |

Er waren geen foutmeldingen nodig om dit te bereiken.

## De exporttool

De webapp biedt bij `DATASET_TOO_LARGE` een export aan (`bootstrapOnmAnalysis`, dialoog *datasetTooLarge*):

```
POST /services/cni/ui-api/pages/site/analysis/execute/site/{siteId}/export-report
{ "datasources": [...], "reportPeriodScale": "DAY|WEEK|MONTH|YEAR" (of DAY met alignmentGranularity QUARTER_HOUR),
  "customPeriod": {"from": "…", "to": "…"}, "chartName": "Custom Chart Export", "userEmail": "…" }
```

SolarEdge **mailt dan een CSV** naar `userEmail`; de response bevat geen data. Voor een automatische import in Home Assistant is dit dus niet bruikbaar. Het zou alleen werken als extra stap: zelf exporteren en het CSV-bestand daarna importeren. Dit is niet aangeroepen, omdat het een e-mail verstuurt.

Waar de exporttool meer oplevert dan generate-chart:
- **optimizers:** 91–365 dagen terug;
- **omvormers:** 15-minutendata ouder dan 730 dagen, als de export dat toestaat. Dat is niet gemeten.
