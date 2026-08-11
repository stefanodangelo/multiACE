"""Config change taxonomy: what has to restart after an ace.cfg edit.

The web UI used to save the file and tell the user "please restart the
printer" for every edit, including ones that need nothing at all. This
module answers the narrower question: given the OLD and NEW text of
ace.cfg, what actually changed, and what is the weakest restart that
makes those changes take effect?

Three levels, ordered:

  none            the value is re-read from the file on demand (the web
                  backend does that for a handful of keys)
  klipper_restart the value is consumed by ace.py at config load, so a
                  FIRMWARE_RESTART is enough
  printer_reboot  the change alters what is enumerated at boot (ACE unit
                  count, serial ports, mode files) - Klipper alone will
                  not pick it up

Kept dependency-free and importable on its own so both the FastAPI
backend and the unit tests can use it.
"""
from __future__ import annotations

__all__ = [
    "RESTART_NONE",
    "RESTART_KLIPPER",
    "RESTART_PRINTER",
    "RESTART_ORDER",
    "LIVE_KEYS",
    "REBOOT_KEYS",
    "parse_sections",
    "classify_key",
    "diff_config",
    "summarize_changes",
]

RESTART_NONE = "none"
RESTART_KLIPPER = "klipper_restart"
RESTART_PRINTER = "printer_reboot"

#: Weakest to strongest. `max(..., key=RESTART_ORDER.index)` picks the
#: restart that satisfies every change in a batch.
RESTART_ORDER = [RESTART_NONE, RESTART_KLIPPER, RESTART_PRINTER]

#: Read from the file by the web backend on every request - editing them
#: needs nothing restarted at all.
LIVE_KEYS = frozenset({
    "display_index_base",
    "update_repo",
    "update_prerelease",
    "update_url_base",
})

#: Changing these changes what exists at boot: how many ACE units are
#: enumerated, which serial device they hang off, which cfg file set is
#: active. Klipper alone cannot re-enumerate USB.
REBOOT_KEYS = frozenset({
    "ace_device_count",
    "serial",
    "serial_1",
    "serial_2",
    "serial_3",
    "baud",
    "usb_vid",
    "usb_pid",
    "mode",
    "ace_mode",
    "ace_head",
})

#: Sections whose mere presence/absence flips the printer between stock
#: and ACE operation - always the strongest level.
_REBOOT_SECTIONS = frozenset({"ace"})


def parse_sections(text: str) -> dict[str, dict[str, str]]:
    """`{section_name: {key: value}}` for a Klipper-style config file.

    Section names keep their raw header text ("ace", "ace 1",
    "ace_tipform"). Comments and blank lines are dropped; a `key: value`
    line outside any section is ignored, as Klipper would reject it
    anyway.
    """
    out: dict[str, dict[str, str]] = {}
    section: str | None = None
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith(";"):
            continue
        if s.startswith("[") and s.endswith("]"):
            section = " ".join(s[1:-1].strip().split())
            out.setdefault(section, {})
            continue
        if section is None or ":" not in s:
            continue
        k, v = s.split(":", 1)
        out[section][k.strip()] = v.strip()
    return out


def _section_kind(section: str) -> str:
    return section.split(None, 1)[0] if section else ""


def classify_key(section: str, key: str) -> str:
    """Restart level for a single key in a single section."""
    kind = _section_kind(section)
    if key in REBOOT_KEYS:
        return RESTART_PRINTER
    if kind in ("ace",) and key in LIVE_KEYS:
        return RESTART_NONE
    return RESTART_KLIPPER


def _strongest(levels) -> str:
    best = RESTART_NONE
    for lv in levels:
        if RESTART_ORDER.index(lv) > RESTART_ORDER.index(best):
            best = lv
    return best


def diff_config(old_text: str, new_text: str) -> list[dict]:
    """Per-key changes between two revisions of ace.cfg.

    Each entry is ``{"section", "key", "old", "new", "kind", "restart"}``
    where kind is "added" | "removed" | "changed", and for a whole
    section appearing/disappearing the key is "" and kind is
    "section_added" / "section_removed".
    """
    old = parse_sections(old_text)
    new = parse_sections(new_text)
    changes: list[dict] = []

    for section in sorted(set(old) | set(new)):
        in_old, in_new = section in old, section in new
        if in_old != in_new:
            kind = "section_added" if in_new else "section_removed"
            restart = (RESTART_PRINTER
                       if _section_kind(section) in _REBOOT_SECTIONS
                       else RESTART_KLIPPER)
            changes.append({"section": section, "key": "", "old": None,
                            "new": None, "kind": kind, "restart": restart})
            continue
        o, n = old[section], new[section]
        for key in sorted(set(o) | set(n)):
            ov, nv = o.get(key), n.get(key)
            if ov == nv:
                continue
            kind = ("added" if ov is None
                    else "removed" if nv is None else "changed")
            changes.append({
                "section": section, "key": key, "old": ov, "new": nv,
                "kind": kind, "restart": classify_key(section, key),
            })
    return changes


def _label(change: dict) -> str:
    section, key = change["section"], change["key"]
    prefix = "" if section == "ace" else f"[{section}] "
    if change["kind"] == "section_added":
        return f"[{section}] added"
    if change["kind"] == "section_removed":
        return f"[{section}] removed"
    if change["kind"] == "added":
        return f"{prefix}{key}: (unset)→{change['new']}"
    if change["kind"] == "removed":
        return f"{prefix}{key}: {change['old']}→(unset)"
    return f"{prefix}{key}: {change['old']}→{change['new']}"


def summarize_changes(old_text: str, new_text: str) -> dict:
    """Human-readable summary plus the restart the batch needs.

    ``{"changes": ["load_length: 100→120", ...],
       "details": [ ...diff_config entries... ],
       "restart_required": "klipper_restart",
       "changed": True}``

    An edit that only moves comments or whitespace yields no changes and
    ``restart_required == "none"``, which is what lets the UI say
    "applied, nothing to restart" instead of always nagging.
    """
    details = diff_config(old_text, new_text)
    return {
        "changes": [_label(c) for c in details],
        "details": details,
        "restart_required": _strongest(c["restart"] for c in details),
        "changed": bool(details),
    }
