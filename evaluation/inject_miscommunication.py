#!/usr/bin/env python3
"""

GENERATED ON 22/08/2026 USING CLAUDE OPUS 5
LAST REVISED ON 05/10/2026 USING CLAUDE OPUS 5

inject_miscommunication.py
==========================

Injects controlled miscommunication patterns into n organizational XES event
logs in order to produce deviating logs for evaluating conformance checking
against a discovered collaboration Petri net (cPN).

Only events that participate in a cross-organizational message exchange are
manipulated, i.e. events whose msgInstanceId occurs in at least two logs.

Injected patterns
-----------------
  sender_move          The RECEIVE side of a message exchange is removed, the
                       send side remains: the message was produced but never
                       consumed. The surviving send event becomes a lone event
                       and is reported by its own organization.
  receiver_move        The SEND side of a message exchange is removed, the
                       receive side remains: the message was consumed although
                       it was never produced.
  async_communication  The timestamps of a correctly correlated send/receive
                       pair are inverted ACROSS two logs, so that the message is
                       consumed before it is produced. Only applied if the local
                       event order of the shifted trace stays intact, so no
                       alignment deviation results.
  swap                 Two COMPLETE communication points exchange their order.
                       The two message instances must occur in exactly the same
                       set of traces and run in the same direction, i.e. have
                       their send side in the same organization; every event of
                       instance A is then swapped with the corresponding event
                       of instance B in each trace, exchanging position AND
                       timestamp.
                       Both instances stay correctly correlated and each channel
                       is still used correctly in isolation; only the order
                       BETWEEN the two channels is wrong. The only pattern that
                       requires a model, since without a prescribed order
                       between the channels there is nothing to violate.
  unclassified         An existing message event is duplicated inside its own
                       trace, so that its msgInstanceId is carried by three
                       events. The identifier then permits neither grouping of
                       classify_miscommunication_type (exactly one event = lone,
                       exactly two events = correlated pair), so every move on
                       it falls into the unknown category. No miscommunication
                       of its own; it measures how far the correlation covers
                       the logs.

Expected report entries per injection
-------------------------------------
  sender_move          1 (SYNC move of the surviving send event)
  receiver_move        1 (SYNC move of the surviving receive event)
  async_communication  2 (both sides violate the order, one per organization)
  swap                 4 per affected trace: both displaced activities carry a
                       log move and a model move. 0 for a trace where the two
                       events sit closer than --swap-min-distance, since the
                       move pair may then land on either of them
  unclassified         3 (all three events carrying the identifier, across two
                       organizations)
The organization whose event was deleted by a sender_move or receiver_move
additionally carries a model move that is not categorized on the correlation
level; it affects fitness but appears in no correlation category.

Swap applicability
------------------
A swap is a deviation only against a model that prescribes an order between the
two channels. The Inductive Miner at noise_threshold 0 guarantees perfect
fitness, so if an organization's clean log shows both orders of the two
activities, the discovered model must permit both and the swap is not a
deviation against it. The injector therefore counts both orders over the clean
activity sequences of each affected organization and only injects a swap where
at least one organization logs them deterministically. The counts are written
to the ground truth per affected trace as `order_in_clean_log`, together with
`detectable`. The condition is necessary and, for the cut structure the
Inductive Miner builds, normally sufficient; it is not provably sufficient,
since loop structures or a higher-level cut can still leave the order unbound.
`--swap-allow-any-order` disables the check, to demonstrate that limit rather
than avoid it.

Swap distance
-------------
An adjacent transposition costs the same alignment whichever of the two
activities is held responsible, so the resulting move pair may land on either
message instance and the second one stays invisible. A swap is therefore only
injected if the two events are at least `--swap-min-distance` positions apart
in at least one affected trace; the per-trace distances are written to the
ground truth, so traces where only one of the two instances can be recognized
stay identifiable.

Invariants
----------
  * No event attribute is ever modified. The script deletes whole events,
    reorders them, exchanges timestamps between them, and inserts verbatim
    copies of existing events. msgInstanceId is therefore never edited and
    never newly assigned: the copy inherits the identifier of its original.
  * No event is ever stripped of an attribute, so no injection can be mistaken
    for a data quality defect that preprocessing is meant to repair.
  * Every trace stays sorted by timestamp, so no reordering can be undone by a
    timestamp-based sort further down the pipeline.
  * A pair of traces is never fully decoupled: an event is only deleted if the
    pair remains connected through at least one further msgInstanceId.
  * Every event takes part in at most one injection, so each entry of the
    ground truth stays attributable to exactly one pattern.
  * Every active pattern is injected at least once, where applicable (coverage
    phase before the random phase). If a pattern is not applicable to the given
    logs at all, it is silently skipped -- the run still proceeds and writes
    whatever patterns ARE applicable, without erroring out.
    To reach coverage, sender_move and receiver_move may -- only as a last
    resort -- remove the last link between two traces (flagged as
    `orphaned_pair`).

Detection levels
----------------
sender_move, receiver_move, async_communication and unclassified are properties
of the message correlation, not of the control flow of any single organization,
and are detected on the model-independent correlation level.
async_communication is by construction NOT detectable through the alignment
when conformance checking runs per organization against a tau-relabelled
projection of the cPN: the event order within each individual organization
stays intact. The pattern is included so that this detection limit can be
demonstrated empirically rather than merely asserted.
swap leaves both message instances correctly correlated, so the correlation
level is blind to it and the alignment is the only source that can report it.

Pipeline note
-------------
The generated logs are meant as input FOR the conformance checking stage, not
for the preprocessing stage. sender_move and receiver_move produce exactly the
single sided message instances that lost message handling is built to repair or
to exclude, so running preprocessing with active lost message handling on these
logs removes the injected deviations before conformance checking ever sees
them. Either disable lost message handling for the dirty run, or inject after
preprocessing.

Example
-------
    python inject_miscommunication.py logs/ \
        --level 0.15 \
        --unit trace \
        --types sender_move,receiver_move,async_communication,swap,unclassified \
        --weights 1,1,1,1,1 \
        --seed 42

Requires: pm4py
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pm4py
from pm4py.objects.log.obj import Event, EventLog, Trace

# Project-wide attribute names -- single source of truth.
# Adjust the import path to the project layout if needed (e.g. src.config).
import config


# ---------------------------------------------------------------------------
# Attribute keys
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Keys:
    """Attribute names used in the XES logs.

    The single source of truth is `config.ATTRIBUTES`. This class is only an
    adapter, so that the code can access keys through descriptive field names.

    Defaults are read via default_factory, so a `config.set_attributes(...)`
    call made before `Keys()` is instantiated is picked up -- unlike a plain
    `= config.ATTRIBUTES.x` default, which would freeze the value at the time
    this module is first imported.
    """
    log_id: str = field(default_factory=lambda: config.ATTRIBUTES.log_id)
    trace_id: str = field(default_factory=lambda: config.ATTRIBUTES.trace_id)
    event_id: str = field(default_factory=lambda: config.ATTRIBUTES.event_id)
    org_group: str = field(default_factory=lambda: config.ATTRIBUTES.org)
    communication_mode: str = field(default_factory=lambda: config.ATTRIBUTES.comm_mode)
    msg_type: str = field(default_factory=lambda: config.ATTRIBUTES.msg_type)
    msg_instance_id: str = field(default_factory=lambda: config.ATTRIBUTES.msg_instance_id)
    timestamp: str = field(default_factory=lambda: config.ATTRIBUTES.timestamp)

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"Keys.{f.name} must be a non-empty string, got {value!r}."
                )


ERROR_TYPES: tuple[str, ...] = (
    "sender_move",
    "receiver_move",
    "async_communication",
    "swap",
    "unclassified",
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class EventRef:
    """Reference to a manipulable message event."""
    log_idx: int
    trace_idx: int
    event: Event
    mid: str
    org: str
    mode: str


@dataclass
class LoadedLog:
    path: Path
    log_id: str
    attributes: dict
    extensions: dict
    classifiers: dict
    omni_present: dict
    trace_attrs: list[dict] = field(default_factory=list)
    traces: list[list[Event]] = field(default_factory=list)


@dataclass
class InjectionConfig:
    level: float = 0.1
    unit: str = "trace"                 # "trace" | "event"
    errors_per_trace: int = 1
    types: tuple[str, ...] = ERROR_TYPES
    weights: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0, 1.0)
    seed: int | None = 42

    # Attribute VALUES (not keys) identifying the two sides of an exchange.
    mode_send: str = "send"
    mode_receive: str = "receive"

    # Offset applied when inverting a send/receive pair.
    async_shift_seconds: float = 1.0

    # Minimum distance between the two swapped events, in at least one of the
    # affected traces.
    swap_min_distance: int = 2

    # Only swap two communication points whose order is deterministic in at
    # least one affected organization's log.
    swap_require_deterministic_order: bool = True


# ---------------------------------------------------------------------------
# Injector
# ---------------------------------------------------------------------------

class NoiseInjector:

    def __init__(self, paths: list[Path], keys: Keys, cfg: InjectionConfig) -> None:
        self.paths = paths
        self.k = keys
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)

        self.logs: list[LoadedLog] = []
        self.mid_index: dict[str, list[EventRef]] = defaultdict(list)
        self.pair_links: dict[frozenset, set[str]] = defaultdict(set)
        self.eligible: list[EventRef] = []
        self.eligible_ids: set[int] = set()
        self.deleted: set[int] = set()
        self.touched: set[int] = set()   # events already part of an injection
        self.ground_truth: list[dict[str, Any]] = []
        self.skipped: dict[str, int] = defaultdict(int)
        self.uncovered: list[str] = []
        self.activity_order: list[list[list[str]]] = []
        self._order_cache: dict[tuple, dict[str, Any]] = {}

    # -- Loading -------------------------------------------------------------

    def load(self) -> None:
        for path in self.paths:
            raw = pm4py.read_xes(str(path), return_legacy_log_object=True)
            loaded = LoadedLog(
                path=path,
                log_id="",
                attributes=dict(getattr(raw, "attributes", {}) or {}),
                extensions=dict(getattr(raw, "extensions", {}) or {}),
                classifiers=dict(getattr(raw, "classifiers", {}) or {}),
                omni_present=dict(getattr(raw, "omni_present", {}) or {}),
            )
            for trace in raw:
                events = [Event(dict(ev)) for ev in trace]
                loaded.trace_attrs.append(dict(trace.attributes))
                loaded.traces.append(events)

            all_events = [e for evs in loaded.traces for e in evs]
            if not all_events:
                raise ValueError(f"{path.name}: log contains no events.")
            self._validate_schema(path, all_events)

            # Read at log level, exactly as check_conformance.py does. At event
            # level the same key carries a different value in these logs.
            log_id_value = loaded.attributes.get(self.k.log_id)
            loaded.log_id = str(log_id_value) if log_id_value is not None else path.stem
            self.logs.append(loaded)

    # Attributes every event must carry for the script to work.
    # msg_instance_id is deliberately absent: purely internal events have none.
    _REQUIRED_EVENT_KEYS = ("event_id", "timestamp", "org_group")

    def _validate_schema(self, path: Path, events: list[Event]) -> None:
        """Verify that the events carry the expected attribute keys."""
        sample = events[0]
        missing = [
            getattr(self.k, name)
            for name in self._REQUIRED_EVENT_KEYS
            if getattr(self.k, name) not in sample
        ]
        if missing:
            raise KeyError(
                f"{path.name}: events do not carry the expected attributes: "
                f"{', '.join(missing)}. Present: {', '.join(sorted(sample))}. "
                f"Check config.ATTRIBUTES and the writing module."
            )

        if self.k.msg_instance_id not in sample:
            has_mid = any(self.k.msg_instance_id in e for e in events)
            if not has_mid:
                raise KeyError(
                    f"{path.name}: no event carries {self.k.msg_instance_id}. "
                    f"Without this key no message exchange can be identified."
                )

    # -- Index ---------------------------------------------------------------

    def build_index(self) -> None:
        # Snapshot of the activity sequences before any injection, so the order
        # statistics describe the clean log the cPN was discovered from.
        self.activity_order = [
            [[str(ev.get(self.k.event_id)) for ev in events] for events in log.traces]
            for log in self.logs
        ]

        for li, log in enumerate(self.logs):
            for ti, events in enumerate(log.traces):
                for ev in events:
                    mid = ev.get(self.k.msg_instance_id)
                    if mid in (None, ""):
                        continue
                    self.mid_index[str(mid)].append(EventRef(
                        log_idx=li,
                        trace_idx=ti,
                        event=ev,
                        mid=str(mid),
                        org=str(ev.get(self.k.org_group, "")),
                        mode=str(ev.get(self.k.communication_mode, "")),
                    ))

        for mid, refs in self.mid_index.items():
            log_idxs = {r.log_idx for r in refs}
            if len(log_idxs) < 2:
                continue  # not a cross-organizational exchange
            for r in refs:
                self.eligible.append(r)
                self.eligible_ids.add(id(r.event))
            trace_keys = {(r.log_idx, r.trace_idx) for r in refs}
            for a in trace_keys:
                for b in trace_keys:
                    if a < b:
                        self.pair_links[frozenset((a, b))].add(mid)

    def _alive(self, mid: str) -> list[EventRef]:
        return [r for r in self.mid_index[mid] if id(r.event) not in self.deleted]

    def _order_counts(self, log_idx: int, activity_a: str,
                      activity_b: str) -> dict[str, Any]:
        """Count, in one organization's clean log, how many traces show each of
        the two orders of the given activities.

        The Inductive Miner at noise_threshold 0 guarantees perfect fitness, so
        if both orders occur the discovered model must permit both and a swap of
        these two communication points is not a deviation against it. A
        one-sided count is therefore a necessary condition for the swap to be
        detectable, and it is the condition that rules out the decisive case.
        """
        key = (log_idx, activity_a, activity_b)
        cached = self._order_cache.get(key)
        if cached is not None:
            return cached

        a_first = 0
        b_first = 0
        for sequence in self.activity_order[log_idx]:
            if activity_a not in sequence or activity_b not in sequence:
                continue
            if sequence.index(activity_a) < sequence.index(activity_b):
                a_first += 1
            else:
                b_first += 1

        result = {
            "a_before_b": a_first,
            "b_before_a": b_first,
            "deterministic": bool(a_first) != bool(b_first),
        }
        self._order_cache[key] = result
        return result

    # -- Selection -----------------------------------------------------------

    def _select_anchors(self) -> list[EventRef]:
        if not self.eligible:
            return []

        if self.cfg.unit == "event":
            n = max(1, round(self.cfg.level * len(self.eligible)))
            n = min(n, len(self.eligible))
            return self.rng.sample(self.eligible, n)

        by_trace: dict[tuple[int, int], list[EventRef]] = defaultdict(list)
        for r in self.eligible:
            by_trace[(r.log_idx, r.trace_idx)].append(r)

        trace_keys = sorted(by_trace)
        n = max(1, round(self.cfg.level * len(trace_keys)))
        n = min(n, len(trace_keys))
        chosen = self.rng.sample(trace_keys, n)

        anchors: list[EventRef] = []
        for tk in chosen:
            pool = by_trace[tk]
            take = min(self.cfg.errors_per_trace, len(pool))
            anchors.extend(self.rng.sample(pool, take))
        return anchors

    # -- Main run ------------------------------------------------------------

    # Coverage order: most restrictive patterns first, so they get the
    # largest pool of still unused events.
    _COVERAGE_ORDER = ("swap", "async_communication", "sender_move",
                       "receiver_move", "unclassified")

    def run(self) -> None:
        handlers = {
            "sender_move": self._inject_sender_move,
            "receiver_move": self._inject_receiver_move,
            "async_communication": self._inject_async_communication,
            "swap": self._inject_swap,
            "unclassified": self._inject_unclassified,
        }

        # Phase 1: guarantee at least one injection per active pattern. Patterns that
        # aren't applicable to this log at all land in self.uncovered and are simply
        # skipped from here on -- no abort, whatever IS applicable still gets injected.
        self._ensure_coverage(handlers)

        # Phase 2: random injections according to --level.
        anchors = self._select_anchors()
        for anchor in anchors:
            if self._is_used(anchor.event):
                continue
            for etype in self._weighted_order():
                if etype in self.uncovered:
                    continue
                record = handlers[etype](anchor)
                if record is not None:
                    self.ground_truth.append(record)
                    break
                self.skipped[etype] += 1

    _ORPHANING_PATTERNS = ("sender_move", "receiver_move")

    def _ensure_coverage(self, handlers: dict) -> None:
        """Inject every active pattern once, searching all eligible events.

        sender_move and receiver_move first try without decoupling a trace
        pair. Only if no such event exists anywhere, one deletion that removes
        the last link between two traces is allowed; it is flagged as
        `orphaned_pair` in the ground truth.
        """
        for etype in (t for t in self._COVERAGE_ORDER if t in self.cfg.types):
            attempts: list[dict] = [{}]
            if etype in self._ORPHANING_PATTERNS:
                attempts.append({"allow_orphaning": True})

            pool = list(self.eligible)
            self.rng.shuffle(pool)

            record = None
            for kwargs in attempts:
                for anchor in pool:
                    if self._is_used(anchor.event):
                        continue
                    record = handlers[etype](anchor, **kwargs)
                    if record is not None:
                        break
                if record is not None:
                    break

            if record is None:
                self.uncovered.append(etype)
            else:
                record["coverage_phase"] = True
                self.ground_truth.append(record)

    def diagnose(self, etype: str) -> str:
        """Explain why a pattern found no applicable event."""
        alive = [r for r in self.eligible if not self._is_used(r.event)]
        if etype == "swap":
            per_trace: dict[tuple[int, int], set] = defaultdict(set)
            for r in alive:
                per_trace[(r.log_idx, r.trace_idx)].add(r.mid)
            n = sum(1 for mids in per_trace.values() if len(mids) >= 2)
            return (f"swap needs two correlated message instances occurring in "
                    f"exactly the same set of traces, running in the same "
                    f"direction, with a distance of >= "
                    f"{self.cfg.swap_min_distance} positions in at least one of "
                    f"them, and a deterministic order of their activities in at "
                    f"least one organization's clean log; found {n} trace(s) "
                    f"with >= 2 distinct message instances. If the activity "
                    f"order varies everywhere, no swap can be a deviation "
                    f"against a model discovered from these logs.")
        if etype in ("sender_move", "receiver_move"):
            side = "receive" if etype == "sender_move" else "send"
            return (f"{etype} needs a message with exactly one "
                    f"'{self.cfg.mode_send}' and one '{self.cfg.mode_receive}' "
                    f"event in different logs, where the trace holding the "
                    f"{side} side has > 1 event; none found. Check "
                    f"--mode-send/--mode-receive.")
        if etype == "async_communication":
            return (f"async_communication needs exactly one "
                    f"'{self.cfg.mode_send}' and one '{self.cfg.mode_receive}' "
                    f"event per message in different logs, and a shift that "
                    f"keeps the local order. Check --mode-send/--mode-receive.")
        if etype == "unclassified":
            n = len({r.mid for r in alive if len(self._alive(r.mid)) == 2})
            return (f"unclassified needs a message instance carried by exactly "
                    f"two still unused events in two different logs; found "
                    f"{n} such instance(s).")
        return f"{etype}: no applicable event found."

    def _is_used(self, ev: Event) -> bool:
        return id(ev) in self.deleted or id(ev) in self.touched

    def _weighted_order(self) -> list[str]:
        """Permute the error types by weight, so that a type which turns out to
        be inapplicable deterministically falls through to the next one."""
        remaining = list(zip(self.cfg.types, self.cfg.weights))
        order: list[str] = []
        while remaining:
            total = sum(w for _, w in remaining) or 1.0
            pick = self.rng.uniform(0, total)
            acc = 0.0
            for i, (t, w) in enumerate(remaining):
                acc += w
                if pick <= acc:
                    order.append(t)
                    remaining.pop(i)
                    break
            else:
                order.append(remaining.pop()[0])
        return order

    # -- Pattern 1+2: sender move / receiver move ----------------------------

    def _inject_sender_move(self, anchor: EventRef,
                            allow_orphaning: bool = False) -> dict | None:
        """Remove the receive side: the message was produced but never consumed."""
        return self._remove_side(anchor, "sender_move", allow_orphaning)

    def _inject_receiver_move(self, anchor: EventRef,
                              allow_orphaning: bool = False) -> dict | None:
        """Remove the send side: the message was consumed but never produced."""
        return self._remove_side(anchor, "receiver_move", allow_orphaning)

    def _remove_side(self, anchor: EventRef, etype: str,
                     allow_orphaning: bool) -> dict | None:
        send_ref, recv_ref = self._resolve_pair(anchor)
        if send_ref is None or recv_ref is None:
            return None
        if self._is_used(send_ref.event) or self._is_used(recv_ref.event):
            return None

        if etype == "sender_move":
            target, remaining = recv_ref, send_ref
        else:
            target, remaining = send_ref, recv_ref

        events = self.logs[target.log_idx].traces[target.trace_idx]
        if len(events) <= 1:
            return None
        i = self._pos(events, target.event)
        if i is None:
            return None

        tk = (target.log_idx, target.trace_idx)
        counterparts = {(r.log_idx, r.trace_idx) for r in self._alive(anchor.mid)} - {tk}
        orphaned_pair = any(
            len(self.pair_links[frozenset((tk, other))]) <= 1
            for other in counterparts
        )
        if orphaned_pair and not allow_orphaning:
            return None

        removed = events.pop(i)
        self.deleted.add(id(removed))
        self.touched.add(id(remaining.event))
        for other in counterparts:
            self.pair_links[frozenset((tk, other))].discard(anchor.mid)

        return self._gt(
            target, etype,
            details={
                "removed_activity": str(removed.get(self.k.event_id)),
                "removed_mode": target.mode,
                "removed_position": i,
                "removed_logId": self.logs[target.log_idx].log_id,
                "removed_org_group": target.org,
                "remaining_activity": str(remaining.event.get(self.k.event_id)),
                "remaining_mode": remaining.mode,
                "remaining_logId": self.logs[remaining.log_idx].log_id,
                "remaining_org_group": remaining.org,
                "remaining_trace_index": remaining.trace_idx,
                "msgType": anchor.event.get(self.k.msg_type),
                "orphaned_pair": orphaned_pair,
            },
        )

    # -- Pattern 3: asynchronous communication (cross-log) -------------------

    def _inject_async_communication(self, anchor: EventRef) -> dict | None:
        """Invert a send/receive pair across two logs.

        Two placements are attempted, in this order:
          (A) move the send event behind the receive event
          (B) move the receive event before the send event
        The first one that keeps the local event order of its own trace is
        applied. If neither does, the pattern is not applicable here.
        """
        send_ref, recv_ref = self._resolve_pair(anchor)
        if send_ref is None or recv_ref is None:
            return None
        if self._is_used(send_ref.event) or self._is_used(recv_ref.event):
            return None

        ts_send = send_ref.event.get(self.k.timestamp)
        ts_recv = recv_ref.event.get(self.k.timestamp)
        if not isinstance(ts_send, datetime) or not isinstance(ts_recv, datetime):
            return None
        if ts_send >= ts_recv:
            return None  # already inverted, nothing to inject

        delta = timedelta(seconds=self.cfg.async_shift_seconds)

        target_ref, new_ts = send_ref, ts_recv + delta                 # (A)
        if not self._fits_local_order(target_ref, new_ts):
            target_ref, new_ts = recv_ref, ts_send - delta             # (B)
            if not self._fits_local_order(target_ref, new_ts):
                return None

        old_ts = target_ref.event.get(self.k.timestamp)
        target_ref.event[self.k.timestamp] = new_ts
        self.touched.update((id(send_ref.event), id(recv_ref.event)))

        shifted_side = "send" if target_ref is send_ref else "receive"
        return self._gt(
            anchor, "async_communication",
            details={
                "shifted_side": shifted_side,
                "shifted_activity": str(target_ref.event.get(self.k.event_id)),
                "shifted_logId": self.logs[target_ref.log_idx].log_id,
                "shifted_org_group": target_ref.org,
                "timestamp_before": old_ts,
                "timestamp_after": new_ts,
                "send_activity": str(send_ref.event.get(self.k.event_id)),
                "receive_activity": str(recv_ref.event.get(self.k.event_id)),
                "send_logId": self.logs[send_ref.log_idx].log_id,
                "receive_logId": self.logs[recv_ref.log_idx].log_id,
                "msgType": anchor.event.get(self.k.msg_type),
            },
        )

    def _resolve_pair(self, anchor: EventRef) -> tuple[EventRef | None, EventRef | None]:
        """Return the send and receive side of the anchor's message.

        Requires exactly one send and one receive event (by communicationMode)
        in two different logs; otherwise the pattern is not applicable.
        """
        alive = self._alive(anchor.mid)
        sends = [r for r in alive if r.mode == self.cfg.mode_send]
        recvs = [r for r in alive if r.mode == self.cfg.mode_receive]
        if len(sends) != 1 or len(recvs) != 1:
            return None, None
        if sends[0].log_idx == recvs[0].log_idx:
            return None, None  # both sides in one log: not cross-org
        return sends[0], recvs[0]

    def _fits_local_order(self, ref: EventRef, new_ts: datetime) -> bool:
        """True if new_ts keeps the event between its trace neighbours."""
        events = self.logs[ref.log_idx].traces[ref.trace_idx]
        i = self._pos(events, ref.event)
        if i is None:
            return False
        prev_ts = events[i - 1].get(self.k.timestamp) if i > 0 else None
        next_ts = events[i + 1].get(self.k.timestamp) if i + 1 < len(events) else None
        if isinstance(prev_ts, datetime) and new_ts <= prev_ts:
            return False
        if isinstance(next_ts, datetime) and new_ts >= next_ts:
            return False
        return True

    # -- Pattern 4: swap of two complete communication points ----------------

    def _inject_swap(self, anchor: EventRef) -> dict | None:
        """Exchange the order of two whole communication points.

        Both message instances keep every one of their events and stay
        correlated; in each trace holding events of both, the two events
        exchange position and timestamp. Applied to all such traces at once, so
        the swap is consistent across organizations and each channel on its own
        is still used correctly.
        """
        found = self._find_swap_partner(anchor)
        if found is None:
            return None
        partner_mid, pairs = found

        affected: list[dict[str, Any]] = []
        anchor_activity_a = ""
        anchor_activity_b = ""

        for tk, ref_a, ref_b, i, j, order in pairs:
            log = self.logs[tk[0]]
            events = log.traces[tk[1]]
            a, b = ref_a.event, ref_b.event
            ts_a = a.get(self.k.timestamp)
            ts_b = b.get(self.k.timestamp)
            a[self.k.timestamp], b[self.k.timestamp] = ts_b, ts_a
            events[i], events[j] = events[j], events[i]
            self.touched.update((id(a), id(b)))

            activity_a = str(a.get(self.k.event_id))
            activity_b = str(b.get(self.k.event_id))
            if tk == (anchor.log_idx, anchor.trace_idx):
                anchor_activity_a = activity_a
                anchor_activity_b = activity_b

            affected.append({
                "logId": log.log_id,
                "source_file": log.path.name,
                "trace_index": tk[1],
                "traceId": log.trace_attrs[tk[1]].get(self.k.trace_id),
                "org_group": ref_a.org,
                "activity_a": activity_a,
                "activity_b": activity_b,
                "mode": ref_a.mode,
                "position_a": i,
                "position_b": j,
                "distance": abs(j - i),
                "timestamp_a_before": ts_a,
                "timestamp_b_before": ts_b,
                "order_in_clean_log": order,
                "detectable": order["deterministic"],
            })

        if not anchor_activity_a:
            anchor_activity_a = affected[0]["activity_a"]
            anchor_activity_b = affected[0]["activity_b"]

        return self._gt(
            anchor, "swap",
            details={
                "msgInstanceId_a": anchor.mid,
                "msgInstanceId_b": partner_mid,
                "msgType_a": anchor.event.get(self.k.msg_type),
                "msgType_b": self._mid_msg_type(partner_mid),
                "activity_a": anchor_activity_a,
                "activity_b": anchor_activity_b,
                "n_affected_traces": len(affected),
                "max_distance": max(t["distance"] for t in affected),
                "n_detectable_traces": sum(1 for t in affected if t["detectable"]),
                "affected_traces": affected,
            },
        )

    def _find_swap_partner(self, anchor: EventRef):
        """Pick a second communication point that can be swapped with the
        anchor's one, preferring the temporally closest candidate."""
        if len(self._alive(anchor.mid)) != 2:
            return None

        events = self.logs[anchor.log_idx].traces[anchor.trace_idx]
        i = self._pos(events, anchor.event)
        if i is None:
            return None

        candidates: list[tuple[int, int, str]] = []
        seen: set[str] = {anchor.mid}
        for j, e in enumerate(events):
            if j == i or id(e) not in self.eligible_ids or self._is_used(e):
                continue
            mid = e.get(self.k.msg_instance_id)
            if mid in (None, ""):
                continue
            mid = str(mid)
            if mid in seen or len(self._alive(mid)) != 2:
                continue
            seen.add(mid)
            candidates.append((abs(j - i), j, mid))

        for _, _, mid in sorted(candidates):
            pairs = self._swap_pairs(anchor.mid, mid)
            if pairs:
                return mid, pairs
        return None

    def _swap_pairs(self, mid_a: str, mid_b: str):
        """Resolve the per-trace event pairs for swapping two message instances.

        Returns an empty list unless the two instances occur in exactly the
        same set of traces, each trace holds exactly one unused event of each,
        the two events of a trace carry the same communicationMode, all
        timestamps are usable and distinct, at least one trace separates the
        two events by the configured minimum distance, and -- unless disabled
        -- at least one affected organization logs the two activities in a
        deterministic order, so that the discovered model can prescribe an
        order between the two channels at all.

        Two conditions keep each channel correctly used, and both are about the
        timestamps that are exchanged. The identical trace sets make every
        organization holding one of the instances take part in the swap, so no
        side is shifted against an unshifted counterpart. The equal
        communicationMode makes the two channels run in the same direction, so
        a send time is only ever exchanged with another send time; with the
        roles crossed, one instance would receive the other's receive time as
        its send time and the pair would be inverted by construction. Either
        violation turns the swap into an asynchronous communication.

        Together the two conditions are sufficient. Each instance ends up
        holding the send and the receive time of the other instance, i.e. a
        pair that was valid in the clean log, so send-before-receive survives
        the swap whatever the relative order of the two channels was. The two
        channels may therefore cross, occurring in one order in the sending
        organization and in the opposite order in the receiving one.
        """
        by_trace_a: dict[tuple[int, int], list[EventRef]] = defaultdict(list)
        by_trace_b: dict[tuple[int, int], list[EventRef]] = defaultdict(list)
        for r in self._alive(mid_a):
            by_trace_a[(r.log_idx, r.trace_idx)].append(r)
        for r in self._alive(mid_b):
            by_trace_b[(r.log_idx, r.trace_idx)].append(r)

        if set(by_trace_a) != set(by_trace_b):
            return []
        shared = sorted(by_trace_a)
        if not shared:
            return []

        pairs = []
        for tk in shared:
            refs_a = by_trace_a[tk]
            refs_b = by_trace_b[tk]
            if len(refs_a) != 1 or len(refs_b) != 1:
                return []
            ref_a, ref_b = refs_a[0], refs_b[0]
            if self._is_used(ref_a.event) or self._is_used(ref_b.event):
                return []
            if ref_a.mode != ref_b.mode:
                return []

            ts_a = ref_a.event.get(self.k.timestamp)
            ts_b = ref_b.event.get(self.k.timestamp)
            if not isinstance(ts_a, datetime) or not isinstance(ts_b, datetime):
                return []
            if ts_a == ts_b:
                return []

            events = self.logs[tk[0]].traces[tk[1]]
            i = self._pos(events, ref_a.event)
            j = self._pos(events, ref_b.event)
            if i is None or j is None or i == j:
                return []

            order = self._order_counts(
                tk[0],
                str(ref_a.event.get(self.k.event_id)),
                str(ref_b.event.get(self.k.event_id)),
            )
            pairs.append((tk, ref_a, ref_b, i, j, order))

        detectable = [
            p for p in pairs
            if abs(p[4] - p[3]) >= self.cfg.swap_min_distance
            and (p[5]["deterministic"]
                 or not self.cfg.swap_require_deterministic_order)
        ]
        if not detectable:
            return []
        return pairs

    def _mid_msg_type(self, mid: str):
        for r in self.mid_index[mid]:
            value = r.event.get(self.k.msg_type)
            if value is not None:
                return value
        return None

    # -- Pattern 5: unclassifiable identifier --------------------------------

    def _inject_unclassified(self, anchor: EventRef) -> dict | None:
        """Duplicate a message event so its identifier carries three events.

        The identifier then fits neither the lone grouping (exactly one event)
        nor the correlated-pair grouping (exactly two), so every move on it
        falls into the unknown category. All three events are locked, so the
        whole instance stays attributable to this one injection.
        """
        alive = self._alive(anchor.mid)
        if len(alive) != 2:
            return None
        if len({r.log_idx for r in alive}) < 2:
            return None
        if any(self._is_used(r.event) for r in alive):
            return None

        events = self.logs[anchor.log_idx].traces[anchor.trace_idx]
        i = self._pos(events, anchor.event)
        if i is None:
            return None

        duplicate = Event(dict(anchor.event))
        ts = self._interpolate_timestamp(events, i)
        if ts is not None:
            duplicate[self.k.timestamp] = ts
        events.insert(i + 1, duplicate)

        self.mid_index[anchor.mid].append(EventRef(
            log_idx=anchor.log_idx,
            trace_idx=anchor.trace_idx,
            event=duplicate,
            mid=anchor.mid,
            org=anchor.org,
            mode=anchor.mode,
        ))
        for r in alive:
            self.touched.add(id(r.event))
        self.touched.add(id(duplicate))

        counterpart = next(r for r in alive if r.event is not anchor.event)
        return self._gt(
            anchor, "unclassified",
            details={
                "duplicated_activity": str(anchor.event.get(self.k.event_id)),
                "duplicated_mode": anchor.mode,
                "duplicate_position": i + 1,
                "duplicate_timestamp": ts,
                "counterpart_activity": str(counterpart.event.get(self.k.event_id)),
                "counterpart_mode": counterpart.mode,
                "counterpart_logId": self.logs[counterpart.log_idx].log_id,
                "counterpart_org_group": counterpart.org,
                "events_on_identifier": 3,
                "msgType": anchor.event.get(self.k.msg_type),
            },
        )

    # -- Helpers -------------------------------------------------------------

    def _interpolate_timestamp(self, events: list[Event], i: int):
        ts = events[i].get(self.k.timestamp)
        if not isinstance(ts, datetime):
            return None
        if i + 1 < len(events):
            nxt = events[i + 1].get(self.k.timestamp)
            if isinstance(nxt, datetime) and nxt >= ts:
                # never later than the next event, so the insertion stays in place
                return ts + min((nxt - ts) / 2, timedelta(seconds=1))
        return ts + timedelta(seconds=1)

    @staticmethod
    def _pos(events: list[Event], target: Event) -> int | None:
        for i, e in enumerate(events):
            if e is target:
                return i
        return None

    def _gt(self, ref: EventRef, etype: str, details: dict) -> dict:
        log = self.logs[ref.log_idx]
        attrs = log.trace_attrs[ref.trace_idx]
        return {
            "logId": log.log_id,
            "source_file": log.path.name,
            "trace_index": ref.trace_idx,
            "traceId": attrs.get(self.k.trace_id),
            "org_group": ref.org,
            "error_type": etype,
            "msgInstanceId": ref.mid,
            "anchor_activity": str(ref.event.get(self.k.event_id)),
            **details,
        }

    # -- Output --------------------------------------------------------------

    def write(self, base_dir: Path, suffix: str = "_noisy",
              log_subdir: str = "dirty_logs") -> list[Path]:
        """Write the manipulated logs and the ground truth into
        `base_dir/log_subdir`. The originals stay untouched."""
        log_dir = base_dir / log_subdir
        log_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        for log in self.logs:
            traces = []
            for attrs, events in zip(log.trace_attrs, log.traces):
                if not events:
                    continue
                traces.append(Trace(list(events), attributes=dict(attrs)))
            out_log = EventLog(
                traces,
                attributes=log.attributes,
                extensions=log.extensions,
                classifiers=log.classifiers,
                omni_present=log.omni_present,
            )
            target = log_dir / f"{log.path.stem}{suffix}.xes"
            pm4py.write_xes(out_log, str(target))
            written.append(target)

        gt_path = log_dir / "ground_truth.json"
        gt_path.write_text(
            json.dumps(
                {
                    "config": {
                        "level": self.cfg.level,
                        "unit": self.cfg.unit,
                        "errors_per_trace": self.cfg.errors_per_trace,
                        "types": list(self.cfg.types),
                        "weights": list(self.cfg.weights),
                        "seed": self.cfg.seed,
                        "mode_send": self.cfg.mode_send,
                        "mode_receive": self.cfg.mode_receive,
                        "async_shift_seconds": self.cfg.async_shift_seconds,
                        "swap_min_distance": self.cfg.swap_min_distance,
                        "swap_require_deterministic_order":
                            self.cfg.swap_require_deterministic_order,
                    },
                    "source_logs": [str(p) for p in self.paths],
                    "uncovered_types": list(self.uncovered),
                    "n_injected": len(self.ground_truth),
                    "expected_report_entries": self.expected_report_entries(),
                    "injections": self.ground_truth,
                    "pattern_lines": self.pattern_lines(),
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )
        written.append(gt_path)
        return written

    # How many entries each injection is expected to produce in the correlation
    # block of check_conformance.py, so recall can be computed against the right
    # denominator instead of against the injection count.
    _ENTRIES_PER_INJECTION = {
        "sender_move": 1,
        "receiver_move": 1,
        "async_communication": 2,
        "unclassified": 3,
    }

    def expected_report_entries(self) -> dict[str, int]:
        expected: dict[str, int] = defaultdict(int)
        for r in self.ground_truth:
            etype = r["error_type"]
            if etype == "swap":
                # One report entry per affected trace, i.e. per organization
                # holding events of both message instances in that case.
                expected[etype] += sum(
                    1 for t in r["affected_traces"] if t["detectable"]
                )
            else:
                expected[etype] += self._ENTRIES_PER_INJECTION.get(etype, 1)
        return dict(expected)

    # Builds a human-readable list of strings, one per distinct injected pattern
    # (grouped and counted) -- same "{count}x ... " style as check_conformance.py's
    # generate_move_report() pattern_lines, so the injected ground truth can be
    # visually compared against what the conformance checking stage actually detects.
    def pattern_lines(self) -> list[str]:
        groups: dict[tuple, int] = defaultdict(int)
        for r in self.ground_truth:
            etype = r["error_type"]
            if etype in ("sender_move", "receiver_move"):
                key = (etype, r["remaining_activity"], r["removed_activity"])
            elif etype == "async_communication":
                key = (etype, r["send_activity"], r["receive_activity"], r["shifted_side"])
            elif etype == "swap":
                key = (etype, r["activity_a"], r["activity_b"])
            elif etype == "unclassified":
                key = (etype, r["duplicated_activity"])
            else:
                key = (etype,)
            groups[key] += 1

        lines = []
        for key, count in groups.items():
            etype = key[0]
            if etype == "sender_move":
                _, remaining, removed = key
                lines.append(f"{count}x SENDER_MOVE of {remaining} "
                             f"(receive side {removed} removed)")
            elif etype == "receiver_move":
                _, remaining, removed = key
                lines.append(f"{count}x RECEIVER_MOVE of {remaining} "
                             f"(send side {removed} removed)")
            elif etype == "async_communication":
                _, send_activity, receive_activity, side = key
                lines.append(f"{count}x ASYNC_COMMUNICATION Move between {send_activity} and "
                             f"{receive_activity} ({side} shifted)")
            elif etype == "swap":
                _, activity_a, activity_b = key
                lines.append(f"{count}x SWAP Move between {activity_a} and {activity_b}")
            elif etype == "unclassified":
                _, activity = key
                lines.append(f"{count}x UNCLASSIFIED of {activity} "
                             f"(identifier carried by 3 events)")
        return lines

    def summary(self) -> str:
        counts: dict[str, int] = defaultdict(int)
        per_log: dict[str, int] = defaultdict(int)
        for r in self.ground_truth:
            counts[r["error_type"]] += 1
            per_log[r["logId"]] += 1

        swaps = [r for r in self.ground_truth if r["error_type"] == "swap"]
        swap_traces = sum(r["n_affected_traces"] for r in swaps)
        detectable = sum(r["n_detectable_traces"] for r in swaps)
        non_deterministic = sum(
            1 for r in swaps for t in r["affected_traces"]
            if not t["order_in_clean_log"]["deterministic"]
        )
        orphaned = sum(1 for r in self.ground_truth if r.get("orphaned_pair"))
        expected = self.expected_report_entries()

        n_traces = sum(len(l.traces) for l in self.logs)
        lines = [
            f"Logs loaded:              {len(self.logs)}",
            f"Traces total:             {n_traces}",
            f"Cross-org message events: {len(self.eligible)}",
            f"Injections total:         {len(self.ground_truth)}",
            "",
            f"{'Pattern':<22}{'injected':>10}{'report entries':>16}",
        ]
        for t in self.cfg.types:
            lines.append(f"{t:<22}{counts.get(t, 0):>10}{expected.get(t, 0):>16}")
        lines += [
            "",
            f"Traces touched by swaps:  {swap_traces}",
            f"  thereof detectable:     {detectable}",
            f"  order not deterministic:{non_deterministic}",
            f"Orphaned trace pairs:     {orphaned}",
            "",
            "By log:",
        ]
        for lid, c in sorted(per_log.items()):
            lines.append(f"  {lid:<22} {c}")

        if self.skipped:
            lines += ["", "Skipped (not applicable):"]
            for t, c in sorted(self.skipped.items()):
                lines.append(f"  {t:<22} {c}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def collect_logs(directory: Path) -> list[Path]:
    """Return all .xes files in `directory`, sorted, non-recursive."""
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() == ".xes" and not p.name.startswith(".")
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Inject miscommunication patterns into cross-organizational "
                    "XES logs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("log_dir", type=Path,
                   help="Directory containing the organizational .xes logs.")
    p.add_argument("--log-subdir", default="dirty_logs",
                   help="Name of the subdirectory created inside the input "
                        "directory for the results.")
    p.add_argument("--level", type=float, default=0.1,
                   help="Fraction of traces resp. events to manipulate (0..1].")
    p.add_argument("--unit", choices=("trace", "event"), default="trace",
                   help="Reference quantity for --level.")
    p.add_argument("--errors-per-trace", type=int, default=1,
                   help="Errors per selected trace (only with --unit trace).")
    p.add_argument("--types", default=",".join(ERROR_TYPES),
                   help="Comma-separated list of active miscommunication patterns.")
    p.add_argument("--weights", default=None,
                   help="Comma-separated weights, same length as --types.")
    p.add_argument("--seed", type=int, default=42,
                   help="Seed for reproducibility.")
    p.add_argument("--suffix", default="_noisy",
                   help="File suffix of the output logs.")
    p.add_argument("--mode-send", default="send",
                   help="communicationMode value marking the send side.")
    p.add_argument("--mode-receive", default="receive",
                   help="communicationMode value marking the receive side.")
    p.add_argument("--async-shift-seconds", type=float, default=1.0,
                   help="Offset used when inverting a send/receive pair.")
    p.add_argument("--swap-min-distance", type=int, default=2,
                   help="Minimum distance between the two swapped events, "
                        "required in at least one affected trace.")
    p.add_argument("--swap-allow-any-order", action="store_true",
                   help="Also swap communication points whose order varies in "
                        "the clean log. Such a swap cannot be a deviation "
                        "against a model discovered from that log; use only to "
                        "demonstrate that limit.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.log_dir.is_dir():
        print(f"Error: {args.log_dir} is not a directory.", file=sys.stderr)
        return 2

    if not args.log_subdir:
        print("Error: --log-subdir must not be empty, otherwise the "
              "manipulated logs would land next to the originals and be "
              "picked up on the next run.", file=sys.stderr)
        return 2

    logs = collect_logs(args.log_dir)
    if len(logs) < 2:
        print(f"Error: found {len(logs)} .xes file(s) in {args.log_dir}, "
              f"need at least two -- otherwise no cross-organizational "
              f"message exchange exists.", file=sys.stderr)
        return 2
    if not 0 < args.level <= 1:
        print("Error: --level must be within (0, 1].", file=sys.stderr)
        return 2
    if args.swap_min_distance < 2:
        print("Error: --swap-min-distance must be at least 2, otherwise the "
              "alignment can attribute the displacement to either activity.",
              file=sys.stderr)
        return 2

    types = tuple(t.strip() for t in args.types.split(",") if t.strip())
    unknown = set(types) - set(ERROR_TYPES)
    if unknown:
        print(f"Error: unknown miscommunication patterns {sorted(unknown)}.",
              file=sys.stderr)
        return 2

    if args.weights:
        weights = tuple(float(w) for w in args.weights.split(","))
        if len(weights) != len(types):
            print("Error: --weights and --types must have the same length.",
                  file=sys.stderr)
            return 2
    else:
        weights = tuple(1.0 for _ in types)

    cfg = InjectionConfig(
        level=args.level,
        unit=args.unit,
        errors_per_trace=max(1, args.errors_per_trace),
        types=types,
        weights=weights,
        seed=args.seed,
        mode_send=args.mode_send,
        mode_receive=args.mode_receive,
        async_shift_seconds=args.async_shift_seconds,
        swap_min_distance=args.swap_min_distance,
        swap_require_deterministic_order=not args.swap_allow_any_order,
    )

    print(f"Found {len(logs)} log(s) in {args.log_dir}:")
    for lp in logs:
        print(f"  {lp.name}")
    print()

    injector = NoiseInjector(logs, Keys(), cfg)
    injector.load()
    injector.build_index()

    if not injector.eligible:
        print("Warning: no cross-organizational message events found. Check "
              "whether the logs belong to the same collaboration process.",
              file=sys.stderr)
        return 1

    injector.run()
    if injector.uncovered:
        print("Note: the following patterns are not applicable to this log and were "
              "skipped:", file=sys.stderr)
        for etype in injector.uncovered:
            print(f"  {etype}: {injector.diagnose(etype)}", file=sys.stderr)

    written = injector.write(args.log_dir, suffix=args.suffix,
                             log_subdir=args.log_subdir)

    print(injector.summary())
    print("\nWritten:")
    for p in written:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())