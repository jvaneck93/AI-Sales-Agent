# Sales AI Agent: prospecting voor automotive dealer (MKB, B2B)

## Doel

Een backend die het prospectingwerk van een accountmanager zakelijke lease/wagenpark automatiseert: van CSV-import tot en met een ingeplande afspraak op locatie. Het telefoongesprek zelf wordt gevoerd door een ElevenLabs voice-agent. Dit project levert alles eromheen: data, compliance, research, belplanning, nabewerking en overdracht aan de menselijke accountmanager.

## Wat wel en niet in dit project zit

Wel: import, datamodel, compliance-poort, researchmodule, aansturing van uitgaande gesprekken, verwerking van gespreksresultaten, afspraken, briefing voor de accountmanager.

Niet: de gespreksprompt en de workflow van de voice-agent. Die worden in ElevenLabs zelf beheerd. Dit project levert alleen de dynamische variabelen aan en verwerkt de uitkomst.

## Harde regels (niet onderhandelbaar)

1. **Rechtsvormfilter.** Koud bellen mag alleen naar ondernemingen met rechtspersoonlijkheid (bv, nv, stichting, vereniging, coöperatie). Eenmanszaak, vof, cv en maatschap worden nooit gebeld zonder vastgelegde, aantoonbare toestemming. Rechtsvorm onbekend = niet bellen.
2. **Verzetregister.** Wie aangeeft niet meer gebeld te willen worden, wordt direct en blijvend geblokkeerd. Elke belpoging controleert dit register vlak voor het starten van het gesprek, niet alleen bij het inplannen.
3. **Bewijslast.** Leg per belpoging vast: waarom dit record gebeld mocht worden (rechtsvorm of toestemming met bron en datum), tijdstip en uitkomst.
4. **AI-melding.** De agent moet zich aan het begin van elk gesprek als AI bekendmaken. Dit project geeft daarvoor een verplichte variabele mee en weigert een gesprek te starten als de agentconfiguratie die melding niet bevat.
5. **Geen echte gesprekken per ongeluk.** `DRY_RUN=true` is de standaard. Echte uitgaande gesprekken alleen als `DRY_RUN=false` expliciet is gezet én het nummer niet op de testblokkade staat. Tests bellen nooit echt.
6. **Beltijden.** Alleen op werkdagen binnen instelbare kantoortijden (standaard 09:00 tot 17:00, Europe/Amsterdam), met een maximum aantal pogingen per bedrijf (standaard 3) en minimale tussenpozen.
7. **Persoonsgegevens.** Namen, directe nummers, opnames en transcripten zijn persoonsgegevens. Niet in logs, niet in foutmeldingen, niet in testfixtures (gebruik verzonnen data). Bouw een bewaartermijn en een verwijderfunctie per bedrijf/contactpersoon in.
8. **Geheimen.** API-sleutels alleen via `.env`, nooit in code of git. Lever een `.env.example` mee.
9. **Niets verzinnen.** De researchmodule presenteert nooit een vermoeden als feit. Bij twijfel weglaten.

De eigenaar laat de juridische kant extern toetsen. Zie je iets wat tegen deze regels ingaat of juridisch twijfelachtig lijkt, meld het en bouw het niet stilzwijgend.

## Stack

- Python 3.12, FastAPI, SQLAlchemy met SQLite (zo opgezet dat Postgres later kan), Alembic voor migraties
- Anthropic Python SDK voor research en classificatie; modelnaam via `ANTHROPIC_MODEL` in `.env`
- ElevenLabs Agents-platform voor de gesprekken
- pytest, ruff
- Een eenvoudige CLI (Typer) voor import, research en belrondes, zodat alles zonder UI te bedienen is

Vertrouw voor de API's van Anthropic en ElevenLabs niet op geheugen. Lees de actuele documentatie voordat je een koppeling bouwt, en noteer in `docs/koppelingen.md` welke endpoints en versies je gebruikt.

## Datamodel (uitgangspunt, mag je verbeteren)

- `bedrijf`: naam, KvK-nummer (uniek), rechtsvorm, adres, plaats, sector, website, algemeen telefoonnummer, e-mail
- `contactpersoon`: functie, naam, telefoon, e-mail (gekoppeld aan bedrijf)
- `voertuig`: merk, model, kenteken, bouwjaar, koop of lease, leasemaatschappij, einddatum contract (gekoppeld aan bedrijf)
- `toestemming`: type (opt-in of verzet), bron, datum, bewijs
- `profiel`: uitkomst van de researchmodule, bronnen, datum, versie van de prompt
- `belpoging`: tijdstip, grondslag, extern gespreks-ID, uitkomst, samenvatting, transcriptverwijzing
- `afspraak`: datum en tijd, locatie, accountmanager, status
- `status` per bedrijf: nieuw, geblokkeerd, research klaar, belbaar, in behandeling, terugbellen, afspraak, geen interesse, verzet

## Fasen

Bouw per fase, met tests, en stop na elke fase voor akkoord.

**Fase 1: Import en datamodel.** CSV-import met kolomtoewijzing via een configuratiebestand (de kolomnamen van de bron-CSV staan nog niet vast), validatie, ontdubbeling op KvK-nummer, importrapport met afgekeurde regels en reden. Klaar als: een voorbeeld-CSV met fouten erin correct wordt verwerkt en gerapporteerd.

**Fase 2: Compliance-poort.** Rechtsvorm bepalen (uit CSV, anders via een KvK-koppeling achter een interface zodat die later kan worden aangesloten), toestemmings- en verzetregister, functie `mag_gebeld_worden(bedrijf)` die een ja/nee met reden teruggeeft. Klaar als: alle harde regels 1 tot en met 3 door tests zijn afgedekt.

**Fase 3: Researchmodule.** Gebruikt de prompt in `prompts/research_prompt.md` via de Anthropic API met web search en web fetch (het hulpmiddel "HTTP" in de prompt is hier web fetch). Behoud de velden `profiel` en `gespreksopener`. Voeg daarnaast korte losse velden toe die een voice-agent makkelijk kan gebruiken: `kernactiviteit`, `omvang`, `wagenparksignalen`, `recent_nieuws`, `vacatures`, `bronnen`. Valideer de JSON, sla op met bronnen en datum, en ververs niet vaker dan nodig (instelbare houdbaarheid, standaard 30 dagen). Klaar als: research voor een lijst bedrijven in batch draait, mislukte gevallen netjes worden afgehandeld en de kosten per profiel worden gelogd.

**Fase 4: Belvoorbereiding en bellen.** Stel per bedrijf de dynamische variabelen samen (profielvelden, gespreksopener, wagenparkgegevens, eerstvolgende aflopende leasecontract, beschikbare afspraakmomenten). Planner die belbare bedrijven binnen de beltijden aanbiedt aan ElevenLabs. Alles eerst in `DRY_RUN`. Klaar als: een droge belronde het volledige pad doorloopt en exact laat zien wat er verstuurd zou worden.

**Fase 5: Nabewerking.** Webhook die het gespreksresultaat ontvangt (handtekening controleren), uitkomst classificeren (afspraak, terugbellen met datum, geen interesse, verzet, niet bereikt, verkeerde persoon), database bijwerken, verzet direct registreren. Klaar als: elke uitkomst met een voorbeeldtranscript getest is.

**Fase 6: Afspraak en overdracht.** Afspraak vastleggen via een agenda-interface (adapter, zie open beslissingen), bevestigingsmail naar de klant, en een briefing voor de accountmanager met profiel, gespreksverslag en wagenpark. Klaar als: één afspraak van begin tot eind doorloopt in de testomgeving.

**Fase 7: Overzicht.** Eenvoudig dashboard of rapport: aantallen per status, conversie per stap, kosten, en een lijst van wat aandacht van een mens nodig heeft.

## Open beslissingen (vraag ernaar, gok niet)

- Welke agenda en welk CRM gebruikt de dealer? Tot dat bekend is: bouw een adapter-interface met een lokale testimplementatie.
- Exacte kolommen van de bron-CSV.
- Is er toegang tot de KvK-API voor rechtsvorm?
- Welke telefonie-aansluiting hangt aan ElevenLabs en welk afzendernummer?
- Bewaartermijnen voor opnames en transcripten.

## Werkwijze

- Maak eerst een plan en leg het voor; begin pas met bouwen na akkoord.
- Kleine commits per onderdeel, met tests.
- Code en commits in het Engels, alles wat een gebruiker of klant ziet in het Nederlands.
- Bij onduidelijkheid: vragen stellen in plaats van aannames maken.
