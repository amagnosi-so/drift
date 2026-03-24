from __future__ import annotations

import os
import shutil
import tarfile

from driftpkg.utils import mkdir, verify_blob


def detect_compression(path: str) -> str:
    with open(path, "rb") as f:
        magic = f.read(2)
    if magic == b"\x1f\x8b":
        return "gz"
    return "tar"


def safe_extract(tar: tarfile.TarFile, path: str) -> None:
    for member in tar.getmembers():
        try:
            if member.isdev() or member.issym():
                continue
            tar.extract(member, path, set_attrs=False)
        except PermissionError:
            print(f"[!] Permission skipped: {member.name}")
        except OSError as e:
            print(f"[!] OS error skipped: {member.name} ({e})")
        except Exception as e:
            print(f"[!] Skipping: {member.name} ({e})")


def extract_layer(blob_path: str, fs_dir: str, marker_dir: str, quarantine_dir: str) -> None:
    layer_id = os.path.basename(blob_path)
    marker = os.path.join(marker_dir, layer_id + ".done")

    if os.path.exists(marker):
        print(f"[=] Skip extracted {layer_id}")
        return

    print(f"[+] Extract {layer_id}")

    try:
        if not verify_blob(blob_path, layer_id.replace("_", ":")):
            raise RuntimeError("Integrity failed")

        comp = detect_compression(blob_path)
        if comp == "gz":
            with tarfile.open(blob_path, "r:gz") as tar:
                safe_extract(tar, fs_dir)
        else:
            with tarfile.open(blob_path, "r:") as tar:
                safe_extract(tar, fs_dir)
    except Exception as e:
        print(f"[!] Extraction failed → quarantining: {e}")
        mkdir(quarantine_dir)
        shutil.move(blob_path, os.path.join(quarantine_dir, layer_id))
        return

    with open(marker, "w") as f:
        f.write("ok")
