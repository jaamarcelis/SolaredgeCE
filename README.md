# SolarEdge Monitoring voor Home Assistant

Custom integratie (HACS) die de data van **monitoring.solaredge.com** in Home Assistant brengt, via dezelfde API als de webapp. Je logt in met je gewone SolarEdge-account. Een API-key is niet nodig.

Ten opzichte van de officiële integratie `solaredge` (publieke API, max. 300 calls per dag) krijg je onder andere:

- **energie en live data per paneel/optimizer**
- **live energiestroom** (zon, huis, net, batterij)
- **omvormertelemetrie**: AC/DC-spanning, stroom, frequentie, temperatuur, cos φ, isolatieweerstand, … De metrics worden automatisch ontdekt per omvormertype.
- weer en weersverwachting op de locatie van de installatie, alerts, curtailment en milieuvoordelen

> ⚠️ **Ongedocumenteerde API.** SolarEdge kan deze API zonder aankondiging wijzigen; de site zit achter Cloudflare. Gebruik op eigen risico en controleer of de gebruiksvoorwaarden van SolarEdge dit toestaan.

## Installatie

1. **HACS** → ⋮ → *Custom repositories* → voeg de URL van deze repository toe met type *Integration*.
2. Installeer **SolarEdge Monitoring** en herstart Home Assistant.
3. **Instellingen → Apparaten & diensten → Integratie toevoegen → SolarEdge Monitoring**.
4. Log in met je e-mailadres en wachtwoord. Heb je tweestapsverificatie, dan vraagt HA daarna om de code. Heb je meerdere installaties, dan kies je er één (voeg de integratie per installatie toe).

Handmatig installeren kan ook: kopieer `custom_components/solaredge_monitoring` naar `config/custom_components/`.

### Inloggen en sessie
- De integratie logt in via AWS Cognito (SRP), net als de website. Je wachtwoord gaat niet over de lijn.
- Wachtwoord en tokens worden in de config entry bewaard. Het access token (1 uur) wordt automatisch vernieuwd met het refresh token (30 dagen). Daarna logt de integratie stil opnieuw in met je wachtwoord.
- Lukt dat niet (wachtwoord gewijzigd, of MFA vereist), dan toont Home Assistant de melding **"Opnieuw inloggen"**.

## Wat je krijgt

Devices: **site → omvormer(s) → optimizer(s)**, plus een **batterij** als je die hebt. Wat er precies verschijnt, hangt af van je installatie: alles wordt bij het opstarten uit de API ontdekt. De volledige mapping (bron per entity, device_class, eenheid, state_class, standaard aan/uit) staat in **[docs/entity-mapping.md](docs/entity-mapping.md)**.

| Device | Belangrijkste entities |
|---|---|
| Site | actueel vermogen, energie vandaag/maand/jaar/**totaal**, specifiek rendement, weer, alerts, vermogen beperkt; met meter ook verbruik, netafname/teruglevering, eigen verbruik, zelfvoorzienendheid |
| Omvormer | vermogen, energie vandaag, communicatie, en **alle telemetriemetrics** die SolarEdge voor jouw omvormertype aanbiedt (kernset aan, rest standaard uit) |
| Optimizer | energie vandaag, relatieve opbrengst, vermogen; spanning en stroom standaard uit; paneelgegevens als attributen |
| Batterij | laadniveau, vermogen (+ laden / − ontladen), status, geladen en ontladen energie vandaag |

### Energy-dashboard
- **Zonne-energie:** *Energie totaal* van de site, of per omvormer *Lifetime Positive AC Energy*.
- **Met meter:** de sensoren *Netafname vandaag* en *Teruglevering vandaag*.

### Historie importeren
De statistieken van **Energie totaal** beginnen pas op het moment dat je de integratie toevoegt. De historie ervoor haal je zo op:

- de knop **Historie importeren** op het site-device. Die importeert de laatste *N* dagen tot en met vandaag met het gekozen interval; beide stel je in bij de opties (standaard 14 dagen, 1 uur). Het resultaat verschijnt als melding.
- de actie `solaredge_monitoring.import_energy_history` met het site-device, een begindatum, optioneel een einddatum (standaard vandaag) en een interval. De actie geeft het resultaat terug als response.

```yaml
action: solaredge_monitoring.import_energy_history
data:
  device_id: <id van het site-device>
  start: "2026-08-01"
  end: "2026-11-19"     # optioneel, tot en met; standaard vandaag
  interval: hour        # five_minutes, hour (standaard) of day
```

| Interval | Wat er geschreven wordt |
|---|---|
| `five_minutes` | 5-minutenstatistieken plus uurstatistieken. Home Assistant bewaart 5-minutenstatistieken maar zo lang als `keep_days` van de recorder (standaard 10 dagen); de uurrijen blijven. |
| `hour` | een uurstatistiek per uur |
| `day` | één uurstatistiek per dag, in het laatste uur van de dag, met de stand aan het eind van de dag. Genoeg voor dagtotalen in het Energy-dashboard. |

Hoe het werkt:
- SolarEdge levert de lifetime-teller van de omvormers per 5 minuten; die wordt altijd per 5 minuten opgehaald en daarna per interval omgezet.
- De sommen sluiten aan op de statistiek vóór de periode. Bestaande statistieken binnen de periode worden met dezelfde reeks herschreven, en alles erna schuift mee, zodat grafieken en het Energy-dashboard zonder sprong doorlopen.
- Dezelfde import nog een keer doen verandert niets. Je kunt ook later een andere periode of een fijner interval importeren.
- De periode eindigt uiterlijk bij de laatste uurstatistiek: vanaf daar telt de recorder zelf verder. De sensor moet dus al minstens één uurstatistiek hebben; wacht na het toevoegen van de integratie ongeveer een uur.

### Opties
**Instellingen → SolarEdge Monitoring → Configureren:**
- pollintervallen per soort data (live 60 s, energie 5 min, telemetrie en panelen 15 min, weer en alerts 30 min)
- devices per optimizer aan/uit
- welke omvormermetrics opgehaald worden (de lijst wordt automatisch gevuld vanuit de API)
- aantal dagen en interval voor de knop **Historie importeren** (standaard 14 dagen, 1 uur)

## Ontwikkeling

```
custom_components/solaredge_monitoring/
  api/            Python-client (port van de .NET-client): srp, auth, transport, models, client
  discovery.py    bouwt het SiteModel (apparaten + metrics) uit de API
  metrics.py      metric-metadata → device_class / eenheid / state_class
  coordinator.py  6 coordinators met eigen intervallen
  sensor.py  binary_sensor.py  button.py  weather.py  config_flow.py  diagnostics.py
  history.py      historie-import in de recorderstatistieken; services.py registreert de actie
tests/api          API-client (draait ook op Windows)
tests/integration  HA-integratietests (pytest-homeassistant-custom-component)
tests/live         opt-in contracttests tegen de echte API
```

Home Assistant draait niet op Windows. Draai de tests op Windows daarom in Docker:

```powershell
docker build -t solaredge-ha-tests -f tests/Dockerfile tests
```
```powershell
docker run --rm -v "${PWD}:/work" solaredge-ha-tests
```

Alleen de API-tests, direct op Windows:

```powershell
.venv\Scripts\python -m pytest tests/api -p no:homeassistant
```

Live contracttests (echte requests, alleen lezen, account zonder MFA):

```powershell
$env:SOLAREDGE_LIVE_TESTS='1'; $env:SOLAREDGE_USERNAME='jij@example.com'; $env:SOLAREDGE_PASSWORD='…'; .venv\Scripts\python -m pytest tests/live -p no:homeassistant -s
```

### Logo
Het logo en icoon staan in `custom_components/solaredge_monitoring/brand/`: `icon.png` (256×256), `icon@2x.png` (512×512) en `logo.png` (256 px hoog). Home Assistant 2026.10+ toont die zelf voor custom integrations, dus indienen bij home-assistant/brands is niet nodig. Ontbrekende varianten (`logo@2x.png`, `dark_*.png`) vult HA aan met de bestaande bestanden.

Voor publicatie: vervang `OWNER` in `manifest.json` door je GitHub-gebruikersnaam.
