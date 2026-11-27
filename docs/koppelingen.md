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

## Anthropic (research en analyse van opnames)

Nagelezen op 27-11-2026 in de actuele Anthropic-documentatie (Messages API, server tools,
structured outputs). Python SDK `anthropic>=2.14`. Model via `ANTHROPIC_MODEL`, standaard
`claude-opus-5-5`.

**Research** (`src/salesagent/enrichment/research.py`, `ClaudeResearchProvider`)

- `client.messages.stream(...)` met `get_final_message()`, `max_tokens=16000`,
  `output_config={"effort": "medium"}`
- Server tools: `web_search_20260209` (max 6 zoekopdrachten, `user_location` NL) en
  `web_fetch_20260209` (max 4). Het hulpmiddel "HTTP" uit `prompts/research_prompt.md` is web fetch.
- `stop_reason == "pause_turn"`: het gesprek wordt hervat (maximaal 4 keer).
  `stop_reason == "refusal"`: mislukt, netjes gelogd, geen profiel.
- Uitvoer: JSON uit de tekst, gevalideerd met pydantic (`ResearchProfile`). Ongeldige JSON = mislukt.
- Kosten per profiel uit `usage` (tokens en `server_tool_use.web_search_requests`), omgerekend met
  `USD_TO_EUR`. **Controleer de prijzen in `PRICES_USD` tegen de actuele prijslijst** voordat je
  op de kostencijfers stuurt.
- Prompt-versie: eerste 8 tekens van de SHA-256 van de systeemprompt.

**Opnames** (`src/salesagent/recordings/features.py`, `ClaudeExtractor`)

- `client.messages.create(...)` met `output_config={"effort": "low", "format": {"type":
  "json_schema", "schema": SCHEMA}}` (structured outputs).
- Het model krijgt alleen het gepseudonimiseerde transcript, nooit de CATI-uitkomst.

**Status: niet live getest.** Getest met een nagebootste client. Eerste echte run: een handvol
bedrijven, daarna kosten en kwaliteit controleren.

## ElevenLabs Agents (gesprekken)

Nagelezen op 27-11-2026 in de ElevenLabs Agents-documentatie (outbound calls via SIP-trunk,
dynamic variables, overrides, post-call webhooks, server tools).

**Gesprek starten** (`src/salesagent/voice/providers.py`, `ElevenLabsProvider`)

- `POST https://api.elevenlabs.io/v1/convai/sip-trunk/outbound-call`, header `xi-api-key`
- Body: `agent_id`, `agent_phone_number_id`, `to_number`,
  `conversation_initiation_client_data.dynamic_variables` (zie `voice/payload.py`) en
  `conversation_initiation_client_data.conversation_config_override.agent.first_message`
  en `language: "nl"`.
- Overrides voor `first_message` en `language` moeten in de agentinstellingen van ElevenLabs
  **aan** staan (Security, Overrides). Anders wordt de AI-melding niet als eerste zin gebruikt.
- Antwoord: `success` en `conversation_id`. Alles behalve `success: true` = niet gestart.
- Harde regel 5: de provider weigert bij `DRY_RUN=true`, bij een nummer op `TEST_BLOCK_NUMBERS`
  en zonder sleutel. Harde regel 4: `check_disclosure` weigert een verzoek zonder AI-melding.

**Tools die de agent aanroept** (`src/salesagent/api/voice_api.py`, header `X-Tool-Secret`)

- `POST /api/tools/afspraakmomenten`: vrije momenten van de accountmanager
- `POST /api/tools/afspraak-inplannen`: boekt een moment (geen dubbele boekingen)
- `POST /api/tools/verzet-registreren`: verzet per merk of voor alles, direct en blijvend
- Instellen in ElevenLabs als server tools (webhook) met `attempt_id` als dynamische variabele.
  De agent heeft de publieke URL van deze server nodig (bijvoorbeeld via een tunnel of hosting).

**Na het gesprek** (`POST /webhooks/elevenlabs`, `operations/postcall.py`)

- Type `post_call_transcription`. Handtekening `ElevenLabs-Signature: t=<unix>,v0=<hex>` met
  HMAC-SHA256 over `"{t}." + body`, geheim `ELEVENLABS_WEBHOOK_SECRET`, tolerantie 30 minuten.
- Gebruikt: `data.conversation_id`, `data.conversation_initiation_client_data.dynamic_variables.
  attempt_id`, `data.transcript`, `data.metadata.call_duration_secs`,
  `data.analysis.data_collection_results` (velden in `voice/prompt.py`, `DATA_COLLECTION`).
- Zonder bruikbare data collection: terugval op de regelgebaseerde classificatie van het transcript.
- Idempotent: dezelfde webhook twee keer verwerkt niets dubbel.

**Status: niet live getest.** Getest met nagebootste HTTP-antwoorden en door de simulator
gemaakte webhook-payloads. Nog nodig van de eigenaar: API-sleutel, agent-ID, telefoonnummer-ID
(SIP-trunk met afzendernummer) en het webhookgeheim.
