"""Speech to text. Two interchangeable engines:

* LocalWhisper : faster-whisper running on your own CPU/GPU. Free, offline, private.
* GroqWhisper  : Groq's hosted Whisper API. Good for small free web hosts that
                 cannot run a speech model themselves.

Both return word-level timestamps, which the styled templates need.
"""

from __future__ import annotations

import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Protocol

from . import subtitles as st
from .config import Config
from .subtitles import Cue, Word

ProgressFn = Callable[[float], None]


class TranscriptionError(RuntimeError):
    """A transcription problem, phrased so it can be shown to the user."""


@dataclass
class Transcript:
    language: str | None
    duration: float
    words: list[Word] = field(default_factory=list)
    segments: list[tuple[int, int, str]] = field(default_factory=list)


class Transcriber(Protocol):
    name: str
    audio_codec: str  # what ffmpeg should extract for this engine

    @property
    def max_audio_bytes(self) -> int | None: ...  # None = no limit

    def transcribe(self, audio_path: Path, language: str | None, progress: ProgressFn | None) -> Transcript: ...


def transcript_to_cues(transcript: Transcript) -> list[Cue]:
    if transcript.words:
        return st.group_words(transcript.words)
    return st.cues_from_segments(transcript.segments)


# --------------------------------------------------------------------------
# Hindi vs Urdu.  Spoken Hindi and Urdu sound almost the same, so Whisper sometimes
# writes a Hindi video in the Urdu (Arabic) script even when "Hindi" is chosen.
# For languages written in Devanagari we therefore forbid every token that contains
# Arabic-script bytes, so the model can only answer in Devanagari (or Latin for
# English words).
_DEVANAGARI_LANGUAGES = {"hi"}


def _byte_decoder() -> dict[str, int]:
    """Whisper's vocabulary stores raw bytes as printable characters (GPT-2 byte-level BPE)."""
    keep = list(range(33, 127)) + list(range(161, 173)) + list(range(174, 256))
    chars = keep[:]
    extra = 0
    for byte in range(256):
        if byte not in keep:
            keep.append(byte)
            chars.append(256 + extra)
            extra += 1
    return {chr(c): b for b, c in zip(keep, chars)}


def arabic_script_token_ids(vocab: dict[str, int]) -> list[int]:
    """Ids of vocabulary tokens that contain UTF-8 bytes of the U+0600-U+07FF blocks (Arabic/Urdu).

    Those blocks are encoded with the lead bytes 0xD8-0xDF. Continuation bytes are always
    0x80-0xBF, so Devanagari, Latin, digits and punctuation are never touched.
    """
    decode = _byte_decoder()
    ids: list[int] = []
    for token, token_id in vocab.items():
        try:
            raw = bytes(decode[ch] for ch in token)
        except KeyError:  # special tokens such as <|endoftext|> are not text
            continue
        if any(0xD8 <= b <= 0xDF for b in raw):
            ids.append(token_id)
    return sorted(ids)


# --------------------------------------------------------------------------
class LocalWhisper:
    name = "local"
    audio_codec = "wav"
    max_audio_bytes = None

    _models: ClassVar[dict[tuple, object]] = {}
    _blocked: ClassVar[dict[tuple, list[int]]] = {}  # Arabic-script token ids per loaded model
    _lock = threading.Lock()

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _model(self):
        cfg = self.cfg
        key = (cfg.whisper_model, cfg.whisper_device, cfg.whisper_compute_type)
        with self._lock:
            if key not in self._models:
                try:
                    from faster_whisper import WhisperModel
                except ImportError:
                    raise TranscriptionError(
                        "faster-whisper is not installed. Run: pip install faster-whisper"
                    ) from None
                try:
                    self._models[key] = WhisperModel(
                        cfg.whisper_model, device=cfg.whisper_device, compute_type=cfg.whisper_compute_type
                    )
                except Exception as exc:
                    raise TranscriptionError(
                        f"Could not load the speech model '{cfg.whisper_model}': {exc}. "
                        "The first run needs an internet connection to download it."
                    ) from exc
            return self._models[key]

    def _suppressed_tokens(self, model, language) -> list[int]:
        """[-1] is faster-whisper's default list; for Hindi we add every Arabic-script token."""
        if language not in _DEVANAGARI_LANGUAGES:
            return [-1]
        key = (self.cfg.whisper_model, self.cfg.whisper_device, self.cfg.whisper_compute_type)
        if key not in self._blocked:
            try:
                self._blocked[key] = arabic_script_token_ids(model.hf_tokenizer.get_vocab())
            except Exception:  # an unusual faster-whisper build: fall back to the normal behaviour
                self._blocked[key] = []
        return [-1, *self._blocked[key]]

    def transcribe(self, audio_path, language, progress=None) -> Transcript:
        model = self._model()
        segments, info = model.transcribe(
            str(audio_path),
            language=language or None,
            word_timestamps=True,
            vad_filter=True,
            beam_size=self.cfg.whisper_beam_size,
            condition_on_previous_text=False,  # stops the "same sentence forever" failure on long audio
            suppress_tokens=self._suppressed_tokens(model, language),
        )
        duration = float(getattr(info, "duration", 0) or 0)
        words: list[Word] = []
        spans: list[tuple[int, int, str]] = []
        for seg in segments:  # a generator: the work happens while we loop
            spans.append((int(seg.start * 1000), int(seg.end * 1000), seg.text.strip()))
            for w in seg.words or []:
                text = w.word.strip()
                if text:
                    start = int(w.start * 1000)
                    words.append(Word(text, start, max(int(w.end * 1000), start)))
            if progress and duration:
                progress(min(1.0, seg.end / duration))
        return Transcript(getattr(info, "language", None), duration, words, spans)


# --------------------------------------------------------------------------
class MockTranscriber:
    """Writes a made-up transcript spread over the length of the audio.

    It does NOT listen to anything. It exists so the tests, the CI pipeline and
    quick demos run without downloading a speech model. Enable it with
    TRANSCRIBER=mock.
    """

    name = "mock"
    audio_codec = "wav"
    max_audio_bytes = None
    SCRIPT = (
        "Welcome to AutoSub Studio. "
        "This sample text was produced by the mock engine, not by a real speech model. "
        "Switch the transcriber to local or groq to caption your own videos. "
        "Every line can be edited, synced and styled with a template."
    )

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def transcribe(self, audio_path, language, progress=None) -> Transcript:
        try:
            with wave.open(str(audio_path), "rb") as wav:
                duration = wav.getnframes() / float(wav.getframerate() or 16000)
        except (wave.Error, EOFError, OSError):
            duration = 10.0
        tokens = self.SCRIPT.split()
        slot = max(0.18, (max(duration, 1.0) - 0.4) / len(tokens))
        words: list[Word] = []
        spans: list[tuple[int, int, str]] = []
        sentence: list[str] = []
        sentence_start = 0
        clock = 0.2
        for token in tokens:
            start = int(clock * 1000)
            end = int((clock + slot * 0.85) * 1000)
            if end > duration * 1000:  # script longer than the clip: stop at the end
                break
            if not sentence:
                sentence_start = start
            words.append(Word(token, start, end))
            sentence.append(token)
            clock += slot
            if token.endswith((".", "?", "!")):
                spans.append((sentence_start, end, " ".join(sentence)))
                sentence = []
                clock += slot * 0.6  # a breath between sentences
        if sentence:
            spans.append((sentence_start, words[-1].end_ms, " ".join(sentence)))
        if progress:
            progress(1.0)
        return Transcript(language or "en", duration, words, spans)


# --------------------------------------------------------------------------
class GroqWhisper:
    name = "groq"
    audio_codec = "flac"
    max_audio_bytes = 24 * 1024 * 1024  # Groq's free tier accepts files up to 25 MB
    URL = "https://api.groq.com/openai/v1/audio/transcriptions"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def transcribe(self, audio_path, language, progress=None) -> Transcript:
        if not self.cfg.groq_api_key:
            raise TranscriptionError("GROQ_API_KEY is not set. Create a free key at console.groq.com.")
        import requests

        audio_path = Path(audio_path)
        form: dict = {
            "model": self.cfg.groq_model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": ["word", "segment"],
            "temperature": "0",
        }
        if language:
            form["language"] = language
        mime = "audio/mpeg" if audio_path.suffix == ".mp3" else "audio/flac"

        if progress:
            progress(0.1)
        response = None
        for attempt in range(3):
            try:
                with open(audio_path, "rb") as fh:
                    response = requests.post(
                        self.URL,
                        headers={"Authorization": f"Bearer {self.cfg.groq_api_key}"},
                        files={"file": (audio_path.name, fh, mime)},
                        data=form,
                        timeout=300,
                    )
            except requests.RequestException as exc:
                if attempt == 2:
                    raise TranscriptionError(f"Could not reach the transcription service: {exc}") from exc
                time.sleep(2 * (attempt + 1))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 2:
                    break
                wait = response.headers.get("retry-after", "")
                time.sleep(min(float(wait), 20) if wait.replace(".", "", 1).isdigit() else 3 * (attempt + 1))
                continue
            break

        assert response is not None
        if response.status_code == 401:
            raise TranscriptionError("The transcription service rejected the API key. Check GROQ_API_KEY.")
        if response.status_code == 429:
            raise TranscriptionError("The free transcription limit was reached. Wait a few minutes and try again.")
        if response.status_code != 200:
            try:
                detail = response.json().get("error", {}).get("message", "")
            except ValueError:
                detail = response.text[:200]
            raise TranscriptionError(f"Transcription failed ({response.status_code}). {detail}".strip())

        body = response.json()
        words = [
            Word(str(w["word"]).strip(), int(float(w["start"]) * 1000), int(float(w["end"]) * 1000))
            for w in body.get("words") or []
            if str(w.get("word", "")).strip()
        ]
        spans = [
            (int(float(s["start"]) * 1000), int(float(s["end"]) * 1000), str(s.get("text", "")).strip())
            for s in body.get("segments") or []
        ]
        if progress:
            progress(1.0)
        return Transcript(body.get("language"), float(body.get("duration") or 0), words, spans)


def build_transcriber(cfg: Config) -> Transcriber:
    if cfg.transcriber == "groq":
        return GroqWhisper(cfg)
    if cfg.transcriber == "local":
        return LocalWhisper(cfg)
    if cfg.transcriber == "mock":
        return MockTranscriber(cfg)
    raise TranscriptionError(f"Unknown TRANSCRIBER '{cfg.transcriber}'. Use 'local', 'groq' or 'mock'.")
