import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pm4py

from split_collab_model import create_org_wide_bpmn_models

LOGS = ["artificial1", "artificial2", "artificial3",  "artificial4", "artificial5",  "real1", "real4"]


def remove_folder(path):
    def make_writable_and_retry(func, file_path, _):
        os.chmod(file_path, stat.S_IWRITE)
        func(file_path)

    shutil.rmtree(path, onerror=make_writable_and_retry)


def clone_corradini_data():
    os.makedirs("corradini", exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp())
    clone_dir = temp_dir / "colliery_validation"

    try:
        subprocess.run(["git", "clone", "https://bitbucket.org/proslabteam/colliery_validation.git",
                        str(clone_dir)], check=True)

        for log in LOGS:
            target_path = Path("corradini") / log
            if target_path.exists():
                remove_folder(target_path)

            shutil.move(str(clone_dir / log), str(target_path))
            reference_model = "collaboration_discovered_inductive.bpmn" \
                if target_path.name == 'real1' else "model.bpmn"
            for file in target_path.iterdir():
                if file.suffix == ".bpmn" and file.name == reference_model:
                    continue
                if file.name.startswith("collectivelog") or file.suffix != ".xes":
                    file.unlink()
    finally:
        remove_folder(temp_dir)


def split_models():
    for log in LOGS:
        log_path = Path("corradini") / log
        model_path = next(log_path.glob("*.bpmn"))
        if not model_path:
            continue
        output_path = log_path / "models"
        os.makedirs(output_path, exist_ok=True)
        create_org_wide_bpmn_models(str(model_path), str(output_path))
        model_path.unlink()


def apply_log_changes():
    for log in LOGS:
        for file in (Path("corradini") / log).glob("*.xes"):
            event_log = pm4py.read_xes(str(file), return_legacy_log_object=True)
            event_log.attributes["concept:name"] = file.stem
            pm4py.write_xes(event_log, str(file))


def main():
    clone_corradini_data()
    split_models()
    apply_log_changes()


if __name__ == "__main__":
    main()
