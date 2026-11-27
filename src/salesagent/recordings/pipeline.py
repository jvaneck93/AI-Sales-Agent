"""From CATI recordings to playbook, objection library and eval set.

Steps (plan "Leren van historische gespreksopnames"):
1. Read the CATI export (call table) and map disposition codes to labels.
2. Filter: unknown codes, voicemail/not reached, shorter than 20 s.
3. Probe the mp4 (channels, sample rate), discard video.
4. Transcribe (sidecar transcript or local Whisper).
5. Pseudonymise before anything else touches the text. Agent ids become
   tokens A01..; the link table with real ids/KvK is stored separately.
6. Extract features WITHOUT the outcome.
7. Guardrail per agent: outlier labelers are excluded from "what works".
8. Analyse per cell (minimum n), then write playbook, library evidence and
   eval cases (never containing the outcome).
"""

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from salesagent.enrichment.fleet import summarize_fleet
from salesagent.models import (
    Company,
    Contact,
    EvalCase,
    ObjectionEntry,
    PlaybookVersion,
    Recording,
)
from salesagent.recordings.audio import AudioError, probe
from salesagent.recordings.features import FeatureExtractor, timing_features
from salesagent.recordings.pseudonymize import pseudonymize_turns
from salesagent.recordings.transcribe import Transcriber, TranscriptionError

ROOT = Path(__file__).resolve().parents[3]
MAPPING_PATH = ROOT / "config" / "cati_mapping.yaml"
POSITIVE = ("afspraak", "warm")
NOT_A_CONVERSATION = ("voicemail", "niet bereikt")


@dataclass
class CatiMapping:
    columns: dict
    dispositions: dict
    delimiter: str = ";"
    encoding: str = "utf-8-sig"
    min_duration_s: float = 20
    guardrail_min_calls: int = 20
    guardrail_factor: float = 2.0
    min_cell: int = 30


def load_cati_mapping(path: Path = MAPPING_PATH) -> CatiMapping:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return CatiMapping(**raw)


@dataclass
class PipelineReport:
    rows: int = 0
    new: int = 0
    duplicates: int = 0
    excluded: Counter = field(default_factory=Counter)
    unknown_codes: Counter = field(default_factory=Counter)
    audio: Counter = field(default_factory=Counter)
    transcribed: int = 0
    guardrail_agents: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"Regels in export: {self.rows}, nieuw: {self.new}, al bekend: {self.duplicates}",
            f"Getranscribeerd en gepseudonimiseerd: {self.transcribed}",
        ]
        if self.audio:
            lines.append("Audio: " + ", ".join(f"{k}: {v}" for k, v in self.audio.items()))
        for reason, n in self.excluded.most_common():
            lines.append(f"  niet gebruikt voor analyse: {n}x {reason}")
        if self.unknown_codes:
            lines.append(
                "Onbekende dispositiecodes (vertaaltabel aanvullen): "
                + ", ".join(f"{c} ({n}x)" for c, n in self.unknown_codes.items())
            )
        if self.guardrail_agents:
            lines.append(
                "Vangrail: buiten analyse gelaten agents " + ", ".join(self.guardrail_agents)
            )
        return "\n".join(lines)


def _segment(company: Company | None) -> dict:
    if company is None:
        return {"vloot": "onbekend", "sector": "onbekend"}
    fleet = summarize_fleet(company)
    size = (
        "klein (<5)" if fleet.count < 5 else "middel (5-19)" if fleet.count < 20 else "groot (20+)"
    )
    top_make = next(iter(fleet.makes), "onbekend")
    return {"vloot": size, "sector": company.sector or "onbekend", "merk": top_make}


def _parse_dt(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


class LinkTable:
    """Separate file with the mapping token -> real agent id / call -> KvK.

    Kept outside the database with transcripts, with restricted access.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {"agents": {}, "calls": {}}
        )

    def agent_token(self, agent_id: str) -> str:
        agents = self.data["agents"]
        if agent_id not in agents:
            agents[agent_id] = f"A{len(agents) + 1:02d}"
        return agents[agent_id]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1), encoding="utf-8")


def ingest_export(
    session: Session,
    export_csv: Path,
    recordings_dir: Path,
    *,
    transcriber: Transcriber,
    extractor: FeatureExtractor,
    workdir: Path,
    mapping: CatiMapping | None = None,
) -> PipelineReport:
    mapping = mapping or load_cati_mapping()
    report = PipelineReport()
    links = LinkTable(workdir / "koppeltabel.json")
    cols = mapping.columns
    known_names = [n for n in session.scalars(select(Contact.name)) if n]
    with export_csv.open(encoding=mapping.encoding, newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter=mapping.delimiter))
    agent_names = [r.get(cols.get("agent_id", ""), "") for r in rows]
    existing = set(session.scalars(select(Recording.call_id)))
    for row in rows:
        report.rows += 1
        call_id = (row.get(cols["call_id"]) or "").strip()
        if not call_id:
            report.excluded["geen call_id"] += 1
            continue
        if call_id in existing:
            report.duplicates += 1
            continue
        existing.add(call_id)
        code = (row.get(cols["disposition"]) or "").strip()
        outcome = mapping.dispositions.get(code)
        kvk = (row.get(cols.get("kvk_number", ""), "") or "").strip()
        company = session.scalar(select(Company).where(Company.kvk_number == kvk)) if kvk else None
        try:
            duration = float((row.get(cols["duration_s"]) or "0").replace(",", "."))
        except ValueError:
            duration = 0.0
        agent_id = (row.get(cols["agent_id"]) or "").strip()
        recording = Recording(
            call_id=call_id,
            file_name=(row.get(cols["file"]) or "").strip() or None,
            agent_token=links.agent_token(agent_id) if agent_id else None,
            started_at=_parse_dt(row.get(cols.get("started_at", ""), "") or ""),
            duration_s=duration,
            disposition_code=code,
            outcome=outcome,
            campaign=(row.get(cols.get("campaign", ""), "") or "").strip() or None,
            segment=_segment(company),
        )
        if kvk:
            links.data["calls"][call_id] = kvk  # KvK stays in the link table only
        session.add(recording)
        report.new += 1
        if outcome is None:
            report.unknown_codes[code or "(leeg)"] += 1
            recording.excluded_reason = "onbekende dispositiecode"
        elif outcome in NOT_A_CONVERSATION:
            recording.excluded_reason = "geen gesprek (voicemail of niet bereikt)"
        elif duration < mapping.min_duration_s:
            recording.excluded_reason = f"korter dan {mapping.min_duration_s:g} s"
        if recording.excluded_reason:
            report.excluded[recording.excluded_reason] += 1
            continue
        media = recordings_dir / (recording.file_name or f"{call_id}.mp4")
        if media.exists() and media.suffix.lower() in (".mp4", ".wav", ".mp3", ".m4a"):
            try:
                recording.audio_info = probe(media)
                info = recording.audio_info
                kind = f"{info['channels']} kanaal/{info['sample_rate']} Hz"
                report.audio[kind] += 1
                if recording.audio_info["has_video"]:
                    report.audio["videospoor genegeerd"] += 1
            except AudioError as exc:
                report.audio[str(exc)] += 1
        try:
            turns = transcriber.transcribe(media, recording.audio_info, workdir / "audio")
        except (TranscriptionError, FileNotFoundError, json.JSONDecodeError) as exc:
            recording.excluded_reason = f"transcriptie mislukt: {exc}"
            report.excluded["transcriptie mislukt"] += 1
            continue
        names = known_names + [n for n in agent_names if n]
        turns = pseudonymize_turns(turns, names)
        recording.transcript = turns
        recording.quality_flag = (
            "OK" if turns and any(t["role"] == "user" for t in turns) else "twijfel"
        )
        # The extractor gets the transcript only. Never the outcome.
        features = extractor.extract(turns)
        features.update(timing_features(turns, duration))
        recording.features = features
        report.transcribed += 1
    links.save()
    session.flush()
    report.guardrail_agents = apply_guardrail(session, mapping)
    return report


def apply_guardrail(session: Session, mapping: CatiMapping) -> list[str]:
    """Exclude agents whose positive-label share deviates strongly within the segment."""
    usable = [
        r
        for r in session.scalars(select(Recording))
        if r.features is not None and (r.excluded_reason in (None, "vangrail agent"))
    ]
    by_segment: dict[str, list[Recording]] = defaultdict(list)
    for r in usable:
        by_segment[r.segment.get("vloot", "onbekend")].append(r)
    flagged: set[str] = set()
    for recs in by_segment.values():
        overall = sum(r.outcome in POSITIVE for r in recs) / len(recs)
        per_agent: dict[str, list[Recording]] = defaultdict(list)
        for r in recs:
            per_agent[r.agent_token or "?"].append(r)
        for agent, agent_recs in per_agent.items():
            if len(agent_recs) < mapping.guardrail_min_calls or overall == 0:
                continue
            share = sum(r.outcome in POSITIVE for r in agent_recs) / len(agent_recs)
            if share > overall * mapping.guardrail_factor or (
                share < overall / mapping.guardrail_factor
            ):
                flagged.add(agent)
    for r in usable:
        r.excluded_reason = "vangrail agent" if r.agent_token in flagged else None
    session.flush()
    return sorted(flagged)


def analyze(session: Session, min_cell: int = 30) -> dict:
    recs = [
        r
        for r in session.scalars(select(Recording))
        if r.features is not None and r.excluded_reason is None
    ]

    def cell(group: list[Recording]) -> dict:
        n = len(group)
        return {
            "n": n,
            "afspraak": round(sum(r.outcome == "afspraak" for r in group) / n, 3) if n else None,
            "warm_of_beter": round(sum(r.outcome in POSITIVE for r in group) / n, 3) if n else None,
            "langer_dan_60s": round(
                sum(bool(r.features.get("longer_than_60s")) for r in group) / n, 3
            )
            if n
            else None,
            "betrouwbaar": n >= min_cell,
        }

    def by(key) -> dict:
        groups: dict = defaultdict(list)
        for r in recs:
            for k in key(r):
                groups[k].append(r)
        return {k: cell(v) for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))}

    objection_counts = Counter(o["category"] for r in recs for o in r.features["objections"])
    return {
        "gesprekken": len(recs),
        "uitkomsten": dict(Counter(r.outcome for r in recs)),
        "min_cel": min_cell,
        "opening": by(lambda r: [r.features["opening_type"]]),
        "opening_per_segment": by(
            lambda r: [f"{r.segment.get('vloot')} · {r.features['opening_type']}"]
        ),
        "afsluiting": by(lambda r: [r.features["closing_type"]]),
        "beslisser_bevestigd": by(
            lambda r: ["ja" if r.features["decision_maker_confirmed"] else "nee"]
        ),
        "bezwaar_reactie": by(
            lambda r: [f"{o['category']} → {o['response_type']}" for o in r.features["objections"]]
        ),
        "spreekaandeel": by(
            lambda r: [
                "agent > 70%" if r.features.get("talk_share_agent", 0) > 0.7 else "agent ≤ 70%"
            ]
        ),
        "top_bezwaren": objection_counts.most_common(10),
        "uitgesloten": dict(
            Counter(
                r.excluded_reason for r in session.scalars(select(Recording)) if r.excluded_reason
            )
        ),
    }


def _pct(value) -> str:
    return "–" if value is None else f"{value * 100:.1f}%"


def _best(cells: dict, prefix: str = "") -> list[tuple[str, dict]]:
    reliable = [(k, v) for k, v in cells.items() if v["betrouwbaar"] and k.startswith(prefix)]
    return sorted(reliable, key=lambda kv: -(kv[1]["warm_of_beter"] or 0))


def _examples(session: Session, category: str, response: str, limit: int = 3) -> list[str]:
    """Agent replies after this objection in positive calls (already pseudonymised)."""
    found = []
    for r in session.scalars(select(Recording).where(Recording.outcome.in_(POSITIVE))):
        if r.excluded_reason or not r.transcript or not r.features:
            continue
        for o in r.features["objections"]:
            if o["category"] != category or o["response_type"] != response:
                continue
            start = o.get("turn_index", 0) + 1
            reply = next((t["message"] for t in r.transcript[start:] if t["role"] == "agent"), None)
            if reply and "[NAAM]" not in reply and reply not in found:
                found.append(reply)
        if len(found) >= limit:
            break
    return found[:limit]


def build_outputs(session: Session, analysis: dict, version: str | None = None) -> PlaybookVersion:
    """Playbook (markdown), objection-library evidence and proposals, eval cases."""
    version = version or datetime.now().strftime("%Y%m%d-%H%M")
    min_cell = analysis["min_cel"]
    lines = [
        f"# Playbook uit historische opnames (versie {version})",
        "",
        f"Gebaseerd op {analysis['gesprekken']} bruikbare gesprekken. Conclusies alleen bij "
        f"minstens {min_cell} gesprekken per cel; de rest staat erbij als 'te weinig data'. "
        "Dit zijn tellingen, geen bewijs van oorzaak: valideer met ervaren agents en een A/B-test.",
        "",
        "## Hard (geldt altijd, ook als de opnames iets anders laten zien)",
        "- Eerste zin: AI-melding en melding dat het gesprek wordt opgenomen.",
        "- Nooit prijzen, kortingen of levertijden noemen.",
        "- Bij 'bel me niet meer': vragen of het alleen voor dit merk geldt of voor alles; "
        "bij twijfel alles blokkeren. Daarna stoppen.",
        "- Nooit zeggen dat je een mens bent; geen bestaande persoon nadoen.",
        "",
        "## Openingen",
    ]
    for key, c in analysis["opening"].items():
        tag = "" if c["betrouwbaar"] else " (te weinig data)"
        lines.append(
            f"- **{key}**: n={c['n']}, gesprek >60 s {_pct(c['langer_dan_60s'])}, "
            f"warm of beter {_pct(c['warm_of_beter'])}{tag}"
        )
    lines += ["", "## Afsluiting"]
    for key, c in analysis["afsluiting"].items():
        tag = "" if c["betrouwbaar"] else " (te weinig data)"
        lines.append(f"- **{key}**: n={c['n']}, afspraak {_pct(c['afspraak'])}{tag}")
    lines += ["", "## Bezwaren: welke reactie houdt het gesprek gaande"]
    library = {e.category: e for e in session.scalars(select(ObjectionEntry))}
    for category, n in analysis["top_bezwaren"][:5]:
        lines.append(f"### {category} ({n}x)")
        cells = {
            k: v for k, v in analysis["bezwaar_reactie"].items() if k.startswith(f"{category} → ")
        }
        for key, c in cells.items():
            tag = "" if c["betrouwbaar"] else " (te weinig data)"
            lines.append(
                f"- {key.split(' → ')[1]}: n={c['n']}, warm of beter "
                f"{_pct(c['warm_of_beter'])}{tag}"
            )
        best = _best(cells)
        entry = library.get(category)
        if entry is not None:
            entry.evidence = {"n": n, "reacties": cells, "bron": f"playbook {version}"}
        if best:
            response = best[0][0].split(" → ")[1]
            examples = _examples(session, category, response)
            lines.append(f"- **Beste reactietype: {response}.** Voorbeelden (gepseudonimiseerd):")
            lines += [f'  - "{e}"' for e in examples] or ["  - (geen geschikt fragment)"]
            proposal = session.scalar(
                select(ObjectionEntry).where(ObjectionEntry.category == f"{category}__voorstel")
            )
            if examples and entry is not None:
                if proposal is None:
                    proposal = ObjectionEntry(
                        category=f"{category}__voorstel",
                        label=f"{entry.label} (voorstel)",
                        triggers=entry.triggers,
                        give_up_when=entry.give_up_when,
                        approved=False,
                        source="opnames",
                    )
                    session.add(proposal)
                proposal.responses = examples[:2]
                proposal.evidence = {"reactietype": response, **best[0][1]}
        lines.append("")
    lines += ["## Stijl (grenzen, geen doelen)"]
    for key, c in analysis["spreekaandeel"].items():
        lines.append(f"- {key}: n={c['n']}, warm of beter {_pct(c['warm_of_beter'])}")
    lines += ["", "## Uitgesloten van analyse"]
    lines += [f"- {k}: {v}" for k, v in analysis["uitgesloten"].items()] or ["- niets"]
    playbook = PlaybookVersion(version=version, content="\n".join(lines), stats=analysis)
    session.add(playbook)
    build_eval_cases(session)
    session.flush()
    return playbook


def build_eval_cases(session: Session, limit_per_category: int = 6) -> int:
    """Test situations: the prospect's real lines up to and after the hard moment.

    Expectations come from the situation (which objection), never from the
    historical outcome, so the agent is not scored on matching the old label.
    """
    added = 0
    counts: Counter = Counter()
    existing = set(session.scalars(select(EvalCase.name)))
    for r in session.scalars(select(Recording).order_by(Recording.id)):
        if r.excluded_reason or not r.transcript or not r.features:
            continue
        user_turns = [t["message"] for t in r.transcript if t["role"] == "user"]
        for o in r.features["objections"][:1]:
            category = o["category"]
            if counts[category] >= limit_per_category:
                continue
            name = f"opname-{r.call_id}-{category}"
            if name in existing:
                continue
            counts[category] += 1
            session.add(
                EvalCase(
                    name=name,
                    category=category,
                    prospect_turns=user_turns[:4],
                    expectations={
                        "objection": category,
                        "stops_after_opt_out": category == "verzet",
                    },
                    source="opnames",
                )
            )
            added += 1
    session.flush()
    return added
