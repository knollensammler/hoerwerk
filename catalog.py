"""Local catalogue corrections and shareable text-only snapshots."""
import csv
import html
import json
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile

from core import Core, InvalidBackendResult

FIELDS = {"title": "Titel", "authors": "Autor", "narrators": "Sprecher",
          "series_title": "Serie", "series_sequence": "Band", "genres": "Genre"}
ID = re.compile(r"(?:[A-Z0-9]{10}|local-[a-f0-9]{64})\Z")


def _file(paths, profile, item):
    folder = paths.data / "local-metadata" if item.get("kind") == "local" else paths.account(profile)
    if folder.resolve() != folder or not ID.fullmatch(item["asin"]):
        raise InvalidBackendResult("Der Metadatenpfad ist ungültig.")
    return folder / (item["asin"] + ".metadata.json")


def corrections(paths, profile, item):
    try:
        path = _file(paths, profile, item)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16_384:
            return {}
        with path.open(encoding="utf-8") as stream:
            value = json.loads(stream.read(16_385))
        return {k: v for k, v in value.items() if k in FIELDS and isinstance(v, str) and len(v) <= 400}
    except (OSError, ValueError, AttributeError, InvalidBackendResult):
        return {}


def save(paths, profile, item, values):
    clean = {k: str(values.get(k, "")).strip()[:400] for k in FIELDS}
    if not clean["title"]:
        raise InvalidBackendResult("Der Titel darf nicht leer sein.")
    path = _file(paths, profile, item)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(clean, output, ensure_ascii=False)
            output.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def tags(item):
    keys = {"title": "title", "authors": "artist", "narrators": "composer",
            "series_title": "album", "series_sequence": "track", "genres": "genre"}
    return [argument for field, key in keys.items()
            if isinstance(item.get(field), str) and item[field]
            for argument in ("-metadata", f"{key}={item[field][:400]}")]


def _rows(items):
    for item in items:
        yield [str(item.get(k) or "") for k in FIELDS] + [
            str(item.get("runtime_length_min") or ""),
            "Lokal" if item.get("kind") == "local" else "Audible",
        ]


def export_catalog(items, target):
    target = Path(target)
    headings = [*FIELDS.values(), "Dauer (Minuten)", "Quelle"]
    with NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=target.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            if target.suffix.casefold() == ".csv":
                output.write("\ufeff")
                writer = csv.writer(output, delimiter=";")
                writer.writerow(headings)
                for row in _rows(items):
                    writer.writerow(["'" + cell if cell.lstrip().startswith(("=", "+", "-", "@")) or
                                     cell.startswith(("\t", "\r", "\n")) else cell for cell in row])
            elif target.suffix.casefold() == ".html":
                output.write("""<!doctype html><html lang="de"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Hörwerk – Bibliothek</title>
<style>body{background:#101e24;color:#eff6ff;font:1rem system-ui;margin:2rem}
h1{color:#78decb}div{overflow:auto}table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:.6em;border-bottom:1px solid #36585d}th{background:#1a3037}</style>
<h1>Hörwerk – Bibliothek</h1><div><table><thead><tr>""")
                output.write("".join(f"<th>{html.escape(cell)}</th>" for cell in headings))
                output.write("</tr></thead><tbody>")
                for row in _rows(items):
                    output.write("<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>")
                output.write("</tbody></table></div></html>")
            else:
                raise InvalidBackendResult("Bitte HTML oder CSV auswählen.")
            output.close()
            Core._publish(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return target
