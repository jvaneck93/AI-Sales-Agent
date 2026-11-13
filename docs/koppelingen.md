# Koppelingen

Per externe koppeling: welke endpoints en versies gebruikt worden en waar dat is nagelezen.

## KvK Basisprofiel API (rechtsvorm)

- Code: `src/salesagent/compliance/kvk.py` (`KvkApiClient`)
- Endpoint: `GET {KVK_API_BASE_URL}/basisprofielen/{kvkNummer}`, standaard `https://api.kvk.nl/api/v1`
- Authenticatie: header `apikey` met `KVK_API_KEY` uit `.env`
- Gebruikt veld: `_embedded.eigenaar.rechtsvorm`, met `_embedded.eigenaar.uitgebreideRechtsvorm` als terugval
- Foutafhandeling: 404 betekent "niet gevonden"; elke andere fout geeft een `KvkError`. In beide
  gevallen blijft de rechtsvorm onbekend en wordt het bedrijf niet gebeld.

**Status: niet geverifieerd.** De ontwikkelomgeving kon `developers.kvk.nl` en `api.kvk.nl` niet
bereiken (netwerkbeleid), dus endpoint en veldnamen zijn niet tegen de actuele documentatie of de
live API gecontroleerd. De client is alleen getest met nagebootste antwoorden. Controleer dit tegen
de documentatie en de KvK-testomgeving voordat de sleutel in gebruik gaat. Een afwijkende
veldnaam leidt tot "onbekend" (niet bellen), niet tot onterecht bellen.

## Anthropic (research, fase 3)

Nog niet gebouwd.

## ElevenLabs (gesprekken, fase 4)

Nog niet gebouwd.
