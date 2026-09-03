import os
import sys
import pm4py
from pathlib import Path
import config

from pm4py.objects.log.exporter.xes import exporter as xes_exporter
from pm4py.objects.log.importer.xes import importer as xes_importer

from log_preprocessing import preprocess, create_public_logs
from merge import mergeLogs
from check_conformance import check_conformance, generate_results

sys.path.insert(0, str(Path(__file__).parent / "external" / "collaborationminer"))
import collaboration_miner as cm


def main():
    if len(sys.argv) < 2:
        print("Path fehlt")
        return

    exec_num = 1
    logs = []
    dirty_logs = None
    os.makedirs(f"{sys.argv[1]}/merged_logs", exist_ok=True)
    os.makedirs(f"{sys.argv[1]}/output", exist_ok=True)

    # -- find the highest existing execution number
    for f in Path(f"{sys.argv[1]}/merged_logs").iterdir():
        if f.is_file() and f.suffix == ".xes" and f.stem.startswith("merged_log_v"):
            suffix = f.stem.removeprefix("merged_log_v")
            if suffix.isdigit() and int(suffix) >= exec_num:
                exec_num = int(suffix) + 1

    # -- collect all event logs
    for f in Path(sys.argv[1]).iterdir():
        if f.is_file() and f.suffix == ".xes":
            if f.name.startswith("merged_log"):
                suffix = f.stem.removeprefix("merged_log_v")
                if int(suffix) >= exec_num:
                    exec_num = int(suffix) + 1
                continue
            else:
                logs.append(xes_importer.apply(str(f)))
        if f.is_dir() and f.name == "dirty_logs":
            dirty_logs = [(xes_importer.apply(str(file))) for file in f.iterdir()
                          if file.is_file() and file.suffix == ".xes"]

    # -- declare attribute names
    config.ATTRIBUTES = config.AttributeNames(
        log_id="concept:name",
        trace_id="concept:name",
        event_id="concept:name",
        org_group="org:group",
        timestamp="time:timestamp",
        communication_mode="msgType",
        msg_instance_id="msgInstanceId",
        msg_type="msgFlow"
    )

    # -- preprocessing
    if dirty_logs:
        dirty_logs = create_public_logs(dirty_logs)

    preprocess(logs)

    # -- merging
    merged_log = mergeLogs(logs)
    merged_log_path = f"{sys.argv[1]}/merged_logs/merged_log_v{exec_num}.xes"
    xes_exporter.apply(merged_log, merged_log_path)

    merged_dirty_log = None
    if dirty_logs:
        merged_dirty_log = mergeLogs(dirty_logs)

    # -- discover model with collaboration miner
    cpn, return_t = cm.discover(disc_type=pm4py.discover_petri_net_inductive,
                                path=merged_log_path,
                                disc_parameters=None,
                                verbose_return=True)

    pm4py.save_vis_petri_net(*cpn, f"{sys.argv[1]}/output/merged_log_collab_v{exec_num}.svg")
    pm4py.write_pnml(*cpn, f"{sys.argv[1]}/output/petri_vis_v{exec_num}.pnml")

    # -- conformance checking
    result = check_conformance(merged_log, logs, cpn, return_t)
    if result:
        for variant_key, report in result["PartyA"]:
            if report["cost"] > 0:
                print(variant_key, report["extra_activities"], report['frequency'], report["sender_moves"])

    # -- results
    generate_results(result)


if __name__ == '__main__':
    main()
