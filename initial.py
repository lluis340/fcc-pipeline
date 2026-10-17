import os
import sys
sys._pm4py_welcome_shown = True

import pm4py
from pathlib import Path
import config

from pm4py.objects.log.exporter.xes import exporter as xes_exporter
from pm4py.objects.log.importer.xes import importer as xes_importer

from log_preprocessing import preprocess
from merge import mergeLogs
from check_conformance import check_conformance, get_globals
from check_local_conformance import check_local_conformance, check_private_layer
from process_reference_model import translate_model

sys.path.insert(0, str(Path(__file__).parent / "external" / "collaborationminer"))
import collaboration_miner as cm


def main(path, custom_attribute_names=False, **kwargs):
    if not path:
        print("ERROR: Path is missing")
        sys.exit()
    if not Path(path).is_dir():
        print("ERROR: Path does not exist")
        sys.exit()

    ''' DECLARE ATTRIBUTE NAMES '''
    if custom_attribute_names:
        config.set_attributes(**kwargs)

    ''' COLLECT INPUT LOGS (AND MODELS) '''
    exec_num = 1
    private_logs = []
    models = None
    dirty_private_logs = None
    os.makedirs(f"{path}/merged_logs", exist_ok=True)
    os.makedirs(f"{path}/output", exist_ok=True)

    # -- find the highest existing execution number
    for f in Path(f"{path}/merged_logs").iterdir():
        if f.is_file() and f.suffix == ".xes" and f.stem.startswith("merged_log_v"):
            suffix = f.stem.removeprefix("merged_log_v")
            if suffix.isdigit() and int(suffix) >= exec_num:
                exec_num = int(suffix) + 1

    # -- collect all event logs
    file_name_to_log_id = {}
    for f in Path(path).iterdir():
        if f.is_file() and f.suffix == ".xes":
            log = xes_importer.apply(str(f))
            private_logs.append(log)
            file_name_to_log_id[f.stem] = log.attributes.get(config.ATTRIBUTES.log_id)
        # -- collect dirty logs
        if f.is_dir() and f.name == "dirty_logs":
            dirty_private_logs = [xes_importer.apply(str(file)) for file in f.iterdir()
                                  if file.is_file() and file.suffix == ".xes"]

    # -- collect models
    log_id_to_model = {}
    for f in Path(path).iterdir():
        if f.is_dir() and f.name == "models":
            if len([1 for file in f.iterdir() if file.is_file() and file.suffix == ".bpmn"]) == len(private_logs):
                models = {model.stem: translate_model(Path(model)) for model in f.iterdir()
                          if model.is_file() and model.suffix == ".bpmn"}
    if models:
        for file_name, model in models.items():
            if file_name not in file_name_to_log_id:
                print(f"No event log {file_name}.xes found for model {file_name}.bpmn")
                sys.exit()
            if not file_name_to_log_id[file_name]:
                print(f"Event log {file_name}.xes has no log attribute {config.ATTRIBUTES.log_id!r}.")
                sys.exit()

            log_id_to_model[file_name_to_log_id[file_name]] = model

    ''' PREPROCESSING '''
    public_logs = preprocess(private_logs)

    ''' MERGING '''
    merged_log = mergeLogs(public_logs)
    merged_log_path = f"{path}/merged_logs/merged_log_v{exec_num}.xes"
    xes_exporter.apply(merged_log, merged_log_path)

    ''' DISCOVER MODEL WITH COLLABORATION MINER '''
    disc_type = pm4py.discover_petri_net_inductive
    cpn, return_t = cm.discover(disc_type=disc_type,
                                path=merged_log_path,
                                disc_parameters=None,
                                verbose_return=True)
    pm4py.save_vis_petri_net(*cpn, f"{path}/output/cPN_v{exec_num}.svg")
    pm4py.write_pnml(*cpn, f"{path}/output/cPN_v{exec_num}.pnml")

    ''' CONFORMANCE CHECKING '''
    local_cc_results = None
    if models:
        local_cc_results = {}
        for log_id, model in log_id_to_model.items():
            res_log = [log for log in private_logs if log.attributes.get(config.ATTRIBUTES.log_id) == log_id]
            local_cc_results[log_id] = check_local_conformance(res_log[0], model)

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
            for log_id, model in log_id_to_model.items():
                dirty_res_log = [log for log in dirty_private_logs if
                                 log.attributes.get(config.ATTRIBUTES.log_id) == log_id]
                dirty_local_cc_results[log_id] = check_local_conformance(dirty_res_log[0], model)
        dirty_result, dirty_vis_result = check_conformance(merged_dirty_log, dirty_public_logs, cpn, return_t,
                                                           dirty=True)

        dirty_private_layer_result = None
        if models:
            dirty_global_params = get_globals()
            dirty_private_layer_result = check_private_layer(dirty_local_cc_results,
                                                             dirty_result.get("Alignment_Report"),
                                                             dirty_global_params)

        print_result_to_markdown(path, vis_result, dirty_vis_result=dirty_vis_result, exec_num=exec_num,
                                 private_layer_result=private_layer_result,
                                 dirty_private_layer_result=dirty_private_layer_result)

        return {
            "result": result,
            "vis_result": vis_result,
            "private_result": private_layer_result,
            "vis_private_result": format_fcc_reference_costs(private_layer_result),
            "dirty_result": dirty_result,
            "dirty_vis_result": dirty_vis_result,
            "dirty_private_result": dirty_private_layer_result,
            "dirty_vis_private_result": format_fcc_reference_costs(dirty_private_layer_result)
        }

    else:
        print_result_to_markdown(path, vis_result, exec_num=exec_num, private_layer_result=private_layer_result)
        return {
            "result": result,
            "vis_result": vis_result,
            "private_result": private_layer_result,
            "vis_private_result": format_fcc_reference_costs(private_layer_result)
        }


def format_fcc_reference_costs(private_layer_result):
    if not private_layer_result:
        return None

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
    svg_filename = f"cPN_v{exec_num}.svg"
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
    if len(sys.argv) < 2:
        print("ERROR: Path is missing")
        sys.exit()
    if not Path(sys.argv[1]).is_dir():
        print("ERROR: Path does not exist")
        sys.exit()
    main(path=sys.argv[1])
