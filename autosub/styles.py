"""Subtitle templates: turn cues into styled ASS subtitles that FFmpeg can burn in.

ASS (Advanced SubStation Alpha) is the format video editors and players use for
styled captions. Each template below builds a complete .ass document.

Colours are written as normal RGB hex here; `_c` / `_i` convert them to the
BGR order that ASS expects.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import subtitles as st
from .subtitles import Cue, ms_to_ass

WHITE = "FFFFFF"
BLACK = "000000"
CAPTION_YELLOW = "FFD21F"
NEON_PINK = "FF3CAC"
NIGHT_PLUM = "1A0B3B"


@dataclass(frozen=True)
class Template:
    key: str
    name: str
    tagline: str
    max_words: int | None  # None = keep the cue as it is
    max_chars: int


TEMPLATES: dict[str, Template] = {
    "classic": Template(
        "classic", "Classic", "Clean white text with a soft outline, like TV captions.", None, 42
    ),
    "pop": Template(
        "pop", "Pop", "Big bold words that light up one at a time.", 3, 18
    ),
    "neon": Template(
        "neon", "Neon", "Glowing text that fills with colour as it is spoken.", 6, 30
    ),
}


def get_template(key: str) -> Template:
    try:
        return TEMPLATES[key]
    except KeyError:
        raise ValueError(f"Unknown template '{key}'. Choose one of: {', '.join(TEMPLATES)}") from None


# --------------------------------------------------------------------------
# ASS building blocks
# --------------------------------------------------------------------------
def _c(rgb: str, alpha: int = 0) -> str:
    """Colour for a Style line: &HAABBGGRR (alpha 0 = opaque, 255 = invisible)."""
    r, g, b = rgb[0:2], rgb[2:4], rgb[4:6]
    return f"&H{alpha:02X}{b}{g}{r}"


def _i(rgb: str) -> str:
    """Colour for an inline override tag: &HBBGGRR&."""
    r, g, b = rgb[0:2], rgb[2:4], rgb[4:6]
    return f"&H{b}{g}{r}&"


def _esc(text: str) -> str:
    """Make plain text safe for an ASS Dialogue line."""
    text = text.replace("\\", "").replace("{", "(").replace("}", ")")
    return text.replace("\r", "").replace("\n", r"\N")


def _base(w: int, h: int) -> float:
    """Reference length that font sizes are measured against.

    Landscape video: its height. Portrait video (phones): capped by the width, so
    captions on a 9:16 clip are not enormous and still fit a few words per line.
    """
    return float(h) if w >= h else min(float(h), w * 1.5)


def _num(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".") if value % 1 else str(int(value))


def _style(
    name: str,
    font: str,
    size: int,
    primary: str,
    secondary: str,
    outline_colour: str,
    back: str,
    outline: float,
    shadow: float,
    margin_v: int,
    margin_lr: int,
    bold: bool = True,
) -> str:
    font = font.replace(",", " ")
    return (
        f"Style: {name},{font},{size},{primary},{secondary},{outline_colour},{back},"
        f"{-1 if bold else 0},0,0,0,100,100,0,0,1,{_num(outline)},{_num(shadow)},2,"
        f"{margin_lr},{margin_lr},{margin_v},1"
    )


def _header(width: int, height: int, styles: list[str]) -> list[str]:
    return [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        *styles,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]


def _dialogue(layer: int, start_ms: int, end_ms: int, style: str, text: str) -> str:
    return f"Dialogue: {layer},{ms_to_ass(start_ms)},{ms_to_ass(end_ms)},{style},,0,0,0,,{text}"


# --------------------------------------------------------------------------
# The three templates
# --------------------------------------------------------------------------
def _classic(cues: list[Cue], w: int, h: int, font: str) -> tuple[list[str], list[str]]:
    b = _base(w, h)
    style = _style(
        "Classic", font, round(b * 0.060), _c(WHITE), _c(WHITE), _c(BLACK), _c(BLACK, 0x70),
        outline=b * 0.0062, shadow=b * 0.0045, margin_v=round(h * 0.07), margin_lr=round(w * 0.07),
    )
    events = [
        _dialogue(0, c.start_ms, c.end_ms, "Classic", r"{\fad(120,120)}" + _esc(c.text))
        for c in cues
    ]
    return [style], events


def _pop(cues: list[Cue], w: int, h: int, font: str) -> tuple[list[str], list[str]]:
    b = _base(w, h)
    style = _style(
        "Pop", font, round(b * 0.100), _c(WHITE), _c(WHITE), _c(BLACK), _c(BLACK, 0x40),
        outline=b * 0.014, shadow=b * 0.006, margin_v=round(h * 0.2), margin_lr=round(w * 0.08),
    )
    events: list[str] = []
    for cue in cues:
        words = cue.words or st.redistribute_words(cue.text, cue.start_ms, cue.end_ms)
        for i in range(len(words)):
            start = cue.start_ms if i == 0 else words[i].start_ms
            end = words[i + 1].start_ms if i + 1 < len(words) else cue.end_ms
            if end <= start:
                continue
            parts = []
            for j, word in enumerate(words):
                token = _esc(word.text.upper())
                if j == i:
                    token = (
                        r"{\c" + _i(CAPTION_YELLOW) + r"\fscx122\fscy122\t(0,110,\fscx100\fscy100)}"
                        + token + r"{\r}"
                    )
                parts.append(token)
            events.append(_dialogue(0, start, end, "Pop", " ".join(parts)))
    return [style], events


def _neon(cues: list[Cue], w: int, h: int, font: str) -> tuple[list[str], list[str]]:
    b = _base(w, h)
    size = round(b * 0.072)
    margin_v, margin_lr = round(h * 0.12), round(w * 0.08)
    glow = _style(
        "NeonGlow", font, size, _c(NEON_PINK, 0xFF), _c(NEON_PINK, 0xFF), _c(NEON_PINK, 0x40), _c(BLACK, 0xFF),
        outline=b * 0.02, shadow=0, margin_v=margin_v, margin_lr=margin_lr,
    )
    main = _style(
        "Neon", font, size, _c(NEON_PINK), _c(WHITE), _c(NIGHT_PLUM), _c(BLACK, 0xFF),
        outline=b * 0.0052, shadow=0, margin_v=margin_v, margin_lr=margin_lr,
    )
    blur = _num(b * 0.013)
    events: list[str] = []
    for cue in cues:
        words = cue.words or st.redistribute_words(cue.text, cue.start_ms, cue.end_ms)
        plain = _esc(" ".join(wd.text for wd in words))
        events.append(_dialogue(0, cue.start_ms, cue.end_ms, "NeonGlow", r"{\blur" + blur + r"\fad(80,120)}" + plain))

        cursor = cue.start_ms
        pieces = [r"{\fad(80,120)}"]
        for k, word in enumerate(words):
            delay_cs = round((word.start_ms - cursor) / 10)
            if delay_cs > 0:
                pieces.append(r"{\k" + str(delay_cs) + "}")
            dur_cs = max(1, round((word.end_ms - max(word.start_ms, cursor)) / 10))
            tail = " " if k + 1 < len(words) else ""
            pieces.append(r"{\kf" + str(dur_cs) + "}" + _esc(word.text) + tail)
            cursor = max(cursor, word.end_ms)
        events.append(_dialogue(1, cue.start_ms, cue.end_ms, "Neon", "".join(pieces)))
    return [glow, main], events


_BUILDERS = {"classic": _classic, "pop": _pop, "neon": _neon}


def prepare_cues(cues: list[Cue], template_key: str) -> list[Cue]:
    """Clean the cues and, for the short-line templates, split long lines."""
    template = get_template(template_key)
    cleaned = st.normalize(cues)
    if template.max_words is None:
        return cleaned
    pieces: list[Cue] = []
    for cue in cleaned:
        pieces.extend(st.rechunk(cue, template.max_words, template.max_chars))
    return pieces


def build_ass(cues: list[Cue], template_key: str, width: int, height: int, font: str = "Poppins") -> str:
    """Return the full text of an .ass file for `cues` drawn with one template."""
    prepared = prepare_cues(cues, template_key)
    styles, events = _BUILDERS[template_key](prepared, width, height, font)
    return "\n".join(_header(width, height, styles) + events) + "\n"
