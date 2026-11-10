from __future__ import annotations

import re
import unicodedata
from collections import Counter

from .models import LanguageAssessment, SiteObservation

MAX_LANGUAGE_CHARACTERS = 20_000
WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")

ENGLISH_WORDS = frozenset(
    {
        "about",
        "and",
        "are",
        "be",
        "brand",
        "business",
        "contact",
        "for",
        "from",
        "get",
        "in",
        "is",
        "it",
        "more",
        "of",
        "our",
        "services",
        "shop",
        "that",
        "the",
        "this",
        "to",
        "we",
        "with",
        "you",
        "your",
    }
)

LATIN_LANGUAGE_WORDS: dict[str, frozenset[str]] = {
    "de": frozenset(
        {"aber", "auch", "der", "die", "für", "ist", "mit", "nicht", "und", "von", "wir"}
    ),
    "es": frozenset(
        {"con", "del", "el", "en", "es", "los", "para", "por", "que", "una", "y"}
    ),
    "fr": frozenset(
        {"avec", "dans", "des", "est", "les", "nous", "pour", "que", "une", "vous", "et"}
    ),
    "it": frozenset(
        {"che", "con", "dei", "del", "della", "il", "la", "per", "sono", "una", "e"}
    ),
    "nl": frozenset(
        {"de", "een", "en", "het", "met", "niet", "ons", "voor", "van", "we", "zijn"}
    ),
    "pt": frozenset(
        {"com", "da", "de", "do", "em", "não", "nos", "para", "por", "que", "uma"}
    ),
}

NON_ENGLISH_LANGUAGE_NAMES = {
    "ar": "Arabic",
    "de": "German",
    "el": "Greek",
    "es": "Spanish",
    "fr": "French",
    "he": "Hebrew",
    "hi": "Hindi",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "nl": "Dutch",
    "pt": "Portuguese",
    "ru": "Russian",
    "th": "Thai",
    "uk": "Ukrainian",
    "vi": "Vietnamese",
    "zh": "Chinese",
}


def _normalized_language(value: str) -> str:
    return value.strip().lower().replace("_", "-").split("-", 1)[0]


def _script_name(character: str) -> str:
    codepoint = ord(character)
    if 0x3040 <= codepoint <= 0x30FF:
        return "japanese"
    if 0x3400 <= codepoint <= 0x9FFF or 0xF900 <= codepoint <= 0xFAFF:
        return "han"
    if 0xAC00 <= codepoint <= 0xD7AF or 0x1100 <= codepoint <= 0x11FF:
        return "korean"
    name = unicodedata.name(character, "")
    for script in ("LATIN", "CYRILLIC", "ARABIC", "HEBREW", "THAI", "DEVANAGARI", "GREEK"):
        if script in name:
            return script.lower()
    return "other"


def _dominant_non_latin_language(counts: Counter[str]) -> str:
    if counts["japanese"] >= max(10, counts["han"] * 0.02):
        return "ja"
    if counts["korean"] >= max(10, counts["han"] * 0.02):
        return "ko"
    if counts["han"]:
        return "zh"
    mapping = {
        "arabic": "ar",
        "cyrillic": "ru",
        "devanagari": "hi",
        "hebrew": "he",
        "thai": "th",
        "greek": "el",
    }
    dominant = max(mapping, key=lambda script: counts[script])
    return mapping[dominant]


def assess_english_presentation(
    text: str,
    *,
    declared_language: str = "",
    title: str = "",
    description: str = "",
) -> LanguageAssessment:
    """Classify clear presentation language locally and abstain on ambiguous pages."""

    declared = _normalized_language(declared_language)
    raw_sample = f"{title} {description} {text}"
    # Some hosted storefronts expose untranslated template expressions as visible text.
    # They are implementation residue, not customer-facing English prose.
    raw_sample = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", raw_sample, flags=re.S)
    sample = re.sub(r"\s+", " ", raw_sample).strip()[:MAX_LANGUAGE_CHARACTERS]
    letters = [character for character in sample if character.isalpha()]
    counts = Counter(_script_name(character) for character in letters)
    analyzed = len(letters)
    latin = counts["latin"]
    non_latin = sum(value for key, value in counts.items() if key != "latin")
    latin_ratio = latin / analyzed if analyzed else 0.0
    tokens = [token.casefold() for token in WORD_RE.findall(sample)]
    english_hits = sum(token in ENGLISH_WORDS for token in tokens)
    english_ratio = english_hits / len(tokens) if tokens else 0.0
    title_letters = [character for character in title if character.isalpha()]
    title_counts = Counter(_script_name(character) for character in title_letters)
    title_non_latin = sum(
        value for key, value in title_counts.items() if key != "latin"
    )
    title_non_latin_ratio = title_non_latin / len(title_letters) if title_letters else 0.0
    title_tokens = [token.casefold() for token in WORD_RE.findall(title)]
    title_english_hits = sum(token in ENGLISH_WORDS for token in title_tokens)

    def result(
        language: str,
        english: bool | None,
        confidence: str,
        reason: str,
    ) -> LanguageAssessment:
        return LanguageAssessment(
            detected_language=language,
            english_presentation=english,
            confidence=confidence,
            reason=reason,
            declared_language=declared_language.strip()[:80],
            analyzed_characters=analyzed,
            latin_character_ratio=round(latin_ratio, 4),
        )

    non_latin_ratio = non_latin / analyzed if analyzed else 0.0
    if declared == "en":
        if non_latin >= 120 and non_latin_ratio >= 0.55:
            detected = _dominant_non_latin_language(counts)
            return result(
                detected,
                False,
                "high",
                "Visible text is predominantly non-Latin despite an English language tag",
            )
        return result("en", True, "high", "The page declares English presentation")

    if (
        declared
        and declared not in {"und", "mul", "zxx"}
        and (analyzed >= 80 or non_latin >= 20)
    ):
        if latin_ratio >= 0.9 and english_hits >= 12 and english_ratio >= 0.055:
            return result(
                "en",
                True,
                "medium",
                "Visible English prose outweighs the conflicting language tag",
            )
        name = NON_ENGLISH_LANGUAGE_NAMES.get(declared, declared.upper())
        return result(
            declared,
            False,
            "high",
            f"The page declares {name} presentation",
        )

    if (
        title_non_latin >= 2
        and title_non_latin_ratio >= 0.5
        and title_english_hits < 2
    ):
        detected = _dominant_non_latin_language(title_counts)
        name = NON_ENGLISH_LANGUAGE_NAMES.get(detected, detected.upper())
        return result(
            detected,
            False,
            "high",
            f"The public page title is predominantly {name} script",
        )

    if non_latin >= 80 and non_latin_ratio >= 0.45:
        detected = _dominant_non_latin_language(counts)
        name = NON_ENGLISH_LANGUAGE_NAMES.get(detected, detected.upper())
        return result(
            detected,
            False,
            "high",
            f"Visible text is predominantly {name} script",
        )

    if non_latin >= 200 and non_latin_ratio >= 0.15 and english_ratio < 0.04:
        detected = _dominant_non_latin_language(counts)
        name = NON_ENGLISH_LANGUAGE_NAMES.get(detected, detected.upper())
        return result(
            detected,
            False,
            "high",
            f"Visible text contains substantial {name} script without sustained English prose",
        )

    if analyzed < 80 or len(tokens) < 15:
        return result(
            "unknown",
            None,
            "low",
            "Too little public text to determine presentation language safely",
        )

    if latin_ratio >= 0.85 and english_hits >= 8 and english_ratio >= 0.05:
        return result("en", True, "medium", "Visible text contains sustained English prose")

    if latin_ratio >= 0.85:
        language_scores = {
            language: sum(token in words for token in tokens)
            for language, words in LATIN_LANGUAGE_WORDS.items()
        }
        detected, hits = max(language_scores.items(), key=lambda item: item[1])
        if hits >= 10 and hits / len(tokens) >= 0.04 and hits > english_hits * 1.5:
            name = NON_ENGLISH_LANGUAGE_NAMES[detected]
            return result(
                detected,
                False,
                "medium",
                f"Visible text contains sustained {name} function-word evidence",
            )

    return result(
        "unknown",
        None,
        "low",
        "Public text does not support a reliable English or non-English determination",
    )


def assess_observation_language(observation: SiteObservation) -> LanguageAssessment:
    """Return stored evidence or derive it for a pre-language snapshot."""

    assessment = observation.language
    if assessment.analyzed_characters or assessment.declared_language:
        return assessment
    assessment = assess_english_presentation(
        observation.text_excerpt,
        title=observation.title,
        description=observation.description,
    )
    observation.language = assessment
    return assessment


def is_english_eligible(observation: SiteObservation, *, english_only: bool = True) -> bool:
    if not english_only:
        return True
    return assess_observation_language(observation).english_presentation is not False
