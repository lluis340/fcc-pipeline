import pm4py
from pm4py.objects.petri_net.utils import petri_utils

import config

from collections import defaultdict
from copy import deepcopy
from pm4py.algo.conformance.alignments.petri_net import algorithm as alignments
from pm4py.algo.conformance.alignments.petri_net.algorithm import Parameters as Params
from pm4py.objects.petri_net.utils.align_utils import STD_MODEL_LOG_MOVE_COST

activity_to_cp = {}
uf = None


def check_local_conformance(private_log, pn):
    net, i_m, f_m = build_inner_net(pn)
    I, O = pn[3], pn[4]

    trace_variants = pm4py.get_variants(private_log,
                                        activity_key=config.ATTRIBUTES.event_id,
                                        timestamp_key=config.ATTRIBUTES.timestamp,
                                        case_id_key=config.ATTRIBUTES.trace_id)

    variant_cases = {variant_key: [(private_log.attributes[config.ATTRIBUTES.log_id],
                                    trace.attributes[config.ATTRIBUTES.trace_id])
                                   for trace in traces]
                     for variant_key, traces in trace_variants.items()}

    sync_cost_function = {t: 0 for t in net.transitions}
    model_cost_function = {t: 0 if t.label is None else STD_MODEL_LOG_MOVE_COST for t in net.transitions}

    local_alignments = {}
    for variant_key, traces in trace_variants.items():
        parameters = {
            Params.PARAM_MODEL_COST_FUNCTION: model_cost_function,
            Params.PARAM_SYNC_COST_FUNCTION: sync_cost_function,
            Params.PARAM_TRACE_COST_FUNCTION: [STD_MODEL_LOG_MOVE_COST] * len(traces[0]),
            Params.ACTIVITY_KEY: config.ATTRIBUTES.event_id
        }
        local_alignments[variant_key] = alignments.apply(traces[0], net, i_m, f_m,
                                                         variant=alignments.VERSION_STATE_EQUATION_A_STAR,
                                                         parameters=parameters
                                                         )

    comm_transitions = {}
    for place in I | O:
        for arc in (place.in_arcs | place.out_arcs):
            transition = arc.source if arc.target is place else arc.target
            if transition.label is not None:
                comm_transitions.setdefault(transition.label, set()).add(transition.name)

    silenced_nets = [(label, build_inner_net(pn, silence_transition_names=names))
                     for label, names in comm_transitions.items()]

    local_communication_cost = {}
    for label, (io_net, io_i_m, io_f_m) in silenced_nets:
        io_sync_cost_function = {t: 0 for t in io_net.transitions}
        io_model_cost_function = {t: 0 if t.label is None else STD_MODEL_LOG_MOVE_COST
                                  for t in io_net.transitions}
        local_communication_cost[label] = {}

        for variant_key, traces in trace_variants.items():
            parameters = {
                Params.PARAM_MODEL_COST_FUNCTION: io_model_cost_function,
                Params.PARAM_SYNC_COST_FUNCTION: io_sync_cost_function,
                Params.PARAM_TRACE_COST_FUNCTION: [STD_MODEL_LOG_MOVE_COST] * len(traces[0]),
                Params.ACTIVITY_KEY: config.ATTRIBUTES.event_id
            }
            alignment = alignments.apply(traces[0], io_net, io_i_m, io_f_m,
                                         variant=alignments.VERSION_STATE_EQUATION_A_STAR,
                                         parameters=parameters
                                         )
            delta = (alignment["cost"] - local_alignments[variant_key]["cost"]) / STD_MODEL_LOG_MOVE_COST
            local_communication_cost[label][variant_key] = delta if delta > 0 else 0.0

    return {composed_id: {
        "lac": local_alignments[variant_key]["cost"] / STD_MODEL_LOG_MOVE_COST,
        "lcc": {label: costs[variant_key] for label, costs in local_communication_cost.items()},
        "bwc": local_alignments[variant_key]["bwc"] / STD_MODEL_LOG_MOVE_COST,
    } for variant_key, composed_ids in variant_cases.items() for composed_id in composed_ids}


def build_inner_net(pn, silence_transition_names=None):
    net, i_m, f_m, I, O = deepcopy(pn)
    if silence_transition_names:
        for transition in net.transitions:
            if transition.name in silence_transition_names:
                transition.label = None

    for place in I | O:
        petri_utils.remove_place(net, place)

    return net, i_m, f_m


DEVIATION_KEYS = ("sender_moves", "receiver_moves", "async_communications")


def check_private_layer(local_cc_results, alignment_report, global_params):
    global activity_to_cp, uf
    activity_to_cp = global_params.get("activity_to_cp")
    uf = global_params.get("uf")

    deviating, per_org = collect_deviating_points(alignment_report)
    realized, unresolved, uncovered = realize_communication_costs(local_cc_results, deviating, per_org)
    federated_costs = compute_federated_alignment_costs(local_cc_results, realized)

    organizations = {}
    for concept, cases in local_cc_results.items():
        total_lac = sum(local["lac"] for local in cases.values())
        total_bwc = sum(local["bwc"] for local in cases.values())
        organizations[concept] = {
            "local_alignment_cost": total_lac,
            "local_fitness": 1 - total_lac / total_bwc if total_bwc > 0 else 1.0,
            "communication_cost": float(sum(sum(per_case.values())
                                            for per_case in realized.get(concept, {}).values())),
            "unresolved_labels": sorted(unresolved.get(concept, set())),
            "uncovered_points": uncovered.get(concept, []),
        }

    return {
        "Organizations": organizations,
        "Federated_Alignment_Costs": federated_costs,
        "Realized_Communication_Costs": realized,
        "Total_Federated_Alignment_Cost": sum(i["fac"] for i in federated_costs.values()),
    }


def collect_deviating_points(alignment_report):
    deviating, per_org = defaultdict(set), defaultdict(set)
    for concept, variants in alignment_report.items():
        for _, org_report in variants:
            for case in org_report["cases"]:
                group_root = uf.find(case["composed_id"])
                for key in DEVIATION_KEYS:
                    for move in case[key]:
                        cp = move.get("communication_point")
                        if cp is not None:
                            deviating[group_root].add(cp)
                            per_org[concept].add(cp)
    return deviating, per_org


def realize_communication_costs(local_cc_results, deviating, per_org):
    realized, uncovered = {}, {}
    unresolved = defaultdict(set)

    for concept, cases in local_cc_results.items():
        realized[concept] = {}
        covered = {activity_to_cp.get(label) for local in cases.values()
                   for label in local["lcc"]}
        uncovered[concept] = sorted(per_org.get(concept, set()) - covered)
        for composed_id, local in cases.items():
            deviating_cps = deviating.get(uf.find(composed_id), set())
            per_case = {}

            for label, cost in local["lcc"].items():
                cp = activity_to_cp.get(label)
                if cp is None:
                    unresolved[concept].add(label)
                    continue
                if cp in deviating_cps:
                    per_case[cp] = per_case.get(cp, 0.0) + cost

            realized[concept][composed_id] = per_case

    return realized, dict(unresolved), uncovered


def compute_federated_alignment_costs(local_cc_results, realized):
    instances = defaultdict(lambda: {"lac": 0.0, "lcc": 0.0, "members": []})

    for concept, cases in local_cc_results.items():
        for composed_id, local in cases.items():
            instance = instances[uf.find(composed_id)]
            instance["lac"] += local["lac"]
            instance["lcc"] += sum(realized.get(concept, {}).get(composed_id, {}).values())
            instance["members"].append((concept, composed_id))

    for instance in instances.values():
        instance["fac"] = instance["lac"] + instance["lcc"]

    return dict(instances)
