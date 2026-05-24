from __future__ import annotations

import argparse
import asyncio
import os
import re
import unicodedata
from pathlib import Path

import aiohttp

from livekit import rtc
from livekit.plugins import cartesia


PHRASES: list[dict[str, object]] = [
    {"text": "Alpha bravo charlie delta."},
    {"text": "Zwingli."},
    {"text": "Zwingli strip alpha bravo charlie."},
    # Render with an explicit pause after "Zwingli".
    {
        "text": "Zwingli, strip alpha bravo charlie.",
        "pause_after": "Zwingli",
        "pause_ms": 500,
        "rest": "strip alpha bravo charlie.",
    },
    {"text": "Zwingly strip alpha bravo charlie."},
    {"text": "Please ignore this; zwingli strip alpha bravo charlie."},
    {"text": "Zwingli banana alpha bravo charlie."},
    {"text": "Zwingli rewrite alpha bravo charlie."},
    {"text": "Zwingli bash list files in my home directory."},
    {"text": "Zwingli email to Kelly subject Lunch body Are we still on for noon."},
    {"text": "Zwingli execute echo hello world."},
    {"text": "Zwingli strip one two three four five."},
]


_PUNCT_TOKENS = {
    ",": "comma",
    ";": "semicolon",
    ":": "colon",
}


def _slugify_for_filename(text: str, *, max_len: int = 180) -> str:
    normalized = unicodedata.normalize("NFKD", text).strip().lower()
    parts: list[str] = []

    for ch in normalized:
        if ch.isalnum():
            parts.append(ch)
            continue

        if ch.isspace():
            parts.append("_")
            continue

        token = _PUNCT_TOKENS.get(ch)
        if token:
            parts.append(f"_{token}_")
            continue

        # Drop other punctuation (quotes, periods, etc.)
        parts.append("_")

    slug = re.sub(r"_+", "_", "".join(parts)).strip("_")
    if not slug:
        slug = "tts"
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("_")
    return slug


async def _synthesize_frame(tts: cartesia.TTS, text: str) -> rtc.AudioFrame:
    async with tts.synthesize(text) as stream:
        return await stream.collect()


async def _render_phrase(tts: cartesia.TTS, spec: dict[str, object]) -> rtc.AudioFrame:
    text = str(spec.get("text") or "").strip()
    if not text:
        raise ValueError("phrase text is empty")

    pause_after = str(spec.get("pause_after") or "").strip()
    rest = str(spec.get("rest") or "").strip()
    pause_ms_raw = spec.get("pause_ms")
    pause_ms = int(pause_ms_raw) if isinstance(pause_ms_raw, int) else None

    if pause_after and rest and pause_ms is not None:
        first = await _synthesize_frame(tts, pause_after)
        second = await _synthesize_frame(tts, rest)
        silence = rtc.AudioFrame.create(
            sample_rate=tts.sample_rate,
            num_channels=tts.num_channels,
            samples_per_channel=max(1, int(tts.sample_rate * (pause_ms / 1000.0))),
        )
        return rtc.combine_audio_frames([first, silence, second])

    return await _synthesize_frame(tts, text)


async def _async_main() -> int:
    parser = argparse.ArgumentParser(description="Render canned phrases to WAV via LiveKit TTS.")
    parser.add_argument(
        "--out-dir",
        default="deploy/livekit/tts_wavs",
        help="Output directory for generated .wav files",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing .wav files",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    model = (os.getenv("LIVEKIT_TTS_MODEL", "sonic-2") or "sonic-2").strip()
    voice = os.getenv("LIVEKIT_TTS_VOICE", "").strip()

    tts_kwargs: dict[str, object] = {"model": model}
    if voice:
        tts_kwargs["voice"] = voice

    async with aiohttp.ClientSession() as http_session:
        tts = cartesia.TTS(http_session=http_session, **tts_kwargs)
        try:
            for spec in PHRASES:
                text = str(spec.get("text") or "").strip()
                if not text:
                    continue
                stem = _slugify_for_filename(text)
                out_path = out_dir / f"{stem}.wav"
                if out_path.exists() and not args.overwrite:
                    print(f"skip (exists): {out_path}")
                    continue

                frame = await _render_phrase(tts, spec)
                out_path.write_bytes(frame.to_wav_bytes())
                print(f"wrote: {out_path}")
        finally:
            await tts.aclose()

    return 0


def main() -> int:
    return asyncio.run(_async_main())


if __name__ == "__main__":
    raise SystemExit(main())

