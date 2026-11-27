# AI-belcentrum voor zakelijk wagenpark (MVP)

Een AI-voice-agent belt namens een merk zakelijke klanten (alleen bv en nv), maakt zich direct
bekend als AI, kwalificeert of er iets speelt met het wagenpark en plant meteen een afspraak met
een accountmanager in. Dit systeem doet alles eromheen: leads, compliance, research, belrondes,
nabewerking, QA, kosten en leren van bestaande CATI-opnames.

**Alles draait standaard in de sandbox.** Er wordt niet echt gebeld (`DRY_RUN=true`), en zonder
API-sleutels worden nagebootste KvK-, research- en telefoniekoppelingen gebruikt. Alle bedrijven,
namen en nummers in de demo zijn verzonnen.

## Starten op Windows

1. Installeer Python 3.12 of nieuwer via <https://www.python.org/downloads/>.
   Vink tijdens de installatie **Add python.exe to PATH** aan.
2. Pak de ZIP uit, bijvoorbeeld naar `Documenten\ai-sales-agent`.
3. Dubbelklik `start.bat`. De eerste keer duurt dit een paar minuten (installeren en demodata).
4. De browser opent `http://127.0.0.1:8000/`. Laat het zwarte venster open; sluiten = stoppen.

Zonder demodata beginnen: `start.bat leeg`. Opnieuw beginnen met verse demodata:
`scripts\reset-demo.bat`.

## Leads importeren (Excel of CSV, ook 40.000+ regels)

Twee manieren, allebei op de pagina **Data en compliance**:

- **Uploaden**: kies een `.xlsx`- of `.csv`-bestand en klik Importeren.
- **Importmap**: zet het bestand in de map `data\import` en kies het op de pagina. Handig voor
  grote bestanden.

Verplicht zijn alleen **Bedrijfsnaam** en **KvK-nummer**. De rest is optioneel maar maakt het
bellen beter:

| Kolom | Waarom |
|---|---|
| Rechtsvorm | Alleen bv en nv worden koud gebeld. Leeg = niet bellen tot de KvK-check van de ronde. |
| Telefoon | Algemeen nummer. Zonder nummer kan er niet gebeld worden. |
| Adres (of Straat, Huisnummer, Postcode), Plaats | Selectie per regio en accountmanager. |
| Sector | Selectie per merk. |
| Website, E-mail | Research en bevestigingsmail. |
| Contactpersoon, Functie, Telefoon/E-mail contactpersoon | Vraag naar de juiste persoon. |
| Kenteken, Merk, Model, Bouwjaar, Koop of lease, Leasemaatschappij, Einddatum contract | Wagenpark; één voertuig per regel, bedrijfskolommen herhalen. |

- Andere kolomnamen worden vaak vanzelf herkend (bv. Handelsnaam, KvK, Vestigingsplaats,
  Branche, Datum eerste toelating). Het importrapport laat zien welke kolom waarvoor is gebruikt.
  Lukt dat niet, pas dan `config\import_mapping.yaml` aan.
- Regels met hetzelfde KvK-nummer worden één bedrijf. Opnieuw importeren werkt bij, maakt niets
  dubbel en laat verzet en status staan.
- Excel laat voorloopnullen weg: een KvK-nummer van 7 cijfers krijgt zijn nul terug (met een
  waarschuwing). Zet de kolom in Excel bij voorkeur op Tekst.
- Een sjabloon met uitleg: de link op de Data-pagina of `salesagent import-sjabloon`.
- Afgekeurde regels en waarschuwingen staan samengevat in het rapport en per regel in
  `data\importrapporten`.

Getest met 40.000 regels (verzonnen data): ongeveer 10 seconden.

## Wat je ziet

| Pagina | Wat |
|---|---|
| Overzicht | Kerncijfers, trechter, kosten per adres en per afspraak, A/B-varianten, wat aandacht nodig heeft |
| Merken en rondes | Merken, accountmanagers, belrondes aanmaken, voorbereiden en (gesimuleerd) bellen |
| Bedrijven | CRM: per bedrijf de compliance-uitkomst, wagenpark, profiel, gesprekken, verzet |
| Gesprek | Transcript, analyse, AI-melding gegeven, verstuurde payload, QA |
| Afspraken en QA | Afspraken controleren voordat ze naar de accountmanager gaan, steekproef van gesprekken |
| Uitgaand | Briefings aan accountmanagers en bevestigingen aan klanten (nog niet echt verstuurd) |
| Opnames | CATI-export verwerken tot playbook, bezwarenbibliotheek en testset |
| Agent | Systeemprompt voor ElevenLabs, testset draaien (harde en zachte criteria) |
| Data | CSV-import en compliance-check per bedrijf |

## Opdrachtregel

In een opdrachtprompt in deze map: `.venv\Scripts\activate`, daarna:

```
salesagent demo                               demodata en een gesimuleerde ronde per merk
salesagent import leads.xlsx                  leads importeren uit Excel of CSV
salesagent import-sjabloon                    Excel-sjabloon met de kolommen
salesagent ronde merken                       merken en belrondes
salesagent ronde maak --merk "Naam" --naam "Ronde 2027-1" --varianten A,B
salesagent ronde voorbereiden 3               selectie, rechtsvorm opnieuw controleren, compliance
salesagent ronde bellen 3                     droge run: laat exact zien wat er verstuurd zou worden
salesagent ronde bellen 3 --modus simulator   gesimuleerde gesprekken (sandbox)
salesagent research --ronde 3 --max 25        profielen voor belbare leads, in porties
salesagent opnames demo                       verzonnen CATI-export om te proberen
salesagent opnames verwerken export.csv --map pad\naar\mp4
salesagent agent prompt --uit prompt.md       systeemprompt voor ElevenLabs
salesagent agent eval                         testset tegen de agent
salesagent rapport                            resultaten en kosten
salesagent privacy opschonen                  bewaartermijnen toepassen
```

## Live gaan (later)

Kopieer `.env.example` naar `.env` (gebeurt automatisch bij de eerste start) en vul in:

- `KVK_API_KEY`: rechtsvorm per ronde. Live bellen vereist een controle via de KvK API.
- `ANTHROPIC_API_KEY`: research met web search en analyse van opnames.
- `ELEVENLABS_API_KEY`, `ELEVENLABS_AGENT_ID`, `ELEVENLABS_PHONE_NUMBER_ID`,
  `ELEVENLABS_WEBHOOK_SECRET`, `TOOL_SECRET`: de voice-agent, het afzendernummer en de webhooks.
- Pas als alles getest is: `DRY_RUN=false`. Zet testnummers in `TEST_BLOCK_NUMBERS`.

De agent in ElevenLabs heeft een publiek bereikbare URL van deze server nodig voor de tools en de
webhook. Zie `docs/koppelingen.md` voor endpoints en wat nog gecontroleerd moet worden.

## Harde regels in de code

- Alleen bv en nv; rechtsvorm onbekend = niet bellen; per ronde opnieuw gecontroleerd.
- Verzet wordt vlak voor elk gesprek gecontroleerd; per merk of voor alles; bij twijfel alles.
- Per poging vastgelegd: grondslag, tijdstip, uitkomst, verstuurde payload.
- Geen gesprek zonder AI-melding als eerste zin.
- Beltijden werkdagen 09:00 tot 17:00, maximaal 3 pogingen, belmoeheid over merken heen.
- Persoonsgegevens niet in logs; opnames worden gepseudonimiseerd voor analyse; bewaartermijnen
  en verwijderen per bedrijf of contactpersoon.

## Voor ontwikkelaars

```
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/ruff check src tests
```

Python 3.12+, FastAPI, SQLAlchemy (SQLite, Postgres-klaar), Alembic, Typer. Optioneel lokaal
transcriberen: `pip install -e ".[whisper]"` plus ffmpeg.
