import sys
from pathlib import Path
from pm4py.objects.log.importer.xes import importer as xes_importer
from pm4py.objects.log.exporter.xes import exporter as xes_exporter

from log_preprocessing import preprocess
from merge import mergeLogs


def main():
    if len(sys.argv) < 2:
        print("Path fehlt")
        return

    merged_log_version = 1
    logs = []
    for f in Path(sys.argv[1]).iterdir():
        if f.is_file():
            if f.name.startswith("merged_log"):
                suffix = f.stem.removeprefix("merged_log_v")
                if int(suffix) >= merged_log_version:
                    merged_log_version = int(suffix) + 1
                continue
            else:
                logs.append(xes_importer.apply(str(f)))

    # -- preprocessing
    preprocess(logs)

    # -- merging
    merged_Log = mergeLogs(logs)

    xes_exporter.apply(merged_Log, f"{sys.argv[1]}/merged_log_v{merged_log_version}.xes")
    print("logs merged")

    # -- conformance checking

    # -- results


if __name__ == '__main__':
    main()
