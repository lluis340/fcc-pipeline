from datetime import timedelta

import pm4py
import config  # Naming Conventions
from collections import defaultdict
from merge import group_traces, UnionFind


# Global variables (used throughout the script)
trace_groups = defaultdict(list)
composed_ids_to_trace = ()
logs = []
uf = UnionFind()


def create_public_logs(private_logs):
    global logs
    logs = private_logs
    for log in logs:
        for trace in log:
            trace[:] = [event for event in trace if
                        event.get(config.ATTRIBUTES.msg_instance_id) not in (None, "") and
                        event.get(config.ATTRIBUTES.msg_type) is not None]
    return logs


def preprocess(input_logs):
    global trace_groups, composed_ids_to_trace, logs, uf
    logs = create_public_logs(input_logs)

    trace_groups, composed_ids_to_trace, uf = group_traces(logs)  # in merge.py
    matched_events, unmatched_events = match_events_by_message_id()

    preprocess_communication_mode(matched_events)
    preprocess_msg_ids(unmatched_events)
    preprocess_timestamps()

    return trace_groups, composed_ids_to_trace


def match_events_by_message_id():
    global logs
    msg_ids_to_events = defaultdict(list)
    for log in logs:
        for trace in log:
            composed_id = (log.attributes[config.ATTRIBUTES.log_id], trace.attributes[config.ATTRIBUTES.trace_id])
            for event in trace:
                msg_ids_to_events[event[config.ATTRIBUTES.msg_instance_id]].append((composed_id, event))

    unmatched_events = {msg_id: events[0] for msg_id, events in msg_ids_to_events.items() if len(events) == 1}
    matched_events = {msg_id: events for msg_id, events in msg_ids_to_events.items() if len(events) == 2}

    return matched_events, unmatched_events


def preprocess_msg_ids(unmatched_events):
    global logs
    # Only the single orphaned event lacks a counterpart - drop just that
    # event, not the whole trace, so its other (matched) events still enter
    # the merge.
    for composed_id, event in unmatched_events.values():
        remove_event_from_merge(composed_id, event)


def preprocess_communication_mode(matched_events):
    for (c_id_1, e_1), (c_id_2, e_2) in matched_events.values():
        if e_1.get(config.ATTRIBUTES.communication_mode) in (None, ""):
            if e_2.get(config.ATTRIBUTES.communication_mode) in (None, ""):
                successful_infer = infer_comm_mode((c_id_1, c_id_2), (e_1, e_2))
                if not successful_infer:
                    remove_event_from_merge(c_id_1, e_1)
                    remove_event_from_merge(c_id_2, e_2)
            elif e_2.get(config.ATTRIBUTES.communication_mode).lower() in {"send", "s", "out", "o"}:
                e_1[config.ATTRIBUTES.communication_mode] = "receive"
                e_2[config.ATTRIBUTES.communication_mode] = "send"
            else:
                e_1[config.ATTRIBUTES.communication_mode] = "send"
                e_2[config.ATTRIBUTES.communication_mode] = "receive"
        elif e_2.get(config.ATTRIBUTES.communication_mode) in (None, ""):
            if e_1.get(config.ATTRIBUTES.communication_mode).lower() in {"send", "s", "out", "o"}:
                e_1[config.ATTRIBUTES.communication_mode] = "send"
                e_2[config.ATTRIBUTES.communication_mode] = "receive"
            else:
                e_1[config.ATTRIBUTES.communication_mode] = "receive"
                e_2[config.ATTRIBUTES.communication_mode] = "send"
        else:
            if e_1[config.ATTRIBUTES.communication_mode].lower() in {"send", "s", "out", "o"}:
                e_1[config.ATTRIBUTES.communication_mode] = "send"
                e_2[config.ATTRIBUTES.communication_mode] = "receive"
            else:
                e_1[config.ATTRIBUTES.communication_mode] = "receive"
                e_2[config.ATTRIBUTES.communication_mode] = "send"


# infers the communication for two events in case no event has sent or receive based on the same events in other traces
# returns false if a: there are mixed send/receive relationships or b: not enough events have a comm mode
def infer_comm_mode(composed_ids, events, treshold=0.9):
    participating_logs = {log for log in logs if log.attributes.get(config.ATTRIBUTES.log_id) in {c_id[0] for c_id in composed_ids}}
    existing_comm_modes_e_1 = [e[config.ATTRIBUTES.communication_mode].lower() for trace in participating_logs
                               for all_events in trace for e in all_events
                               if e[config.ATTRIBUTES.event_id] == events[0][config.ATTRIBUTES.event_id] and e.get(config.ATTRIBUTES.communication_mode) is not None]
    if not existing_comm_modes_e_1:
        return False

    count_sent = sum(1 for e in existing_comm_modes_e_1 if e in {"send", "s", "out", "o"})
    count_receive = sum(1 for e in existing_comm_modes_e_1 if e in {"receive", "r", "in", "i"})
    if count_sent > 0 and count_receive > 0:
        return False
    elif count_sent > 0:
        if count_sent / len(existing_comm_modes_e_1) > treshold:
            events[0][config.ATTRIBUTES.communication_mode] = "send"
            events[1][config.ATTRIBUTES.communication_mode] = "receive"
        else:
            return False
    else:
        if count_receive / len(existing_comm_modes_e_1) > treshold:
            events[0][config.ATTRIBUTES.communication_mode] = "receive"
            events[1][config.ATTRIBUTES.communication_mode] = "send"
        else:
            return False
    return True


def exist_missing_event_ids(log):
    return any(e.get(config.ATTRIBUTES.event_id) is None for trace in log for e in trace)


# If missing, assign timestamp based on the median of timestamps from the msgExchange of other traces
def preprocess_timestamps():
    all_events = [event for log in logs for trace in log for event in trace]
    events_missing_timestamps = {((log.attributes[config.ATTRIBUTES.log_id], trace.attributes[config.ATTRIBUTES.trace_id]), e) for log in logs
                                 for trace in log for e in trace if e.get(config.ATTRIBUTES.timestamp) in (None, "")}

    # Build reference msgPairs
    send_reference_events = {event.get(config.ATTRIBUTES.msg_instance_id): event for event in all_events
                             if event.get(config.ATTRIBUTES.timestamp) not in (None, "")
                             and event.get(config.ATTRIBUTES.communication_mode) == "send"}
    receive_counterpart_events = {event.get(config.ATTRIBUTES.msg_instance_id): event for event in all_events
                                  if event.get(config.ATTRIBUTES.msg_instance_id) in {e for e in send_reference_events.keys()}
                                  and event.get(config.ATTRIBUTES.timestamp) not in (None, "")
                                  and event.get(config.ATTRIBUTES.communication_mode) == "receive"}
    msg_pairs = [(send_reference_events[msg_id], receive_counterpart_events[msg_id]) for msg_id
                 in send_reference_events.keys() & receive_counterpart_events.keys()]

    for composed_id, event in events_missing_timestamps:
        event_id, event_comm_mode = event[config.ATTRIBUTES.event_id], event[config.ATTRIBUTES.communication_mode]
        this_event_counterpart = [(e, (log.attributes[config.ATTRIBUTES.log_id], trace.attributes[config.ATTRIBUTES.trace_id]))
                                  for log in logs for trace in log for e in trace
                                  if e[config.ATTRIBUTES.communication_mode] != event_comm_mode
                                  and e[config.ATTRIBUTES.msg_instance_id] == event[config.ATTRIBUTES.msg_instance_id]
                                  and e.get(config.ATTRIBUTES.timestamp) not in (None, "")]
        if not this_event_counterpart:
            # print(f"No Counterpart for event: {event.get(ATTRIBUTES.event_id)} with /
            # msgInstanceId: {event.get(ATTRIBUTES.msg_instance_id)}")
            remove_event_from_merge(composed_id, event)
            continue

        this_msg_pairs = [(ref, ctp) for ref, ctp in msg_pairs
                          if ref[config.ATTRIBUTES.event_id] in (event_id, this_event_counterpart[0][0][config.ATTRIBUTES.event_id])]
        if not this_msg_pairs:  # no other msg_pairs exist, so both events of this exchange are discarded
            remove_event_from_merge(composed_id, event)
            remove_event_from_merge(this_event_counterpart[0][1], this_event_counterpart[0][0])
            continue

        latencies = sum((abs(ref[config.ATTRIBUTES.timestamp] - ctp[config.ATTRIBUTES.timestamp]) for ref, ctp in this_msg_pairs), start=timedelta())
        mean = latencies / len(this_msg_pairs)

        if event_comm_mode == "send":
            event[config.ATTRIBUTES.timestamp] = this_event_counterpart[0][0][config.ATTRIBUTES.timestamp] - mean
        else:
            event[config.ATTRIBUTES.timestamp] = this_event_counterpart[0][0][config.ATTRIBUTES.timestamp] + mean


def remove_trace_from_merge(*composed_ids):
    for composed_id in composed_ids:
        root = uf.find(composed_id)
        group = trace_groups.get(root)
        if group is None:
            continue
        if composed_id in group:
            group.remove(composed_id)
        if not group:
            trace_groups.pop(root, None)


# Removes only the given event(s) from a trace, keeping the trace (and its other, unrelated events) in the merge.
def remove_event_from_merge(composed_id, *events):
    trace = composed_ids_to_trace.get(composed_id)
    if trace is None:
        return
    drop_ids = {id(event) for event in events}
    trace[:] = [e for e in trace if id(e) not in drop_ids]


# Debugging
if __name__ == '__main__':
    event_log = pm4py.read_xes("logs/corradini_logs/artificial_logs_5/PartyA.xes", return_legacy_log_object=True)
    # removeIncompleteTraces(log)
    print(sum(len(trace) for trace in event_log), f", First event: {event_log[0][0][config.ATTRIBUTES.event_id]}")
    # remove_internal_events(event_log)
    print(sum(len(trace) for trace in event_log), f", First event: {event_log[0][0][config.ATTRIBUTES.event_id]}")

