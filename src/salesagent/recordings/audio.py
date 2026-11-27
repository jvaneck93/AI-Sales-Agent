"""ffprobe/ffmpeg helpers. Video tracks are always discarded."""

import json
import shutil
import subprocess
from pathlib import Path


class AudioError(Exception):
    pass


def tools_available() -> bool:
    return bool(shutil.which("ffprobe") and shutil.which("ffmpeg"))


def probe(path: Path) -> dict:
    if not shutil.which("ffprobe"):
        raise AudioError("ffprobe niet gevonden (installeer ffmpeg)")
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,channels,sample_rate:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise AudioError("ffprobe kon het bestand niet lezen")
    info = json.loads(result.stdout or "{}")
    audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        raise AudioError("geen audiospoor")
    return {
        "channels": int(audio[0].get("channels") or 1),
        "sample_rate": int(audio[0].get("sample_rate") or 0),
        "codec": audio[0].get("codec_name"),
        "has_video": any(s.get("codec_type") == "video" for s in info.get("streams", [])),
        "duration_s": float(info.get("format", {}).get("duration") or 0),
    }


def extract_wav(path: Path, out_dir: Path, channels: int) -> list[Path]:
    """16 kHz mono wav per channel (stereo: agent and prospect apart). No video."""
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for channel in range(channels if channels in (1, 2) else 1):
        out = out_dir / f"{path.stem}_k{channel}.wav"
        filter_args = ["-af", f"pan=mono|c0=c{channel}"] if channels == 2 else ["-ac", "1"]
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(path),
                "-vn",
                *filter_args,
                "-ar",
                "16000",
                str(out),
            ],
            capture_output=True,
            timeout=300,
            check=False,
        )
        if result.returncode != 0:
            raise AudioError("ffmpeg kon de audio niet omzetten")
        outputs.append(out)
    return outputs
