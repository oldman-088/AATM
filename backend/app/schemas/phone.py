"""Phone-number normalization to the E.164 international format.

Normalization rules (applied in order):

1. Translate Arabic-Indic (U+0660..U+0669) and Extended Arabic-Indic / Persian
   (U+06F0..U+06F9) digits to ASCII digits, so trilingual (EN/AR/FA) clients can
   submit numbers in their native numerals.
2. Remove visual separators: any whitespace, hyphens, dots, parentheses,
   forward and backward slashes.
3. A leading international prefix ``00`` becomes ``+``.
4. The result must match ``^[1-9]\\d{1,14}$`` after the ``+`` — i.e. E.164:
   ``+`` followed by 2 to 15 digits whose first digit is not zero.

Anything else is rejected with ``ValueError`` and surfaces as HTTP 422 at the
API boundary. In particular, national numbers without an international prefix
(e.g. ``0721 234 567``) cannot be normalized without a region and are rejected
rather than guessed.
"""

from __future__ import annotations

import re

E164_PATTERN = re.compile(r"^\+[1-9]\d{1,14}$")
_E164_EXAMPLE = "+14155552671"
PHONE_INVALID_ERROR = f"phone must be a valid E.164 number such as {_E164_EXAMPLE}"

_SEPARATOR_PATTERN = re.compile(r"[\s\-\.\(\)/\\]+")


def _digits_to_ascii(value: str) -> str:
    chars: list[str] = []
    for ch in value:
        code = ord(ch)
        if 0x0660 <= code <= 0x0669:  # Arabic-Indic digits ٠-٩
            chars.append(chr(ord("0") + code - 0x0660))
        elif 0x06F0 <= code <= 0x06F9:  # Extended Arabic-Indic (Persian) digits ۰-۹
            chars.append(chr(ord("0") + code - 0x06F0))
        else:
            chars.append(ch)
    return "".join(chars)


def normalize_phone_e164(value: str) -> str:
    """Return the canonical E.164 form of ``value`` or raise ``ValueError``."""
    if not isinstance(value, str):
        raise ValueError(PHONE_INVALID_ERROR)
    cleaned = _SEPARATOR_PATTERN.sub("", _digits_to_ascii(value))
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if not E164_PATTERN.fullmatch(cleaned):
        raise ValueError(PHONE_INVALID_ERROR)
    return cleaned
