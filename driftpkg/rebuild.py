from __future__ import annotations

import json
import os
import subprocess


def build_docker_image(fs_dir: str, config_path: str, image_name: str) -> None:
    if (
        subprocess.run(
            ["docker", "image", "inspect", image_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    ):
        print(f"[=] Exists {image_name}")
        return

    with open(config_path) as f:
        cfg = json.load(f)

    runtime = cfg.get("config", {})
    changes: list[str] = []

    if runtime.get("Cmd"):
        changes.append(f'CMD {json.dumps(runtime["Cmd"])}')
    if runtime.get("Entrypoint"):
        changes.append(f'ENTRYPOINT {json.dumps(runtime["Entrypoint"])}')
    for env in runtime.get("Env", []):
        changes.append(f"ENV {env}")
    if runtime.get("WorkingDir"):
        changes.append(f'WORKDIR {runtime["WorkingDir"]}')
    if runtime.get("User"):
        changes.append(f'USER {runtime["User"]}')

    tar_path = os.path.join(fs_dir, "../fs.tar")
    subprocess.run(["tar", "-C", fs_dir, "-cf", tar_path, "."], check=True)

    cmd = ["docker", "import"]
    for c in changes:
        cmd.extend(["--change", c])
    cmd.extend([tar_path, image_name])

    subprocess.run(cmd, check=True)
    print(f"[✓] Built {image_name}")
