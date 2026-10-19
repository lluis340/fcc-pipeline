from copy import deepcopy

from lxml import etree

BPMN_URI = "http://www.omg.org/spec/BPMN/20100524/MODEL"
NS = f"{{{BPMN_URI}}}"


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

        org_bpmn = etree.Element('definitions', nsmap={None: BPMN_URI})
        org_bpmn.set('targetNamespace', f'http://fcc-pipeline.test/bpmn_{name}')

        collab = etree.SubElement(org_bpmn, f'{NS}collaboration',
                                  id=f'collab_{participant.get("id")}')
        etree.SubElement(collab, f'{NS}participant',
                         id=participant.get('id'), name=name, processRef=process_ref)
        own_ids = {elem.get('id') for elem in process.iter(etree.Element) if elem.get('id')}
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
