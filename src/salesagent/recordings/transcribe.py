"""Transcribers behind one interface.

- ``SidecarTranscriber``: reads an existing transcript next to the recording
  (``<name>.json`` or ``<name>.txt`` with lines ``A: ...`` for agent and
  ``K: ...`` for klant). For the demo and for manually transcribed calibration sets.
- ``WhisperTranscriber``: local faster-whisper (optional dependency), per
  channel for stereo files. Self-hosted, so no audio leaves the machine.
"""

import json
import re
from pathlib import Path
from typing import Protocol

from salesagent.recordings.audio import extract_wav


class TranscriptionError(Exception):
    pass


class Transcriber(Protocol):
    name: str

    def transcribe(self, media: Path, audio_info: dict | None, workdir: Path) -> list[dict]: ...


LINE = re.compile(
    r"^\s*(?:\[(?P<t>[\d.]+)\]\s*)?(?P<who>[AK]|AGENT|KLANT)\s*:\s*(?P<text>.+)$", re.I
)


class SidecarTranscriber:
    name = "bestaand transcript"

    def transcribe(self, media: Path, audio_info: dict | None, workdir: Path) -> list[dict]:
        for suffix in (".json", ".txt"):
            sidecar = media.with_suffix(suffix)
            if sidecar.exists():
                break
        else:
            raise TranscriptionError("geen transcript naast de opname gevonden")
        if sidecar.suffix == ".json":
            return json.loads(sidecar.read_text(encoding="utf-8"))
        turns, clock = [], 0.0
        for line in sidecar.read_text(encoding="utf-8").splitlines():
            match = LINE.match(line)
            if not match:
                continue
            role = "agent" if match["who"].upper().startswith("A") else "user"
            clock = float(match["t"]) if match["t"] else clock
            turns.append({"role": role, "message": match["text"].strip(), "t": clock})
            clock += len(match["text"].split()) / 2.5 + 0.7
        return turns


class WhisperTranscriber:
    name = "whisper (lokaal)"

    def __init__(self, model_size: str = "large-v2", agent_channel: int = 0) -> None:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:
            raise TranscriptionError("faster-whisper is niet geïnstalleerd") from exc
        self._model = WhisperModel(model_size)
        self._agent_channel = agent_channel

    def transcribe(self, media: Path, audio_info: dict | None, workdir: Path) -> list[dict]:
        channels = (audio_info or {}).get("channels", 1)
        wavs = extract_wav(media, workdir, channels)
        turns = []
        for index, wav in enumerate(wavs):
            role = (
                ("agent" if index == self._agent_channel else "user")
                if len(wavs) == 2
                else "onbekend"
            )
            segments, _ = self._model.transcribe(str(wav), language="nl", vad_filter=True)
            turns += [{"role": role, "message": s.text.strip(), "t": s.start} for s in segments]
            wav.unlink(missing_ok=True)  # do not keep audio longer than needed
        return sorted(turns, key=lambda t: t["t"])
