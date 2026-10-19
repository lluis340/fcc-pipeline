import contextlib
import io
import json
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, "..")
sys.path.insert(0, "../external/collaborationminer")

import pm4py
from pm4py.objects.log.importer.xes import importer as xes_importer

import collaboration_miner as cm
import config
import inject_miscommunication
from check_conformance import check_conformance, compute_fitness, get_globals, CASE_MOVE_CATEGORIES
from check_local_conformance import check_local_conformance, check_private_layer
from log_preprocessing import preprocess
from merge import merge_logs
from process_reference_model import process_models, ReferenceModelError

LOGS = ["artificial1", "artificial2", "artificial3",  "artificial4", "artificial5",  "real1", "real4"]
LEVELS = [("10", "0.1", "1"), ("20", "0.2", "2"), ("30", "0.3", "3")]


def set_attributes(log):
    if log == "real1":
        config.set_attributes(msg_instance_id="msgInstanceID", msg_type="msgName", comm_mode="msgRole")
    else:
        config.set_attributes(msg_instance_id="msgInstanceId", msg_type="msgFlow", comm_mode="msgType")


def load_logs(log_path):
    return [xes_importer.apply(str(file), parameters={"show_progress_bar": False})
            for file in sorted(log_path.glob("*.xes"))]


def load_models(log_path, logs):
    try:
        models = process_models(log_path / "models")
    except ReferenceModelError as e:
        print(f"Optional layer skipped: {e}")
        return None
    log_ids = {file.stem.lower(): log.attributes[config.ATTRIBUTES.log_id]
               for file, log in zip(sorted(log_path.glob("*.xes")), logs)}
    return {log_ids[name.lower()]: model for name, model in models.items()}


def inject_errors(log_path):
    for level, share, errors_per_trace in LEVELS:
        argv = [str(log_path), "--level", share, "--log-subdir", f"dirty_logs_{level}", "--unit", "trace",
                "--errors-per-trace", errors_per_trace, "--seed", "42"]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            inject_miscommunication.main(argv)
        (log_path / f"dirty_logs_{level}" / "injection_stdout.txt").write_text(stdout.getvalue(), encoding="utf-8")


def discover_cpn(logs, output_path):
    merged_log = merge_logs(preprocess(deepcopy(logs)))
    merged_log_path = output_path / "merged_clean_log.xes"
    pm4py.write_xes(merged_log, str(merged_log_path))
    cpn, return_t = cm.discover(disc_type=pm4py.discover_petri_net_inductive, path=str(merged_log_path),
                                disc_parameters=None, verbose_return=True)
    pm4py.write_pnml(*cpn, str(output_path / "cpn_clean.pnml"))
    return cpn, return_t


def run_cc(private_logs, cpn, return_t, models, dirty):
    public_logs = preprocess(private_logs)
    merged_log = merge_logs(public_logs)

    local_results = None
    if models:
        local_results = {}
        for log in private_logs:
            log_id = log.attributes[config.ATTRIBUTES.log_id]
            local_results[log_id] = check_local_conformance(log, models[log_id])

    result, vis = check_conformance(merged_log, public_logs, deepcopy(cpn), return_t, dirty=dirty)
    private_result = check_private_layer(local_results, result["Alignment_Report"], get_globals()) if models else None

    out = {
        "fitness": result["Fitness"],
        "precision": result["Precision"],
        "generalization": result["Generalization"],
        "simplicity": result["Simplicity"],
        "org_fitness": {},
        "entries": {},
        "local": None,
        "vis": vis,
    }
    for log_id, variants in result["Alignment_Report"].items():
        out["org_fitness"][log_id] = compute_fitness(report for _, report in variants)
        counts = {category: 0 for category in CASE_MOVE_CATEGORIES}
        for _, report in variants:
            for case in report["cases"]:
                for category in CASE_MOVE_CATEGORIES:
                    counts[category] += len(case[category])
        out["entries"][log_id] = counts
    if private_result:
        out["local"] = {log_id: {key: org[key] for key in ("local_alignment_cost", "local_fitness", "communication_cost")}
                        for log_id, org in private_result["Organizations"].items()}
        out["total_federated_alignment_cost"] = private_result["Total_Federated_Alignment_Cost"]
    return out


def evaluate_log(log_path):
    output_path = log_path / "output"
    output_path.mkdir(exist_ok=True)
    set_attributes(log_path.name)

    inject_errors(log_path)

    clean_logs = load_logs(log_path)
    cpn, return_t = discover_cpn(clean_logs, output_path)
    models = load_models(log_path, clean_logs)

    results = {"0": run_cc(load_logs(log_path), cpn, return_t, models, dirty=False)}
    for level, _, _ in LEVELS:
        results[level] = run_cc(load_logs(log_path / f"dirty_logs_{level}"), cpn, return_t, models, dirty=True)

    for level, result in results.items():
        (output_path / f"report_level_{level}.md").write_text(result.pop("vis"), encoding="utf-8")
    (output_path / "results.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")


def main():
    if all(any((Path("corradini") / log).glob("*.xes")) for log in LOGS):
        for log in LOGS:
            evaluate_log(Path("corradini") / log)
    else:
        print("Corradini logs not found, run evaluation_setup.py first. Only the synthetic logs are evaluated.")
    evaluate_log(Path("synthetic_model"))


if __name__ == '__main__':
    main()
