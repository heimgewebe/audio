#!/usr/bin/env python3
"""Non-destructive local WAV editing roundtrip for archived H2 material.

No editor process is launched and no original material is modified.
Only the explicit local CLI has filesystem authority; this is not a remote API.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import sys
import tempfile

import h2_ingest

SCHEMA = 1
ID_RE = re.compile(r"^[0-9a-f]{24}$")
DEFAULT_EDIT_ROOT = Path.home() / "Music" / "Audio-Aufnahmen" / "H2-Bearbeitungen"
COPY_SIZE = 1024 * 1024
MAX_MANIFEST = 16 * 1024
MAX_RENDER_BYTES = (2**32) - 1
MAX_WAVE_CHUNKS = 4096


class EditError(RuntimeError):
    """Invalid or unsafe user material state."""


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _id(domain: bytes, value: dict) -> str:
    return hashlib.sha256(domain + b"\0" + _canonical(value)).hexdigest()[:24]


def _directory(path: Path, *, create: bool = False, private: bool = False) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise EditError("Verzeichnispfad muss absolut sein.")
    if ".." in path.parts:
        raise EditError("Verzeichnispfad darf keine Parent-Traversal-Segmente enthalten.")
    # Check all ancestors before opening or creating anything: no symlink traversal.
    for part in reversed((path, *path.parents)):
        try:
            mode = part.lstat().st_mode
        except FileNotFoundError:
            if not create:
                raise EditError("Erforderliches Verzeichnis fehlt.")
            part.mkdir(mode=0o700)
            mode = part.lstat().st_mode
        if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            raise EditError("Verzeichnisbaum darf keine Symlinks enthalten.")
    if private:
        st = path.lstat()
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise EditError("Arbeitsverzeichnis muss privat (0700) und im eigenen Besitz sein.")
    return path


def _file(path: Path, *, owner: bool = False) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise EditError("Erwartete reguläre Datei fehlt.") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise EditError("Datei muss regulär und symlinkfrei sein.")
    if owner and info.st_uid != os.getuid():
        raise EditError("Datei hat einen fremden Eigentümer.")
    return info


def _digest(path: Path, *, expected_size: int | None = None, owner: bool = False) -> str:
    before = _file(path, owner=owner)
    if expected_size is not None and before.st_size != expected_size:
        raise EditError("Dateigröße passt nicht zur gebundenen Quelle.")
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise EditError("Dateigeneration änderte sich vor dem Lesen.")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            while chunk := stream.read(COPY_SIZE):
                digest.update(chunk)
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
            opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns
        ):
            raise EditError("Dateigeneration änderte sich beim Lesen.")
    finally:
        os.close(fd)
    return digest.hexdigest()


def _copy_checked(source: Path, dest: Path, *, expected_sha256: str, expected_size: int) -> None:
    before = _file(source)
    if before.st_size != expected_size:
        raise EditError("Quelldatei wich vor der Kopie vom Beleg ab.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(source, flags)
    digest = hashlib.sha256()
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise EditError("Quelldateigeneration wechselte vor Kopie.")
        total = 0
        with os.fdopen(fd, "rb", closefd=False) as src, dest.open("xb") as dst:
            os.chmod(dest, 0o444)
            while chunk := src.read(COPY_SIZE):
                total += len(chunk)
                if total > expected_size:
                    raise EditError("Kopie überschreitet gebundene Größe.")
                digest.update(chunk)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        after = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise EditError("Quelldateigeneration wechselte bei Kopie.")
    finally:
        os.close(fd)
    if total != expected_size or digest.hexdigest() != expected_sha256:
        raise EditError("Kopierter Inhalt stimmt nicht mit dem Quellbeleg überein.")
    if _digest(dest, expected_size=expected_size) != expected_sha256:
        raise EditError("Zielkopie verfehlte den vollständigen Hash-Readback.")


def _read_manifest(path: Path) -> dict:
    info = _file(path)
    if not 0 < info.st_size <= MAX_MANIFEST:
        raise EditError("Manifestgröße ist ungültig.")
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise EditError("Manifest enthält doppelte Schlüssel.")
            result[key] = value
        return result
    with path.open("rb") as stream:
        data = stream.read(MAX_MANIFEST + 1)
    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise EditError("Manifest ist ungültiges JSON.") from exc
    if not isinstance(result, dict):
        raise EditError("Manifest ist kein Objekt.")
    return result


def _matches_file(path: Path, size: int, expected_sha: str) -> bool:
    try:
        return _digest(path, expected_size=size) == expected_sha
    except EditError:
        return False


def _atomic_dir_publish(root: Path, final_name: str, manifest: dict, copied: tuple[Path, str, int, str]) -> Path:
    stage = Path(tempfile.mkdtemp(prefix=".audio-edit-staging-", dir=root))
    try:
        source, target_name, size, sha = copied
        _copy_checked(source, stage / target_name, expected_sha256=sha, expected_size=size)
        manifest_path = stage / "manifest.json"
        with manifest_path.open("xb") as stream:
            stream.write(_canonical(manifest) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(manifest_path, 0o444)
        dir_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        target = root / final_name
        if target.exists() or target.is_symlink():
            raise EditError("Zielobjekt existiert bereits; es wird nichts überschrieben.")
        os.rename(stage, target)
        dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return target
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _safe_roots(library_root: Path, edit_root: Path, *, create: bool) -> tuple[Path, Path]:
    library = _directory(h2_ingest._effective_library_root(library_root))
    editing = edit_root.expanduser()
    try:
        common = os.path.commonpath((str(library), str(editing)))
    except ValueError as exc:
        raise EditError("Material- und Bearbeitungsroots sind nicht vergleichbar.") from exc
    if common == str(library) or common == str(editing):
        raise EditError("Bearbeitung darf nicht innerhalb der Masterbibliothek liegen.")
    return library, _directory(editing, create=create, private=True)


def _workspace_manifest(material_id: str, master: dict, set_sha: str) -> dict:
    source = {
        "material_id": material_id,
        "master_name": master["name"],
        "master_sha256": master["sha256"],
        "master_bytes": master["bytes"],
        "master_set_sha256": set_sha,
    }
    return {"schema_version": SCHEMA, "kind": "audio_edit_workspace",
            "edit_id": _id(b"audio-edit-workspace-v1", source), "source": source}


def prepare(material_id: str, master_name: str, *, library_root: Path = h2_ingest.DEFAULT_LIBRARY_ROOT,
            edit_root: Path = DEFAULT_EDIT_ROOT) -> dict:
    if ID_RE.fullmatch(material_id) is None:
        raise EditError("Ungültige Material-ID.")
    if not isinstance(master_name, str) or h2_ingest.ROLE_RE.fullmatch(master_name) is None:
        raise EditError("Ein gültiger H2-Masterdateiname ist erforderlich.")
    library, editing = _safe_roots(library_root, edit_root, create=True)
    verified = h2_ingest.verify_material(material_id, library_root=library)
    master = next((item for item in verified["masters"] if item["name"] == master_name), None)
    if master is None:
        raise EditError("Der gewählte Master gehört nicht zum verifizierten Material.")
    manifest = _workspace_manifest(material_id, master, verified["master_set_sha256"])
    workspace_root = _directory(editing / "working", create=True, private=True)
    target = workspace_root / manifest["edit_id"]
    if target.exists() or target.is_symlink():
        _directory(target, private=True)
        if _read_manifest(target / "manifest.json") != manifest:
            raise EditError("Vorhandener Arbeitsbereich besitzt eine fremde Bindung.")
        if not _matches_file(target / "input.wav", master["bytes"], master["sha256"]):
            raise EditError("Arbeitskopie wurde verändert; kein automatisches Überschreiben.")
    else:
        _directory(library / material_id / "master")
        _atomic_dir_publish(workspace_root, manifest["edit_id"], manifest,
                            (library / material_id / "master" / master_name, "input.wav",
                             master["bytes"], master["sha256"]))
    return {"kind": "audio_edit_prepared", "edit_id": manifest["edit_id"],
            "working_copy": str(target / "input.wav"),
            "expected_render": str(target / "render.wav"),
            "source_verified": True, "original_untouched": True}


def _wave_info(path: Path) -> dict:
    info = _file(path, owner=True)
    if not 44 <= info.st_size <= MAX_RENDER_BYTES:
        raise EditError("Render-WAV hat eine ungültige oder zu große Dateigröße.")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
        before_identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if not stat.S_ISREG(opened.st_mode) or identity != before_identity:
            raise EditError("Render-WAV änderte sich vor der Formatprüfung.")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            header = stream.read(12)
            if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
                raise EditError("Rendering muss eine RIFF/WAVE-Datei sein.")
            if struct.unpack("<I", header[4:8])[0] != info.st_size - 8:
                raise EditError("RIFF-Dateilänge ist inkonsistent.")
            pos = 12
            fmt = None
            data_size = None
            for _ in range(MAX_WAVE_CHUNKS):
                if pos == info.st_size:
                    break
                if pos + 8 > info.st_size:
                    raise EditError("WAV-Chunkheader ist unvollständig.")
                stream.seek(pos)
                chunk_id, length = struct.unpack("<4sI", stream.read(8))
                start, end = pos + 8, pos + 8 + length
                if end > info.st_size:
                    raise EditError("WAV-Chunk liegt außerhalb der Dateigrenze.")
                if chunk_id == b"fmt ":
                    if fmt is not None or not 16 <= length <= 256:
                        raise EditError("WAV-Format-Chunk ist ungültig.")
                    fmt = struct.unpack("<HHIIHH", stream.read(16))
                elif chunk_id == b"data":
                    if data_size is not None:
                        raise EditError("WAV besitzt mehrere Daten-Chunks.")
                    data_size = length
                pos = end + (length % 2)
                if pos > info.st_size:
                    raise EditError("Ungültiges WAV-Chunk-Padding.")
            if pos != info.st_size or fmt is None or data_size is None or data_size == 0:
                raise EditError("WAV-Format oder Audiodaten fehlen.")
            stream.seek(0)
            content_sha = hashlib.sha256()
            while chunk := stream.read(COPY_SIZE):
                content_sha.update(chunk)
        after = os.fstat(fd)
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise EditError("Render-WAV änderte sich bei der Format- oder Hashprüfung.")
    finally:
        os.close(fd)
    codec, channels, rate, byte_rate, frame_bytes, bits = fmt
    if (codec, bits) not in {(1, 16), (1, 24), (1, 32), (3, 32)}:
        raise EditError("Nur PCM 16/24/32 oder Float32 WAV wird unterstützt.")
    if not 1 <= channels <= 2 or not 8000 <= rate <= 192000:
        raise EditError("WAV-Kanäle oder Abtastrate sind ungültig.")
    if frame_bytes != channels * bits // 8 or byte_rate != frame_bytes * rate or data_size % frame_bytes:
        raise EditError("WAV-Blockgröße oder Byterate ist inkonsistent.")
    return ({"codec": "float32" if codec == 3 else f"pcm{bits}",
             "channels": channels, "sample_rate_hz": rate, "frames": data_size // frame_bytes},
            content_sha.hexdigest(), opened.st_size)


def finish(edit_id: str, *, library_root: Path = h2_ingest.DEFAULT_LIBRARY_ROOT,
           edit_root: Path = DEFAULT_EDIT_ROOT) -> dict:
    if ID_RE.fullmatch(edit_id) is None:
        raise EditError("Ungültige Bearbeitungs-ID.")
    library, editing = _safe_roots(library_root, edit_root, create=False)
    workspace = _directory(editing / "working" / edit_id, private=True)
    manifest = _read_manifest(workspace / "manifest.json")
    source = manifest.get("source")
    if (manifest.get("schema_version") != SCHEMA or manifest.get("kind") != "audio_edit_workspace"
            or not isinstance(source, dict) or set(source) !=
            {"material_id", "master_name", "master_sha256", "master_bytes", "master_set_sha256"}
            or _workspace_manifest(source.get("material_id"), {
                "name": source.get("master_name"), "sha256": source.get("master_sha256"),
                "bytes": source.get("master_bytes")}, source.get("master_set_sha256")) != manifest
            or manifest.get("edit_id") != edit_id):
        raise EditError("Arbeitsbereich ist nicht eindeutig an seinen Master gebunden.")
    verified = h2_ingest.verify_material(source["material_id"], library_root=library)
    master = next((item for item in verified["masters"] if item["name"] == source["master_name"]), None)
    if (master is None or master["sha256"] != source["master_sha256"]
            or master["bytes"] != source["master_bytes"]
            or verified["master_set_sha256"] != source["master_set_sha256"]):
        raise EditError("Ursprungs-Master kann nicht mehr nachgewiesen werden.")
    if not _matches_file(workspace / "input.wav", source["master_bytes"], source["master_sha256"]):
        raise EditError("Arbeitskopie wurde verändert; Masterbindung ungültig.")
    render = workspace / "render.wav"
    wav, sha, size = _wave_info(render)
    derivation = {"source": source, "render": {"sha256": sha, "bytes": size, "wav": wav}}
    derivative_id = _id(b"audio-edit-render-v1", derivation)
    archive_manifest = {"schema_version": SCHEMA, "kind": "audio_edit_render",
                        "derived_id": derivative_id, "edit_id": edit_id, **derivation}
    renders = _directory(editing / "renders", create=True, private=True)
    destination = renders / derivative_id
    if destination.exists() or destination.is_symlink():
        _directory(destination, private=True)
        if _read_manifest(destination / "manifest.json") != archive_manifest:
            raise EditError("Archivergebnis hat eine unvereinbare vorhandene Bindung.")
        if not _matches_file(destination / "audio.wav", size, sha):
            raise EditError("Archivergebnis weicht vom Hashbeleg ab.")
    else:
        _atomic_dir_publish(renders, derivative_id, archive_manifest,
                            (render, "audio.wav", size, sha))
    return {"kind": "audio_edit_render_archived", "edit_id": edit_id,
            "derived_id": derivative_id, "audio": str(destination / "audio.wav"),
            "render_sha256": sha, "original_untouched": True, "verified_current": True}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "finish"):
        cmd = sub.add_parser(name)
        if name == "prepare":
            cmd.add_argument("material_id")
            cmd.add_argument("master_name")
        else:
            cmd.add_argument("edit_id")
        cmd.add_argument("--library-root", type=Path, default=h2_ingest.DEFAULT_LIBRARY_ROOT)
        cmd.add_argument("--edit-root", type=Path, default=DEFAULT_EDIT_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = (prepare(args.material_id, args.master_name, library_root=args.library_root,
                          edit_root=args.edit_root) if args.command == "prepare" else
                  finish(args.edit_id, library_root=args.library_root, edit_root=args.edit_root))
    except (EditError, h2_ingest.H2IngestError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())