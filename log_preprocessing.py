import math
import random
import pm4py

# Naming conventions of the logs attributes
LOG_ID = "concept:name"
TRACE_ID = "concept:name"
EVENT_ID = "concept:name"
MSG_ID = "msgInstanceId"
MSG_TYPE = "msgType"
TIMESTAMP = "time:timestamp"

threshold = 0.79  # Threshold of traces that the log must still contain after preprocessing
completionThreshold = 0.93  # Threshold of events of a trace that must be correct for the log to be kept
debug = True

anonymized_msgIds = {}  # for anonymizing the msgId for increased privacy protection


def preprocess(logs):
    for eventlog in logs:
        removeInternalEvents(eventlog)
        for trace in eventlog:
            anonymizeMsgId(trace)
            continue
    return logs


# Remove all Events without message exchanges, WORKS
def removeInternalEvents(log):
    for trace in log:
        trace[:] = [event for event in trace if
                    event.get(MSG_ID) is not None or
                    event.get(MSG_TYPE) is not None]


# unfinished
def removeIncompleteTraces(logs):
    result = tuple()


def checkTraceForCompleteness(trace):
    if debug:
        return random.uniform(0.9, 1.0)

    completionRate = len(trace)
    for e in trace:
        if any(x is None for x in e.attributes.values()):
            completionRate -= 1
            continue
        # still needs check if a variable is missing
        # also still need to somewhere track the attribute names from the log
    return completionRate / len(trace)


# Only works if the MsgInstanceID is required
def anonymizeMsgId(trace):
    for event in trace:
        msgId = event.get(MSG_ID)
        if msgId not in anonymized_msgIds:
            anonymized_msgIds[msgId] = hash(msgId)
        event[MSG_ID] = anonymized_msgIds[msgId]


# Debugging
if __name__ == '__main__':
    event_log = pm4py.read_xes("logs/corradini_logs/PartyA.xes", return_legacy_log_object=True)
    # removeIncompleteTraces(log)
    print(sum(len(trace) for trace in event_log), f", First event: {event_log[0][0][EVENT_ID]}")
    removeInternalEvents(event_log)
    print(sum(len(trace) for trace in event_log), f", First event: {event_log[0][0][EVENT_ID]}")

    print(len(anonymized_msgIds))
    anonymizeMsgId(event_log[1])
    print(len(anonymized_msgIds), anonymized_msgIds.values())
