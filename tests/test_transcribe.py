import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from autosub import transcribe as tr
from autosub.transcribe import GroqWhisper, LocalWhisper, TranscriptionError


# ---- local engine against a stand-in for faster-whisper --------------------
class _W:
    def __init__(self, word, start, end):
        self.word, self.start, self.end = word, start, end


class _Seg:
    def __init__(self, start, end, text, words):
        self.start, self.end, self.text, self.words = start, end, text, words


def install_fake_faster_whisper(monkeypatch, calls):
    class WhisperModel:
        def __init__(self, name, device="cpu", compute_type="int8"):
            calls["init"] = (name, device, compute_type)

        def transcribe(self, audio, **kwargs):
            calls["kwargs"] = kwargs
            segs = [_Seg(0.0, 1.5, " Hello brave world.", [_W(" Hello", 0.1, 0.5), _W(" brave", 0.6, 0.9), _W(" world.", 1.0, 1.4)]),
                    _Seg(2.0, 3.0, " Second one", [_W(" Second", 2.0, 2.4), _W(" ", 2.4, 2.5), _W(" one", 2.5, 3.0)])]
            return iter(segs), types.SimpleNamespace(language="en", duration=4.0)

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)


def test_local_whisper_maps_words_and_reports_progress(cfg, monkeypatch):
    LocalWhisper._models.clear()
    calls = {}
    install_fake_faster_whisper(monkeypatch, calls)
    progress = []
    result = LocalWhisper(cfg).transcribe("audio.wav", None, progress.append)
    assert [w.text for w in result.words] == ["Hello", "brave", "world.", "Second", "one"]  # blank token dropped
    assert (result.words[0].start_ms, result.words[0].end_ms) == (100, 500)
    assert result.language == "en" and result.duration == 4.0
    assert progress == [1.5 / 4.0, 3.0 / 4.0]
    assert calls["kwargs"]["word_timestamps"] is True and calls["kwargs"]["vad_filter"] is True
    assert calls["kwargs"]["language"] is None and calls["init"] == (cfg.whisper_model, "cpu", "int8")
    assert calls["kwargs"]["suppress_tokens"] == [-1]  # nothing extra is blocked outside Hindi


def _vocab_from(*texts):
    """A tiny stand-in vocabulary stored the way Whisper stores it: bytes as printable characters."""
    from autosub.transcribe import _byte_decoder

    to_char = {b: c for c, b in _byte_decoder().items()}
    vocab = {"<|endoftext|>": 0}
    for text in texts:
        for byte in text.encode("utf-8"):
            vocab.setdefault(to_char[byte], len(vocab))  # one token per byte is enough for the test
    return vocab


def test_arabic_script_tokens_are_found_but_hindi_and_english_are_not():
    from autosub.transcribe import _byte_decoder, arabic_script_token_ids

    vocab = _vocab_from("یہ رہی ایک ویڈیو", "यह एक वीडियो है।", "hello, world 123!")
    blocked = arabic_script_token_ids(vocab)
    decode = _byte_decoder()
    raw_of = {token_id: bytes(decode[c] for c in token) for token, token_id in vocab.items() if token != "<|endoftext|>"}
    assert blocked  # the Urdu lead bytes (0xD8-0xDF) are blocked
    assert all(any(0xD8 <= b <= 0xDF for b in raw_of[i]) for i in blocked)
    assert 0 not in blocked  # special tokens are left alone
    # every byte of the Hindi / English text is still allowed
    allowed = {raw_of[i] for i in raw_of if i not in blocked}
    for byte in "यह एक वीडियो है। hello, world 123!".encode():
        assert bytes([byte]) in allowed


def test_hindi_blocks_urdu_script_tokens(cfg, monkeypatch):
    LocalWhisper._models.clear()
    LocalWhisper._blocked.clear()
    calls = {}
    install_fake_faster_whisper(monkeypatch, calls)
    vocab = _vocab_from("یہ رہی", "यह एक")
    sys.modules["faster_whisper"].WhisperModel.hf_tokenizer = types.SimpleNamespace(get_vocab=lambda: vocab)
    LocalWhisper(cfg).transcribe("audio.wav", "hi")
    suppressed = calls["kwargs"]["suppress_tokens"]
    assert suppressed[0] == -1 and len(suppressed) > 1
    # Urdu is still allowed when the user really chooses Urdu
    LocalWhisper(cfg).transcribe("audio.wav", "ur")
    assert calls["kwargs"]["suppress_tokens"] == [-1]


def test_hindi_still_works_if_the_tokenizer_cannot_be_read(cfg, monkeypatch):
    LocalWhisper._models.clear()
    LocalWhisper._blocked.clear()
    calls = {}
    install_fake_faster_whisper(monkeypatch, calls)  # this stand-in has no hf_tokenizer
    LocalWhisper(cfg).transcribe("audio.wav", "hi")
    assert calls["kwargs"]["suppress_tokens"] == [-1]


def test_local_whisper_explains_missing_package(cfg, monkeypatch):
    LocalWhisper._models.clear()
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(TranscriptionError, match="pip install faster-whisper"):
        LocalWhisper(cfg).transcribe("a.wav", None)


def test_model_is_loaded_once(cfg, monkeypatch):
    LocalWhisper._models.clear()
    calls = {"n": 0}
    install_fake_faster_whisper(monkeypatch, calls)
    sys.modules["faster_whisper"].WhisperModel.__init__ = lambda self, *a, **k: calls.__setitem__("n", calls["n"] + 1)
    LocalWhisper._models.clear()
    engine = LocalWhisper(cfg)
    assert engine._model() is engine._model() and calls["n"] == 1


def test_transcript_to_cues_uses_words_then_segments():
    from autosub.subtitles import Word

    words = [Word("one", 0, 300), Word("two", 400, 700)]
    assert tr.transcript_to_cues(tr.Transcript("en", 1, words, [])) [0].text == "one two"
    fallback = tr.transcript_to_cues(tr.Transcript("en", 1, [], [(0, 2000, "just segment text here")]))
    assert fallback[0].text == "just segment text here" and fallback[0].words


# ---- Groq engine against a local fake server --------------------------------
class _Handler(BaseHTTPRequestHandler):
    script: ClassVar[list] = []
    seen: ClassVar[list] = []

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length)
        type(self).seen.append((self.headers.get("authorization"), body))
        status, payload, headers = type(self).script.pop(0)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def groq_server(monkeypatch):
    _Handler.script, _Handler.seen = [], []
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(GroqWhisper, "URL", f"http://127.0.0.1:{server.server_port}/v1/audio/transcriptions")
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    yield _Handler
    server.shutdown()


def groq_cfg(cfg, key="gsk_test"):
    from dataclasses import replace

    return replace(cfg, transcriber="groq", groq_api_key=key)


OK = {"language": "english", "duration": 3.0,
      "words": [{"word": "Hello", "start": 0.1, "end": 0.5}, {"word": "there", "start": 0.6, "end": 1.0}],
      "segments": [{"start": 0.1, "end": 1.0, "text": " Hello there"}]}


def test_groq_success_sends_key_and_word_granularity(cfg, groq_server, tmp_path):
    audio = tmp_path / "a.flac"
    audio.write_bytes(b"fLaC fake")
    groq_server.script = [(200, OK, {})]
    result = GroqWhisper(groq_cfg(cfg)).transcribe(audio, "en", None)
    auth, body = groq_server.seen[0]
    assert auth == "Bearer gsk_test"
    for needle in (b"verbose_json", b'name="timestamp_granularities[]"', b"word", b"whisper-large-v3-turbo", b'name="language"'):
        assert needle in body
    assert [(w.text, w.start_ms, w.end_ms) for w in result.words] == [("Hello", 100, 500), ("there", 600, 1000)]
    assert result.duration == 3.0


def test_groq_retries_rate_limits_then_succeeds(cfg, groq_server, tmp_path):
    audio = tmp_path / "a.flac"
    audio.write_bytes(b"x")
    groq_server.script = [(429, {"error": {"message": "slow down"}}, {"retry-after": "1"}), (200, OK, {})]
    assert GroqWhisper(groq_cfg(cfg)).transcribe(audio, None, None).words
    assert len(groq_server.seen) == 2


@pytest.mark.parametrize("status,needle", [(401, "API key"), (429, "limit"), (400, "Transcription failed")])
def test_groq_errors_are_translated(cfg, groq_server, tmp_path, status, needle):
    audio = tmp_path / "a.flac"
    audio.write_bytes(b"x")
    groq_server.script = [(status, {"error": {"message": "nope"}}, {})] * 3
    with pytest.raises(TranscriptionError, match=needle):
        GroqWhisper(groq_cfg(cfg)).transcribe(audio, None, None)


def test_groq_needs_a_key(cfg, tmp_path):
    with pytest.raises(TranscriptionError, match="GROQ_API_KEY"):
        GroqWhisper(groq_cfg(cfg, key="")).transcribe(tmp_path / "a.flac", None, None)


def test_build_transcriber_picks_engine(cfg):
    from dataclasses import replace

    assert isinstance(tr.build_transcriber(cfg), tr.MockTranscriber)
    assert isinstance(tr.build_transcriber(replace(cfg, transcriber="local")), LocalWhisper)
    assert isinstance(tr.build_transcriber(replace(cfg, transcriber="groq")), GroqWhisper)
    with pytest.raises(TranscriptionError):
        tr.build_transcriber(replace(cfg, transcriber="magic"))
