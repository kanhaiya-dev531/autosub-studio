"""Languages offered in the user interfaces (Whisper understands many more; these are the common ones)."""

LANGUAGES: list[tuple[str, str]] = [
    ("auto", "Detect automatically"),
    ("en", "English"), ("hi", "Hindi"), ("bn", "Bengali"), ("ta", "Tamil"), ("te", "Telugu"),
    ("mr", "Marathi"), ("gu", "Gujarati"), ("kn", "Kannada"), ("ml", "Malayalam"), ("pa", "Punjabi"),
    ("ur", "Urdu"), ("ne", "Nepali"),
    ("es", "Spanish"), ("fr", "French"), ("de", "German"), ("pt", "Portuguese"), ("it", "Italian"),
    ("ru", "Russian"), ("ar", "Arabic"), ("tr", "Turkish"), ("id", "Indonesian"),
    ("ja", "Japanese"), ("ko", "Korean"), ("zh", "Chinese"),
]
LANGUAGE_CODES = {code for code, _ in LANGUAGES}
LANGUAGE_NAMES = dict(LANGUAGES)
# Groq's hosted Whisper answers with the English name of the language, local Whisper with the code.
_NAME_TO_CODE = {name.lower(): code for code, name in LANGUAGES if code != "auto"}


def language_label(value: str | None) -> str:
    """Human-readable name for whatever the speech engine reported ('en', 'english', None)."""
    if not value:
        return "Unknown"
    low = value.strip().lower()
    if low in LANGUAGE_NAMES:
        return LANGUAGE_NAMES[low]
    if low in _NAME_TO_CODE:
        return LANGUAGE_NAMES[_NAME_TO_CODE[low]]
    return value.strip().title()
