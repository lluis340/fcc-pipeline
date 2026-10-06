import os
import sys
import time

import pm4py
from pathlib import Path
import config

from pm4py.objects.log.exporter.xes import exporter as xes_exporter
from pm4py.objects.log.importer.xes import importer as xes_importer

from log_preprocessing import preprocess
from merge import mergeLogs
from check_conformance import check_conformance, get_globals
from check_local_conformance import check_local_conformance, check_private_layer
from process_reference_model import process_models

sys.path.insert(0, str(Path(__file__).parent / "external" / "collaborationminer"))
import collaboration_miner as cm


def main(custom_attribute_names=False, **kwargs):
    if len(sys.argv) < 2:
        print("Path fehlt")
        return

    exec_num = 1
    private_logs = []
    models = None
    dirty_private_logs = None
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
                private_logs.append(xes_importer.apply(str(f)))
        # -- collect dirty logs
        if f.is_dir() and f.name == "dirty_logs":
            dirty_private_logs = [xes_importer.apply(str(file)) for file in f.iterdir()
                                  if file.is_file() and file.suffix == ".xes"]

    # -- collect models
    for f in Path(sys.argv[1]).iterdir():
        if f.is_dir() and f.name == "models":
            if len([1 for f in f.iterdir() if f.is_file()]) == len(private_logs):
                models = process_models(f)

    ''' DECLARE ATTRIBUTE NAMES '''
    if custom_attribute_names:
        config.set_attributes(**kwargs)

    ''' PREPROCESSING '''
    public_logs = preprocess(private_logs)

    ''' MERGING '''
    merged_log = mergeLogs(public_logs)
    merged_log_path = f"{sys.argv[1]}/merged_logs/merged_log_v{exec_num}.xes"
    xes_exporter.apply(merged_log, merged_log_path)

    ''' DISCOVER MODEL WITH COLLABORATION MINER '''
    disc_type = pm4py.discover_petri_net_inductive
    cpn, return_t = cm.discover(disc_type=disc_type,
                                path=merged_log_path,
                                disc_parameters=None,
                                verbose_return=True)
    pm4py.save_vis_petri_net(*cpn, f"{sys.argv[1]}/output/merged_log_collab_v{exec_num}.svg")
    pm4py.write_pnml(*cpn, f"{sys.argv[1]}/output/petri_vis_v{exec_num}.pnml")

    ''' CONFORMANCE CHECKING '''
    local_cc_results = None
    if models:
        local_cc_results = {}
        for concept, model in models.items():
            res_log = [log for log in private_logs if log.attributes.get(config.ATTRIBUTES.log_id) == concept]
            local_cc_results[concept] = check_local_conformance(res_log[0], model)

    result, vis_result = check_conformance(merged_log, public_logs, cpn, return_t)

    private_layer_result = None
    if models:
        global_params = get_globals()
        private_layer_result = check_private_layer(local_cc_results, result.get("Alignment_Report"), global_params)

    ''' PROCESS PIPELINE AGAIN FOR DIRTY LOGS '''
    if dirty_private_logs:
        dirty_public_logs = preprocess(dirty_private_logs)
        merged_dirty_log = mergeLogs(dirty_public_logs)
        dirty_local_cc_results = None
        if models:
            dirty_local_cc_results = {}
            for concept, model in models.items():
                res_log = [log for log in dirty_private_logs if log.attributes.get(config.ATTRIBUTES.log_id) == concept]
                dirty_local_cc_results[concept] = check_local_conformance(res_log[0], model)
        dirty_result, vis_dirty_result = check_conformance(merged_dirty_log, dirty_public_logs, cpn, return_t, dirty=True)

        dirty_private_layer_result = None
        if models:
            dirty_global_params = get_globals()
            dirty_private_layer_result = check_private_layer(dirty_local_cc_results, dirty_result.get("Alignment_Report"),
                                                              dirty_global_params)

        print_result_to_markdown(sys.argv[1], vis_result, dirty_vis_result=vis_dirty_result, exec_num=exec_num,
                                 private_layer_result=private_layer_result,
                                 dirty_private_layer_result=dirty_private_layer_result)

        return result, vis_result, dirty_result, vis_dirty_result

    print_result_to_markdown(sys.argv[1], vis_result, exec_num=exec_num, private_layer_result=private_layer_result)
    return result, vis_result


def format_fcc_reference_costs(private_layer_result):
    lines = ["", "### Costs from FCC with reference Model", "",
             "| Organization | Local Alignment Cost | Local Fitness | Communication Cost |",
             "|---|---|---|---|"]
    for concept, org in private_layer_result["Organizations"].items():
        lines.append(f"| {concept} | {org['local_alignment_cost']:.4f} | {org['local_fitness']:.4f} | "
                      f"{org['communication_cost']:.4f} |")
    lines.append("")
    lines.append(f"**Total Federated Alignment Cost:** {private_layer_result['Total_Federated_Alignment_Cost']:.4f}")
    lines.append("")
    return "\n".join(lines)


def print_result_to_markdown(path, vis_result, dirty_vis_result=None, exec_num=1,
                             private_layer_result=None, dirty_private_layer_result=None):
    svg_filename = f"merged_log_collab_v{exec_num}.svg"
    with open(f"{path}/output/result_{exec_num}.md", "w", encoding="utf-8") as f:
        f.write("# RESULTS\n\n")
        f.write(f"![Collaborative Petri Net]({svg_filename})\n\n")
        f.write(f"{vis_result}\n")
        if private_layer_result is not None:
            f.write(format_fcc_reference_costs(private_layer_result))
        if dirty_vis_result is not None:
            f.write(f"\n{dirty_vis_result}\n")
            if dirty_private_layer_result is not None:
                f.write(format_fcc_reference_costs(dirty_private_layer_result))


if __name__ == '__main__':
    main()
