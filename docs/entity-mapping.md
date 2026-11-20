# Mapping: SolarEdge API → Home Assistant

Dit document beschrijft hoe de integratie de data uit de interne API van monitoring.solaredge.com omzet naar Home Assistant-**devices**, **entities** en hun **eigenschappen**. Alles wordt bij het opstarten **dynamisch** opgebouwd uit wat de API over jouw site teruggeeft; er staat geen apparatenlijst in de code.

## 1. Hoe de opbouw werkt

```
setup
 └─ discovery.async_discover()            → SiteModel
     ├─ components                        welke onderdelen: batterij? meter? PR?
     ├─ layout/logical                    boom: site → omvormers → strings → optimizers
     ├─ v2/equipment                      batterijen (fabrikant, capaciteit, firmware)
     ├─ equipment-inverter/{serial}       firmware DSP1/DSP2/COMM, communicatie
     ├─ layout/information/optimizers     paneelmodel, STC-vermogen, oriëntatie, helling
     └─ device-types/{type}/metrics/supported   alle telemetriemetrics per omvormertype
 └─ sensor.build_sensors(SiteModel)       → devices + entities
```

De `StaticCoordinator` voert de discovery elke 24 uur opnieuw uit. Komen er apparaten of metrics bij (of verdwijnen ze), dan herlaadt de integratie zichzelf en verschijnen de nieuwe entities vanzelf.

**Unique ID's:** `{site_id}_{apparaat}_{sleutel}`, bijvoorbeeld `1571936_7307A2C0-DC_metric_measurements.ac_voltage`. Ze blijven stabiel, ook na het hernoemen van entities.

## 2. Devices

| Device | Identifier | Naam | Model | Serienummer | Softwareversie | `via_device` |
|---|---|---|---|---|---|---|
| **Site** | `site_{site_id}` | sitenaam | "Residential site (3.84 kWp)" | site-ID | – | – |
| **Omvormer** | volledig serienummer (`7307A2C0-DC`) | naam uit de lay-out ("Inverter 1") | lay-out `model` / onderdeelnummer | serienummer | `DSP1 … / DSP2 … / COMM …` | site |
| **Optimizer** | serienummer (`12A33800-ED`) | "Optimizer 1.1.7" | optimizermodel (`P370-…`); `model_id` = paneelmodel | serienummer | – | eigen omvormer |
| **Batterij** | serienummer | "Batterij" | onderdeelnummer + capaciteit | serienummer | firmware | site |

Alle devices hebben `manufacturer` = SolarEdge (of de fabrikant uit de API) en een `configuration_url` naar de site in de monitoring-portal.

## 3. Entities per device

Legenda:
- **Coordinator:** waar de data vandaan komt en hoe vaak (zie §5).
- **Std.:** ✓ = standaard ingeschakeld, – = standaard uitgeschakeld (in te schakelen in HA).
- **Cat.:** D = diagnostic, C = config.

### Site
| Entity | API-bron → veld | device_class | eenheid | state_class | Coordinator | Std. | Cat. |
|---|---|---|---|---|---|---|---|
| Actueel vermogen | `power-flow/v2` → `solarProduction.currentPower` | power | kW (weergave W) | measurement | live | ✓ | |
| AC-vermogen | `live-power` → `currentAcPower` (attr. `max_ac_power`) | power | W | measurement | live | – | |
| Laatste update | `power-flow/v2` → `lastUpdateTime` | timestamp | | | live | ✓ | D |
| Energie vandaag / deze maand / dit jaar | `sitesMeasurements` → `energyToday/Monthly/Yearly` | energy | kWh | total_increasing | energy | ✓ | |
| **Energie totaal** | `sitesMeasurements` → `energyLifeTime` | energy | kWh | total_increasing | energy | ✓ | |
| Specifiek rendement vandaag | `energy` → `summary.yield` | | kWh/kWp | measurement | energy | ✓ | |
| Specifiek rendement maand / jaar | `sitesMeasurements` → `monthlyYield/yearlyYield` | | kWh/kWp | measurement | energy | ✓ | |
| Performance ratio *(als `hasPerformanceRatio`)* | `energy` → `summary.performanceRatio` | | % | measurement | energy | ✓ | |
| Piekvermogen | site → `peakPower` | | kWp | | static | ✓ | D |
| Open alerts | `alertTotalCount`; attribuut `alerts` uit `partialData` | | | measurement | info | ✓ | |
| CO₂ bespaard / Equivalent geplante bomen | `environmental-benefits` | weight / – | kg / – | total_increasing | info | ✓ | D |
| Zonsopkomst / Zonsondergang | `weather` → `sunTime` | timestamp | | | info | – | |
| **Weer** (weather-entity) | `weather` → `liveWeather` + `weatherForecast` | | °C, km/u | | info | ✓ | |
| Communiceert (binary) | `power-flow/v2` → `isCommunicating` | connectivity | | | live | ✓ | D |
| Vermogen beperkt (binary) | `curtailment` → `powerLimited` (attr. `reasons`) | problem | | | energy | ✓ | |
| Alerts (binary) | `alertTotalCount > 0` | problem | | | info | ✓ | |
| Historie importeren (knop) | `generate-chart` → `measurements.lifetime_positive_ac_energy` per 5 min, zie README *Statistieken importeren* | | | | – | ✓ | C |

**Alleen met een consumptie-/netmeter** (`components.hasConsumptionAndGrid`):

| Entity | API-bron | device_class | eenheid | state_class |
|---|---|---|---|---|
| Verbruik | `power-flow/v2` → `consumption.currentPower` | power | kW | measurement |
| Netafname / Teruglevering | `grid.currentPower` bij `status` import / export (anders 0) | power | kW | measurement |
| Netstatus | `grid.status` (import, export, grid-outage, idle) | enum | | |
| Verbruik vandaag | `energy` (met `*-distribution-*`) → `summary.consumption` | energy | Wh | total_increasing |
| Netafname / Teruglevering vandaag | `summary.import/export` (of de verdeling) | energy | Wh | total_increasing |
| Eigen verbruik vandaag | `productionDistribution.productionToHome` | energy | Wh | total_increasing |
| Eigenverbruiksgraad / Zelfvoorzienendheid | `selfConsumptionRatio` / `selfSufficiencyRatio` × 100 | | % | measurement |

### Omvormer
| Entity | API-bron | device_class | eenheid | state_class | Coordinator | Std. |
|---|---|---|---|---|---|---|
| Vermogen | `inverters/power` → laatste niet-null in `inverterPowerArray` | power | W | measurement | energy | ✓ |
| Energie vandaag | `inverters/energy` → `inverterEnergy` | energy | Wh | total_increasing | energy | ✓ |
| Communicatie | `equipment-inverter` → `communication` (WIFI/LAN/…) | | | | static | ✓ (D) |
| **Telemetrie (dynamisch)** | `generate-chart` met alle ontdekte metrics; laatste niet-null punt uit de afgelopen 2 uur; attributen `measured_at`, `metric` | zie §4 | zie §4 | zie §4 | telemetry | kernset ✓, rest – |

Voorbeeld van wat SolarEdge voor een SE3500H teruggeeft (`ONE_PHASE_INVERTER`):

| Metric (`categorie.uri`) | Wordt | Std. |
|---|---|---|
| `pv_measurements.power` | DC-vermogen, power, W | ✓ |
| `measurements.active_power` | power, W | ✓ |
| `measurements.ac_voltage` / `ac_current` / `ac_frequency` | voltage V / current A / frequency Hz | ✓ |
| `measurements.dc_voltage` | voltage, V | ✓ |
| `measurements.temperature` | temperature, °C | ✓ |
| `measurements.power_factor` | power_factor, % | ✓ |
| `measurements.lifetime_positive_ac_energy` | energy, Wh, **total_increasing** | ✓ |
| `measurements.apparent_power` / `reactive_power` | apparent_power VA / reactive_power var | – |
| `measurements.ground_fault_resistance` | kΩ (geen device_class) | – |
| `measurements.ac_current_dc_component`, `negative_active_power` | current / power | – |
| `measurements.status` | tekst | – |
| `*_since_last_telemetry` (DELTA) | *geen entity* | |

### Optimizer / paneel (optie "Per optimizer een apparaat")
| Entity | API-bron | device_class | eenheid | state_class | Std. |
|---|---|---|---|---|---|
| Energie vandaag | `layout/energy/by-inverter` → `optimizers[].energy.value` | energy | Wh | total_increasing | ✓ |
| Relatieve opbrengst | `optimizers[].color` × 100 (t.o.v. beste paneel) | | % | measurement | ✓ |
| Vermogen | `layout/information/optimizers` → `power_W` | power | W | measurement | ✓ |
| Paneelspanning | `voltage_V` | voltage | V | measurement | – |
| Uitgangsspanning | `optimizerVoltage_V` | voltage | V | measurement | – |
| Stroom | `current_A` | current | A | measurement | – |

**Diagnostic-entities** (categorie *diagnostic*, standaard aan) met de vaste eigenschappen van optimizer en paneel. Ze komen uit de lay-out en `layout/information/optimizers` en worden dagelijks ververst via de static coordinator:

| Entity | API-bron | device_class | eenheid |
|---|---|---|---|
| Positie | lay-out `displayOrder` (bijv. 1.1.7) | | |
| Paneelfabrikant | `modules[0].manufacturer` | | |
| Paneelmodel | `modules[0].model` | | |
| Nominaal paneelvermogen | `modules[0].stcPower` | power | W |
| Paneeloriëntatie | `modules[0].orientation` (landscape, portrait) | enum | |
| Paneelhelling | `modules[0].tilt` (radialen → graden) | | ° |
| Paneelazimut | `modules[0].azimuth` (radialen → graden) | | ° |
| Paneelligging | windrichting bij de azimut, uitgeschreven met afkorting, bijv. `Zuid (Z)`, `Oostnoordoost (ONO)` | | |

De paneelligging volgt een 16-puntskompas (0° = N, 90° = O, 180° = Z, 270° = W) in de taal van Home Assistant: Nederlands voor `nl`, anders Engels (`South (S)`, `East-northeast (ENE)`).

Op de live sensoren staat het attribuut `last_measurement`.

### Batterij (als `hasStorage`)
| Entity | API-bron | device_class | eenheid | state_class |
|---|---|---|---|---|
| Laadniveau | `power-flow/v2` → `dcStorage/acStorage.chargeLevel` | battery | % | measurement |
| Batterijvermogen | `currentPower` (positief = laden, negatief = ontladen) | power | kW | measurement |
| Batterijstatus | `status` (charging, discharging, idle) | enum | | |
| Geladen uit zon / net vandaag | `storage/energy/distribution` → `sources.pv` / `sources.grid` | energy | Wh | total_increasing |
| Ontladen naar huis / net vandaag | `destinations.building` / `destinations.grid` | energy | Wh | total_increasing |

## 4. Dynamische metric → sensor-eigenschappen (`metrics.py`)

| API `units` | device_class | HA-eenheid |
|---|---|---|
| W / kW | power | W / kW |
| Wh / kWh | energy | Wh / kWh |
| V | voltage | V |
| A | current | A |
| Hz | frequency | Hz |
| C | temperature | °C |
| VA | apparent_power | VA |
| VAr | reactive_power | var |
| s | duration | s |
| W/m^2 | irradiance | W/m² |
| % | power_factor (als de uri `power_factor` bevat), anders geen | % |
| overig (kΩ, Wh/Wp, …) | geen | de eenheid als tekst |

| API `kind` | state_class |
|---|---|
| GAUGE | measurement (energy → total) |
| CUMULATIVE | total_increasing |
| DELTA | geen entity |

| API `dataType` | Gevolg |
|---|---|
| STRING | tekstsensor zonder eenheid en state_class |

- **Naam:** `displayName` uit de API (Engels). Komt een naam vaker voor, of botst hij met een vaste sensor (bijvoorbeeld "Power"), dan krijgt hij de categorie erbij: "Power (PV measurements)".
- **Standaard ingeschakeld:** alleen de kernset in `const.CORE_METRICS`. Alle andere metrics, ook nieuwe die SolarEdge later toevoegt, worden **uitgeschakelde** entities. Via de opties kun je ook beperken welke metrics überhaupt opgehaald worden.

## 5. Pollintervallen (aan te passen in de opties)

| Coordinator | Endpoints | Standaard |
|---|---|---|
| static | discovery | 24 uur |
| live | power-flow v2, live-power | 60 s |
| energy | sitesMeasurements, energy vandaag, omvormers energie/vermogen, curtailment, batterij | 5 min |
| telemetry | generate-chart (omvormermetrics) | 15 min |
| panels | energie per paneel + optimizer-live-data | 15 min |
| info | weer, alerts, milieuvoordelen, communicatie | 30 min |

Endpoints die voor jouw site **niet van toepassing** zijn (`BAD_ARGUMENTS` zonder batterij, `EMS_FEATURE_NOT_ENABLED`, 403 op alerts voor eigenaren) worden na de eerste keer overgeslagen. De bijbehorende entities blijven dan "onbekend". Diagnostics toont welke endpoints overgeslagen worden.
