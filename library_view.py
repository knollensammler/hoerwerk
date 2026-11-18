"""Ordering and filtering independent of the desktop widgets."""
from datetime import date
import math
import unicodedata

ORDERINGS = {
    "Titel A–Z": ("title", False), "Titel Z–A": ("title", True),
    "Autor A–Z": ("authors", False), "Autor Z–A": ("authors", True),
    "Neu hinzugefügt": ("date", True), "Alt hinzugefügt": ("date", False),
    "Längste Laufzeit": ("runtime_length_min", True), "Kürzeste Laufzeit": ("runtime_length_min", False),
    "Größte lokale Datei": ("size", True), "Kleinste lokale Datei": ("size", False),
    "Neu erschienen": ("release_date", True), "Alt erschienen": ("release_date", False),
    "Freigegeben zuerst": ("download_allowed", True),
    "Gesperrt zuerst": ("download_allowed", False),
}


def _text(value):
    return "".join(c for c in unicodedata.normalize("NFKD", str(value or "").casefold())
                   if not unicodedata.combining(c))


def _date(value):
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


def _duration(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def ordered_items(items, ordering, query="", sizes=None):
    sizes = sizes or {}
    field, descending = ORDERINGS.get(ordering, ORDERINGS["Titel A–Z"])
    needle = _text(query).strip()
    rows = [item for item in items if needle in _text(f"{item['title']} {item.get('authors') or ''}")]
    rows.sort(key=lambda item: (_text(item["title"]), item["asin"]))

    def key(item):
        if field == "date":
            return _date(item.get("purchase_date")) or _date(item.get("date_added"))
        if field == "release_date":
            return _date(item.get(field))
        if field == "download_allowed":
            allowed = item.get(field)
            return allowed if item.get("kind") != "local" and type(allowed) is bool else None
        if field == "size":
            return sizes.get(item["asin"])
        if field == "runtime_length_min":
            return _duration(item.get(field))
        return _text(item.get(field)).strip() or None

    present, missing = [], []
    for item in rows:
        value = key(item)
        if value is None:
            missing.append(item)
        else:
            present.append((value, item))
    present.sort(key=lambda pair: pair[0], reverse=descending)
    return [item for _, item in present] + missing


def font_scale(width, height):
    """One continuous curve with readable limits, independent of widget state."""
    return min(1.55, max(.84, ((width / 1000) * (height / 680)) ** .18))


def can_download(item):
    return item.get("kind") != "local" and item.get("download_allowed") is True


def download_status(item):
    if item.get("kind") == "local":
        return "Lokal"
    if can_download(item):
        return "Freigegeben"
    if item.get("download_allowed") is False:
        return "Gesperrt"
    return "Ungeprüft"
