from datetime import timedelta

import pm4py
from collections import defaultdict
from merge import group_traces, UnionFind

# Naming conventions of the logs attributes
LOG_ID = "concept:name"
TRACE_ID = "concept:name"

EVENT_ID = "concept:name"
ORG_GROUP = "org:group"
COMMUNICATION_MODE = "communicationMode"
MSG_TYPE = "msgType"
MSG_INSTANCE_ID = "msgInstanceId"
TIMESTAMP = "time:timestamp"

# Global variables (used throughout the script)
anonymized_msgIds = {}  # for anonymizing the msgId for increased privacy protection
anonymized_comm_channels = {}
trace_groups = defaultdict(list)
composed_ids_to_trace = ()
logs = []
uf = UnionFind()


def preprocess(input_logs):
    global trace_groups, composed_ids_to_trace, logs, uf
    logs = input_logs
    for eventlog in logs:
        remove_internal_events(eventlog)
        if exist_missing_event_ids(eventlog):
            ...  # TBD

        # anonymize_msg_exchange(eventlog)

    trace_groups, composed_ids_to_trace, uf = group_traces(input_logs)  # in merge.py
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
            composed_id = (log.attributes[LOG_ID], trace.attributes[TRACE_ID])
            for event in trace:
                msg_ids_to_events[event[MSG_INSTANCE_ID]].append((composed_id, event))

    unmatched_events = {msg_id: events[0] for msg_id, events in msg_ids_to_events.items() if len(events) == 1}
    matched_events = {msg_id: events for msg_id, events in msg_ids_to_events.items() if len(events) == 2}

    return matched_events, unmatched_events


def preprocess_msg_ids(unmatched_events):
    global logs
    # For now, remove every trace group with missing msgInstanceIds
    remove_trace_from_merge(*[composed_id for composed_id, event in unmatched_events.values()])


def preprocess_communication_mode(matched_events):
    for (c_id_1, e_1), (c_id_2, e_2) in matched_events.values():
        if e_1.get(COMMUNICATION_MODE) in (None, ""):
            if e_2.get(COMMUNICATION_MODE) in (None, ""):
                successful_infer = infer_comm_mode((c_id_1, c_id_2), (e_1, e_2))
                if not successful_infer:
                    remove_trace_from_merge(c_id_1, c_id_2)
            elif e_2.get(COMMUNICATION_MODE).lower() in {"send", "s", "out", "o"}:
                e_1[COMMUNICATION_MODE] = "receive"
                e_2[COMMUNICATION_MODE] = "send"
            else:
                e_1[COMMUNICATION_MODE] = "send"
                e_2[COMMUNICATION_MODE] = "receive"
        elif e_2.get(COMMUNICATION_MODE) in (None, ""):
            if e_1.get(COMMUNICATION_MODE).lower() in {"send", "s", "out", "o"}:
                e_1[COMMUNICATION_MODE] = "send"
                e_2[COMMUNICATION_MODE] = "receive"
            else:
                e_1[COMMUNICATION_MODE] = "receive"
                e_2[COMMUNICATION_MODE] = "send"
        else:
            if e_1[COMMUNICATION_MODE].lower() in {"send", "s", "out", "o"}:
                e_1[COMMUNICATION_MODE] = "send"
                e_2[COMMUNICATION_MODE] = "receive"
            else:
                e_1[COMMUNICATION_MODE] = "receive"
                e_2[COMMUNICATION_MODE] = "send"


# infers the communication for two events in case no event has sent or receive based on the same events in other traces
# returns false if a: there are mixed send/receive relationships or b: not enough events have a comm mode
def infer_comm_mode(composed_ids, events, treshold=0.9):
    participating_logs = {log for log in logs if log.attributes.get(LOG_ID) in {c_id[0] for c_id in composed_ids}}
    existing_comm_modes_e_1 = [e[COMMUNICATION_MODE].lower() for trace in participating_logs
                               for all_events in trace for e in all_events
                               if e[EVENT_ID] == events[0][EVENT_ID] and e.get(COMMUNICATION_MODE) is not None]
    if not existing_comm_modes_e_1:
        return False

    count_sent = sum(1 for e in existing_comm_modes_e_1 if e in {"send", "s", "out", "o"})
    count_receive = sum(1 for e in existing_comm_modes_e_1 if e in {"receive", "r", "in", "i"})
    if count_sent > 0 and count_receive > 0:
        return False
    elif count_sent > 0:
        if count_sent / len(existing_comm_modes_e_1) > treshold:
            events[0][COMMUNICATION_MODE] = "send"
            events[1][COMMUNICATION_MODE] = "receive"
        else:
            return False
    else:
        if count_receive / len(existing_comm_modes_e_1) > treshold:
            events[0][COMMUNICATION_MODE] = "receive"
            events[1][COMMUNICATION_MODE] = "send"
        else:
            return False
    return True


# Remove all Events without message exchanges, WORKS
def remove_internal_events(log):
    for trace in log:
        trace[:] = [event for event in trace if
                    event.get(MSG_INSTANCE_ID) not in (None, "") and
                    event.get(MSG_TYPE) is not None]


# Only works if the MsgInstanceID is required
def anonymize_msg_exchange(log):
    global anonymized_msgIds, anonymized_comm_channels
    for trace in log:
        for event in trace:
            msgId = event.get(MSG_INSTANCE_ID)
            channel = event.get(MSG_TYPE)
            if msgId not in anonymized_msgIds:
                anonymized_msgIds[msgId] = hash(msgId)
            if channel not in anonymized_comm_channels:
                anonymized_comm_channels[channel] = hash(channel)
            event[MSG_INSTANCE_ID] = anonymized_msgIds[msgId]
            event[MSG_TYPE] = anonymized_comm_channels[channel]


def exist_missing_event_ids(log):
    return any(e.get(EVENT_ID) is None for trace in log for e in trace)


# If missing, assign timestamp based on the median of timestamps from the msgExchange of other traces
def preprocess_timestamps():
    all_events = [event for log in logs for trace in log for event in trace]
    events_missing_timestamps = {((log.attributes[LOG_ID], trace.attributes[TRACE_ID]), e) for log in logs
                                 for trace in log for e in trace if e.get(TIMESTAMP) in (None, "")}

    # Build reference msgPairs
    send_reference_events = {event.get(MSG_INSTANCE_ID): event for event in all_events
                             if event.get(TIMESTAMP) not in (None, "")
                             and event.get(COMMUNICATION_MODE) == "send"}
    receive_counterpart_events = {event.get(MSG_INSTANCE_ID): event for event in all_events
                                  if event.get(MSG_INSTANCE_ID) in {e for e in send_reference_events.keys()}
                                  and event.get(TIMESTAMP) not in (None, "")
                                  and event.get(COMMUNICATION_MODE) == "receive"}
    msg_pairs = [(send_reference_events[msg_id], receive_counterpart_events[msg_id]) for msg_id
                 in send_reference_events.keys() & receive_counterpart_events.keys()]

    for composed_id, event in events_missing_timestamps:
        event_id, event_comm_mode = event[EVENT_ID], event[COMMUNICATION_MODE]
        this_event_counterpart = [(e, (log.attributes[LOG_ID], trace.attributes[TRACE_ID])) for log in logs
                                  for trace in log for e in trace if e[COMMUNICATION_MODE] != event_comm_mode
                                  and e[MSG_INSTANCE_ID] == event[MSG_INSTANCE_ID]
                                  and e.get(TIMESTAMP) not in (None, "")]
        if not this_event_counterpart:
            # print(f"No Counterpart for event: {event.get(EVENT_ID)} with msgInstanceId: {event.get(MSG_INSTANCE_ID)}")
            remove_trace_from_merge(composed_id)
            continue

        this_msg_pairs = [(ref, ctp) for ref, ctp in msg_pairs
                          if ref[EVENT_ID] in (event_id, this_event_counterpart[0][0][EVENT_ID])]
        if not this_msg_pairs:  # no other msg_pairs exist, so the traces will be discarded
            remove_trace_from_merge(composed_id, this_event_counterpart[0][1])
            continue

        latencies = sum((abs(ref[TIMESTAMP] - ctp[TIMESTAMP]) for ref, ctp in this_msg_pairs), start=timedelta())
        mean = latencies / len(this_msg_pairs)

        if event_comm_mode == "send":
            event[TIMESTAMP] = this_event_counterpart[0][0][TIMESTAMP] - mean
        else:
            event[TIMESTAMP] = this_event_counterpart[0][0][TIMESTAMP] + mean


def remove_trace_from_merge(*composed_ids):
    roots = [uf.find(composed_id) for composed_id in composed_ids]
    for root in roots:
        trace_groups.pop(root, None)


'''
def build_trace_groups(log, *attributes):
    trace_groups = defaultdict(list)  # List of traces that share the same event structure
    trace_group_counter = Counter()  # Counter of trace groups (-> to identify most common ones)
    for trace in log:
        if any(e.get(attribute) is None for attribute in attributes for e in trace):
            continue
        event_tuple = tuple(e.get(EVENT_ID) for e in trace)

        trace_group_counter[event_tuple] += 1
        trace_groups[event_tuple].append(trace)

    return trace_groups, trace_group_counter
'''


# Debugging
if __name__ == '__main__':
    event_log = pm4py.read_xes("logs/corradini_logs/PartyA.xes", return_legacy_log_object=True)
    # removeIncompleteTraces(log)
    print(sum(len(trace) for trace in event_log), f", First event: {event_log[0][0][EVENT_ID]}")
    remove_internal_events(event_log)
    print(sum(len(trace) for trace in event_log), f", First event: {event_log[0][0][EVENT_ID]}")

    print(len(anonymized_msgIds))
    # anonymize_msg_exchange(event_log[1])
    print(len(anonymized_msgIds), anonymized_msgIds.values())
