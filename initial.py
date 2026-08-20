import os
import sys
import pm4py
from pathlib import Path

from pm4py.objects.log.exporter.xes import exporter as xes_exporter
from pm4py.objects.log.importer.xes import importer as xes_importer

from log_preprocessing import preprocess
from merge import mergeLogs

sys.path.insert(0, str(Path(__file__).parent / "external" / "collaborationminer"))
import collaboration_miner as cm


def main():
    if len(sys.argv) < 2:
        print("Path fehlt")
        return

    merged_log_version = 1
    logs = []
    os.makedirs(f"{sys.argv[1]}/output", exist_ok=True)
    for f in Path(sys.argv[1]).iterdir():
        if f.is_file():
            if f.name.startswith("merged_log"):
                suffix = f.stem.removeprefix("merged_log_v")
                if int(suffix) >= merged_log_version:
                    merged_log_version = int(suffix) + 1
                continue
            else:
                logs.append(xes_importer.apply(str(f)))

    # -- preprocessing
    trace_groups = preprocess(logs)

    # -- merging
    merged_log = mergeLogs(logs, trace_groups)

    merged_log_path = f"{sys.argv[1]}/merged_log_v{merged_log_version}.xes"
    xes_exporter.apply(merged_log, merged_log_path)
    print("Logs merged")

    # -- discover model with collaboration miner
    cpn, return_t = cm.discover(disc_type=pm4py.discover_petri_net_inductive,
                                path=merged_log_path,
                                disc_parameters=None,
                                verbose_return=True)

    pm4py.save_vis_petri_net(*cpn, f"{sys.argv[1]}/output/merged_log_collab.svg")
    print("Model discovered")

    # -- conformance checking

    # -- results


if __name__ == '__main__':
    main()
