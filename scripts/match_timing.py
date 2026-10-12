"""Profile recorded matches; never operate the client or infer game effects."""
import argparse
from datetime import datetime
import json
from pathlib import Path
from statistics import median
import sys

from mtga_data import read_jsonl, write_json


def distribution(values):
    return {"count": len(values),
            "total_s": round(sum(values), 6) if values else None,
            "median_s": round(median(values), 6) if values else None,
            "max_s": round(max(values), 6) if values else None}


def analyze_events(events, start_seq=None, end_seq=None):
    if not events:
        raise ValueError("Empty action journal")
    session_id = events[0].get("session_id")
    times = {}
    for index, event in enumerate(events, 1):
        if event.get("seq") != index or event.get("session_id") != session_id or not session_id:
            raise ValueError("Journal sequence or session mismatch")
        timestamp = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None or (index > 1 and timestamp < times[index - 1]):
            raise ValueError("Timestamps must be timezone-aware and nondecreasing")
        times[index] = timestamp
    by_seq = {e["seq"]: e for e in events}
    if start_seq is None:
        start_event = next((e for e in events if e["type"] in {"match_start", "game_start"}), None)
        if start_event:
            start_source = "explicit_start_event"
        else:
            start_event = next((e for e in events if e["type"] == "decision"
                                and e.get("stage") == "gameplay"), events[0])
            start_source = "first_gameplay_decision_or_first_event; loading may be excluded"
        start_seq = start_event["seq"]
    else:
        start_source = "caller_selected_sequence"
    if start_seq not in by_seq:
        raise ValueError("Start sequence not in journal")
    if end_seq is None:
        end_seq = next((e["seq"] for e in events if e["seq"] >= start_seq
                        and e["type"] == "game_result"), events[-1]["seq"])
    if end_seq not in by_seq or end_seq < start_seq:
        raise ValueError("End sequence not in journal or before start")
    selected = [e for e in events if start_seq <= e["seq"] <= end_seq]
    decisions = {e["seq"]: e for e in selected if e["type"] == "decision"}
    spans, paired, warnings = [], set(), []
    last_execution = None
    for event in selected:
        if event["type"] != "execution":
            continue
        source = event.get("decision_seq")
        if source not in decisions:
            warnings.append(f"Execution {event['seq']} has no decision inside selected boundary")
            continue
        if source >= event["seq"] or source in paired or (last_execution and source < last_execution):
            raise ValueError("Duplicate, reversed, or overlapping decision/execution intervals")
        paired.add(source)
        last_execution = event["seq"]
        spans.append((times[event["seq"]] - times[source]).total_seconds())
    unpaired = sorted(set(decisions) - paired)
    if unpaired:
        warnings.append(f"Decisions without execution records: {unpaired}")
    metrics = {}
    for event_type, field in [("decision", "since_capture_ms"), ("execution", "input_ms"),
                              ("execution", "capture_ms"), ("execution", "action_cycle_ms"),
                              ("observation", "capture_ms")]:
        values = []
        for event in selected:
            timing = event.get("timing", {})
            value = timing.get(field) if isinstance(timing, dict) else None
            if event["type"] == event_type and type(value) in (int, float):
                if not 0 <= value < float("inf"):
                    raise ValueError("Timing values must be finite and nonnegative")
                values.append(value / 1000)
        metrics[event_type + "." + field] = distribution(values)
    gaps = [{"from_seq": a["seq"], "to_seq": b["seq"],
             "from_type": a["type"], "to_type": b["type"],
             "seconds": round((times[b["seq"]] - times[a["seq"]]).total_seconds(), 6)}
            for a, b in zip(selected, selected[1:])]
    elapsed = (times[end_seq] - times[start_seq]).total_seconds()
    result_event = by_seq[end_seq]
    return {
        "schema_version": 1, "session_id": session_id,
        "boundary": {"start_seq": start_seq, "end_seq": end_seq, "start_source": start_source},
        "elapsed_s": round(elapsed, 6), "decision_count": len(decisions),
        "own_turns_observed": sorted({e["turn"] for e in selected
                                       if type(e.get("turn")) is int and e["turn"] > 0}),
        "decision_to_execution": distribution(spans),
        "outside_decision_execution_intervals_s": round(elapsed - sum(spans), 6),
        "standalone_observation_count": sum(e["type"] == "observation" for e in selected),
        "ui_recovery_decision_count": sum(e.get("training_use") == "ui_recovery_only"
                                          or e.get("decision_kind") == "ui_recovery"
                                          for e in decisions.values()),
        "instrumented": metrics,
        "largest_event_gaps": sorted(gaps, key=lambda g: g["seconds"], reverse=True)[:10],
        "result_recorded": result_event["type"] == "game_result",
        "recorded_result": result_event.get("result") if result_event["type"] == "game_result" else None,
        "warnings": warnings,
        "interpretation": (
            "Decision-to-execution spans include journaling, input and refresh; they are not pure API latency. "
            "Remaining time includes inspection, analysis, tool routing, animation, user interruptions and logging gaps. "
            "since_capture_ms starts at screenshot API return; it is not a model reasoning timer. "
            "game_result time is when the result was recorded, not the exact instant victory first appeared. "
            "Instrumented metrics overlap and must not be added to elapsed_s. Missing metrics are unknown, not zero."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--start-seq", type=int)
    parser.add_argument("--end-seq", type=int)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        result = analyze_events(read_jsonl(args.session / "actions.jsonl"), args.start_seq, args.end_seq)
        if args.report:
            write_json(args.report, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
