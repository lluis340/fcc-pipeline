from pathlib import Path

import streamlit as st
import pm4py

import pipeline
from evaluation import inject_miscommunication

st.write('## FCC PIPELINE PROTOTYPE')


def start():
    page = st.session_state.get("page", "initial")
    if page == "initial":
        initial_input_page()
    elif page == "invoke":
        invoke_pipeline()
    elif page == "result":
        display_result()
    elif page == "error":
        display_error()


def create_run_dir(process_name):
    base = Path("logs")
    base.mkdir(parents=True, exist_ok=True)
    exec_num = 1
    for f in base.iterdir():
        if f.is_dir() and f.name.startswith(process_name):
            suffix = f.name.removeprefix(process_name)
            if suffix.isdigit() and int(suffix) >= exec_num:
                exec_num = int(suffix) + 1
    run_dir = base / f"{process_name}_{exec_num}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def initial_input_page():
    process_name = st.text_input("Name your Process")
    process_name = process_name.lower().replace(" ", "_")
    st.session_state["process_name"] = process_name
    event_logs = st.file_uploader(
        "Upload XES Event Logs here",
        type=["xes"],
        accept_multiple_files=True,
        on_change=logs_changed
    )
    models = st.file_uploader(
        "Upload organizational reference models here",
        type=["bpmn"],
        accept_multiple_files=True,
        on_change=models_changed
    )

    if models and len(models) != len(event_logs):
        st.warning("If you have organizational reference models, "
                   "please provide one model for each organizational event log",
                   icon="⚠️")

    if not st.button("Upload logs") and not st.session_state.get("files_uploaded"):
        st.stop()
    if not process_name:
        st.error("Please name your process",
                 icon=":material/priority_high:")
        st.stop()
    missing_models = logs_without_model(event_logs, models)
    if missing_models:
        st.error("Every event log needs a reference model with the same name "
                 f"(e.g. PartyA.xes and PartyA.bpmn). Missing models for: {', '.join(missing_models)}. "
                 "Please adjust the uploaded files and press \"Upload logs\" again.",
                 icon=":material/priority_high:")
        st.session_state["files_uploaded"] = False
        st.stop()

    if st.session_state.get("files_changed"):
        if "run_dir" not in st.session_state:
            st.session_state["run_dir"] = create_run_dir(process_name)
        run_dir = st.session_state["run_dir"]
        logs = []
        for event_log in event_logs:
            log_path = run_dir / event_log.name
            log_path.write_bytes(event_log.read())
            logs.append(pm4py.read_xes(str(log_path), return_legacy_log_object=True))
        if logs:
            st.session_state["logs"] = logs
        st.session_state["files_changed"] = False
        st.session_state["files_uploaded"] = True

    if st.session_state.get("models_changed"):
        if "run_dir" not in st.session_state:
            st.session_state["run_dir"] = create_run_dir(process_name)
        run_dir = st.session_state["run_dir"]
        (run_dir / "models").mkdir(exist_ok=True)
        for model in models:
            model_path = run_dir / "models" / model.name
            model_path.write_bytes(model.read())
        st.session_state["models_changed"] = False

    st.write("Please specify the attribute names for the following attributes: ")
    attributes = {"Log Identifier": ("log_id", "concept:name"),
                  "Trace Identifier": ("trace_id", "concept:name"),
                  "Event Identifier": ("event_id", "concept:name"),
                  "Organisation": ("org_group", "org:group"),
                  "Timestamp": ("timestamp", "time:timestamp"),
                  "Message Instance Id": ("msgInstanceId", ""),
                  "Message Channel": ("msgFlow", ""),
                  "Message Direction": ("msgType", "")}

    for attribute, (key, value) in attributes.items():
        st.text_input(attribute, key=key, value=value)

    st.session_state["inject_errors"] = st.checkbox(label="Use Logs as well as Dirty Logs with injected errors")
    if st.session_state["inject_errors"]:
        seed_input = st.text_input("Seed (optional)")
        try:
            st.session_state["seed"] = int(seed_input) if seed_input.strip() else None
        except ValueError:
            st.session_state["seed"] = None
    if not st.button("START PIPELINE"):
        st.stop()
    st.session_state["page"] = "invoke"
    st.rerun()


def logs_without_model(event_logs, models):
    if not models:
        return []
    model_names = {Path(model.name).stem for model in models}
    return sorted(event_log.name for event_log in event_logs if Path(event_log.name).stem not in model_names)


def logs_changed():
    st.session_state["files_changed"] = True


def models_changed():
    st.session_state["models_changed"] = True


def invoke_pipeline():
    attribute_kwargs = dict(
        log_id=st.session_state.get("log_id"),
        trace_id=st.session_state.get("trace_id"),
        event_id=st.session_state.get("event_id"),
        org_group=st.session_state.get("org_group"),
        timestamp=st.session_state.get("timestamp"),
        msg_instance_id=st.session_state.get("msgInstanceId"),
        msg_type=st.session_state.get("msgFlow"),
        communication_mode=st.session_state.get("msgType"),
    )

    if st.session_state["inject_errors"]:
        run_dir = st.session_state["run_dir"]
        paths = inject_miscommunication.collect_logs(run_dir)
        injector = inject_miscommunication.NoiseInjector(
            paths,
            inject_miscommunication.Keys(),
            inject_miscommunication.InjectionConfig(seed=st.session_state["seed"]),
        )
        injector.load()
        injector.build_index()
        injector.run()
        injector.write(run_dir)

    try:
        result = pipeline.execute(str(st.session_state["run_dir"]), **attribute_kwargs)
    except Exception as e:
        st.session_state["error"] = f"{type(e).__name__}: {e}"
        st.session_state["page"] = "error"
        st.rerun()
    st.session_state["result"] = result
    st.session_state["page"] = "result"
    st.rerun()


def display_error():
    st.write("## An Error has occurred! ")
    st.error(st.session_state.get("error"), icon=":material/priority_high:")
    if st.button("Start again from begin"):
        st.session_state.clear()
        st.rerun()


def display_result():
    result = st.session_state["result"]
    if isinstance(result, str):
        st.write(result)
    else:
        st.write("# RESULTS")
        st.image(str(st.session_state["run_dir"] / "output" / "cPN_v1.svg"))
        st.write(result.get("vis_result"))
        if result.get("vis_private_result"):
            st.write(result.get("vis_private_result"))
        if result.get("dirty_vis_result"):
            st.write(result.get("dirty_vis_result"))
            if result.get("dirty_vis_private_result"):
                st.write(result.get("dirty_vis_private_result"))

    if st.button("GO BACK TO START"):
        st.session_state.clear()
        st.rerun()


if __name__ == '__main__':
    start()
