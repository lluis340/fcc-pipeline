import os
import sys
from functools import partial

import pm4py
from pathlib import Path
import config

from pm4py.objects.log.exporter.xes import exporter as xes_exporter
from pm4py.objects.log.importer.xes import importer as xes_importer

from log_preprocessing import preprocess
from merge import mergeLogs
from check_conformance import check_conformance

sys.path.insert(0, str(Path(__file__).parent / "external" / "collaborationminer"))
import collaboration_miner as cm


def main(custom_attribute_names=False, **kwargs):
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

    ''' DECLARE ATTRIBUTE NAMES '''
    if custom_attribute_names:
        config.set_attributes(**kwargs)

    ''' PREPROCESSING '''
    preprocess(logs)

    ''' MERGING '''
    merged_log = mergeLogs(logs)
    merged_log_path = f"{sys.argv[1]}/merged_logs/merged_log_v{exec_num}.xes"
    xes_exporter.apply(merged_log, merged_log_path)

    ''' DISCOVER MODEL WITH COLLABORATION MINER '''
    disc_type = partial(pm4py.discover_petri_net_inductive, noise_threshold=0.3)
    cpn, return_t = cm.discover(disc_type=disc_type,
                                path=merged_log_path,
                                disc_parameters=None,
                                verbose_return=True)

    pm4py.save_vis_petri_net(*cpn, f"{sys.argv[1]}/output/merged_log_collab_v{exec_num}.svg")
    pm4py.write_pnml(*cpn, f"{sys.argv[1]}/output/petri_vis_v{exec_num}.pnml")

    ''' CONFORMANCE CHECKING '''
    result, vis_result = check_conformance(merged_log, logs, cpn, return_t)

    ''' PROCESS PIPELINE AGAIN FOR DIRTY LOGS '''
    if dirty_logs:
        preprocess(dirty_logs)
        merged_dirty_log = mergeLogs(dirty_logs)
        dirty_result, vis_dirty_result = check_conformance(merged_dirty_log, dirty_logs, cpn, return_t, dirty=True)
        print_result_to_markdown(sys.argv[1], vis_result, dirty_vis_result=vis_dirty_result, exec_num=exec_num)
        return result, vis_result, dirty_result, vis_dirty_result

    print_result_to_markdown(sys.argv[1], vis_result, exec_num=exec_num)

    return result, vis_result


def print_result_to_markdown(path, vis_result, dirty_vis_result=None, exec_num=1):
    svg_filename = f"merged_log_collab_v{exec_num}.svg"
    with open(f"{path}/output/result_{exec_num}.md", "w", encoding="utf-8") as f:
        f.write("# RESULTS\n\n")
        f.write(f"![Collaborative Petri Net]({svg_filename})\n\n")
        f.write(f"{vis_result}\n")
        if dirty_vis_result is not None:
            f.write(f"\n{dirty_vis_result}\n")


if __name__ == '__main__':
    main()
