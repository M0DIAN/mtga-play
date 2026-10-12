"""Profile recorded matches; never operate the client or infer game effects."""
import argparse
from datetime import datetime
import json
from math import ceil
from pathlib import Path
from statistics import median
import sys

from mtga_data import read_jsonl, write_json


def distribution(values, eligible_count=None):
    ordered = sorted(values)
    result = {"count": len(values),
            "total_s": round(sum(values), 6) if values else None,
            "median_s": round(median(values), 6) if values else None,
            "p95_s": round(ordered[ceil(.95 * len(ordered)) - 1], 6) if ordered else None,
            "max_s": round(max(values), 6) if values else None}
    if eligible_count is not None:
        result.update(eligible_count=eligible_count, missing_count=eligible_count - len(values),
                      coverage_fraction=round(len(values) / eligible_count, 6) if eligible_count else None)
    return result


def capture_summary(events, start_seq, end_seq):
    """Count evidenced captures, not text observations or repeated journal references.

    Controller IDs scope generation across controller restarts. Legacy generations
    are scoped to an explicit game boundary and only an ordered, explicitly marked
    fallback reference can resolve a repeated generation without a controller ID.
    """
    groups = {}
    epoch = 0
    missing_evidence, missing_identity, unresolved_references = [], [], []
    empty_screenshots, uncertain_without_capture = [], []
    ignored_text_observations = evidenced_records = 0
    inside = lambda e: start_seq <= e["seq"] <= end_seq
    for event in events:
        if event["seq"] > end_seq:
            break
        if event["type"] in {"match_start", "game_start"}:
            epoch += 1
        if event["type"] not in {"observation", "execution"}:
            continue
        timing = event.get("timing")
        timing = timing if isinstance(timing, dict) else {}
        shots = event.get("screenshots")
        has_shots = isinstance(shots, list) and bool(shots)
        generation = event.get("generation")
        has_generation = type(generation) is int and generation > 0
        reference = event.get("capture_already_counted") is True
        if event["type"] == "execution" and event.get("execution") == "uncertain" and not has_shots and not reference:
            if inside(event):
                uncertain_without_capture.append(event["seq"])
            continue
        if shots == [] and not reference:
            if inside(event):
                empty_screenshots.append(event["seq"])
            continue
        candidate = (has_generation or has_shots or reference or "generation" in event
                     or "screenshots" in event or "capture_ms" in timing
                     or event.get("execution") == "sent_and_refreshed")
        if not candidate:
            if inside(event) and event["type"] == "observation":
                ignored_text_observations += 1
            continue
        if not has_generation and not has_shots:
            if inside(event):
                missing_evidence.append(event["seq"])
            continue
        if inside(event):
            evidenced_records += 1
        if not has_generation:
            if inside(event):
                missing_identity.append(event["seq"])
            continue
        controller = event.get("controller_id")
        controller = controller if isinstance(controller, str) and controller else None
        key = ("controller", controller, generation) if controller else ("legacy", epoch, generation)
        group = groups.setdefault(key, {"sources": [], "references": [], "ambiguous": False})
        if reference:
            decision_seq = event.get("decision_seq")
            observations = [e for e in group["sources"] if e["type"] == "observation"
                            and (type(decision_seq) is not int or e["seq"] > decision_seq)]
            if not observations or (not controller and len(observations) != 1):
                if inside(event):
                    unresolved_references.append(event["seq"])
                continue
            group["references"].append(event)
        else:
            if group["sources"] and not controller:
                group["ambiguous"] = True
            group["sources"].append(event)

    confirmed = []
    ambiguous, outside_references = [], []
    duplicate_count = 0
    for group in groups.values():
        members = sorted(group["sources"] + group["references"], key=lambda e: e["seq"])
        selected_members = [e for e in members if inside(e)]
        if not selected_members or not group["sources"]:
            continue
        # A capture before an input cannot be that input's refresh, even if a
        # malformed record reuses its generation. Keep the resulting count unknown.
        first = group["sources"][0]
        executions = [e for e in members if e["type"] == "execution"]
        if len(executions) > 1 or any(type(e.get("decision_seq")) is int
                                      and first["seq"] <= e["decision_seq"] for e in executions):
            group["ambiguous"] = True
        if group["ambiguous"]:
            ambiguous.extend(e["seq"] for e in selected_members)
            continue
        if not inside(first):
            outside_references.extend(e["seq"] for e in selected_members)
            continue
        duplicate_count += len(selected_members) - 1
        # The capture-producing record takes precedence over a copied fallback
        # timing; a reference may supply the measurement if the source omitted it.
        value = None
        for event in group["sources"] + group["references"]:
            timing = event.get("timing")
            measured = timing.get("capture_ms") if isinstance(timing, dict) else None
            if type(measured) in (int, float) and 0 <= measured < float("inf"):
                value = measured / 1000
                break
        confirmed.append({"input_refresh": bool(executions), "seconds": value})

    unknown = bool(missing_evidence or missing_identity or unresolved_references or ambiguous)
    counts = {"unique_count": len(confirmed),
              "input_refresh_count": sum(e["input_refresh"] for e in confirmed),
              "extra_observe_count": sum(not e["input_refresh"] for e in confirmed)}
    timings = {}
    for name, captures in [("all", confirmed),
                           ("input_refresh", [e for e in confirmed if e["input_refresh"]]),
                           ("extra_observe", [e for e in confirmed if not e["input_refresh"]])]:
        timings[name] = distribution([e["seconds"] for e in captures if e["seconds"] is not None], len(captures))
    return {
        **{name: None if unknown else count for name, count in counts.items()},
        "confirmed_counts": counts,
        "duplicate_record_count": duplicate_count,
        "coverage": {
            "status": "partial" if unknown and confirmed else "unknown" if unknown else "complete",
            "evidenced_record_count": evidenced_records,
            "missing_capture_evidence_seqs": missing_evidence,
            "missing_capture_identity_seqs": missing_identity,
            "unresolved_reference_seqs": unresolved_references,
            "ambiguous_generation_seqs": sorted(ambiguous),
            "capture_outside_boundary_seqs": outside_references,
            "empty_screenshot_seqs": empty_screenshots,
            "uncertain_without_capture_seqs": uncertain_without_capture,
            "text_observation_count": ignored_text_observations,
        },
        "capture_timing": timings,
        "interpretation": (
            "Counts are screenshot capture calls, not individual image files or successful game actions. "
            "Text-only observations, empty screenshot results and uncertain executions without capture evidence are excluded. "
            "A fallback observation plus capture_already_counted execution is one input refresh. "
            "controller_id and generation identify a capture within this session; unscoped legacy collisions stay unknown. "
            "Null totals indicate incomplete identity/evidence; confirmed_counts and capture_timing cover only resolved captures. "
            "Capture time uses its producing record, or an explicit reference if the source timing is absent."
        ),
    }


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
        metrics[event_type + "." + field] = distribution(values, sum(e["type"] == event_type for e in selected))
    captures = capture_summary(events, start_seq, end_seq)
    if captures["coverage"]["status"] != "complete":
        warnings.append("Some screenshot captures cannot be uniquely identified; see captures.coverage, not an estimated total")
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
        "decision_to_execution": distribution(spans, len(decisions)),
        "outside_decision_execution_intervals_s": round(elapsed - sum(spans), 6),
        "raw_observation_event_count": sum(e["type"] == "observation" for e in selected),
        "standalone_observation_count": sum(e["type"] == "observation" for e in selected),
        "ui_recovery_decision_count": sum(e.get("training_use") == "ui_recovery_only"
                                          or e.get("decision_kind") == "ui_recovery"
                                          for e in decisions.values()),
        "instrumented": metrics,
        "captures": captures,
        "largest_event_gaps": sorted(gaps, key=lambda g: g["seconds"], reverse=True)[:10],
        "result_recorded": result_event["type"] == "game_result",
        "recorded_result": result_event.get("result") if result_event["type"] == "game_result" else None,
        "warnings": warnings,
        "interpretation": (
            "Decision-to-execution spans include journaling, input and refresh; they are not pure API latency. "
            "Remaining time includes inspection, analysis, tool routing, animation, user interruptions and logging gaps. "
            "since_capture_ms starts at screenshot API return; it is not a model reasoning timer. "
            "game_result time is when the result was recorded, not the exact instant victory first appeared. "
            "Instrumented metrics preserve raw journal measurements, can reference the same capture, overlap and must not be added to elapsed_s. "
            "Missing metrics are unknown, not zero; eligible_count and missing_count describe field coverage, not inferred API calls. "
            "P95 uses the nearest rank (ceil(0.95 * count)); small-sample P95 is often the maximum. "
            "standalone_observation_count is a compatibility alias of raw_observation_event_count, not an extra-screenshot count. "
            "Use captures for deduplicated input refreshes, extra observes and capture timings."
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
