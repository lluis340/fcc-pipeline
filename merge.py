from collections import defaultdict
from datetime import date
from pm4py.objects.log.obj import Event, Trace, EventLog

# attributes from input log
LOG_ID = "concept:name"
TRACE_ID = "concept:name"

CONCEPT_NAME = "concept:name"
ORG_GROUP = "org:group"
COMMUNICATION_MODE = "communicationMode"
MSG_TYPE = "msgType"
MSG_INSTANCE_ID = "msgInstanceId"
TIMESTAMP = "time:timestamp"

trace_id_map = {}


# Prerequisites:
# All events that are contained in these logs have message exchange activities
# All events have the same attribute names, as well as the same attributes
# All events are totally ordered based on their ascending timestamp

# Possible:
# All traces where organizations interact with each other share the same concept:Name throughout the logs


# merges the logs and returns the merged log -> to merge sequentially
def mergeLogs(logs):
    merged_log = EventLog()  # Eventuell später Extensions/Globals hinzufügen, falls gewünscht
    # for fixed (shared) traceId
    # for i in range(len(logs)):
    #    merged_log = mergeTwoLogs(merged_log, logs[i])
    # for matching via msgInstanceId
    merged_log = merge_via_msg_id(logs)
    return merged_log


# merge two logs and return new merged log
# only works with fixed (shared) traceId
def mergeTwoLogs(merged_log, log):
    for trace_index, trace in enumerate(log):

        # find trace through identifier
        trace_id = trace.attributes.get(TRACE_ID)
        for i, merged_trace in enumerate(merged_log):
            if merged_trace.attributes.get(CONCEPT_NAME) == trace_id:
                pos = i
                break
        else:
            merged_log.append(Trace(attributes={CONCEPT_NAME: trace_id}))
            pos = len(merged_log) - 1

        for event in log[trace_index]:
            # Create new event for the merged log from current event
            new_event = create_new_event(event)

            # Should the merging not be done only by ascending timeSlots, this is where the logic for finding the
            # correct place must go

            for i in range(len(merged_log[pos])):
                if merged_log[pos][i]["time:timestamp"] > new_event["time:timestamp"]:
                    merged_log[pos].insert(i, new_event)
                    break
            else:
                merged_log[pos].append(new_event)  # if the event is the latest event

    return merged_log


# merges the traces using union-find for trace matching
# composed_id = (log_id, trace_id)
def merge_via_msg_id(logs):
    trace_list = []
    composed_id_to_trace = {}
    for log in logs:
        for trace in log:
            trace_list.append((log.attributes[LOG_ID], trace.attributes[TRACE_ID]))
            composed_id_to_trace[(log.attributes[LOG_ID], trace.attributes[TRACE_ID])] = trace

    uf = UnionFind(trace_list)
    connected_traces = defaultdict(list)
    for composed_id in trace_list:
        trace = composed_id_to_trace[composed_id]
        for event in trace:
            connected_traces[event[MSG_INSTANCE_ID]].append(composed_id)  # connects all traces with msg exchange between them

    for traces in connected_traces.values():
        for i in range(len(traces)):
            uf.union(traces[0], traces[i])

    groups_of_traces = defaultdict(list)  # for eliminating duplicates
    for composed_id in trace_list:
        root = uf.find(composed_id)
        groups_of_traces[root].append(composed_id)

    merged_log = EventLog(attributes={LOG_ID: f"merged_log_{date.today()}"})
    for i, (root, composed_ids) in enumerate(groups_of_traces.items()):
        merged_trace = Trace(attributes={TRACE_ID: str(i)})
        events = [event for composed_id in composed_ids for event in composed_id_to_trace[composed_id]]
        events.sort(key=lambda ev: ev[TIMESTAMP])

        for e in events:
            new_event = create_new_event(e)
            merged_trace.append(new_event)
        merged_log.append(merged_trace)

    return merged_log


def create_new_event(event):
    new_event = Event()
    # Copy preprocessed values
    new_event["concept:name"] = event.get(CONCEPT_NAME)
    new_event["org:group"] = event.get(ORG_GROUP)
    new_event["communicationMode"] = event.get(COMMUNICATION_MODE)
    new_event["msgType"] = event.get(MSG_TYPE)
    new_event["msgInstanceID"] = event.get(MSG_INSTANCE_ID)
    new_event["time:timestamp"] = event.get(TIMESTAMP)

    return new_event


class UnionFind:
    def __init__(self, log_trace_tuples):
        self.parent = {}
        self.rank = {}
        for i in log_trace_tuples:
            self.addTuple(i)

    def addTuple(self, log_trace_tuple):
        if log_trace_tuple not in self.parent:
            self.parent[log_trace_tuple] = log_trace_tuple
            self.rank[log_trace_tuple] = 0

    def find(self, x):
        root = self.parent[x]
        if self.parent[root] != root:
            self.parent[x] = self.find(root)
            return self.parent[x]
        return root

    def union(self, x, y):
        root_x, root_y = self.find(x), self.find(y)
        if root_x == root_y:
            return
        if self.rank[root_x] < self.rank[root_y]:
            self.parent[root_x] = root_y
        elif self.rank[root_y] < self.rank[root_x]:
            self.parent[root_y] = root_x
        else:
            self.parent[root_y] = root_x
            self.rank[root_x] += 1
