# Startprompts voor Claude Code

Plak deze één voor één in Claude Code. Ga pas naar de volgende als de vorige fase werkt en je die hebt bekeken.

## 0. Kennismaking en plan

```
Lees CLAUDE.md en prompts/research_prompt.md. Bouw nog niets.
Geef mij:
1. een korte samenvatting van wat je gaat bouwen, in je eigen woorden;
2. de voorgestelde mappenstructuur;
3. je vragen over de open beslissingen in CLAUDE.md;
4. een plan voor fase 1 en 2.
Wacht daarna op mijn akkoord.
```

## 1. Import en datamodel

```
Bouw fase 1 uit CLAUDE.md. Zet het project op (dependencies, .env.example,
.gitignore, ruff, pytest), maak het datamodel met migraties en de CSV-import
met kolomtoewijzing via een configuratiebestand. Maak zelf een voorbeeld-CSV
met verzonnen bedrijven, inclusief foute regels en dubbele KvK-nummers, en
laat zien dat het importrapport klopt. Stop daarna en vat samen wat er staat.
```

Heb je een echte export? Geef dan alleen de kolomkoppen en een paar geanonimiseerde regels mee, zodat de kolomtoewijzing meteen klopt.

## 2. Compliance-poort

```
Bouw fase 2 uit CLAUDE.md. Schrijf eerst de tests voor harde regels 1 tot en
met 3, daarna de implementatie. Laat mij per voorbeeldbedrijf zien of het
gebeld mag worden en met welke reden. Zet de KvK-koppeling achter een
interface met een testimplementatie.
```

## 3. Researchmodule

```
Bouw fase 3 uit CLAUDE.md. Lees eerst de actuele Anthropic-documentatie over
web search, web fetch en gestructureerde output en leg je keuzes vast in
docs/koppelingen.md. Draai de research daarna op 3 bedrijven die ik opgeef,
laat mij de volledige JSON, de gebruikte bronnen en de kosten per profiel
zien. Bouw de batchverwerking pas als ik de kwaliteit goedkeur.
```

Controleer deze eerste profielen zelf tegen de bronnen. Dit is het moment om de prompt bij te stellen.

## 4. Belvoorbereiding en bellen (droog)

```
Bouw fase 4 uit CLAUDE.md, volledig in DRY_RUN. Lees eerst de actuele
ElevenLabs-documentatie over uitgaande gesprekken en dynamische variabelen en
leg vast wat je gebruikt in docs/koppelingen.md. Laat mij voor 3 bedrijven
exact zien welke variabelen naar de agent zouden gaan. Geef mij ook een lijst
van de variabelen die ik in de ElevenLabs-agent moet aanmaken.
```

## 5. Nabewerking

```
Bouw fase 5 uit CLAUDE.md. Maak voor elke uitkomst een verzonnen
voorbeeldtranscript en test de classificatie daarop. Toon mij de gevallen
waarin de classificatie twijfelt en hoe die bij een mens terechtkomen.
```

## 6. Afspraak en overdracht

```
Bouw fase 6 uit CLAUDE.md met de agenda-adapter. Laat één afspraak van begin
tot eind doorlopen in de testomgeving en toon mij de bevestigingsmail en de
briefing voor de accountmanager.
```

## 7. Eerste echte test

```
Help mij een proef op te zetten met alleen mijn eigen telefoonnummer als
bestemming. Loop vooraf de harde regels in CLAUDE.md na en laat zien dat elk
ervan actief is. Zet DRY_RUN pas uit nadat ik dat expliciet bevestig.
```

## 8. Overzicht

```
Bouw fase 7 uit CLAUDE.md.
```
