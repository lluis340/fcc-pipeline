import config  # Naming Conventions
from collections import defaultdict
from datetime import date
from pm4py.objects.log.obj import Event, Trace, EventLog


# Prerequisites:
# All events that are contained in these logs have message exchange activities
# All events have the same attribute names, as well as the same attributes
# All events are totally ordered based on their ascending timestamp

# Possible:
# All traces where organizations interact with each other share the same concept:Name throughout the logs


# merges the logs and returns the merged log -> to merge sequentially
def mergeLogs(logs, trace_groups=None):
    # for fixed (shared) traceId
    # for i in range(len(logs)):
    #    merged_log = mergeTwoLogs(merged_log, logs[i])
    # for matching via msgInstanceId
    if not trace_groups:
        *trace_groups, uf = group_traces(logs)
    merged_log = merge(*trace_groups)
    return merged_log


# merges the traces using union-find for trace matching
# composed_id = (log_id, trace_id)
def group_traces(logs):
    trace_list = []
    composed_id_to_trace = {}
    for log in logs:
        for trace in log:
            trace_list.append((log.attributes[config.ATTRIBUTES.log_id], trace.attributes[config.ATTRIBUTES.trace_id]))
            composed_id_to_trace[(log.attributes[config.ATTRIBUTES.log_id], trace.attributes[config.ATTRIBUTES.trace_id])] = trace

    uf = UnionFind(trace_list)
    connected_traces = defaultdict(list)
    for composed_id in trace_list:
        trace = composed_id_to_trace[composed_id]
        for event in trace:
            connected_traces[event[config.ATTRIBUTES.msg_instance_id]].append(
                composed_id)  # connects all traces with msg exchange between them

    for traces in connected_traces.values():
        for i in range(len(traces)):
            uf.union(traces[0], traces[i])

    groups_of_traces = defaultdict(list)  # for eliminating duplicates
    for composed_id in trace_list:
        root = uf.find(composed_id)
        groups_of_traces[root].append(composed_id)

    return groups_of_traces, composed_id_to_trace, uf


def merge(groups_of_traces, composed_id_to_trace):
    merged_log = EventLog(attributes={config.ATTRIBUTES.log_id: f"merged_log_{date.today()}"})
    for i, (root, composed_ids) in enumerate(groups_of_traces.items()):
        merged_trace = Trace(attributes={config.ATTRIBUTES.trace_id: f"trace_{str(i + 1)}"})
        events = [event for composed_id in composed_ids for event in composed_id_to_trace[composed_id]]
        events.sort(key=lambda ev: ev[config.ATTRIBUTES.timestamp])

        for e in events:
            new_event = create_new_event(e)
            merged_trace.append(new_event)
        merged_log.append(merged_trace)

    return merged_log


def create_new_event(event):
    new_event = Event()
    # Copy preprocessed values
    new_event["concept:name"] = event.get(config.ATTRIBUTES.event_id)
    new_event["org:group"] = event.get(config.ATTRIBUTES.org_group)
    new_event["communicationMode"] = event.get(config.ATTRIBUTES.communication_mode)
    new_event["msgType"] = event.get(config.ATTRIBUTES.msg_type)
    new_event["msgInstanceID"] = event.get(config.ATTRIBUTES.msg_instance_id)
    new_event["time:timestamp"] = event.get(config.ATTRIBUTES.timestamp)

    return new_event


# Validates that all messages are in the correct order (sent -> receive)
def validate_message_ordering(log):
    ...


class UnionFind:
    def __init__(self, log_trace_tuples=None):
        self.parent = {}
        self.rank = {}
        if log_trace_tuples:
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
