"""Snapmaker U1 / ACE firmware compatibility table.

Single source of truth for "which printer firmware has multiACE been run
against". Imported by the web backend (`/api/version`) and mirrored by a
tiny inline fallback in `klipper/extras/ace.py`, which cannot rely on this
package being importable from inside Klippy.

Deliberately data, not policy: nothing here blocks an operation. An
untested firmware yields status "untested" and the caller decides whether
to warn - a user on a firmware newer than this table should still be able
to print.
"""
from __future__ import annotations

import re

__all__ = [
    "FIRMWARE_COMPAT",
    "STATUS_SUPPORTED",
    "STATUS_UNSUPPORTED",
    "STATUS_UNTESTED",
    "STATUS_UNKNOWN",
    "normalize_version",
    "version_tuple",
    "compat_for",
]

STATUS_SUPPORTED = "supported"
STATUS_UNSUPPORTED = "unsupported"
STATUS_UNTESTED = "untested"
STATUS_UNKNOWN = "unknown"

#: Exact versions first, then "x"-wildcard families. Order does not matter;
#: `compat_for` prefers the exact match and only then the family.
FIRMWARE_COMPAT: dict[str, dict] = {
    # --- Snapmaker U1 printer firmware -------------------------------
    "1.4.x": {
        "status": STATUS_UNSUPPORTED,
        "device": "Snapmaker U1",
        "reason": "old ACE protocol; slot/RFID fields differ",
        "known_issues": ["multiACE will not drive the ACE reliably"],
    },
    "1.5.0": {
        "status": STATUS_SUPPORTED,
        "device": "Snapmaker U1",
        "known_issues": ["RFID identity can lag a slot insert by a few seconds"],
    },
    "1.5.1": {
        "status": STATUS_SUPPORTED,
        "device": "Snapmaker U1",
        "known_issues": ["RFID identity can lag a slot insert by a few seconds"],
    },
    "1.5.2": {
        "status": STATUS_SUPPORTED,
        "device": "Snapmaker U1",
        "known_issues": [],
        "notes": "Reference firmware: spool-id (print_task_config) is present.",
    },
    # --- ACE unit firmware -------------------------------------------
    "1.1.31": {
        "status": STATUS_SUPPORTED,
        "device": "ACE Pro 2",
        "known_issues": [],
    },
}

#: At least one dot required: printer strings carry stray digits ("U1",
#: "build 7") that a bare \d+ would happily return as the version.
_VER_RE = re.compile(r"(\d+(?:\.\d+)+)")


def normalize_version(raw) -> str:
    """Pull a dotted numeric version out of whatever the printer reports.

    Accepts 'V1.5.2', '1.5.2-release', b'1.5.2', 1.5 - anything without a
    numeric run yields "".
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    m = _VER_RE.search(str(raw))
    return m.group(1) if m else ""


def version_tuple(raw) -> tuple[int, ...]:
    """Dotted version as a comparable tuple; () when unparseable."""
    v = normalize_version(raw)
    if not v:
        return ()
    return tuple(int(p) for p in v.split("."))


def _family_key(version: str) -> str:
    parts = version.split(".")
    if len(parts) < 2:
        return ""
    return ".".join(parts[:2]) + ".x"


def compat_for(raw) -> dict:
    """Compatibility record for a reported firmware version.

    Always returns the same shape::

        {"firmware_version": "1.5.2",   # normalized, "" when unknown
         "status": "supported",         # supported|unsupported|untested|unknown
         "known_issues": [...],
         "device": "Snapmaker U1",      # when the table knows it
         "reason": "...",               # only for unsupported
         "supported": True}             # convenience for callers

    An unparseable/absent version is "unknown"; a parseable version the
    table has never heard of is "untested" - both are non-blocking.
    """
    version = normalize_version(raw)
    if not version:
        return {
            "firmware_version": "",
            "status": STATUS_UNKNOWN,
            "known_issues": [],
            "supported": False,
        }
    entry = FIRMWARE_COMPAT.get(version) or FIRMWARE_COMPAT.get(_family_key(version))
    if entry is None:
        return {
            "firmware_version": version,
            "status": STATUS_UNTESTED,
            "known_issues": [],
            "supported": False,
        }
    out = dict(entry)
    out["firmware_version"] = version
    # Copy the list too: callers hand this straight to JSON serialisers
    # and UI code, and a shallow copy let one of them append to the
    # module-level table for the rest of the process.
    out["known_issues"] = list(entry.get("known_issues") or [])
    out["supported"] = out.get("status") == STATUS_SUPPORTED
    return out
