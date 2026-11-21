"""Build and verify Hörwerk on native x64 release runners."""
from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import distribution
import json
import sys
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import zipfile

import imageio_ffmpeg
from build_backend import build_backend
from build_deb import build_deb
from build_portable import _write_zip, build


def checksum(file):
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_notices(package, output):
    dist = distribution(package)
    copied = []
    for item in dist.files or ():
        if ("licenses" in item.parts or item.name in {"LICENSE", "COPYING"}) and dist.locate_file(item).is_file():
            path = output / f"{package}-{item.name}"
            shutil.copy2(dist.locate_file(item), path)
            copied.append(path.name)
    return copied


def main(*, required_target=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    target = {"win32": "windows", "linux": "linux"}.get(sys.platform)
    if target is None or platform.machine().lower() not in {"amd64", "x86_64"}:
        parser.error("Ein nativer Linux- oder Windows-x64-Runner ist erforderlich.")
    if required_target is not None and target != required_target:
        parser.error("Der Runner entspricht nicht dem erwarteten Zielsystem.")
    suffix = ".exe" if target == "windows" else ""
    source = Path(__file__).resolve().parent
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hoerwerk-build-") as temp:
        staging = Path(temp)
        backend = staging / ("audible" + suffix)
        build_backend(source, backend)
        ffmpeg = staging / ("ffmpeg" + suffix)
        shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), ffmpeg)
        ffmpeg.chmod(0o755)
        ffmpeg_notice = subprocess.check_output([ffmpeg, "-L"], stderr=subprocess.STDOUT, text=True)
        (staging / "ffmpeg.txt").write_text(ffmpeg_notice, encoding="utf-8")
        audible_notices = copy_notices("audible", staging)
        if not audible_notices:
            raise RuntimeError("Audible-Lizenztext fehlt.")
        notices = [
            ("audible", backend, audible_notices[0],
             "https://github.com/mkb79/audible-cli/tree/v0.6.0", "audible-cli 0.6.0 / audible 0.13.1"),
            ("ffmpeg", ffmpeg, "ffmpeg.txt",
             "https://github.com/imageio/imageio-ffmpeg/tree/v0.8.1", f"imageio-ffmpeg {imageio_ffmpeg.__version__}"),
        ]
        (staging / "manifest.json").write_text(json.dumps({"backends": [
            {"name": name, "file": binary.name, "license": license, "source": url, "version": version,
             "sha256": checksum(binary), "os": target, "arch": "x86_64"}
            for name, binary, license, url, version in notices
        ]}), encoding="utf-8")
        result = build(source, args.output, staging, source_sha=args.source_sha)

    installed = args.output / f"Hoerwerk-{target}-x86_64"
    licenses = installed / "licenses"
    for package in ("audible-cli", "imageio-ffmpeg", "playwright", "greenlet", "Pillow", "miniaudio", "cffi", "pycparser"):
        copy_notices(package, licenses)
    if target == "linux":
        copy_notices("python-xlib", licenses)
    shutil.copy2(source / "COPYING-FFmpeg.txt", licenses / "FFmpeg-GPL-3.0.txt")
    original = installed / "source"
    original.mkdir()
    for path in sorted(source.iterdir()):
        if path.is_file() and path.suffix in {".py", ".md", ".txt"}:
            shutil.copy2(path, original / path.name)
    readme = installed / "LIESMICH.txt"
    readme.write_text(f"Hörwerk für {target.capitalize()} x64. ZIP vollständig entpacken und Hoerwerk{suffix} starten. "
                      "Der Ordner data enthält die lokalen Profile und gehört zur Datensicherung.\n",
                      encoding="utf-8")
    hashes = {path.relative_to(installed).as_posix(): checksum(path)
              for path in installed.rglob("*") if path.is_file() and path.name != "files.sha256.json"}
    (installed / "files.sha256.json").write_text(json.dumps(hashes, sort_keys=True) + "\n", encoding="utf-8")
    result.unlink()
    archive = args.output / f"Hoerwerk-{args.version}-{target.capitalize()}-x86_64.zip"
    if archive.exists():
        raise FileExistsError(archive)
    _write_zip(installed, archive)
    with tempfile.TemporaryDirectory(prefix="hoerwerk-check-") as temp:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(temp)
        extracted = Path(temp) / installed.name
        programs = (extracted / ("Hoerwerk" + suffix), extracted / "bin" / ("audible" + suffix))
        if target == "linux":
            for program in programs:
                program.chmod(0o755)
        subprocess.run([programs[0], "--self-check"], check=True, timeout=40)
        subprocess.run([programs[1], "--hoerwerk-tls-self-check"], check=True, timeout=40)
        if list((extracted / "data").iterdir()):
            raise RuntimeError("Das Auslieferungsarchiv enthält Laufzeitdaten.")
    print(f"{checksum(archive)}  {archive.name}", flush=True)
    if target == "linux":
        deb = build_deb(installed, args.output / f"hoerwerk_{args.version}_amd64.deb", args.version,
                        args.source_sha)
        with tempfile.TemporaryDirectory(prefix="hoerwerk-deb-check-") as temp:
            subprocess.run(["dpkg-deb", "-x", deb, temp], check=True, timeout=90)
            subprocess.run([Path(temp) / "opt/hoerwerk/Hoerwerk", "--self-check"], check=True, timeout=40)
        print(f"{checksum(deb)}  {deb.name}", flush=True)


if __name__ == "__main__":
    main()
