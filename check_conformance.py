import pm4py
import config

from collections import defaultdict
from copy import deepcopy
from merge import group_traces
from pm4py.algo.conformance.alignments.petri_net import algorithm as alignments
from pm4py.algo.conformance.alignments.petri_net.algorithm import Parameters as Params
from pm4py.algo.evaluation.precision import algorithm as evaluate_precision
from pm4py.objects.petri_net.utils.align_utils import SKIP, STD_MODEL_LOG_MOVE_COST

''' Helper Mappings for CC '''
buddy_events = {}
lone_events = {}
transition_to_org_mapping = {}
communication_points = {}
label_to_transition_mapping = {}
activity_to_cp = {}
cpn = ()
return_t = []
public_logs = []
trace_groups = {}
uf = None


def check_conformance(log, logs, input_cpn, input_return_t):
    global cpn, return_t, public_logs
    cpn = input_cpn
    return_t = input_return_t
    public_logs = logs

    ''' Create Helper Mappings for CC '''
    prepare_cc_data(*cpn)

    ''' Check Conformance '''
    # check_collaborative_log(log)
    res = check_public_logs()

    # TODO: Precision on federated level, relabeling removes all possible deviations/escaping edges
    result = evaluate_precision.apply(log, *cpn, variant=evaluate_precision.Variants.ALIGN_ETCONFORMANCE)
    print(f"Komplettes cpn/merged_log: {result}")

    return res


def prepare_cc_data(net, initial_marking, final_marking):
    global buddy_events, lone_events, transition_to_org_mapping, communication_points, t_com, t_int, \
        activity_to_cp, label_to_transition_mapping, public_logs, trace_groups, uf

    concepts, sent_msgs, rec_msgs, async_types, nets = return_t[0], return_t[1], return_t[2], return_t[5], return_t[6]

    # Check if net is closed
    if not verify_closed_net(net, initial_marking, final_marking):
        raise Exception("Net is not closed")  # Missing error handling

    trace_groups, _, uf = group_traces(public_logs)
    buddy_events, lone_events = classify_miscommunication_type()
    transition_to_org_mapping = build_transition_to_org_mapping(net, nets, concepts)
    communication_points = build_communication_points(async_types, sent_msgs, rec_msgs)
    activity_to_cp = build_activity_to_communication_point()
    label_to_transition_mapping = build_label_to_transitions_mapping(net)

    return True


# Enriches each move of an alignment with its move type (SYNC/LOG/MODEL), the organisation
# it belongs to, and the communication point it participates in.
def enrich_moves(moves, trace):
    enriched = []
    event_idx = 0
    tau_transition_count = 0
    for log_side, model_side in moves:
        if log_side == SKIP and model_side is None:  # Silent transition
            tau_transition_count += 1
            # for debugging
            # enriched.append({"log_activity": SKIP, "model_activity": None, "move_type": "Silent", "org": "None", "communication_point": "None"})
            continue
        elif log_side == model_side:
            move_type = "SYNC"
            org = orgs_for_label(model_side)
            comm_point = activity_to_cp.get(model_side)
        elif model_side == SKIP:
            move_type = "LOG"
            event_org = trace[event_idx].get(config.ATTRIBUTES.org_group)
            org = {event_org} if event_org is not None else set()
            comm_point = activity_to_cp.get(log_side)
        else:
            move_type = "MODEL"
            org = orgs_for_label(model_side)
            comm_point = activity_to_cp.get(model_side)

        enriched.append({
            "log_activity": log_side if log_side != SKIP else None,
            "model_activity": model_side if model_side != SKIP else None,
            "move_type": move_type,
            "org": org,
            "communication_point": comm_point
        })

        if log_side != SKIP:
            event_idx += 1

    return enriched, tau_transition_count


# ONLY FOR EVALUATION, WILL NOT BE USED IN PRODUCTION
def check_collaborative_log(log):
    net, i_m, f_m = cpn
    alignments_result = {}
    representative_traces = {}
    # Get Trace variants from merged log
    trace_variants = pm4py.get_variants(log,
                                        activity_key=config.ATTRIBUTES.event_id,
                                        timestamp_key=config.ATTRIBUTES.timestamp,
                                        case_id_key=config.ATTRIBUTES.trace_id,
                                        max_repetitions=2)  # don't know what will do best yet

    model_cost_function = {t: 0 if t.label is None else STD_MODEL_LOG_MOVE_COST for t in net.transitions}
    sync_cost_function = {t: 0 for t in net.transitions}

    for c_id in trace_variants.keys():
        variant = trace_variants[c_id]
        if not variant:
            continue
        representative_traces[c_id] = variant[0]
        trace_cost_function = [STD_MODEL_LOG_MOVE_COST] * len(representative_traces[c_id])

        parameters = {
            Params.PARAM_MODEL_COST_FUNCTION: model_cost_function,
            Params.PARAM_SYNC_COST_FUNCTION: sync_cost_function,
            Params.PARAM_TRACE_COST_FUNCTION: trace_cost_function
        }

        alignments_result[c_id] = (alignments.apply(variant[0], *cpn,
                                                    variant=alignments.Variants.VERSION_STATE_EQUATION_A_STAR,
                                                    parameters=parameters), len(variant))

    # Try with dirty logs
    for t in alignments_result.keys():
        # print(f"Trace: {t}, fitness: {alignments_result[t][0]["fitness"]}")
        ...

    # enrich moves with information about org, comm_point and move-type
    enriched_alignments = {}
    for c_id, (result, freq) in alignments_result.items():
        moves = result["alignment"]  # List<(log_side, model_side)>
        enriched_alignments[c_id], _ = enrich_moves(moves, representative_traces[c_id])

    total_cost = sum(res['cost'] * freq for res, freq in alignments_result.values()) / STD_MODEL_LOG_MOVE_COST
    total_cases = sum(frequency for res, frequency in alignments_result.values())
    print(f"Collaborative Log: Total cost: {total_cost}\n Total cases: {total_cases}\n\n")


def check_public_logs():
    projections = {concept: (log, *deepcopy(cpn)) for concept in return_t[0] for log in public_logs
                   if concept in [e.get(config.ATTRIBUTES.org_group) for trace in log for e in trace]}
    move_results = {concept: list() for concept in return_t[0]}

    for concept, (log, net, i_m, f_m) in projections.items():
        log_id_value = log.attributes[config.ATTRIBUTES.log_id]

        trace_variants = pm4py.get_variants(log,
                                            activity_key=config.ATTRIBUTES.event_id,
                                            timestamp_key=config.ATTRIBUTES.timestamp,
                                            case_id_key=config.ATTRIBUTES.trace_id)
        for t in net.transitions:
            # τ-Relabeling
            if concept not in transition_to_org_mapping.get(t.name, set()):
                t.label = None  # τ = None for pm4py

        model_cost_function = {t: 0 if t.label is None else STD_MODEL_LOG_MOVE_COST for t in net.transitions}
        sync_cost_function = {t: 0 for t in net.transitions}
        alignments_result = {}
        representative_traces = {}

        for variant_key, traces in trace_variants.items():
            representative_trace = traces[0]
            if not representative_trace:
                continue
            representative_traces[variant_key] = representative_trace
            trace_cost_function = [STD_MODEL_LOG_MOVE_COST] * len(representative_trace)

            # Cost functions
            parameters = {
                Params.PARAM_MODEL_COST_FUNCTION: model_cost_function,
                Params.PARAM_SYNC_COST_FUNCTION: sync_cost_function,
                Params.PARAM_TRACE_COST_FUNCTION: trace_cost_function
            }

            res = alignments.apply(representative_trace, net, i_m, f_m,
                                   variant=alignments.Variants.VERSION_STATE_EQUATION_A_STAR,
                                   parameters=parameters)
            alignments_result[variant_key] = (res, len(traces))

        enrichment_results = {}

        # DEBUG
        total_cost = sum(res['cost'] * freq for res, freq in alignments_result.values()) / STD_MODEL_LOG_MOVE_COST
        total_cases = sum(frequency for res, frequency in alignments_result.values())
        # print(f"Org: {concept}\n Total cost: {total_cost}\n Total cases: {total_cases}\nMoves: ")

        for variant_key, (res, frequency) in alignments_result.items():
            enriched, tau_count = enrich_moves(res["alignment"], representative_traces[variant_key])
            enrichment_results[variant_key] = (enriched, tau_count)
            for move in enriched:
                ...
                # print(f"({move["log_activity"]}, {move["model_activity"]}) Typ: {move['move_type']}")
            # print(f"Fitness of this variant: {res["fitness"]}\n")

        # print(f"Org: {concept}, Result: {[res["alignment"] for res, freq in alignments_result.values()]},
        # Anzahl alignments: {len(alignments_result.values())}\n")

        for variant_key, (enriched, tau_count) in enrichment_results.items():
            res, _ = alignments_result[variant_key]
            cases = [((log_id_value, case_trace.attributes[config.ATTRIBUTES.trace_id]), case_trace)
                     for case_trace in trace_variants[variant_key]]
            report = create_conformance_report(enriched, tau_count, res, cases)
            move_results[concept].append((variant_key, report))

    return move_results


# Creates report with the error types of moves per variant per concept
def create_conformance_report(enrichment, tau_count, alignment_result, cases):
    sync_moves, extra_activities = [], []
    for move in enrichment:
        if move['move_type'] == "SYNC":
            sync_moves.append(move)
        elif move['move_type'] == "LOG" and not label_to_transition_mapping.get(move['log_activity']):
            extra_activities.append(move)

    return {
        "fitness": alignment_result['fitness'],
        "cost": alignment_result['cost'] / STD_MODEL_LOG_MOVE_COST,
        "tau_count": tau_count,
        "sync_moves": sync_moves,
        "extra_activities": extra_activities,
        "cases": [classify_case_moves(enrichment, composed_id, trace) for composed_id, trace in cases]
    }


# actually classifies sender move, receiver move, async comm
def classify_case_moves(enrichment, composed_id, trace):
    sender_moves, receiver_moves, async_communications = [], [], []

    correlated_group = set(trace_groups.get(uf.find(composed_id), ())) if uf is not None else set()

    event_idx = 0
    for move in enrichment:
        move_type = move['move_type']
        if move_type == "SYNC":
            event_idx += 1

        elif move_type == "LOG":
            event = trace[event_idx]
            event_idx += 1
            msg_instance_id = event.get(config.ATTRIBUTES.msg_instance_id)

            if msg_instance_id in lone_events:
                if event.get(config.ATTRIBUTES.communication_mode) == "send":
                    sender_moves.append(move)
                else:
                    receiver_moves.append(move)

            elif msg_instance_id in buddy_events:
                pair = buddy_events[msg_instance_id]
                buddy_event = next((e for _, e in pair if e is not event), None)
                if buddy_event is None:
                    continue

                order_violated = (
                        (event.get(config.ATTRIBUTES.communication_mode) == "send"
                         and event.get(config.ATTRIBUTES.timestamp) > buddy_event.get(config.ATTRIBUTES.timestamp))
                        or
                        (event.get(config.ATTRIBUTES.communication_mode) == "receive"
                         and event.get(config.ATTRIBUTES.timestamp) < buddy_event.get(config.ATTRIBUTES.timestamp))
                )
                if order_violated:
                    async_communications.append(move)

        elif move_type == "MODEL":
            comm_point = activity_to_cp.get(move['model_activity'])
            if comm_point is None:
                continue

            candidates = [event for (ev_composed_id, event) in lone_events.values()
                          if ev_composed_id in correlated_group
                          and event.get(config.ATTRIBUTES.msg_type) == comm_point]

            if len(candidates) == 1:
                lone = candidates[0]
                if lone.get(config.ATTRIBUTES.communication_mode) == "send":
                    receiver_moves.append(move)
                else:
                    sender_moves.append(move)

    return {
        "composed_id": composed_id,
        "sender_moves": sender_moves,
        "receiver_moves": receiver_moves,
        "async_communications": async_communications
    }


# TODO: Generate readable output for user
def generate_results(results):
    report = {}
    ...
    return report


# HELPER METHODS

# Groups events with and without buddy (for miscommunication identification)
def classify_miscommunication_type():
    events_by_msg_id = defaultdict(list)
    for log in public_logs:
        log_id = log.attributes[config.ATTRIBUTES.log_id]
        for trace in log:
            composed_id = (log_id, trace.attributes[config.ATTRIBUTES.trace_id])
            for event in trace:
                events_by_msg_id[event.get(config.ATTRIBUTES.msg_instance_id)].append((composed_id, event))

    buddies = {msg_id: tuple(entries) for msg_id, entries in events_by_msg_id.items() if len(entries) == 2}
    loners = {msg_id: entries[0] for msg_id, entries in events_by_msg_id.items() if len(entries) == 1}

    return buddies, loners


# Resolves a move's activity label to the orgs owning the matching model transitions.
def orgs_for_label(label):
    orgs = set()
    for t in label_to_transition_mapping.get(label, []):
        orgs |= transition_to_org_mapping.get(t.name, set())
    return orgs


# Reverse index of build_communication_points: activity label -> communication point
def build_activity_to_communication_point():
    mapping = {}
    for cp_id, cp in communication_points.items():
        for activities in cp["senders"].values():
            for activity in activities:
                mapping[activity] = cp_id
        for activities in cp["receivers"].values():
            for activity in activities:
                mapping[activity] = cp_id
    return mapping


def verify_closed_net(net, initial_marking, final_marking):
    initial_places = set(initial_marking.keys())
    final_places = set(final_marking.keys())
    open_input_places = {p for p in net.places if len(p.in_arcs) == 0 and p not in initial_places}
    open_output_places = {p for p in net.places if len(p.out_arcs) == 0 and p not in final_places}
    return not (open_input_places or open_output_places)


# Maps transition NAMES (not objects) to participating orgs (not the transitions for the copied nets)
def build_transition_to_org_mapping(net, nets, concepts):
    label_to_orgs = defaultdict(set)
    tau_owner_by_name = {}
    for org in concepts:
        for t in nets[org][0].transitions:
            if t.label is not None:
                label_to_orgs[t.label].add(org)
            else:
                tau_owner_by_name[t.name] = org

    mapping = {}
    for t in net.transitions:
        if t.label is not None:
            mapping[t.name] = label_to_orgs[t.label]
        else:
            mapping[t.name] = {tau_owner_by_name[t.name]} if t.name in tau_owner_by_name else set()
    return mapping


# Maps msgTypes to send/receive activities
def build_communication_points(async_types, sent_messages, rec_messages):
    return {
        cp: {
            "senders": sent_messages.get(cp, {}),
            "receivers": rec_messages.get(cp, {}),
        }
        for cp in async_types
    }


# Builds map of activity label -> transitions (for potential duplicate activity names)
# if there are now duplicates, it's just a map (name -> {transition})
def build_label_to_transitions_mapping(net):
    index = defaultdict(list)
    for t in net.transitions:
        if t.label is not None:
            index[t.label].append(t)
    return dict(index)
