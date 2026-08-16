import random
import pm4py
from collections import Counter, defaultdict
from merge import group_traces

# Naming conventions of the logs attributes
LOG_ID = "concept:name"
TRACE_ID = "concept:name"
EVENT_ID = "concept:name"
ORG_GROUP = "org:group"
COMMUNICATION_MODE = "msgType"
MSG_ID = "msgInstanceId"
MSG_TYPE = "msgFlow"
TIMESTAMP = "time:timestamp"

anonymized_msgIds = {}  # for anonymizing the msgId for increased privacy protection
anonymized_comm_channels = {}


def preprocess(logs):
    for eventlog in logs:
        remove_internal_events(eventlog)
        if missing_event_ids(eventlog):
            ...  # TBD

        '''
        for trace in eventlog:
            assign_communication_mode(trace)
            # anonymize_msg_exchange(trace)
            continue
        '''
    *trace_groups, uf = group_traces(logs)  # in merge.py
    group_msg_ids(logs, *trace_groups, uf)
    return trace_groups


def group_msg_ids(logs, groups_of_traces, composed_id_to_trace, uf):
    msg_ids_to_events = defaultdict(list)
    for log in logs:
        for trace in log:
            composed_id = (log.attributes[LOG_ID], trace.attributes[TRACE_ID])
            for event in trace:
                msg_ids_to_events[event[MSG_ID]].append((composed_id, event))

    unmatched_events = {msg_id: events[0] for msg_id, events in msg_ids_to_events.items() if len(events) == 1}
    matched_events = {msg_id: events for msg_id, events in msg_ids_to_events.items() if len(events) == 2}
    # MsgType Handling
    for (c_id_1, e_1), (c_id_2, e_2) in matched_events.values():
        if e_1.get(COMMUNICATION_MODE) is None:
            if e_2.get(COMMUNICATION_MODE) is None:
                successful_infer = infer_comm_mode(logs,(c_id_1, c_id_2), (e_1, e_2))
                if not successful_infer:
                    roots = uf.find(c_id_1), uf.find(c_id_2)
                    for root in roots:
                        groups_of_traces.pop(root, None)
            elif e_2.get(COMMUNICATION_MODE).lower() in {"send", "s", "out", "o"}:
                e_1[COMMUNICATION_MODE] = "receive"
            else:
                e_1[COMMUNICATION_MODE] = "send"
        elif e_2.get(COMMUNICATION_MODE) is None:
            if e_1.get(COMMUNICATION_MODE).lower() in {"send", "s", "out", "o"}:
                e_2[COMMUNICATION_MODE] = "receive"
            else:
                e_2[COMMUNICATION_MODE] = "send"
        else:
            if e_1[COMMUNICATION_MODE].lower() in {"send", "s", "out", "o"}:
                e_1[COMMUNICATION_MODE] = "send"
                e_2[COMMUNICATION_MODE] = "receive"
            else:
                e_1[COMMUNICATION_MODE] = "receive"
                e_2[COMMUNICATION_MODE] = "send"

    # MsgInstanceId Handling
    for msg_id, (composed_id, event) in unmatched_events.items():
        # For now, remove every trace group with missing msgInstaceIds
        root = uf.find(composed_id)
        groups_of_traces.pop(root, None)


# infers the communication for two events in case no event has sent or receive based on the same events in other traces
# returns false if a) there are mixed send/receive relationships or b) not enough events have a comm mode
def infer_comm_mode(logs, composed_ids, events):
    participating_logs = {log for log in logs if log.attributes.get(LOG_ID) in {c_id[0] for c_id in composed_ids}}
    existing_comm_modes_e_1 = [e[COMMUNICATION_MODE].lower() for trace in participating_logs
                               for all_events in trace for e in all_events
                               if e[EVENT_ID] == events[0][EVENT_ID] and e.get(COMMUNICATION_MODE) is not None]
    count_sent = sum(1 for e in existing_comm_modes_e_1 if e in {"send", "s", "out", "o"})
    count_receive = sum(1 for e in existing_comm_modes_e_1 if e in {"receive", "r", "in", "i"})
    if count_sent > 0 and count_receive > 0:
        return False
    elif count_sent > 0:
        if count_sent / len(existing_comm_modes_e_1) > 0.9:
            events[0][COMMUNICATION_MODE] = "send"
            events[1][COMMUNICATION_MODE] = "receive"
        else:
            return False
    else:
        if count_receive / len(existing_comm_modes_e_1) > 0.9:
            events[0][COMMUNICATION_MODE] = "receive"
            events[1][COMMUNICATION_MODE] = "send"
        else:
            return False
    return True


# Remove all Events without message exchanges, WORKS
def remove_internal_events(log):
    for trace in log:
        trace[:] = [event for event in trace if
                    event.get(MSG_ID) is not None or
                    event.get(MSG_TYPE) is not None]


# Only works if the MsgInstanceID is required
def anonymize_msg_exchange(trace):
    for event in trace:
        msgId = event.get(MSG_ID)
        channel = event.get(MSG_TYPE)
        if msgId not in anonymized_msgIds:
            anonymized_msgIds[msgId] = hash(msgId)
        if channel not in anonymized_comm_channels:
            anonymized_comm_channels[channel] = hash(channel)
        event[MSG_ID] = anonymized_msgIds[msgId]
        event[MSG_TYPE] = anonymized_comm_channels[channel]


def missing_event_ids(log):
    return any(e.get(EVENT_ID) is None for trace in log for e in trace)


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


# Debugging
if __name__ == '__main__':
    event_log = pm4py.read_xes("logs/corradini_logs/PartyA.xes", return_legacy_log_object=True)
    # removeIncompleteTraces(log)
    print(sum(len(trace) for trace in event_log), f", First event: {event_log[0][0][EVENT_ID]}")
    remove_internal_events(event_log)
    print(sum(len(trace) for trace in event_log), f", First event: {event_log[0][0][EVENT_ID]}")

    print(len(anonymized_msgIds))
    anonymize_msg_exchange(event_log[1])
    print(len(anonymized_msgIds), anonymized_msgIds.values())
