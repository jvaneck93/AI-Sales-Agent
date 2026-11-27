"""Synthetic CATI export + transcripts to try the pipeline. All fictional.

Built so the analysis has something to find: an opening with a fleet reason
and asking through on objections work better, and one agent labels
optimistically (the guardrail should catch that agent).
"""

import csv
import random
import shutil
import subprocess
from pathlib import Path

AGENTS = [
    f"{first} Proefmans"
    for first in (
        "Sanne",
        "Mark",
        "Lotte",
        "Bram",
        "Iris",
        "Daan",
        "Noor",
        "Ruben",
        "Femke",
        "Joost",
    )
]
OPENINGS = {
    "aanleiding_wagenpark": "Goedemiddag, u spreekt met {agent} namens Voltera. Ik zie dat u "
    "een aantal bestelbussen uit 2018 rijdt; speelt vervanging bij u?",
    "pitch": "Goedemiddag, met {agent} van Voltera, wij zijn specialist in bedrijfswagens en "
    "hebben een mooie actie.",
    "vraag": "Goedemiddag, met {agent} namens Voltera. Hoe gaat het bij u met de planning?",
}
OBJECTIONS = {
    "geen_tijd": (
        "Ik heb nu echt geen tijd.",
        "Wanneer kan ik u beter terugbellen?",
        "Natuurlijk, ik begrijp het, fijne dag.",
    ),
    "contract_loopt": (
        "Ons contract loopt nog tot 2027.",
        "Wanneer loopt het af? Dan kunnen we vooraf vergelijken?",
        "Oké, dan is het niet interessant, prima.",
    ),
    "stuur_mail": (
        "Stuur maar een mailtje naar info@demo.example.",
        "Dat kan, maar wat is voor u belangrijk in het wagenpark?",
        "Prima, ik stuur een mail, fijne dag.",
    ),
    "geen_beslisser": (
        "Daar ga ik niet over, dat doet mijn collega Jansen.",
        "Wie gaat er over het wagenpark en wanneer is die bereikbaar?",
        "Oké, jammer, fijne dag.",
    ),
    "tevreden_huidige": (
        "We zijn tevreden met onze vaste dealer.",
        "Goed om te horen. Wanneer staat het volgende vervangmoment gepland?",
        "Ik begrijp het, dan houden we het hierbij.",
    ),
}


def generate(target: Path, n: int = 600, seed: int = 11, mp4: bool = True) -> Path:
    rng = random.Random(seed)
    recordings = target / "opnames"
    recordings.mkdir(parents=True, exist_ok=True)
    export = target / "cati_export.csv"
    rows = []
    for i in range(n):
        call_id = f"C-{10000 + i}"
        agent = rng.choice(AGENTS)
        roll = rng.random()
        if roll < 0.25:
            code = rng.choice(["VM", "NB"])
            rows.append(
                [
                    call_id,
                    f"{call_id}.mp4",
                    f"{90000000 + rng.randrange(160)}",
                    agent,
                    f"2025-0{rng.randint(1, 9)}-1{rng.randint(0, 9)} 10:{rng.randint(10, 59)}",
                    rng.randint(5, 30),
                    code,
                    "Voltera 2025-1",
                    "",
                ]
            )
            continue
        opening = rng.choice(list(OPENINGS))
        category = rng.choice(list(OBJECTIONS))
        asks_through = rng.random() < 0.5
        base = (
            0.10
            + (0.10 if opening == "aanleiding_wagenpark" else 0)
            + (0.08 if asks_through else -0.04)
        )
        positive = rng.random() < base
        appointment = positive and rng.random() < 0.5
        if agent == AGENTS[0] and rng.random() < 0.35:  # optimistic labeler
            positive, appointment = True, False
        code = (
            "AFS" if appointment else "WARM" if positive else rng.choice(["GI", "GI", "TB", "ONG"])
        )
        if rng.random() < 0.01:
            code = "XX9"  # unknown code, must be reported
        objection, ask, accept = OBJECTIONS[category]
        lines = [
            f"[0] A: {OPENINGS[opening].format(agent=agent.split()[0])}",
            "[6] K: Ja, met Pieter de Vries, waar gaat het over?"
            if rng.random() < 0.3
            else "[6] K: Ja, dat ben ik.",
            f"[9] K: {objection}",
            f"[12] A: {ask if asks_through else accept}",
        ]
        if asks_through and positive:
            lines += [
                "[18] K: Ja, dit jaar moeten er een paar bussen vervangen worden.",
                "[23] A: Zal de accountmanager dinsdag om tien uur langskomen?",
            ]
            lines.append(
                "[28] K: Dinsdag is goed, bel me op 06-12345678."
                if appointment
                else "[28] K: Laat hem eerst maar bellen."
            )
        elif asks_through:
            lines += ["[18] K: Nee, dat speelt nu echt niet.", "[22] A: Helder, fijne dag."]
        duration = 30 + 6 * len(lines) + rng.randint(0, 40)
        (recordings / f"{call_id}.txt").write_text("\n".join(lines), encoding="utf-8")
        rows.append(
            [
                call_id,
                f"{call_id}.mp4",
                f"{90000000 + rng.randrange(160)}",
                agent,
                f"2025-0{rng.randint(1, 9)}-1{rng.randint(0, 9)} 11:{rng.randint(10, 59)}",
                duration,
                code,
                "Voltera 2025-1",
                "",
            ]
        )
    with export.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(
            [
                "call_id",
                "bestand",
                "kvk",
                "agent",
                "start",
                "duur_sec",
                "dispositie",
                "campagne",
                "notitie",
            ]
        )
        writer.writerows(rows)
    if mp4 and shutil.which("ffmpeg"):
        # A few tiny stereo mp4 files with a video track, to show probing.
        for row in [r for r in rows if r[6] not in ("VM", "NB")][:3]:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=f=440:d=2",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=f=660:d=2",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=64x64:d=2",
                    "-filter_complex",
                    "[0:a][1:a]amerge=inputs=2[a]",
                    "-map",
                    "2:v",
                    "-map",
                    "[a]",
                    "-ar",
                    "8000",
                    "-shortest",
                    str(recordings / row[1]),
                ],
                capture_output=True,
                timeout=60,
                check=False,
            )
    return export
