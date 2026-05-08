#!/usr/bin/env python3
"""Merge the bundled idlegit.conf template into the user's config file.

Adds any [idlegit] keys present in the template but missing in the user's
file, using each template line as-is (including inline ; comments).
Existing keys and the rest of the file are left unchanged.

Resolves the destination path the same way as runtime config (``config``
module — respects IDLEGIT_CONFIG_DIR).

Usage:
  PYTHONPATH=<dir-with-config.py> python install_merge_config.py [path/to/template.conf]

With no template path, uses ``idlegit.conf.sample`` beside this script if present,
otherwise ``idlegit.conf`` in the same directory.

Exits 0 on success. Prints a single summary line for the installer to log:
  merge: created <path>
  merge: added N key(s) to <path>
  merge: no new keys (<path> already complete)
"""
from __future__ import annotations

import configparser
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_KEY = re.compile(r"^([A-Za-z0-9_]+)\s*=")


def _default_template_path() -> Path:
    """Prefer shipped sample next to this script; else ``idlegit.conf`` (dev tree)."""
    bundled = ROOT / "idlegit.conf.sample"
    if bundled.is_file():
        return bundled
    return ROOT / "idlegit.conf"


def _idlegit_span(lines: list[str]) -> tuple[int, int] | None:
    """Line indices [start, end) covering [idlegit], header inclusive."""
    i = 0
    while i < len(lines):
        m = _SECTION.match(lines[i].rstrip())
        if m and m.group(1).strip().lower() == "idlegit":
            start = i
            i += 1
            while i < len(lines):
                if _SECTION.match(lines[i].rstrip()):
                    return (start, i)
                i += 1
            return (start, len(lines))
        i += 1
    return None


def _template_key_lines(lines: list[str]) -> list[tuple[str, str]]:
    """(lowercase key, full line text) in template order, first assignment
    wins per key."""
    span = _idlegit_span(lines)
    if not span:
        return []
    s, e = span
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in lines[s + 1 : e]:
        raw = line.rstrip("\n")
        st = raw.strip()
        if not st or st.startswith("#"):
            continue
        if st.startswith(";") and "=" not in st:
            continue
        mk = _KEY.match(st)
        if not mk:
            continue
        k = mk.group(1).lower()
        if k in seen:
            continue
        seen.add(k)
        out.append((k, raw))
    return out


def _user_idlegit_options(path: Path) -> set[str] | None:
    cp = configparser.ConfigParser(inline_comment_prefixes=(";",))
    try:
        cp.read(path, encoding="utf-8")
    except (configparser.Error, OSError) as e:
        print(f"merge: error reading {path} ({e})", file=sys.stderr)
        return None
    if not cp.has_section("idlegit"):
        return set()
    return {o.lower() for o in cp.options("idlegit")}


def main() -> int:
    template_path = (
        Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else _default_template_path()
    )
    if not template_path.is_file():
        print(f"merge: template missing: {template_path}", file=sys.stderr)
        return 1

    sys.path.insert(0, str(ROOT))
    import config as idlegit_config  # noqa: E402

    dest = idlegit_config.CONFIG_FILE
    dest.parent.mkdir(parents=True, exist_ok=True)

    tmpl_lines = template_path.read_text(encoding="utf-8").splitlines()
    entries = _template_key_lines(tmpl_lines)
    if not entries:
        print(f"merge: template has no [idlegit] keys: {template_path}", file=sys.stderr)
        return 1

    if not dest.exists():
        dest.write_text(template_path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"merge: created {dest}")
        return 0

    user_opts = _user_idlegit_options(dest)
    if user_opts is None:
        return 1

    to_add = [line for k, line in entries if k not in user_opts]
    if not to_add:
        print(f"merge: no new keys ({dest} already complete)")
        return 0

    user_lines = dest.read_text(encoding="utf-8").splitlines()
    span = _idlegit_span(user_lines)
    if not span:
        tpl_span = _idlegit_span(tmpl_lines)
        if not tpl_span:
            return 1
        chunk_lines = tmpl_lines[tpl_span[0] : tpl_span[1]]
        chunk = "\n".join(chunk_lines)
        base = "\n".join(user_lines).rstrip()
        out = (base + "\n\n" + chunk + "\n") if base else chunk + "\n"
        dest.write_text(out, encoding="utf-8")
        print(f"merge: appended [idlegit] section ({len(to_add)} keys) to {dest}")
        return 0

    insert_at = span[1]
    new_text = "\n".join(user_lines[:insert_at] + to_add + user_lines[insert_at:])
    if not new_text.endswith("\n"):
        new_text += "\n"
    dest.write_text(new_text, encoding="utf-8")
    print(f"merge: added {len(to_add)} key(s) to {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
