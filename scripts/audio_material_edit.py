#!/usr/bin/env python3
"""Local, non-destructive edit/render roundtrip for archived H2 WAV material.

The only writer is the explicit local CLI. No external editor, remote endpoint,
PipeWire routing or H2 source file is changed by this module.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import sys
from typing import Iterator

import h2_ingest

SCHEMA = 1
ID_RE = re.compile(r"^[0-9a-f]{24}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_EDIT_ROOT = Path.home() / "Music" / "Audio-Aufnahmen" / "H2-Bearbeitungen"
COPY_SIZE = 1024 * 1024
MAX_MANIFEST = 16 * 1024
MAX_RENDER_BYTES = (2**32) - 1
MAX_WAVE_CHUNKS = 4096
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
RENAME_NOREPLACE = 1


class EditError(RuntimeError):
    """An edit cannot safely proceed."""


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _id(domain: bytes, value: dict) -> str:
    return hashlib.sha256(domain + b"\0" + _canonical(value)).hexdigest()[:24]


def _path_parts(path: Path) -> tuple[str, ...]:
    if not path.is_absolute() or ".." in path.parts:
        raise EditError("Verzeichnispfad muss absolut und ohne Parent-Traversal sein.")
    return tuple(path.parts[1:])


def _private_dir(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise EditError("Bearbeitungsverzeichnis muss privat (0700) und im eigenen Besitz sein.")


@contextmanager
def _directory_tree(path: Path, *, create: bool = False, private: bool = False) -> Iterator[int]:
    """Walk each directory from /, never reopening an unchecked pathname."""
    parts = _path_parts(path.expanduser())
    fd = os.open("/", DIR_FLAGS)
    try:
        for name in parts:
            try:
                next_fd = os.open(name, DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(name, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                next_fd = os.open(name, DIR_FLAGS, dir_fd=fd)
                try:
                    os.fsync(fd)  # persist the newly created entry in its parent
                except BaseException:
                    os.close(next_fd)
                    raise
            os.close(fd)
            fd = next_fd
        if private:
            _private_dir(fd)
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _child_dir(parent_fd: int, name: str, *, create: bool = False,
               private: bool = False) -> Iterator[int]:
    if not name or name in {".", ".."} or "/" in name:
        raise EditError("Ungültiger Verzeichnisname.")
    try:
        fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
        try:
            os.fsync(parent_fd)  # persist working/renders entry before publication
        except BaseException:
            os.close(fd)
            raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise EditError("Verzeichnispfad ist nicht symlinkfrei.") from exc
        raise
    try:
        if private:
            _private_dir(fd)
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _file_at(parent_fd: int, name: str, *, owner: bool = False) -> Iterator[tuple[int, os.stat_result]]:
    if not name or name in {".", ".."} or "/" in name:
        raise EditError("Ungültiger Dateiname.")
    try:
        fd = os.open(name, FILE_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise EditError("Datei muss regulär und symlinkfrei sein.") from exc
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise EditError("Datei ist keine reguläre Datei.")
        if owner and (info.st_uid != os.getuid() or info.st_nlink != 1):
            raise EditError("Renderdatei muss einzeln und im eigenen Besitz liegen.")
        yield fd, info
    finally:
        os.close(fd)


def _generation(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _hash_open(fd: int, opened: os.stat_result) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, COPY_SIZE):
        digest.update(chunk)
    if _generation(os.fstat(fd)) != _generation(opened):
        raise EditError("Dateigeneration änderte sich während der Hashprüfung.")
    return digest.hexdigest()


def _digest_at(parent_fd: int, name: str, *, expected_size: int | None = None,
               owner: bool = False) -> str:
    with _file_at(parent_fd, name, owner=owner) as (fd, opened):
        if expected_size is not None and opened.st_size != expected_size:
            raise EditError("Dateigröße passt nicht zur gebundenen Quelle.")
        return _hash_open(fd, opened)


def _matches_at(parent_fd: int, name: str, size: int, sha256: str,
                *, owner: bool = False) -> bool:
    try:
        return _digest_at(parent_fd, name, expected_size=size, owner=owner) == sha256
    except (EditError, OSError):
        return False


def _verify_master_set_at(master_fd: int, verification: dict) -> None:
    """Recheck every manifest-bound master through the SAME anchored directory."""
    masters = verification.get("masters")
    if (verification.get("verified_current") is not True
        or not isinstance(masters, list) or not masters
        or len(masters) > h2_ingest.MAX_SESSION_FILES):
        raise EditError("H2-Master-Set hat keine vollständige Integritätsbindung.")
    for master in masters:
        if not isinstance(master, dict):
            raise EditError("H2-Master-Set enthält ungültige Metadaten.")
        name, size, sha = master.get("name"), master.get("bytes"), master.get("sha256")
        if (not isinstance(name, str) or h2_ingest.ROLE_RE.fullmatch(name) is None
            or type(size) is not int or not 0 < size <= (2**63 - 1)
            or not isinstance(sha, str) or SHA_RE.fullmatch(sha) is None):
            raise EditError("H2-Master-Set enthält eine ungültige Dateiidentität.")
        if not _matches_at(master_fd, name, size, sha):
            raise EditError("H2-Master-Set änderte sich nach der Archivverifikation.")


def _write_all(fd: int, data: bytes | memoryview) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise EditError("Datei konnte nicht vollständig geschrieben werden.")
        remaining = remaining[written:]


def _copy_checked_at(source_fd: int, source_name: str, target_fd: int, target_name: str,
                     *, expected_sha256: str, expected_size: int) -> None:
    with _file_at(source_fd, source_name) as (src, opened):
        if opened.st_size != expected_size:
            raise EditError("Quelldatei stimmt in der Größe nicht mit dem Herkunftsbeleg überein.")
        dst = os.open(target_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                      | os.O_NOFOLLOW | os.O_CLOEXEC, 0o400, dir_fd=target_fd)
        try:
            digest = hashlib.sha256()
            copied = 0
            while chunk := os.read(src, COPY_SIZE):
                copied += len(chunk)
                if copied > expected_size:
                    raise EditError("Dateikopie überschreitet den Herkunftsbeleg.")
                digest.update(chunk)
                _write_all(dst, chunk)
            os.fchmod(dst, 0o444)
            os.fsync(dst)
            if _generation(opened) != _generation(os.fstat(src)):
                raise EditError("Quelldateigeneration wechselte während der Kopie.")
        finally:
            os.close(dst)
        if copied != expected_size or digest.hexdigest() != expected_sha256:
            raise EditError("Quelldatei stimmt nicht mit ihrem SHA-256-Beleg überein.")
        if _digest_at(target_fd, target_name, expected_size=expected_size) != expected_sha256:
            raise EditError("Zielkopie konnte nicht vollständig verifiziert werden.")


def _read_manifest_at(fd: int) -> dict:
    with _file_at(fd, "manifest.json") as (handle, info):
        if not 0 < info.st_size <= MAX_MANIFEST:
            raise EditError("Manifestgröße ist ungültig.")
        data = os.read(handle, MAX_MANIFEST + 1)
        if _generation(info) != _generation(os.fstat(handle)):
            raise EditError("Manifest änderte sich während des Lesens.")

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise EditError("Manifest enthält doppelte Schlüssel.")
            result[key] = value
        return result

    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise EditError("Manifest ist kein gültiges JSON.") from exc
    if not isinstance(result, dict):
        raise EditError("Manifest ist kein Objekt.")
    return result


def _write_manifest_at(fd: int, manifest: dict) -> None:
    body = _canonical(manifest) + b"\n"
    if len(body) > MAX_MANIFEST:
        raise EditError("Manifest überschreitet seine Größenobergrenze.")
    handle = os.open("manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | os.O_NOFOLLOW | os.O_CLOEXEC, 0o400, dir_fd=fd)
    try:
        _write_all(handle, body)
        os.fchmod(handle, 0o444)
        os.fsync(handle)
    finally:
        os.close(handle)


def _rename_noreplace(source_fd: int, source: str, target_fd: int, target: str) -> None:
    """Linux renameat2(2): atomic publication that can never replace a target."""
    libc = ctypes.CDLL(None, use_errno=True)
    primitive = getattr(libc, "renameat2", None)
    if primitive is None:
        raise EditError("Atomare RENAME_NOREPLACE-Veröffentlichung ist nicht verfügbar.")
    primitive.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    primitive.restype = ctypes.c_int
    if primitive(source_fd, os.fsencode(source), target_fd, os.fsencode(target), RENAME_NOREPLACE) != 0:
        error = ctypes.get_errno()
        if error in (errno.EEXIST, errno.ENOTEMPTY):
            raise EditError("Zielobjekt existiert bereits; Veröffentlichung überschreibt niemals.")
        if error in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise EditError("Dateisystem unterstützt keine sichere No-Replace-Veröffentlichung.")
        raise OSError(error, os.strerror(error))


def _atomic_dir_publish(root_fd: int, final_name: str, manifest: dict,
                        source_fd: int, source_name: str, destination_name: str,
                        *, expected_size: int, expected_sha256: str) -> None:
    """Publish atomically, never deleting a staging directory on failure.

    mkdirat has no return descriptor: a same-user process could swap the name
    before our first open. No failure-path unlink/rmdir can safely claim that
    name belongs to us. Preserve failed staging for explicit later inspection.
    """
    if not ID_RE.fullmatch(final_name):
        raise EditError("Ungültige Ziel-ID.")
    staging = ".audio-edit-staging-" + secrets.token_hex(16)
    os.mkdir(staging, mode=0o700, dir_fd=root_fd)
    with _child_dir(root_fd, staging, private=True) as staging_fd:
        _copy_checked_at(source_fd, source_name, staging_fd, destination_name,
                         expected_sha256=expected_sha256, expected_size=expected_size)
        _write_manifest_at(staging_fd, manifest)
        os.fsync(staging_fd)
    _rename_noreplace(root_fd, staging, root_fd, final_name)
    os.fsync(root_fd)


def _roots(library_root: Path, edit_root: Path) -> tuple[Path, Path]:
    library = h2_ingest._effective_library_root(library_root).expanduser()
    editing = edit_root.expanduser()
    _path_parts(library)
    _path_parts(editing)
    common = os.path.commonpath((str(library), str(editing)))
    if common in (str(library), str(editing)):
        raise EditError("Bearbeitung darf nicht innerhalb des Materialarchivs liegen.")
    return library, editing


def _assert_visible(editing: Path, root_fd: int, section: str, section_fd: int,
                    item_id: str, expected_manifest: dict,
                    filename: str, size: int, sha: str) -> None:
    """Fail closed if a checked fd's directory was renamed or replaced."""
    with _directory_tree(editing, private=True) as live_root:
        if _generation(os.fstat(live_root))[:2] != _generation(os.fstat(root_fd))[:2]:
            raise EditError("Bearbeitungsroot wurde während der Aktion ausgetauscht.")
        with _child_dir(live_root, section, private=True) as live_section:
            if _generation(os.fstat(live_section))[:2] != _generation(os.fstat(section_fd))[:2]:
                raise EditError("Bearbeitungsunterverzeichnis wurde während der Aktion ausgetauscht.")
            with _child_dir(live_section, item_id, private=True) as live_item:
                if (_read_manifest_at(live_item) != expected_manifest
                    or not _matches_at(live_item, filename, size, sha, owner=True)):
                    raise EditError("Veröffentlichtes Audio ist am erwarteten Pfad nicht mehr verifizierbar.")


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


def prepare(material_id: str, master_name: str,
            *, library_root: Path = h2_ingest.DEFAULT_LIBRARY_ROOT,
            edit_root: Path = DEFAULT_EDIT_ROOT) -> dict:
    if not isinstance(material_id, str) or ID_RE.fullmatch(material_id) is None:
        raise EditError("Ungültige Material-ID.")
    if not isinstance(master_name, str) or h2_ingest.ROLE_RE.fullmatch(master_name) is None:
        raise EditError("Ein gültiger H2-Masterdateiname ist erforderlich.")
    library, editing = _roots(library_root, edit_root)
    verified = h2_ingest.verify_material(material_id, library_root=library)
    master = next((item for item in verified["masters"] if item["name"] == master_name), None)
    if master is None:
        raise EditError("Der gewählte Master gehört nicht zum verifizierten Material.")
    manifest = _workspace_manifest(material_id, master, verified["master_set_sha256"])
    with _directory_tree(library) as library_fd, \
         _child_dir(library_fd, material_id) as material_fd, \
         _child_dir(material_fd, "master") as master_fd, \
         _directory_tree(editing, create=True, private=True) as edit_fd, \
         _child_dir(edit_fd, "working", create=True, private=True) as working_fd:
        _verify_master_set_at(master_fd, verified)
        try:
            with _child_dir(working_fd, manifest["edit_id"], private=True) as existing_fd:
                if _read_manifest_at(existing_fd) != manifest:
                    raise EditError("Vorhandener Arbeitsbereich besitzt eine fremde Bindung.")
                if not _matches_at(existing_fd, "input.wav", master["bytes"], master["sha256"],
                                   owner=True):
                    raise EditError("Arbeitskopie wurde verändert; kein automatisches Überschreiben.")
        except FileNotFoundError:
            _atomic_dir_publish(
                working_fd, manifest["edit_id"], manifest, master_fd, master_name, "input.wav",
                expected_size=master["bytes"], expected_sha256=master["sha256"]
            )
        _assert_visible(editing, edit_fd, "working", working_fd, manifest["edit_id"],
                        manifest, "input.wav", master["bytes"], master["sha256"])
    workspace = editing / "working" / manifest["edit_id"]
    return {"kind": "audio_edit_prepared", "edit_id": manifest["edit_id"],
            "working_copy": str(workspace / "input.wav"),
            "expected_render": str(workspace / "render.wav"),
            "source_verified": True, "original_untouched": True}


def _wave_info_at(parent_fd: int, name: str) -> tuple[dict, str, int]:
    with _file_at(parent_fd, name, owner=True) as (fd, info):
        if not 44 <= info.st_size <= MAX_RENDER_BYTES:
            raise EditError("Render-WAV hat eine ungültige oder zu große Dateigröße.")
        with os.fdopen(os.dup(fd), "rb") as stream:
            header = stream.read(12)
            if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
                raise EditError("Rendering muss eine RIFF/WAVE-Datei sein.")
            if struct.unpack("<I", header[4:8])[0] != info.st_size - 8:
                raise EditError("RIFF-Dateilänge ist inkonsistent.")
            pos, fmt, data_size = 12, None, None
            for _ in range(MAX_WAVE_CHUNKS):
                if pos == info.st_size:
                    break
                if pos + 8 > info.st_size:
                    raise EditError("WAV-Chunkheader ist unvollständig.")
                stream.seek(pos)
                raw_header = stream.read(8)
                if len(raw_header) != 8:
                    raise EditError("WAV-Chunkheader ist unvollständig.")
                chunk_id, length = struct.unpack("<4sI", raw_header)
                end = pos + 8 + length
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
            digest = hashlib.sha256()
            while chunk := stream.read(COPY_SIZE):
                digest.update(chunk)
        if _generation(info) != _generation(os.fstat(fd)):
            raise EditError("Render-WAV änderte sich während seiner Format- und Hashprüfung.")
    codec, channels, rate, byte_rate, block_bytes, bits = fmt
    if (codec, bits) not in {(1, 16), (1, 24), (1, 32), (3, 32)}:
        raise EditError("Nur PCM 16/24/32 oder Float32 WAV wird unterstützt.")
    if not 1 <= channels <= 2 or not 8000 <= rate <= 192000:
        raise EditError("WAV-Kanäle oder Abtastrate sind ungültig.")
    if block_bytes != channels * bits // 8 or byte_rate != block_bytes * rate or data_size % block_bytes:
        raise EditError("WAV-Blockgröße oder Byterate ist inkonsistent.")
    return ({"codec": "float32" if codec == 3 else f"pcm{bits}",
             "channels": channels, "sample_rate_hz": rate, "frames": data_size // block_bytes},
            digest.hexdigest(), info.st_size)


def _wave_info(path: Path) -> tuple[dict, str, int]:
    """Read-only inspection helper; production finish uses the already held fd."""
    with _directory_tree(path.parent) as parent_fd:
        return _wave_info_at(parent_fd, path.name)


def _valid_workspace_manifest(manifest: dict, edit_id: str) -> dict:
    source = manifest.get("source")
    if not isinstance(source, dict) or set(source) != {
        "material_id", "master_name", "master_sha256", "master_bytes", "master_set_sha256"
    }:
        raise EditError("Arbeitsbereich besitzt keine gültige Herkunftsbindung.")
    material_id, name = source["material_id"], source["master_name"]
    size, sha, set_sha = source["master_bytes"], source["master_sha256"], source["master_set_sha256"]
    if (not isinstance(material_id, str) or ID_RE.fullmatch(material_id) is None
        or not isinstance(name, str) or h2_ingest.ROLE_RE.fullmatch(name) is None
        or type(size) is not int or not 0 < size <= (2**63 - 1)
        or not isinstance(sha, str) or SHA_RE.fullmatch(sha) is None
        or not isinstance(set_sha, str) or SHA_RE.fullmatch(set_sha) is None
        or manifest != _workspace_manifest(material_id, {
            "name": name, "sha256": sha, "bytes": size}, set_sha)
        or manifest.get("edit_id") != edit_id):
        raise EditError("Arbeitsbereich ist nicht eindeutig an seinen Master gebunden.")
    return source


def finish(edit_id: str, *, library_root: Path = h2_ingest.DEFAULT_LIBRARY_ROOT,
           edit_root: Path = DEFAULT_EDIT_ROOT) -> dict:
    if not isinstance(edit_id, str) or ID_RE.fullmatch(edit_id) is None:
        raise EditError("Ungültige Bearbeitungs-ID.")
    library, editing = _roots(library_root, edit_root)
    with _directory_tree(editing, private=True) as edit_fd, \
         _child_dir(edit_fd, "working", private=True) as working_fd, \
         _child_dir(working_fd, edit_id, private=True) as workspace_fd:
        source = _valid_workspace_manifest(_read_manifest_at(workspace_fd), edit_id)
        verified = h2_ingest.verify_material(source["material_id"], library_root=library)
        master = next((item for item in verified["masters"] if item["name"] == source["master_name"]), None)
        if (master is None or master["sha256"] != source["master_sha256"]
            or master["bytes"] != source["master_bytes"]
            or verified["master_set_sha256"] != source["master_set_sha256"]):
            raise EditError("Ursprungs-Master kann nicht mehr nachgewiesen werden.")
        # Check immutable library master again through fd-relative source path,
        # rather than trusting an intermediate library directory symlink.
        with _directory_tree(library) as library_fd, \
             _child_dir(library_fd, source["material_id"]) as material_fd, \
             _child_dir(material_fd, "master") as master_fd:
            _verify_master_set_at(master_fd, verified)
        if not _matches_at(workspace_fd, "input.wav", source["master_bytes"], source["master_sha256"],
                           owner=True):
            raise EditError("Arbeitskopie wurde verändert; Masterbindung ungültig.")
        wav, sha, size = _wave_info_at(workspace_fd, "render.wav")
        derivation = {"source": source, "render": {"sha256": sha, "bytes": size, "wav": wav}}
        derivative_id = _id(b"audio-edit-render-v1", derivation)
        archive_manifest = {"schema_version": SCHEMA, "kind": "audio_edit_render",
                            "derived_id": derivative_id, "edit_id": edit_id, **derivation}
        with _child_dir(edit_fd, "renders", create=True, private=True) as renders_fd:
            try:
                with _child_dir(renders_fd, derivative_id, private=True) as existing_fd:
                    if _read_manifest_at(existing_fd) != archive_manifest:
                        raise EditError("Archivergebnis hat eine unvereinbare vorhandene Bindung.")
                    if not _matches_at(existing_fd, "audio.wav", size, sha):
                        raise EditError("Archivergebnis weicht vom Hashbeleg ab.")
            except FileNotFoundError:
                _atomic_dir_publish(renders_fd, derivative_id, archive_manifest,
                                    workspace_fd, "render.wav", "audio.wav",
                                    expected_size=size, expected_sha256=sha)
            _assert_visible(editing, edit_fd, "renders", renders_fd, derivative_id,
                            archive_manifest, "audio.wav", size, sha)
    return {"kind": "audio_edit_render_archived", "edit_id": edit_id,
            "derived_id": derivative_id, "audio": str(editing / "renders" / derivative_id / "audio.wav"),
            "render_sha256": sha, "original_untouched": True, "verified_current": True}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "finish"):
        command = sub.add_parser(name)
        if name == "prepare":
            command.add_argument("material_id")
            command.add_argument("master_name")
        else:
            command.add_argument("edit_id")
        command.add_argument("--library-root", type=Path, default=h2_ingest.DEFAULT_LIBRARY_ROOT)
        command.add_argument("--edit-root", type=Path, default=DEFAULT_EDIT_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = (prepare(args.material_id, args.master_name,
                          library_root=args.library_root, edit_root=args.edit_root)
                  if args.command == "prepare" else
                  finish(args.edit_id, library_root=args.library_root, edit_root=args.edit_root))
    except (EditError, h2_ingest.H2IngestError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())