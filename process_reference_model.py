import sys

import pm4py
import xml.etree.ElementTree as ET

from collections import deque
from copy import deepcopy
from pathlib import Path
from lxml import etree
from pm4py import PetriNet
from pm4py.objects.bpmn.importer import importer as bpmn_importer
from pm4py.objects.conversion.bpmn.variants import to_petri_net
from pm4py.objects.petri_net import semantics
from pm4py.objects.petri_net.utils import petri_utils

BPMN_URI = "http://www.omg.org/spec/BPMN/20100524/MODEL"
NS = f"{{{BPMN_URI}}}"


class ReferenceModelError(ValueError):
    # A reference model that cannot be translated into a correct Petri net, or whose net cannot be aligned.
    # Subclass of ValueError because the one-pool check below raised a ValueError before.
    def __init__(self, reason, file=None, element_id=None, element_type=None, organization=None, variant=None):
        self.reason = reason
        self.file = file
        self.element_id = element_id
        self.element_type = element_type
        self.organization = organization
        self.variant = variant
        where = [part for part in (file and Path(file).name,
                                   organization and f"organization {organization}",
                                   element_type and f"{element_type} {element_id!r}",
                                   variant is not None and f"variant {variant}") if part]
        super().__init__(f"{', '.join(where)}: {reason}" if where else reason)


# Just a helper method to split collaborative bpmns into org-wide bpmns
def create_org_wide_bpmn_models(path, output_path):
    root = etree.parse(path).getroot()
    collaboration = root.find(f'{NS}collaboration')
    models = {}
    if len(collaboration.findall(f'{NS}participant')) == 1:
        return None
    message_flows = {flow: [flow.get('sourceRef'), flow.get('targetRef')]
                     for flow in collaboration.findall(f'{NS}messageFlow')}
    participants_by_id = {p.get('id'): p for p in collaboration.findall(f'{NS}participant')}
    pool = {el.get('id'): pid
            for pid, p in participants_by_id.items() if p.get('processRef')
            for el in root.find(f'{NS}process[@id="{p.get("processRef")}"]').iter(etree.Element)
            if el.get('id')}

    for participant in collaboration.findall(f'{NS}participant'):
        name = (participant.get('name') or '').strip()
        process_ref = participant.get('processRef')
        if not process_ref:
            continue

        process = root.find(f'{NS}process[@id="{process_ref}"]')
        process_ids = {elem.get('id') for elem in process.iter(etree.Element) if elem.get('id')}

        org_bpmn = etree.Element('definitions', nsmap={None: BPMN_URI})
        org_bpmn.set('targetNamespace', f'http://fcc-pipeline.local/bpmn_{name}')

        collab = etree.SubElement(org_bpmn, f'{NS}collaboration',
                                  id=f'collab_{participant.get("id")}')
        etree.SubElement(collab, f'{NS}participant',
                         id=participant.get('id'), name=name, processRef=process_ref)
        own_ids = {el.get('id') for el in process.iter(etree.Element) if el.get('id')}
        own_flows = [flow for flow, refs in message_flows.items()
                     if (refs[0] in own_ids) != (refs[1] in own_ids)]
        other_process_refs = (ref for flow in own_flows for ref in message_flows[flow] if ref not in own_ids)
        for box in dict.fromkeys(pool.get(ref, ref) for ref in other_process_refs):
            etree.SubElement(collab, f'{NS}participant',
                             id=box, name=participants_by_id[box].get('name') or box)
        for flow in own_flows:
            new_flow = deepcopy(flow)
            for end in ('sourceRef', 'targetRef'):
                if new_flow.get(end) not in own_ids:
                    new_flow.set(end, pool.get(new_flow.get(end), new_flow.get(end)))
            collab.append(new_flow)

        org_bpmn.append(deepcopy(process))
        org_bpmn_path = f"{output_path}/{name}.bpmn"
        etree.ElementTree(org_bpmn).write(org_bpmn_path, xml_declaration=True, encoding='UTF-8')
        models[name] = org_bpmn_path
    return models


# Guarantee of this module: for every valid BPMN collaboration model, the translation either yields a structurally
# and contextually correct open net or stops with a ReferenceModelError that names the element and the reason.
# Constructs are rejected exactly where no correct translation to a classical Petri net exists (OR-join,
# cancellation of concurrent regions, non-interrupting boundary events, multi-instance without a cardinality known at
# design time, ...); everything else is rewritten in memory (the file stays untouched) into the subset pm4py
# translates natively. Literature for the rewrites: Dijkman, Dumas & Ouyang (2008), Semantics and analysis of
# business process models in BPMN; normalization of implicit splits/joins as in Claes et al. (arXiv 1511.04052).

EVENT_KINDS = {"startEvent": "start", "endEvent": "end", "intermediateCatchEvent": "catch",
               "intermediateThrowEvent": "throw", "boundaryEvent": "boundary"}
EVENT_DIRECTIONS = {"start": "receive", "catch": "receive", "boundary": "receive", "end": "send", "throw": "send"}
# Events that represent communication and therefore show up as activities in the log.
# Timer, error, conditional, link, ... events usually leave no log event and would cause
# one model move per trace if they were visible, so they stay silent.
OBSERVABLE_INTERMEDIATE_DEFINITIONS = {"message", "signal", None}
OBSERVABLE_START_END_DEFINITIONS = {"message", "signal"}
OBSERVABLE_BOUNDARY_DEFINITIONS = {"message", "signal"}

TASK_TAGS = ("task", "userTask", "manualTask", "serviceTask", "scriptTask", "businessRuleTask",
             "sendTask", "receiveTask")
# root-level elements a callActivity may call instead of a process; called as a single task
GLOBAL_TASK_TAGS = {"globalTask", "globalUserTask", "globalManualTask", "globalScriptTask",
                    "globalBusinessRuleTask"}

# Classification of everything below <process> (and below every <subProcess> and every called <process>), checked on
# the raw XML before anything is rewritten. Default deny: a tag, event definition or child element that is not listed
# raises a ReferenceModelError.
# Sources (pm4py 2.7.23.4): importer objects/bpmn/importer/variants/lxml.py, converter
# objects/conversion/bpmn/variants/to_petri_net.py; see there for the line numbers.
NATIVE, REWRITTEN, IGNORED = "native", "rewritten", "ignored"
NODE_CLASSES = {
    # every tag ending in "task" is imported as BPMN.Task (lxml.py:121-135) and becomes a transition named by the id
    # and labelled by the name (to_petri_net.py:184-200); an unnamed task gets label None and is silent
    **dict.fromkeys(TASK_TAGS, NATIVE),
    "exclusiveGateway": NATIVE,  # lxml.py:347, to_petri_net.py:175
    "parallelGateway": NATIVE,  # lxml.py:374, to_petri_net.py:176, 206-229
    "startEvent": NATIVE,  # lxml.py:139, to_petri_net.py:173, 234-239 (silent); see EVENT_DEFINITION_CLASSES
    "endEvent": NATIVE,  # lxml.py:170, to_petri_net.py:174, 240-245 (silent); see EVENT_DEFINITION_CLASSES
    # imported (lxml.py:211, 256) but not translated (to_petri_net.py:170-178): rewritten to tasks by
    # make_events_observable, to silent gateways by make_silent_events_passable or resolve_link_events
    "intermediateCatchEvent": REWRITTEN,
    "intermediateThrowEvent": REWRITTEN,
    # imported (lxml.py:428) but not translated (to_petri_net.py:170-178, 247-252): rewritten to an
    # exclusiveGateway by rewrite_event_based_gateways after validate_event_based_gateways
    "eventBasedGateway": REWRITTEN,
    # imported as BPMN.SubProcess (lxml.py:92), not translated (to_petri_net.py:170-178): inlined by
    # inline_sub_process (Dijkman et al. 2008 map a sub-process without exception handling onto its content)
    "subProcess": REWRITTEN,
    # not imported (no branch in lxml.py:59-483): replaced by a copy of the called process (then inlined like a
    # sub-process) or, for a called global task, by a task (expand_call_activities)
    "callActivity": REWRITTEN,
    # imported (lxml.py:291) but not translated (to_petri_net.py:170-178): only interrupting boundary events on a
    # task, rewritten to an exclusive choice between the task and the event path (rewrite_boundary_events)
    "boundaryEvent": REWRITTEN,
}
FLOW_CLASSES = {"sequenceFlow": NATIVE}  # lxml.py:456-483, 611-617, to_petri_net.py:124-136, 247-271
# no control flow: lanes, data, artifacts and vendor extensions; skipped together with their children
IGNORED_TAGS = {"laneSet", "dataObject", "dataObjectReference", "dataStoreReference", "property",
                "ioSpecification", "association", "textAnnotation", "group", "documentation", "extensionElements"}
REJECTED_TAGS = {
    "inclusiveGateway": "the OR-join has non-local semantics (it waits only for the branches the matching split "
                        "activated) and has no counterpart in a classical Petri net; pm4py approximates it with "
                        "shortest paths (to_petri_net.py:143-152, 273-299)",
    "complexGateway": "the activation condition of a complex gateway has no counterpart in a classical Petri net "
                      "(and it is not imported by pm4py, no branch in lxml.py:59-483)",
    "adHocSubProcess": "an ad-hoc sub-process executes its activities in any order and number, which is not given "
                       "by its sequence flows",
    "transaction": "a transaction can be cancelled and compensated, which removes tokens from a concurrent region "
                   "(cancellation needs reset arcs, not a classical Petri net)",
}
# definition -> class per event kind; None is an event without definition, missing pairs are rejected
EVENT_DEFINITION_CLASSES = {
    # timer/conditional start: imported as NormalStartEvent (lxml.py:160-168), a silent start is correct
    "start": {None: NATIVE, "timer": NATIVE, "conditional": NATIVE, "message": REWRITTEN, "signal": REWRITTEN},
    # terminate: only accepted in a scope without concurrency, where it ends the scope like a plain end event
    # (resolve_terminate_events); error/escalation/cancel/compensate end are imported (lxml.py:188-199) but translated
    # like a plain end event (to_petri_net.py:240-245), so their effect on the enclosing scope is lost
    "end": {None: NATIVE, "message": REWRITTEN, "signal": REWRITTEN, "terminate": REWRITTEN},
    # timer/conditional: silent node by make_silent_events_passable; link: silent node by resolve_link_events;
    # error/escalation/compensate/cancel catch events are only valid as boundary events
    "catch": {None: REWRITTEN, "message": REWRITTEN, "signal": REWRITTEN,
              "timer": REWRITTEN, "conditional": REWRITTEN, "link": REWRITTEN},
    "throw": {None: REWRITTEN, "message": REWRITTEN, "signal": REWRITTEN, "link": REWRITTEN},
    # interrupting boundary events on a task (rewrite_boundary_events); compensate/cancel boundary events need
    # compensation handlers or transactions
    "boundary": {"message": REWRITTEN, "signal": REWRITTEN, "timer": REWRITTEN, "conditional": REWRITTEN,
                 "error": REWRITTEN, "escalation": REWRITTEN},
}
# allowed child elements of flow nodes; the children of these are not inspected
NODE_CHILDREN = {"incoming", "outgoing", "documentation", "extensionElements", "ioSpecification", "property",
                 "dataInputAssociation", "dataOutputAssociation", "dataInput", "dataOutput", "inputSet",
                 "outputSet"}
# loop characteristics, only on tasks (validate_loop_characteristics)
LOOP_CHILDREN = {"standardLoopCharacteristics", "multiInstanceLoopCharacteristics"}
FLOW_CHILDREN = {"documentation", "extensionElements", "conditionExpression"}
REJECTED_CHILDREN = {
    "eventDefinitionRef": "event definitions by reference are not read (lxml.py:149-152 only reads inline ones)",
}
# catch events translated as a silent node with the same id (make_silent_events_passable)
SILENT_CATCH_DEFINITIONS = {"timer", "conditional"}
# targets an event-based gateway may have (BPMNR062) that are translated; multiple definitions are rejected
EVENT_GATEWAY_TARGET_DEFINITIONS = {"message", "signal", "timer", "conditional"}
# upper bound of reachable markings for check_soundness
SOUNDNESS_STATE_LIMIT = 100_000


def local_name(element):
    return element.tag.removeprefix(NS) if isinstance(element.tag, str) and element.tag.startswith(NS) else None


def event_definitions(node):
    return [local_name(child).removesuffix("EventDefinition") for child in node
            if local_name(child) and local_name(child).endswith("EventDefinition")]


def is_gateway(node):
    return (local_name(node) or "").endswith("Gateway")


def strip_event_definitions(node):
    for child in list(node):
        if (local_name(child) or "").endswith("EventDefinition"):
            node.remove(child)


class IdFactory:
    # fresh ids for nodes and flows added by the rewrites; never collides with an id of the model
    def __init__(self, root):
        self.used = {element.get("id") for element in root.iter() if element.get("id")}

    def __call__(self, base):
        candidate, number = base, 1
        while candidate in self.used:
            number += 1
            candidate = f"{base}_{number}"
        self.used.add(candidate)
        return candidate


def add_node(scope, tag, new_id):
    return ET.SubElement(scope, f"{NS}{tag}", id=new_id)


def add_flow(scope, ids, source, target, base):
    return ET.SubElement(scope, f"{NS}sequenceFlow", id=ids(base), sourceRef=source, targetRef=target)


def flow_maps(scope):
    incoming, outgoing = {}, {}
    for flow in scope.findall(f"{NS}sequenceFlow"):
        incoming.setdefault(flow.get("targetRef"), []).append(flow)
        outgoing.setdefault(flow.get("sourceRef"), []).append(flow)
    return incoming, outgoing


def scope_nodes(scope):
    return {node.get("id"): node for node in scope if local_name(node) in NODE_CLASSES and node.get("id")}


# ----------------------------------------------------------------------------------------------------------------
# Selecting the pool
# ----------------------------------------------------------------------------------------------------------------

def select_main_process(root, model_path):
    # One white-box pool per file. Further processes are only allowed as targets of call activities.
    processes = {process.get("id"): process for process in root.findall(f"{NS}process")}
    pool_refs = {participant.get("processRef") for participant in root.iter(f"{NS}participant")
                 if participant.get("processRef")}
    called = {activity.get("calledElement") for activity in root.iter(f"{NS}callActivity")}
    if pool_refs:
        if len(pool_refs) != 1:
            raise ReferenceModelError(f"expected one pool per file, found {len(pool_refs)}", file=model_path)
        main = processes.get(next(iter(pool_refs)))
        if main is None:
            raise ReferenceModelError(f"the pool refers to process {next(iter(pool_refs))!r}, which is not in the "
                                      f"file", file=model_path)
    else:
        candidates = [process for process_id, process in processes.items() if process_id not in called]
        if len(candidates) != 1:
            raise ReferenceModelError(f"expected one pool per file, found {len(candidates)} processes that are not "
                                      f"called by a call activity", file=model_path)
        main = candidates[0]
    stray = [process_id for process_id, process in processes.items() if process is not main
             and process_id not in called]
    if stray:
        raise ReferenceModelError(f"expected one pool per file, found further processes {stray} that are neither "
                                  f"the pool nor called by a call activity", file=model_path)
    return main, processes


# ----------------------------------------------------------------------------------------------------------------
# Validation (raw XML, before any rewrite)
# ----------------------------------------------------------------------------------------------------------------

def validate_event_based_gateways(scope, model_path):
    nodes = {node.get("id"): node for node in scope if node.get("id")}
    incoming, outgoing = flow_maps(scope)

    for gateway in scope.findall(f"{NS}eventBasedGateway"):
        def reject(reason):
            raise ReferenceModelError(reason, file=model_path, element_id=gateway.get("id"),
                                      element_type="eventBasedGateway")

        if gateway.get("instantiate") == "true":
            reject(f"instantiating event-based gateways ({gateway.get('eventGatewayType', 'Exclusive')}) "
                   f"are not translated")
        if gateway.get("gatewayDirection") not in (None, "Unspecified", "Diverging"):
            reject(f"only diverging event-based gateways are translated, "
                   f"found gatewayDirection {gateway.get('gatewayDirection')!r}")
        flows_in = incoming.get(gateway.get("id"), [])
        flows_out = outgoing.get(gateway.get("id"), [])
        if len(flows_in) != 1:
            reject(f"expected exactly one incoming sequence flow, found {len(flows_in)}")
        if len(flows_out) < 2:
            reject(f"expected at least two outgoing sequence flows, found {len(flows_out)}")
        for flow in flows_out:
            if flow.find(f"{NS}conditionExpression") is not None:
                reject(f"outgoing sequence flow {flow.get('id')!r} has a conditionExpression")
            target = nodes.get(flow.get("targetRef"))
            target_tag = local_name(target) if target is not None else None
            definitions = event_definitions(target) if target is not None else []
            if target_tag == "receiveTask":
                continue
            if (target_tag == "intermediateCatchEvent" and len(definitions) == 1
                    and definitions[0] in EVENT_GATEWAY_TARGET_DEFINITIONS):
                continue
            reject(f"target {flow.get('targetRef')!r} of {flow.get('id')!r} is no receive task or message, "
                   f"signal, timer or conditional catch event (found {target_tag} with definitions {definitions})")


def validate_loop_characteristics(node, reject):
    loops = [child for child in node if local_name(child) in LOOP_CHILDREN]
    if not loops:
        return
    if local_name(node) not in TASK_TAGS:
        reject(node, f"loop or multi-instance characteristics are only translated on tasks, not on "
                     f"{local_name(node)}")
    if len(loops) > 1:
        reject(node, "more than one loop characteristics element")
    loop = loops[0]
    if local_name(loop) == "standardLoopCharacteristics":
        if loop.get("loopMaximum") is not None:
            reject(node, "a loop with loopMaximum bounds the number of iterations, which an unbounded loop "
                         "structure would not respect")
        return
    # multi-instance: only with a cardinality known at design time (Workflow pattern "Multiple Instances with a Priori
    # Design-Time Knowledge"); without it the number of instances has no counterpart in a classical Petri net
    for child in loop:
        if local_name(child) in ("completionCondition", "complexBehaviorDefinition"):
            reject(node, f"multi-instance activity with {local_name(child)} (instances may end early)")
    if loop.get("behavior") not in (None, "All"):
        reject(node, f"multi-instance activity with behavior {loop.get('behavior')!r} throws events per instance")
    multi_instance_cardinality(node, reject)


def multi_instance_cardinality(node, reject):
    loop = next(child for child in node if local_name(child) == "multiInstanceLoopCharacteristics")
    cardinality = loop.find(f"{NS}loopCardinality")
    text = (cardinality.text or "").strip() if cardinality is not None else ""
    if not text.isdigit() or int(text) < 1:
        reject(node, f"multi-instance activity without a constant loopCardinality (found {text!r}): the number of "
                     f"instances is not known at design time")
    return int(text)


def validate_scope(scope, model_path, root, processes, validated):
    # Validates the direct children of a process or sub-process and recurses into sub-processes and called processes.
    validate_event_based_gateways(scope, model_path)
    is_sub_process = local_name(scope) == "subProcess"

    def reject(node, reason):
        raise ReferenceModelError(reason, file=model_path, element_id=node.get("id"),
                                  element_type=local_name(node) or node.tag)

    nodes = {}
    flows = []
    for node in scope:
        tag = local_name(node)
        if tag is None:
            reject(node, f"element outside the BPMN namespace directly in {local_name(scope)}")
        if tag in IGNORED_TAGS:
            continue
        if is_sub_process and tag in NODE_CHILDREN:
            continue  # incoming/outgoing/data associations of the sub-process itself
        if is_sub_process and tag in LOOP_CHILDREN:
            reject(scope, "loop or multi-instance characteristics are only translated on tasks")
        if tag in REJECTED_TAGS:
            reject(node, REJECTED_TAGS[tag])
        if tag not in NODE_CLASSES and tag not in FLOW_CLASSES:
            reject(node, "unclassified element (default deny)")
        if not node.get("id"):
            reject(node, "element without id is skipped by pm4py (lxml.py:58)")

        if tag in FLOW_CLASSES:
            for child in node:
                if local_name(child) not in FLOW_CHILDREN:
                    reject(node, f"unclassified child element {local_name(child) or child.tag} (default deny)")
            flows.append(node)
            continue

        if tag not in ("subProcess",):
            for child in node:
                child_tag = local_name(child)
                if child_tag in REJECTED_CHILDREN:
                    reject(node, REJECTED_CHILDREN[child_tag])
                if child_tag in NODE_CHILDREN or child_tag in LOOP_CHILDREN:
                    continue
                if child_tag and child_tag.endswith("EventDefinition") and tag in EVENT_KINDS:
                    continue
                reject(node, f"unclassified child element {child_tag or child.tag} (default deny)")
            validate_loop_characteristics(node, reject)

        if node.get("isForCompensation") == "true":
            reject(node, "compensation activities are not translated")
        if node.get("instantiate") == "true":
            reject(node, "instantiating receive tasks are not translated")

        if tag in EVENT_KINDS:
            kind = EVENT_KINDS[tag]
            definitions = event_definitions(node)
            if len(definitions) > 1:
                reject(node, "events with more than one event definition (multiple/parallel multiple) are not "
                             "translated")
            definition = definitions[0] if definitions else None
            if definition not in EVENT_DEFINITION_CLASSES[kind]:
                reject(node, f"{definition} {tag} is not translated")
            if is_sub_process and kind == "start" and definition is not None:
                reject(node, "the start event of a sub-process must not have a trigger (BPMN 2.0.2, 10.5.2)")

        if tag == "subProcess":
            if node.get("triggeredByEvent") == "true":
                reject(node, "event sub-processes interrupt or run alongside their enclosing scope, which needs "
                             "cancellation (no classical Petri net)")
            starts = node.findall(f"{NS}startEvent")
            if len(starts) != 1:
                reject(node, f"expected exactly one start event in the sub-process, found {len(starts)}")
            if not node.findall(f"{NS}endEvent"):
                reject(node, "sub-process without end event (implicit end is not translated)")
            validate_scope(node, model_path, root, processes, validated)

        if tag == "callActivity":
            called = node.get("calledElement")
            if not called:
                reject(node, "call activity without calledElement")
            if called in processes:
                if called not in validated:
                    validated.add(called)
                    target = processes[called]
                    starts = target.findall(f"{NS}startEvent")
                    if len(starts) != 1 or event_definitions(starts[0]):
                        reject(node, f"the called process {called!r} must have exactly one start event without "
                                     f"trigger (it is inlined like a sub-process)")
                    if not target.findall(f"{NS}endEvent"):
                        reject(node, f"the called process {called!r} has no end event")
                    validate_scope(target, model_path, root, processes, validated)
            elif not any(local_name(element) in GLOBAL_TASK_TAGS and element.get("id") == called
                         for element in root):
                reject(node, f"called element {called!r} is neither a process nor a global task in this file")

        nodes[node.get("id")] = node

    incoming, outgoing = {}, {}
    for flow in flows:
        for end in ("sourceRef", "targetRef"):
            if flow.get(end) not in nodes:
                reject(flow, f"{end} {flow.get(end)!r} is no translated flow node of this {local_name(scope)}")
        incoming.setdefault(flow.get("targetRef"), []).append(flow)
        outgoing.setdefault(flow.get("sourceRef"), []).append(flow)

    for node_id, node in nodes.items():
        tag = local_name(node)
        flows_out = outgoing.get(node_id, [])
        if not is_gateway(node):
            # Conditional flows out of an activity or event are evaluated independently, i.e. they form an inclusive
            # split (OR), which is not translated; a default flow only exists together with them.
            for flow in flows_out:
                if flow.find(f"{NS}conditionExpression") is not None:
                    reject(flow, f"conditional sequence flow out of {tag} {node_id!r} (inclusive split semantics)")
            if node.get("default") is not None:
                reject(node, "default sequence flow out of an activity (only meaningful with conditional flows)")
        if tag == "boundaryEvent":
            attached = nodes.get(node.get("attachedToRef"))
            if attached is None:
                reject(node, f"attachedToRef {node.get('attachedToRef')!r} is no flow node of this scope")
            if local_name(attached) not in TASK_TAGS:
                reject(node, f"boundary events are only translated on tasks; on a {local_name(attached)} they cancel "
                             f"a region that may hold concurrent tokens (needs reset arcs)")
            if any(local_name(child) in LOOP_CHILDREN for child in attached):
                reject(node, "boundary event on a loop or multi-instance task (per instance or for all instances "
                             "is not determined by the net)")
            if node.get("cancelActivity") == "false":
                reject(node, "non-interrupting boundary events may only occur while the activity runs; an atomic "
                             "transition has no such interval")
            if incoming.get(node_id):
                reject(node, "boundary event with incoming sequence flow")
            if not flows_out:
                reject(node, "boundary event without outgoing sequence flow")
        if tag == "intermediateThrowEvent" and "link" in event_definitions(node) and flows_out:
            reject(node, "link throw event with outgoing sequence flow")
        if tag == "intermediateCatchEvent" and "link" in event_definitions(node) and incoming.get(node_id):
            reject(node, "link catch event with incoming sequence flow")


# ----------------------------------------------------------------------------------------------------------------
# Rewrites (in memory, on the parsed XML)
# ----------------------------------------------------------------------------------------------------------------

def prefix_ids(element, prefix):
    for child in element.iter():
        for attribute in ("id", "sourceRef", "targetRef", "attachedToRef", "default"):
            if child.get(attribute):
                child.set(attribute, prefix + child.get(attribute))
        if local_name(child) in ("incoming", "outgoing") and child.text:
            child.text = prefix + child.text.strip()


def expand_call_activities(scope, root, processes, model_path, stack=()):
    # A call activity of a process becomes a sub-process holding a copy of the called process (ids prefixed with the
    # call activity's id, so several calls of one process stay distinct); it is inlined with the other sub-processes.
    # A call activity of a global task becomes a task with the call activity's id and name.
    for index, node in enumerate(list(scope)):
        tag = local_name(node)
        if tag == "subProcess":
            expand_call_activities(node, root, processes, model_path, stack)
            continue
        if tag != "callActivity":
            continue
        called = node.get("calledElement")
        if called in stack:
            raise ReferenceModelError(f"recursive call of process {called!r} has no finite translation",
                                      file=model_path, element_id=node.get("id"), element_type="callActivity")
        if called in processes:
            sub_process = ET.Element(f"{NS}subProcess", {key: value for key, value in node.attrib.items()
                                                         if key in ("id", "name")})
            for child in node:
                if local_name(child) in ("incoming", "outgoing"):
                    sub_process.append(deepcopy(child))
            for child in processes[called]:
                if local_name(child) in IGNORED_TAGS:
                    continue
                copy = deepcopy(child)
                prefix_ids(copy, f"{node.get('id')}__")
                sub_process.append(copy)
            scope.remove(node)
            scope.insert(index, sub_process)
            expand_call_activities(sub_process, root, processes, model_path, stack + (called,))
        else:
            node.tag = f"{NS}task"
            node.attrib.pop("calledElement", None)


def resolve_link_events(scope, ids, model_path):
    # Link events are a GOTO within one process level (BPMN 2.0.2, 10.5.5): every throw jumps to the catch with the
    # same link name. Both become silent pass-through nodes connected by a sequence flow.
    def link_name(node):
        definition = next(child for child in node if local_name(child) == "linkEventDefinition")
        return definition.get("name") or node.get("name")

    def reject(node, reason):
        raise ReferenceModelError(reason, file=model_path, element_id=node.get("id"), element_type=local_name(node))

    catches, throws = {}, []
    for node in scope:
        if "link" not in event_definitions(node):
            continue
        name = link_name(node)
        if not name:
            reject(node, "link event without name (cannot be paired)")
        if local_name(node) == "intermediateCatchEvent":
            if name in catches:
                reject(node, f"two link catch events named {name!r} in one process level")
            catches[name] = node
        else:
            throws.append((name, node))

    thrown = set()
    for name, node in throws:
        if name not in catches:
            reject(node, f"no link catch event named {name!r} in the same process level")
        thrown.add(name)
        add_flow(scope, ids, node.get("id"), catches[name].get("id"), f"{node.get('id')}__link")
        strip_event_definitions(node)
        node.tag = f"{NS}exclusiveGateway"
    for name, node in catches.items():
        if name not in thrown:
            reject(node, f"link catch event {name!r} without a link throw event (unreachable)")
        strip_event_definitions(node)
        node.tag = f"{NS}exclusiveGateway"


def has_concurrency(scope):
    # Can two tokens be in this scope at the same time? Nested sub-processes do not count: they complete with all
    # their tokens before the scope continues.
    _, outgoing = flow_maps(scope)
    for node in scope:
        tag = local_name(node)
        if tag == "parallelGateway":
            return True
        if tag in NODE_CLASSES and not is_gateway(node) and len(outgoing.get(node.get("id"), [])) > 1:
            return True  # implicit parallel split
        for child in node:
            if (local_name(child) == "multiInstanceLoopCharacteristics"
                    and child.get("isSequential", "false") != "true"):
                return True
    return False


def resolve_terminate_events(scope, model_path):
    # A terminate end event removes all tokens of its scope. Without concurrency in the scope there is no other token,
    # so it behaves like a plain end event; with concurrency it needs reset arcs (no classical Petri net).
    terminates = [node for node in scope if local_name(node) == "endEvent" and "terminate" in event_definitions(node)]
    if not terminates:
        return
    if has_concurrency(scope):
        raise ReferenceModelError("terminate end event in a scope with concurrent branches (cancelling the other "
                                  "branches needs reset arcs, not a classical Petri net)", file=model_path,
                                  element_id=terminates[0].get("id"), element_type="endEvent")
    for node in terminates:
        strip_event_definitions(node)


def normalize_implicit_gateways(scope, ids):
    # BPMN gives an activity or event with several outgoing flows AND-split semantics and one with several incoming
    # flows XOR-join semantics. pm4py turns several outgoing flows into an exclusive choice (to_petri_net.py:256-261),
    # so the split is made explicit; the join is made explicit too, so every rewrite below sees one incoming flow.
    nodes = scope_nodes(scope)
    incoming, outgoing = flow_maps(scope)
    for node_id, node in nodes.items():
        if is_gateway(node):
            continue
        if len(outgoing.get(node_id, [])) > 1:
            split = add_node(scope, "parallelGateway", ids(f"{node_id}__split"))
            for flow in outgoing[node_id]:
                flow.set("sourceRef", split.get("id"))
            add_flow(scope, ids, node_id, split.get("id"), f"{node_id}__split_flow")
        if len(incoming.get(node_id, [])) > 1:
            join = add_node(scope, "exclusiveGateway", ids(f"{node_id}__merge"))
            for flow in incoming[node_id]:
                flow.set("targetRef", join.get("id"))
            add_flow(scope, ids, join.get("id"), node_id, f"{node_id}__merge_flow")


def inline_sub_process(scope, sub_process, events, ids):
    # The sub-process is replaced by its content: its start event becomes the entry, all its end events lead to one
    # exclusive exit gateway. Several end events reached concurrently would leave the exit with several tokens; that
    # case is caught by check_soundness (improper completion), not translated silently.
    sub_id = sub_process.get("id")
    start = sub_process.find(f"{NS}startEvent")
    start.tag = f"{NS}exclusiveGateway"
    strip_event_definitions(start)
    exit_gateway = add_node(sub_process, "exclusiveGateway", ids(f"{sub_id}__exit"))
    for end in sub_process.findall(f"{NS}endEvent"):
        info = events.get(end.get("id"), {})
        if info.get("definition") in OBSERVABLE_START_END_DEFINITIONS:
            end.tag = f"{NS}task"  # sending end event: stays an observable activity
        else:
            end.tag = f"{NS}exclusiveGateway"
            strip_event_definitions(end)
        add_flow(sub_process, ids, end.get("id"), exit_gateway.get("id"), f"{end.get('id')}__exit_flow")

    for flow in scope.findall(f"{NS}sequenceFlow"):
        if flow.get("targetRef") == sub_id:
            flow.set("targetRef", start.get("id"))
        if flow.get("sourceRef") == sub_id:
            flow.set("sourceRef", exit_gateway.get("id"))

    index = list(scope).index(sub_process)
    scope.remove(sub_process)
    for child in list(sub_process):
        if local_name(child) in NODE_CLASSES or local_name(child) in FLOW_CLASSES:
            scope.insert(index, child)
            index += 1


def normalize_scope(scope, events, ids, model_path):
    # Bottom-up: inner sub-processes first, then the scope-local rewrites (links and terminate are scoped to one
    # process level and must be resolved before the sub-processes are merged into it), then inlining.
    for node in list(scope):
        if local_name(node) == "subProcess":
            normalize_scope(node, events, ids, model_path)
    resolve_link_events(scope, ids, model_path)
    resolve_terminate_events(scope, model_path)
    normalize_implicit_gateways(scope, ids)
    for node in list(scope):
        if local_name(node) == "subProcess":
            inline_sub_process(scope, node, events, ids)


def rewrite_boundary_events(process, events, ids):
    # An interrupting boundary event on a task: either the task completes, or the event occurs and the task is
    # interrupted (it never completes, so it leaves no completed activity). With atomic transitions this is an
    # exclusive choice before the task between the task and every boundary event attached to it.
    incoming, _ = flow_maps(process)
    choices = {}
    for event in list(process.findall(f"{NS}boundaryEvent")):
        task_id = event.get("attachedToRef")
        if task_id not in choices:
            choice = add_node(process, "exclusiveGateway", ids(f"{task_id}__boundary"))
            for flow in incoming.get(task_id, []):
                flow.set("targetRef", choice.get("id"))
            add_flow(process, ids, choice.get("id"), task_id, f"{task_id}__boundary_flow")
            choices[task_id] = choice
        add_flow(process, ids, choices[task_id].get("id"), event.get("id"), f"{event.get('id')}__boundary_flow")
        for attribute in ("attachedToRef", "cancelActivity", "parallelMultiple"):
            event.attrib.pop(attribute, None)
        if events[event.get("id")]["definition"] in OBSERVABLE_BOUNDARY_DEFINITIONS:
            event.tag = f"{NS}task"  # received message/signal: observable like a catch event
        else:
            event.tag = f"{NS}exclusiveGateway"  # timer/conditional/error/escalation: no log event
            strip_event_definitions(event)


def expand_loops(process, ids, origin, model_path):
    # Runs after normalize_implicit_gateways, so every task has at most one incoming and one outgoing flow.
    incoming, outgoing = flow_maps(process)
    for task in [node for node in process if local_name(node) in TASK_TAGS]:
        loop = next((child for child in task if local_name(child) in LOOP_CHILDREN), None)
        if loop is None:
            continue
        task_id = task.get("id")
        flows_in, flows_out = incoming.get(task_id, []), outgoing.get(task_id, [])
        task.remove(loop)

        if local_name(loop) == "standardLoopCharacteristics":
            # loopCondition is data and, like a gateway condition, becomes a nondeterministic choice.
            entry = add_node(process, "exclusiveGateway", ids(f"{task_id}__loop_entry"))
            decision = add_node(process, "exclusiveGateway", ids(f"{task_id}__loop_decision"))
            for flow in flows_in:
                flow.set("targetRef", entry.get("id"))
            for flow in flows_out:
                flow.set("sourceRef", decision.get("id"))
            if loop.get("testBefore") == "true":
                # while: test, then body; the body may run zero times
                add_flow(process, ids, entry.get("id"), decision.get("id"), f"{task_id}__loop_test")
                add_flow(process, ids, decision.get("id"), task_id, f"{task_id}__loop_body")
                add_flow(process, ids, task_id, entry.get("id"), f"{task_id}__loop_back")
            else:
                # do-while (default testBefore=false): body, then test; the body runs at least once
                add_flow(process, ids, entry.get("id"), task_id, f"{task_id}__loop_body")
                add_flow(process, ids, task_id, decision.get("id"), f"{task_id}__loop_test")
                add_flow(process, ids, decision.get("id"), entry.get("id"), f"{task_id}__loop_back")
            continue

        # multi-instance with a constant cardinality: unfolded into that many copies of the task
        def reject(node, reason):
            raise ReferenceModelError(reason, file=model_path, element_id=node.get("id"),
                                      element_type=local_name(node))

        task.append(loop)
        cardinality = multi_instance_cardinality(task, reject)
        task.remove(loop)
        copies = []
        for number in range(1, cardinality + 1):
            copy = deepcopy(task)
            copy.set("id", ids(f"{task_id}__mi{number}"))
            for child in list(copy):
                if local_name(child) in ("incoming", "outgoing"):
                    copy.remove(child)
            process.append(copy)
            origin[copy.get("id")] = task_id
            copies.append(copy.get("id"))
        process.remove(task)
        if loop.get("isSequential") == "true":
            first, last = copies[0], copies[-1]
            for previous, following in zip(copies, copies[1:]):
                add_flow(process, ids, previous, following, f"{previous}__next")
        else:
            split = add_node(process, "parallelGateway", ids(f"{task_id}__mi_split"))
            join = add_node(process, "parallelGateway", ids(f"{task_id}__mi_join"))
            for copy_id in copies:
                add_flow(process, ids, split.get("id"), copy_id, f"{copy_id}__in")
                add_flow(process, ids, copy_id, join.get("id"), f"{copy_id}__out")
            first, last = split.get("id"), join.get("id")
        for flow in flows_in:
            flow.set("targetRef", first)
        for flow in flows_out:
            flow.set("sourceRef", last)


def read_event_definitions(process):
    # Read before the rewrite: pm4py's importer drops signal definitions (they become Normal*Event).
    # Signal definitions are carried but never turned into I/O places: an interface place models a
    # buffered point-to-point channel, a BPMN signal is a broadcast without buffering or exclusivity.
    # Carried anyway so that broadcast communication can be added later without re-reading the model.
    events = {}
    for node in process.iter():
        kind = EVENT_KINDS.get(local_name(node))
        if kind is None:
            continue
        definitions = [child for child in node if (local_name(child) or "").endswith("EventDefinition")]
        definition = definitions[0] if definitions else None
        events[node.get("id")] = {
            "kind": kind,
            "definition": (local_name(definition).removesuffix("EventDefinition")
                           if definition is not None else None),
            "ref": (definition.get("messageRef") or definition.get("signalRef")
                    if definition is not None else None),
            "direction": EVENT_DIRECTIONS[kind],
        }
    return events


def make_events_observable(process, events, ids):
    # pm4py's BPMN -> Petri net conversion only translates tasks, start/end events and
    # XOR/AND/OR gateways (to_petri_net.py): intermediate events are dropped together with their
    # sequence flows, and start/end events always become silent. Rewriting these events to tasks
    # (same id and name) makes pm4py emit a visible transition named by the id and labelled by the name.
    # Timer/conditional catch events and event-based gateways: make_silent_events_passable and
    # rewrite_event_based_gateways. Only nodes that are still events are touched (earlier rewrites may have turned
    # an event into a task or gateway already).
    for node in list(process):
        info = events.get(node.get("id"))
        if info is None or local_name(node) not in EVENT_KINDS:
            continue
        if info["kind"] in ("catch", "throw"):
            if info["definition"] in OBSERVABLE_INTERMEDIATE_DEFINITIONS:
                node.tag = f"{NS}task"
            continue
        if info["definition"] not in OBSERVABLE_START_END_DEFINITIONS:
            continue

        # start/end event with a definition: a plain (silent) event keeps the role of starting or
        # ending the instance, the former event becomes the observable task right after/before it
        node_id = node.get("id")
        node.tag = f"{NS}task"
        plain = ET.SubElement(process, f"{NS}{'startEvent' if info['kind'] == 'start' else 'endEvent'}",
                              id=ids(f"{node_id}__plain"))
        source, target = ((plain.get("id"), node_id) if info["kind"] == "start"
                          else (node_id, plain.get("id")))
        add_flow(process, ids, source, target, f"{node_id}__flow")


def make_silent_events_passable(process, events):
    # The event-based gateway admits message, signal, timer, conditional and multiple catch events as targets
    # (BPMN rule BPMNR062), and pm4py drops catch events it does not translate together with their sequence flows.
    # Timer and conditional catch events leave no log event, so they become an exclusiveGateway with the same id,
    # i.e. a silent transition between their one incoming and one outgoing flow.
    for node in process:
        info = events.get(node.get("id"))
        if (info is not None and local_name(node) == "intermediateCatchEvent" and info["kind"] == "catch"
                and info["definition"] in SILENT_CATCH_DEFINITIONS):
            node.tag = f"{NS}exclusiveGateway"
            strip_event_definitions(node)


def rewrite_event_based_gateways(process):
    # Only the control flow is translated: the target event that occurs first takes the token, the other branches
    # are discarded. Without time and message data this event race is a nondeterministic choice, which is exactly
    # what an exclusiveGateway translates to (non-instantiating event-based gateways are exclusive by the spec).
    for node in process.findall(f"{NS}eventBasedGateway"):
        node.tag = f"{NS}exclusiveGateway"


def drop_flow_references(process):
    # pm4py reads <incoming>/<outgoing> as a fallback for the ends of a flow (lxml.py:484-513). After the rewrites the
    # sequenceFlow elements are the only valid source, so the (now partly stale) references are removed.
    for node in process.iter():
        for child in list(node):
            if local_name(child) in ("incoming", "outgoing"):
                node.remove(child)


# ----------------------------------------------------------------------------------------------------------------
# Checks on the translated net
# ----------------------------------------------------------------------------------------------------------------

def check_soundness(net, i_m, f_m, model_path, process_id):
    # Breadth-first search over the reachable markings instead of pm4py's check_easy_soundness_net_in_fin_marking,
    # whose A* search (explore_path.py) has no bound and does not terminate on an unbounded net. Checked:
    # - the final marking is reachable,
    # - proper completion: no reachable marking covers the final marking with further tokens left (e.g. end events
    #   or sub-process ends reached concurrently, which BPMN ends by implicit termination; a single final marking
    #   cannot express that),
    # - option to complete: the final marking stays reachable from every reachable marking. This also catches every
    #   case in which a rewrite left a token behind.
    def fail(reason):
        raise ReferenceModelError(reason, file=model_path, element_id=process_id, element_type="process")

    seen = {i_m}
    order = [i_m]
    successors = {}
    queue = deque([i_m])
    while queue:
        marking = queue.popleft()
        successors[marking] = []
        if f_m <= marking and marking != f_m:
            fail(f"improper completion: a reachable marking covers the final marking and holds further tokens "
                 f"({', '.join(sorted(place.name for place in marking))}), e.g. end events reached concurrently")
        for transition in semantics.enabled_transitions(net, marking):
            successor = semantics.execute(transition, net, marking)
            successors[marking].append(successor)
            if successor in seen:
                continue
            if len(seen) >= SOUNDNESS_STATE_LIMIT:
                fail(f"soundness check aborted: more than {SOUNDNESS_STATE_LIMIT} reachable markings "
                     f"(SOUNDNESS_STATE_LIMIT)")
            seen.add(successor)
            order.append(successor)
            queue.append(successor)
    if f_m not in seen:
        fail("final marking not reachable from the initial marking (not easy sound)")

    predecessors = {}
    for marking, targets in successors.items():
        for target in targets:
            predecessors.setdefault(target, []).append(marking)
    can_complete = {f_m}
    queue = deque([f_m])
    while queue:
        for previous in predecessors.get(queue.popleft(), []):
            if previous not in can_complete:
                can_complete.add(previous)
                queue.append(previous)
    stuck = [marking for marking in order if marking not in can_complete]
    if stuck:
        fail(f"no option to complete: from {len(stuck)} reachable marking(s) the final marking cannot be reached, "
             f"e.g. {', '.join(sorted(place.name for place in stuck[0]))}")


def check_coverage(bpmn_graph, process, net, model_path):
    # Every flow node and sequence flow of the (rewritten) process needs a counterpart in the net. Checked on
    # an unreduced translation: RETURN_FLOW_TRANS_MAP disables the reduction (to_petri_net.py:104-105) and
    # returns the place of every flow and the transitions of every translated node. The reduction of the net
    # used later (to_petri_net.py:301-302) then only merges silent transitions with their places, so silent
    # gateways, start/end events and flow places may be missing there; visible transitions may not.
    unreduced, _, _, flow_place, trans_map = to_petri_net.apply(
        bpmn_graph, parameters={to_petri_net.Parameters.RETURN_FLOW_TRANS_MAP: True})
    translated_nodes = {node.get_id() for node in trans_map}
    flow_places = {flow.get_id(): place for flow, place in flow_place.items()}

    def missing(node, reason):
        raise ReferenceModelError(f"no counterpart in the Petri net: {reason}", file=model_path,
                                  element_id=node.get("id"), element_type=local_name(node))

    for node in process:
        tag = local_name(node)
        if tag in IGNORED_TAGS:
            continue
        if tag in FLOW_CLASSES:
            place = flow_places.get(node.get("id"))
            if place is None or not place.in_arcs or not place.out_arcs:
                missing(node, "sequence flow is not connected")
        elif node.get("id") not in translated_nodes:
            missing(node, "flow node was dropped by pm4py")

    visible = sorted((t.name, t.label) for t in unreduced.transitions if t.label is not None)
    if visible != sorted((t.name, t.label) for t in net.transitions if t.label is not None):
        raise ReferenceModelError("the reduction of the Petri net changed its visible transitions", file=model_path)


# ----------------------------------------------------------------------------------------------------------------
# Interface places
# ----------------------------------------------------------------------------------------------------------------

def take_message_flows(root):
    # Message flows only feed the interface places. They are removed before pm4py imports the model: pm4py resolves
    # their ends to nodes (lxml.py:589-601) and fails on an end that a rewrite replaced (a sub-process, a call activity,
    # an unfolded multi-instance task). Their ends are checked in add_interface_places instead.
    message_flows = []
    for parent in list(root.iter()):
        for child in list(parent):
            if local_name(child) == "messageFlow":
                message_flows.append(child)
                parent.remove(child)
    return message_flows


def add_interface_places(root, main, own_nodes, message_flows, net, origin, model_path):
    # Every message flow with one end in this pool yields an input or output place attached to exactly the
    # transitions of that BPMN node (matched by id, not by label: two nodes may share a name). A message flow whose
    # end in this pool cannot be matched to a visible transition is an error, not a silent omission.
    def reject(reason, message_flow):
        raise ReferenceModelError(reason, file=model_path, element_id=message_flow.get("id"),
                                  element_type="messageFlow")

    own_pools = {participant.get("id") for participant in root.iter(f"{NS}participant")
                 if participant.get("processRef") == main.get("id")}
    other_pools = {participant.get("id") for participant in root.iter(f"{NS}participant")} - own_pools
    transitions_by_node = {}
    for transition in net.transitions:
        if transition.label is not None:
            transitions_by_node.setdefault(origin.get(transition.name, transition.name), []).append(transition)

    I, O = set(), set()
    for message_flow in message_flows:
        message = message_flow.get("name") or message_flow.get("id")
        ends = ((message_flow.get("sourceRef"), True), (message_flow.get("targetRef"), False))
        for ref, _ in ends:
            if ref not in own_nodes and ref not in own_pools and ref not in other_pools:
                reject(f"end {ref!r} is neither a node of this pool nor a pool (e.g. a node of a called process, "
                       f"which has no own interface)", message_flow)
        own_ends = [(ref, outgoing) for ref, outgoing in ends if ref in own_nodes or ref in own_pools]
        if len(own_ends) == 2:
            reject("message flow with both ends in the same pool (BPMN 2.0.2, 9.4)", message_flow)
        for ref, outgoing in own_ends:
            direction = "send" if outgoing else "receive"
            if ref in own_pools:
                reject(f"message flow ({direction}) attached to the boundary of this pool: the sending or receiving "
                       f"activity is not modelled, so the interface place has no transition", message_flow)
            tag, name = own_nodes[ref]
            transitions = transitions_by_node.get(ref)
            if not transitions:
                if tag in ("subProcess", "callActivity"):
                    reason = (f"attached to the {tag} {ref!r}, which is replaced by its content and has no single "
                              f"transition; attach the message flow to the sending or receiving node inside it")
                elif not name:
                    reason = f"attached to the unnamed {tag} {ref!r}, whose transition is silent (pm4py labels by name)"
                else:
                    reason = f"attached to the {tag} {ref!r} ({name!r}), which has no visible transition in the net"
                reject(f"message flow ({direction}) {reason}", message_flow)

            place = PetriNet.Place(f"io_{message}_{'out' if outgoing else 'in'}")
            net.places.add(place)
            for transition in transitions:
                if outgoing:
                    petri_utils.add_arc_from_to(transition, place, net)
                else:
                    petri_utils.add_arc_from_to(place, transition, net)
            (O if outgoing else I).add(place)
    return I, O


# ----------------------------------------------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------------------------------------------

def translate_model(model_path):
    # The events are rewritten on the parsed XML (in memory only, the file stays untouched) and the
    # result is re-imported from a string. Replacing the nodes in pm4py's BPMN graph instead does not
    # work: BPMNNode defines __eq__ and __hash__ over the id, so a replacement node with the same id
    # is the same node for the underlying networkx graph and remove_node(old) removes the new one too.
    root = ET.parse(model_path).getroot()
    # pm4py keys its nodes by id (lxml.py:58-454); a second element with the same id silently replaces the first
    seen_ids = set()
    for element in root.iter():
        element_id = element.get("id")
        if element_id is None or local_name(element) is None:
            continue
        if element_id in seen_ids:
            raise ReferenceModelError("id is used by more than one element (BPMN ids are unique per document)",
                                      file=model_path, element_id=element_id, element_type=local_name(element))
        seen_ids.add(element_id)
    main, processes = select_main_process(root, model_path)
    # the pool's own nodes as modelled, for the message flows (which refer to the original ids)
    own_nodes = {node.get("id"): (local_name(node), node.get("name")) for node in main.iter()
                 if node.get("id") and local_name(node) in NODE_CLASSES}

    validate_scope(main, model_path, root, processes, set())
    ids = IdFactory(root)
    expand_call_activities(main, root, processes, model_path)
    for process in list(root.findall(f"{NS}process")):
        if process is not main:
            root.remove(process)  # copied into the call activities; would otherwise be imported as further nodes
    events = read_event_definitions(main)
    origin = {}  # id of a node added by a rewrite -> id of the BPMN node it stands for

    normalize_scope(main, events, ids, model_path)
    rewrite_boundary_events(main, events, ids)
    expand_loops(main, ids, origin, model_path)
    make_events_observable(main, events, ids)
    make_silent_events_passable(main, events)
    rewrite_event_based_gateways(main)
    drop_flow_references(main)
    message_flows = take_message_flows(root)

    try:
        bpmn_graph = bpmn_importer.deserialize(ET.tostring(root))
        net, i_m, f_m = pm4py.convert_to_petri_net(bpmn_graph)
    except Exception as error:
        # e.g. a sequence flow to an element pm4py does not import: its end stays a string (lxml.py:611-617)
        # and BPMN.Flow fails on it (obj.py:734)
        raise ReferenceModelError(f"pm4py could not translate the model: {type(error).__name__}: {error}",
                                  file=model_path) from error
    check_coverage(bpmn_graph, main, net, model_path)
    # checked before the I/O places are added: the local alignments run on the net without them
    check_soundness(net, i_m, f_m, model_path, main.get("id"))
    # the name survives the deepcopy in build_inner_net, net.properties does not (PetriNet.__deepcopy__)
    net.name = Path(model_path).name
    for transition in net.transitions:
        if transition.label is not None and transition.name in events:
            transition.properties["bpmn_event"] = events[transition.name]

    I, O = add_interface_places(root, main, own_nodes, message_flows, net, origin, model_path)
    return net, i_m, f_m, I, O


def process_models(model_dir_path):
    models = {}
    for model_path in model_dir_path.iterdir():
        if model_path.suffix != ".bpmn":
            continue
        models[model_path.stem] = translate_model(model_path)
    return models
