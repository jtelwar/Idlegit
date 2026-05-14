"""Centralised glyph table with an ASCII fallback for terminals whose
locale or font can't render the Unicode characters the rest of the UI
relies on (Braille spinner, status icons, box-drawing borders, ...).

Modern Windows Terminal, iTerm2, gnome-terminal, kitty, alacritty all
render the Unicode set fine, so the Unicode variants are the default.
The fallback fires when:

  - `locale.getpreferredencoding()` doesn't advertise UTF-8 (e.g. an
    SSH session that inherited `LANG=C`), or
  - the user sets `IDLEGIT_ASCII_GLYPHS=1` to force ASCII (escape hatch
    for legacy `conhost.exe` or any environment where the default
    detection guesses wrong).

Only `SPINNER_FRAMES` is consumed today — that's the most-visible
animated glyph, and the place where tofu rendering reads as "the app
is broken" rather than "this one icon is wrong". The static glyphs
(✓ ✗ ⚠ ● ↳ etc.) stay inline at their call sites for now; this module
is the seam to migrate them through if any user reports trouble."""
from __future__ import annotations

import locale
import os


_SPINNER_UNICODE = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_SPINNER_ASCII = ["|", "/", "-", "\\"]


def _supports_unicode() -> bool:
    """True when the runtime environment looks UTF-8 capable. Env-var
    override wins so users on locale-broken machines have a way out."""
    override = os.environ.get("IDLEGIT_ASCII_GLYPHS", "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return False
    if override in ("0", "false", "no", "off"):
        return True
    try:
        encoding = (locale.getpreferredencoding(False) or "").lower()
    except Exception:
        return True
    return encoding.startswith("utf")


SPINNER_FRAMES = _SPINNER_UNICODE if _supports_unicode() else _SPINNER_ASCII
