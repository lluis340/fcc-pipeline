from dataclasses import dataclass


@dataclass
class AttributeNames:
    log_id: str = "concept:name"
    trace_id: str = "concept:name"
    event_id:   str = "concept:name"
    org_group:  str = "org:group"
    timestamp: str = "time:timestamp"
    communication_mode:    str = "msgType"
    msg_instance_id:   str = "msgInstanceId"
    msg_type:    str = "msgFlow"


ATTRIBUTES: AttributeNames = AttributeNames()
