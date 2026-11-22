"""Shared speech-to-text (faster-whisper) and text-to-speech (edge-tts / macOS say) utilities."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
import warnings
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .storage import get_data_dir

logger = logging.getLogger(__name__)

DEFAULT_VOICE_ZH = "zh-CN-XiaoxiaoNeural"
DEFAULT_VOICE_EN = "en-US-JennyNeural"
MACOS_VOICE_ZH = "Tingting"
MACOS_VOICE_EN = "Samantha"

_whisper_models: Dict[str, Any] = {}


def detect_spoken_language(text: str) -> str:
    """Return 'zh' when CJK ideographs appear in text, otherwise 'en'."""
    for ch in text or "":
        if "\u4e00" <= ch <= "\u9fff":
            return "zh"
    return "en"


def distill_spoken_briefing(text: str, max_chars: int = 900) -> str:
    """Strip Markdown, code blocks, URLs, and paths to produce clean TTS narration."""
    raw = (text or "").strip()
    if not raw:
        return ""

    clean = re.sub(r"```[\s\S]*?```", " Code changes updated. ", raw)
    clean = re.sub(r"(?:^\|[^\n]+\|(?:\r?\n|$)){2,}", " ", clean, flags=re.MULTILINE)
    clean = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", clean)
    clean = re.sub(r"https?://\S+|file://\S+", "", clean)
    clean = re.sub(r"(?:/[a-zA-Z0-9_.-]+)+/([a-zA-Z0-9_.-]+)", r"\1", clean)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"^#{1,6}\s+", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"^[\*\-\+]\s+", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"^\d+\.\s+", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"[`*_#~|<>{}]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()

    if len(clean) > max_chars:
        terminators = ".!?\n;\uff01\uff1f\u3002\uff1b"
        cut_idx = -1
        min_cut = max(120, max_chars - 240)
        for idx in range(max_chars - 1, min_cut - 1, -1):
            if clean[idx] in terminators:
                cut_idx = idx + 1
                break
        if cut_idx > 0:
            clean = clean[:cut_idx].strip()
        else:
            last_sp = clean[:max_chars].rfind(" ")
            clean = (clean[:last_sp] if last_sp > min_cut else clean[:max_chars]).strip()

    if clean and clean[-1] not in ".!?\u3002\uff01\uff1f":
        clean += "." if detect_spoken_language(clean) == "en" else "\u3002"
    return clean


def _resolve_ffmpeg_bin() -> Optional[str]:
    """Locate ffmpeg executable on PATH or standard macOS/Linux locations."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if Path(candidate).is_file():
            return candidate
    return None


def _get_whisper_model_sync(model_size: str = "base", device: str = "cpu", compute_type: str = "int8") -> Any:
    """Lazily initialize and cache a faster-whisper WhisperModel instance."""
    key = f"{model_size}_{device}_{compute_type}"
    if key not in _whisper_models:
        from faster_whisper import WhisperModel

        logger.info("Loading faster-whisper model '%s' (device=%s, compute=%s)...", model_size, device, compute_type)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=RuntimeWarning)
            _whisper_models[key] = WhisperModel(model_size, device=device, compute_type=compute_type)
    return _whisper_models[key]


def _transcribe_sync(
    audio_path: str,
    model_size: str = "base",
    beam_size: int = 5,
    language: Optional[str] = None,
) -> Tuple[str, str]:
    """Run synchronous faster-whisper transcription in worker thread."""
    if not os.path.isfile(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    model = _get_whisper_model_sync(model_size=model_size)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=RuntimeWarning)
        segments, info = model.transcribe(
            audio_path,
            beam_size=beam_size,
            language=language,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        parts = [seg.text.strip() for seg in segments if getattr(seg, "text", "").strip()]
        transcript = " ".join(parts).strip()
        detected = getattr(info, "language", None) or "en"
    return transcript, detected


async def transcribe_voice(
    audio_path: str | Path,
    model_size: Optional[str] = None,
    beam_size: int = 5,
    language: Optional[str] = None,
) -> Tuple[str, str]:
    """Asynchronously transcribe an audio file without blocking the event loop."""
    resolved = str(Path(audio_path).resolve())
    eff_size = (model_size or os.environ.get("WHISPER_MODEL_SIZE", "base") or "base").strip()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _transcribe_sync, resolved, eff_size, beam_size, language)


def _default_voice_cache_dir() -> Path:
    cache_dir = Path(get_data_dir()) / "runtime" / "voice"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


async def _synthesize_audio(
    text: str,
    target_format: str = "ogg",
    lang: Optional[str] = None,
    output_path: Optional[Path] = None,
    voice: Optional[str] = None,
) -> Optional[Path]:
    """Synthesize spoken audio via edge-tts with macOS say + ffmpeg offline fallback."""
    clean_text = distill_spoken_briefing(text)
    if not clean_text:
        return None

    eff_lang = (lang or detect_spoken_language(clean_text)).lower()[:2]
    is_en = eff_lang.startswith("en")
    if voice:
        voice_name = voice
    elif is_en:
        voice_name = os.environ.get("TTS_VOICE_EN", DEFAULT_VOICE_EN) or DEFAULT_VOICE_EN
    else:
        voice_name = os.environ.get("TTS_VOICE_ZH", DEFAULT_VOICE_ZH) or DEFAULT_VOICE_ZH

    ext = ".mp3" if target_format == "mp3" else ".ogg"
    if output_path is None:
        ts_str = time.strftime("%Y%m%d_%H%M%S")
        tag = f"{os.getpid()}_{abs(hash(clean_text)) % 10000}"
        output_path = _default_voice_cache_dir() / f"voice_{ts_str}_{tag}{ext}"
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg_bin = _resolve_ffmpeg_bin()

    # 1. Primary engine: Edge-TTS neural voices
    try:
        import edge_tts

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            temp_mp3 = Path(tf.name)
        try:
            communicate = edge_tts.Communicate(clean_text, voice_name)
            await communicate.save(str(temp_mp3))
            if temp_mp3.is_file() and temp_mp3.stat().st_size > 0:
                if target_format == "mp3":
                    shutil.copyfile(temp_mp3, output_path)
                elif ffmpeg_bin:
                    proc = await asyncio.create_subprocess_exec(
                        ffmpeg_bin,
                        "-y",
                        "-i",
                        str(temp_mp3),
                        "-c:a",
                        "libopus",
                        "-b:a",
                        "32k",
                        "-ar",
                        "48000",
                        str(output_path),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.communicate()
                else:
                    shutil.copyfile(temp_mp3, output_path)

                if output_path.is_file() and output_path.stat().st_size > 0:
                    return output_path
        finally:
            temp_mp3.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Edge-TTS failed (%s); attempting offline macOS fallback.", exc)

    # 2. Offline fallback: macOS native 'say' + ffmpeg
    say_bin = shutil.which("say") or ("/usr/bin/say" if Path("/usr/bin/say").is_file() else None)
    if say_bin and ffmpeg_bin:
        temp_aiff: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tf:
                temp_aiff = Path(tf.name)
            macos_voice = MACOS_VOICE_EN if is_en else MACOS_VOICE_ZH
            say_proc = await asyncio.create_subprocess_exec(
                say_bin,
                "-v",
                macos_voice,
                clean_text,
                "-o",
                str(temp_aiff),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await say_proc.communicate()

            if temp_aiff.is_file() and temp_aiff.stat().st_size > 0:
                codec_args = (
                    ["-c:a", "libmp3lame", "-q:a", "4"]
                    if target_format == "mp3"
                    else ["-c:a", "libopus", "-b:a", "32k", "-ar", "48000"]
                )
                ff_proc = await asyncio.create_subprocess_exec(
                    ffmpeg_bin,
                    "-y",
                    "-i",
                    str(temp_aiff),
                    *codec_args,
                    str(output_path),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await ff_proc.communicate()
                if output_path.is_file() and output_path.stat().st_size > 0:
                    return output_path
        except Exception as fallback_exc:
            logger.error("macOS fallback TTS failed: %s", fallback_exc)
        finally:
            if temp_aiff is not None:
                temp_aiff.unlink(missing_ok=True)

    return None


async def synthesize_voice_ogg(
    text: str,
    lang: Optional[str] = None,
    output_path: Optional[Path] = None,
    voice: Optional[str] = None,
) -> Optional[Path]:
    """Synthesize Telegram-compatible OGG Opus voice note."""
    return await _synthesize_audio(text=text, target_format="ogg", lang=lang, output_path=output_path, voice=voice)


async def synthesize_voice_mp3(
    text: str,
    lang: Optional[str] = None,
    output_path: Optional[Path] = None,
    voice: Optional[str] = None,
) -> Optional[Path]:
    """Synthesize browser-compatible MP3 speech audio."""
    return await _synthesize_audio(text=text, target_format="mp3", lang=lang, output_path=output_path, voice=voice)


def cleanup_old_voice_files(cache_dir: Optional[Path] = None, max_age_days: int = 1) -> int:
    """Prune generated voice files older than max_age_days."""
    target = cache_dir or _default_voice_cache_dir()
    if not target.is_dir():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for pattern in ("voice_*.ogg", "voice_*.oga", "voice_*.mp3", "voice_*.wav"):
        for fp in target.glob(pattern):
            try:
                if fp.stat().st_mtime < cutoff:
                    fp.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                pass
    return removed


if __name__ == "__main__":
    assert detect_spoken_language("Project status check") == "en"
    assert detect_spoken_language("\u4f60\u597d") == "zh"
    distilled = distill_spoken_briefing("### Progress\n- Updated `/Users/demo/app.py` and **GO-5**.\n```js\nconst x=1;\n```")
    assert "###" not in distilled and "```" not in distilled and "app.py" in distilled
    print("orchestrator.voice self-test passed!")
