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

# Bijlage: meting per metric, resolutie en periode

Gemeten op 2026-11-20 met `scripts/probe_history_limits.py` (Python-client van de integratie), voor één installatie (1 eenfase-omvormer, optimizers). Elke cel is één request van *periode* tot nu.

- Productiedata beschikbaar vanaf: **2020-04-08** (`dashboard/data-availability`); *max* in de tabellen is de periode vanaf die datum.
- Beschikbare rapporten: PERIODIC_AC_ENERGY, SITE_STATUS, INVERTER_SETTINGS, SITE_COMMISSIONING, MODULE_MISMATCH

Legenda: `✓ 5min` = gelukt, met de werkelijk teruggegeven afstand tussen punten; `✓ leeg` = gelukt maar zonder waarden; `✗ CODE` = foutmelding (zie *Foutmeldingen*).

### ONE_PHASE_INVERTER · `measurements.ac_current` (GAUGE, A)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✓ 5min | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✓ 15min | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✓ 1h | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### ONE_PHASE_INVERTER · `measurements.ac_current_dc_component` (GAUGE, A)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✓ 5min | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✓ 15min | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✓ 1h | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### ONE_PHASE_INVERTER · `measurements.ac_frequency` (GAUGE, Hz)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✓ 5min | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✓ 15min | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✓ 1h | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### ONE_PHASE_INVERTER · `measurements.ac_voltage` (GAUGE, V)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✓ 5min | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✓ 15min | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✓ 1h | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### ONE_PHASE_INVERTER · `measurements.active_power` (GAUGE, W)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✓ 5min | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✓ 15min | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✓ 1h | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### ONE_PHASE_INVERTER · `measurements.apparent_power` (GAUGE, VA)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✓ 5min | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✓ 15min | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✓ 1h | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### ONE_PHASE_INVERTER · `measurements.dc_voltage` (GAUGE, V)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✓ 5min | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✓ 15min | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✓ 1h | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### ONE_PHASE_INVERTER · `measurements.ground_fault_resistance` (GAUGE, kΩ)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✓ 5min | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✓ 15min | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✓ 1h | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✓ 1 punt | ✓ 1 punt | ✓ 745h | ✓ 31d | ✓ 31d | ✓ 31d |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### ONE_PHASE_INVERTER · `measurements.lifetime_positive_ac_energy` (CUMULATIVE, Wh)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✓ 5min | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✓ 15min | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✓ 1h | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✓ 1 punt | ✓ 1 punt | ✓ 745h | ✓ 31d | ✓ 31d | ✓ 31d |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### ONE_PHASE_INVERTER · `measurements.negative_ac_energy_since_last_telemetry` (DELTA, Wh)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✓ 5min | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✓ 15min | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✓ 1h | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✓ 1 punt | ✓ 1 punt | ✓ 745h | ✓ 31d | ✓ 31d | ✓ 31d |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### ONE_PHASE_INVERTER · `measurements.negative_active_power` (GAUGE, W)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✓ 5min | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✓ 15min | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✓ 1h | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### ONE_PHASE_INVERTER · `measurements.positive_ac_energy_since_last_telemetry` (DELTA, Wh)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✓ 5min | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✓ 15min | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✓ 1h | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✓ 1 punt | ✓ 1 punt | ✓ 745h | ✓ 31d | ✓ 31d | ✓ 31d |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### ONE_PHASE_INVERTER · `measurements.power_factor` (GAUGE, %)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✓ leeg (5min) | ✓ leeg (5min) | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ leeg (15min) | ✓ leeg (15min) | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ leeg (1h) | ✓ leeg (1h) | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### ONE_PHASE_INVERTER · `measurements.reactive_power` (GAUGE, VAr)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✓ leeg (5min) | ✓ leeg (5min) | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ leeg (15min) | ✓ leeg (15min) | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ leeg (1h) | ✓ leeg (1h) | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### ONE_PHASE_INVERTER · `measurements.status` (GAUGE, –)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✓ 5min | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✓ 15min | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✓ 1h | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d | ✗ Internal Server Error | ✗ Internal Server Error |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✓ 1 punt | ✓ 1 punt | ✓ 745h | ✓ 31d | ✗ Internal Server Error | ✗ Internal Server Error |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### ONE_PHASE_INVERTER · `measurements.temperature` (GAUGE, C)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✓ 5min | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✓ 15min | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✓ 1h | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✓ 1 punt | ✓ 1 punt | ✓ 745h | ✓ 31d | ✓ 31d | ✓ 31d |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### ONE_PHASE_INVERTER · `pv_measurements.energy_since_last_telemetry` (DELTA, Wh)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✓ 5min | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✓ 15min | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✓ 1h | ✓ 1d | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d | ✓ 1d |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✓ 1 punt | ✓ 1 punt | ✓ 745h | ✓ 31d | ✓ 31d | ✓ 31d |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### ONE_PHASE_INVERTER · `pv_measurements.power` (GAUGE, W)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✓ 5min | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✓ 15min | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✓ 1h | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE | ✗ DATASET_TOO_LARGE |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### OPTIMIZER · `measurements.energy_since_last_telem` (DELTA, Wh)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✓ 1 punt | ✓ 1 punt | ✓ 745h | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### OPTIMIZER · `measurements.output_power` (DELTA, W)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### OPTIMIZER · `measurements.output_voltage` (GAUGE, V)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### OPTIMIZER · `measurements.panel_current` (GAUGE, A)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

### OPTIMIZER · `measurements.panel_voltage` (GAUGE, V)

| Resolutie | 1d | 14d | 1m | 1y | 3y | max |
|---|---|---|---|---|---|---|
| 5 min | ✓ 5min | ✓ 5min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✓ 15min | ✓ 15min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✓ 1h | ✓ 1h | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| dag | ✓ 1d | ✓ 1d | ✓ 1d | ✗ DATASET_TOO_LARGE | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| week | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request | ✗ Bad Request |
| maand | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

Venster van 1 dag, *n* dagen geleden:

| Resolutie | 700 d | 729 d | 731 d | 800 d | 2415 d |
|---|---|---|---|---|---|
| 5 min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| 15 min | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |
| uur | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED | ✗ EXPORT_LIMIT_REACHED |

## Grootste periode per request

| Resolutie | Max. dagen (één metric, één apparaat) |
|---|---|
| 5 min | 32 |
| 15 min | 32 |
| uur | 32 |

## Meer apparaten in één request

| Resolutie | Periode | Apparaten | Resultaat |
|---|---|---|---|
| 5 min | 7d | 1 | ✓ 5min |
| 5 min | 7d | 12 | ✓ 5min |
| 5 min | 14d | 1 | ✓ 5min |
| 5 min | 14d | 12 | ✓ 5min |
| 5 min | 15d | 1 | ✗ EXPORT_LIMIT_REACHED |
| 5 min | 15d | 12 | ✗ EXPORT_LIMIT_REACHED |
| uur | 31d | 1 | ✗ EXPORT_LIMIT_REACHED |
| uur | 31d | 12 | ✗ EXPORT_LIMIT_REACHED |
| uur | 62d | 1 | ✗ EXPORT_LIMIT_REACHED |
| uur | 62d | 12 | ✗ EXPORT_LIMIT_REACHED |

## Foutmeldingen

| Code | Melding | Voorkomen (resolutie/periode) |
|---|---|---|
| Bad Request | POST cni/ui-api/pages/site/analysis/custom/site/<site>/generate-chart mislukt: 400 Bad Request {"status":400,"error":"Bad Request","path":"/ui-api/pages/site/analysis/custom/site/<site>/generate-chart"} | WEEK/14d, WEEK/1d, WEEK/1m, WEEK/1y, WEEK/3y, WEEK/max |
| DATASET_TOO_LARGE | One phase inverter ac current charts are limited to 32 days. For larger ranges, please use the export tool. | DAY/1y, DAY/3y, DAY/max, FIVE_MINUTES/1y, HOUR/1y, QUARTER_HOUR/1y |
| DATASET_TOO_LARGE | One phase inverter ac current dc component charts are limited to 32 days. For larger ranges, please use the export tool. | DAY/1y, DAY/3y, DAY/max, FIVE_MINUTES/1y, HOUR/1y, QUARTER_HOUR/1y |
| DATASET_TOO_LARGE | One phase inverter ac frequency charts are limited to 32 days. For larger ranges, please use the export tool. | DAY/1y, DAY/3y, DAY/max, FIVE_MINUTES/1y, HOUR/1y, QUARTER_HOUR/1y |
| DATASET_TOO_LARGE | One phase inverter ac voltage charts are limited to 32 days. For larger ranges, please use the export tool. | DAY/1y, DAY/3y, DAY/max, FIVE_MINUTES/1y, HOUR/1y, QUARTER_HOUR/1y |
| DATASET_TOO_LARGE | One phase inverter active power charts are limited to 32 days. For larger ranges, please use the export tool. | DAY/1y, DAY/3y, DAY/max, FIVE_MINUTES/1y, HOUR/1y, QUARTER_HOUR/1y |
| DATASET_TOO_LARGE | One phase inverter apparent power charts are limited to 32 days. For larger ranges, please use the export tool. | DAY/1y, DAY/3y, DAY/max, FIVE_MINUTES/1y, HOUR/1y, QUARTER_HOUR/1y |
| DATASET_TOO_LARGE | One phase inverter dc voltage charts are limited to 32 days. For larger ranges, please use the export tool. | DAY/1y, DAY/3y, DAY/max, FIVE_MINUTES/1y, HOUR/1y, QUARTER_HOUR/1y |
| DATASET_TOO_LARGE | One phase inverter negative active power charts are limited to 32 days. For larger ranges, please use the export tool. | DAY/1y, DAY/3y, DAY/max, FIVE_MINUTES/1y, HOUR/1y, QUARTER_HOUR/1y |
| DATASET_TOO_LARGE | One phase inverter power charts are limited to 32 days. For larger ranges, please use the export tool. | DAY/1y, DAY/3y, DAY/max, FIVE_MINUTES/1y, HOUR/1y, QUARTER_HOUR/1y |
| DATASET_TOO_LARGE | One phase inverter power factor charts are limited to 7 days. For larger ranges, please use the export tool. | DAY/14d, DAY/1m, DAY/1y, DAY/3y, DAY/max, FIVE_MINUTES/14d … |
| DATASET_TOO_LARGE | One phase inverter reactive power charts are limited to 7 days. For larger ranges, please use the export tool. | DAY/14d, DAY/1m, DAY/1y, DAY/3y, DAY/max, FIVE_MINUTES/14d … |
| DATASET_TOO_LARGE | Optimizer energy since last telem data older than 90 days is not available in the chart view. For older data, please use the export tool. | DAY/1y, MONTH/1y |
| DATASET_TOO_LARGE | Optimizer output power data older than 90 days is not available in the chart view. For older data, please use the export tool. | DAY/1y |
| DATASET_TOO_LARGE | Optimizer output voltage data older than 90 days is not available in the chart view. For older data, please use the export tool. | DAY/1y |
| DATASET_TOO_LARGE | Optimizer panel current data older than 90 days is not available in the chart view. For older data, please use the export tool. | DAY/1y |
| DATASET_TOO_LARGE | Optimizer panel voltage data older than 90 days is not available in the chart view. For older data, please use the export tool. | DAY/1y |
| EXPORT_LIMIT_REACHED | One phase inverter ac current data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution. | FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/731, FIVE_MINUTES/800, FIVE_MINUTES/max, HOUR/2415 … |
| EXPORT_LIMIT_REACHED | One phase inverter ac current data cannot be viewed at monthly or yearly resolution. Please select a daily or finer resolution. | MONTH/14d, MONTH/1d, MONTH/1m, MONTH/1y, MONTH/3y, MONTH/max |
| EXPORT_LIMIT_REACHED | One phase inverter ac current dc component data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution. | FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/731, FIVE_MINUTES/800, FIVE_MINUTES/max, HOUR/2415 … |
| EXPORT_LIMIT_REACHED | One phase inverter ac current dc component data cannot be viewed at monthly or yearly resolution. Please select a daily or finer resolution. | MONTH/14d, MONTH/1d, MONTH/1m, MONTH/1y, MONTH/3y, MONTH/max |
| EXPORT_LIMIT_REACHED | One phase inverter ac frequency data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution. | FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/731, FIVE_MINUTES/800, FIVE_MINUTES/max, HOUR/2415 … |
| EXPORT_LIMIT_REACHED | One phase inverter ac frequency data cannot be viewed at monthly or yearly resolution. Please select a daily or finer resolution. | MONTH/14d, MONTH/1d, MONTH/1m, MONTH/1y, MONTH/3y, MONTH/max |
| EXPORT_LIMIT_REACHED | One phase inverter ac voltage data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution. | FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/731, FIVE_MINUTES/800, FIVE_MINUTES/max, HOUR/2415 … |
| EXPORT_LIMIT_REACHED | One phase inverter ac voltage data cannot be viewed at monthly or yearly resolution. Please select a daily or finer resolution. | MONTH/14d, MONTH/1d, MONTH/1m, MONTH/1y, MONTH/3y, MONTH/max |
| EXPORT_LIMIT_REACHED | One phase inverter active power data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution. | FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/731, FIVE_MINUTES/800, FIVE_MINUTES/max, HOUR/2415 … |
| EXPORT_LIMIT_REACHED | One phase inverter active power data cannot be viewed at monthly or yearly resolution. Please select a daily or finer resolution. | MONTH/14d, MONTH/1d, MONTH/1m, MONTH/1y, MONTH/3y, MONTH/max |
| EXPORT_LIMIT_REACHED | One phase inverter apparent power data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution. | FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/731, FIVE_MINUTES/800, FIVE_MINUTES/max, HOUR/2415 … |
| EXPORT_LIMIT_REACHED | One phase inverter apparent power data cannot be viewed at monthly or yearly resolution. Please select a daily or finer resolution. | MONTH/14d, MONTH/1d, MONTH/1m, MONTH/1y, MONTH/3y, MONTH/max |
| EXPORT_LIMIT_REACHED | One phase inverter dc voltage data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution. | FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/731, FIVE_MINUTES/800, FIVE_MINUTES/max, HOUR/2415 … |
| EXPORT_LIMIT_REACHED | One phase inverter dc voltage data cannot be viewed at monthly or yearly resolution. Please select a daily or finer resolution. | MONTH/14d, MONTH/1d, MONTH/1m, MONTH/1y, MONTH/3y, MONTH/max |
| EXPORT_LIMIT_REACHED | One phase inverter energy since last telemetry data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution. | FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/731, FIVE_MINUTES/800, FIVE_MINUTES/max, HOUR/2415 … |
| EXPORT_LIMIT_REACHED | One phase inverter ground fault resistance data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution. | FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/731, FIVE_MINUTES/800, FIVE_MINUTES/max, HOUR/2415 … |
| EXPORT_LIMIT_REACHED | One phase inverter lifetime positive ac energy data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution. | FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/731, FIVE_MINUTES/800, FIVE_MINUTES/max, HOUR/2415 … |
| EXPORT_LIMIT_REACHED | One phase inverter negative ac energy since last telemetry data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution. | FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/731, FIVE_MINUTES/800, FIVE_MINUTES/max, HOUR/2415 … |
| EXPORT_LIMIT_REACHED | One phase inverter negative active power data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution. | FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/731, FIVE_MINUTES/800, FIVE_MINUTES/max, HOUR/2415 … |
| EXPORT_LIMIT_REACHED | One phase inverter negative active power data cannot be viewed at monthly or yearly resolution. Please select a daily or finer resolution. | MONTH/14d, MONTH/1d, MONTH/1m, MONTH/1y, MONTH/3y, MONTH/max |
| EXPORT_LIMIT_REACHED | One phase inverter positive ac energy since last telemetry data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution. | FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/731, FIVE_MINUTES/800, FIVE_MINUTES/max, HOUR/2415 … |
| EXPORT_LIMIT_REACHED | One phase inverter power data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution. | FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/731, FIVE_MINUTES/800, FIVE_MINUTES/max, HOUR/2415 … |
| EXPORT_LIMIT_REACHED | One phase inverter power data cannot be viewed at monthly or yearly resolution. Please select a daily or finer resolution. | MONTH/14d, MONTH/1d, MONTH/1m, MONTH/1y, MONTH/3y, MONTH/max |
| EXPORT_LIMIT_REACHED | One phase inverter power factor data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution. | FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/731, FIVE_MINUTES/800, FIVE_MINUTES/max, HOUR/2415 … |
| EXPORT_LIMIT_REACHED | One phase inverter power factor data cannot be viewed at monthly or yearly resolution. Please select a daily or finer resolution. | MONTH/14d, MONTH/1d, MONTH/1m, MONTH/1y, MONTH/3y, MONTH/max |
| EXPORT_LIMIT_REACHED | One phase inverter reactive power data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution. | FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/731, FIVE_MINUTES/800, FIVE_MINUTES/max, HOUR/2415 … |
| EXPORT_LIMIT_REACHED | One phase inverter reactive power data cannot be viewed at monthly or yearly resolution. Please select a daily or finer resolution. | MONTH/14d, MONTH/1d, MONTH/1m, MONTH/1y, MONTH/3y, MONTH/max |
| EXPORT_LIMIT_REACHED | One phase inverter status data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution. | FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/731, FIVE_MINUTES/800, FIVE_MINUTES/max, HOUR/2415 … |
| EXPORT_LIMIT_REACHED | One phase inverter temperature data at hourly or finer resolution is only available for the last 730 days. Please select a more recent date range or switch to daily resolution. | FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/731, FIVE_MINUTES/800, FIVE_MINUTES/max, HOUR/2415 … |
| EXPORT_LIMIT_REACHED | Optimizer energy since last telem data at hourly or finer resolution is limited to 14 days. Please select a shorter date range or switch to daily resolution. | FIVE_MINUTES/1m, FIVE_MINUTES/1y, HOUR/1m, HOUR/1y, QUARTER_HOUR/1m, QUARTER_HOUR/1y |
| EXPORT_LIMIT_REACHED | Optimizer energy since last telem data is only available for the last 365 days. Please select a more recent date range. | DAY/3y, DAY/max, FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/700, FIVE_MINUTES/729 … |
| EXPORT_LIMIT_REACHED | Optimizer output power data at hourly or finer resolution is limited to 14 days. Please select a shorter date range or switch to daily resolution. | FIVE_MINUTES/15d, FIVE_MINUTES/1m, FIVE_MINUTES/1y, HOUR/1m, HOUR/1y, HOUR/31d … |
| EXPORT_LIMIT_REACHED | Optimizer output power data cannot be viewed at monthly or yearly resolution. Please select a daily or finer resolution. | MONTH/14d, MONTH/1d, MONTH/1m, MONTH/1y |
| EXPORT_LIMIT_REACHED | Optimizer output power data is only available for the last 365 days. Please select a more recent date range. | DAY/3y, DAY/max, FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/700, FIVE_MINUTES/729 … |
| EXPORT_LIMIT_REACHED | Optimizer output voltage data at hourly or finer resolution is limited to 14 days. Please select a shorter date range or switch to daily resolution. | FIVE_MINUTES/1m, FIVE_MINUTES/1y, HOUR/1m, HOUR/1y, QUARTER_HOUR/1m, QUARTER_HOUR/1y |
| EXPORT_LIMIT_REACHED | Optimizer output voltage data cannot be viewed at monthly or yearly resolution. Please select a daily or finer resolution. | MONTH/14d, MONTH/1d, MONTH/1m, MONTH/1y |
| EXPORT_LIMIT_REACHED | Optimizer output voltage data is only available for the last 365 days. Please select a more recent date range. | DAY/3y, DAY/max, FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/700, FIVE_MINUTES/729 … |
| EXPORT_LIMIT_REACHED | Optimizer panel current data at hourly or finer resolution is limited to 14 days. Please select a shorter date range or switch to daily resolution. | FIVE_MINUTES/1m, FIVE_MINUTES/1y, HOUR/1m, HOUR/1y, QUARTER_HOUR/1m, QUARTER_HOUR/1y |
| EXPORT_LIMIT_REACHED | Optimizer panel current data cannot be viewed at monthly or yearly resolution. Please select a daily or finer resolution. | MONTH/14d, MONTH/1d, MONTH/1m, MONTH/1y |
| EXPORT_LIMIT_REACHED | Optimizer panel current data is only available for the last 365 days. Please select a more recent date range. | DAY/3y, DAY/max, FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/700, FIVE_MINUTES/729 … |
| EXPORT_LIMIT_REACHED | Optimizer panel voltage data at hourly or finer resolution is limited to 14 days. Please select a shorter date range or switch to daily resolution. | FIVE_MINUTES/1m, FIVE_MINUTES/1y, HOUR/1m, HOUR/1y, QUARTER_HOUR/1m, QUARTER_HOUR/1y |
| EXPORT_LIMIT_REACHED | Optimizer panel voltage data cannot be viewed at monthly or yearly resolution. Please select a daily or finer resolution. | MONTH/14d, MONTH/1d, MONTH/1m, MONTH/1y |
| EXPORT_LIMIT_REACHED | Optimizer panel voltage data is only available for the last 365 days. Please select a more recent date range. | DAY/3y, DAY/max, FIVE_MINUTES/2415, FIVE_MINUTES/3y, FIVE_MINUTES/700, FIVE_MINUTES/729 … |
| Internal Server Error | POST cni/ui-api/pages/site/analysis/custom/site/<site>/generate-chart mislukt: 500 Internal Server Error {"status":500,"error":"Internal Server Error","path":"/ui-api/pages/site/analysis/custom/site/<site>/generate-chart"} | DAY/3y, DAY/max, MONTH/3y, MONTH/max |
