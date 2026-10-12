"""Bounded deck extraction, read-only card export, and reviewed data curation.

No UI automation, hidden opponent state, network calls, or game-file changes.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from uuid import uuid4


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_jsonl(path, optional=False):
    path = Path(path)
    if optional and not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise ValueError(f"{path.name}:{number}: blank JSONL line")
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"{path.name}:{number}: expected JSON object")
        rows.append(row)
    return rows


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def write_jsonl(path, rows):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_log_tail(path, tail_mib=8):
    if not 0 < tail_mib <= 64:
        raise ValueError("tail_mib must be between 1 and 64")
    path = Path(path)
    limit = tail_mib * 1024 * 1024
    with path.open("rb") as stream:
        start = max(0, path.stat().st_size - limit)
        stream.seek(start)
        discarded = b""
        if start:
            discarded = stream.readline(limit)  # Bound even an unterminated line.
        data = stream.read(limit - len(discarded))
    return data.decode("utf-8-sig", errors="replace")


def normalize_cards(cards):
    quantities = Counter()
    for item in cards:
        card_id, quantity = item["cardId"], item["quantity"]
        if type(card_id) is not int or type(quantity) is not int or card_id <= 0 or quantity <= 0:
            raise ValueError("Card IDs and quantities must be positive integers")
        quantities[card_id] += quantity
    return [{"cardId": key, "quantity": value} for key, value in quantities.items()]


def parse_decks(text):
    decks, malformed = {}, 0
    for line_number, line in enumerate(text.splitlines(), 1):
        if "DeckUpsertDeckV3" not in line:
            continue
        try:
            outer, _ = json.JSONDecoder().raw_decode(line[line.index("{"):])
            request = outer["request"]
            if isinstance(request, str):
                request = json.loads(request)
            summary, deck = request["Summary"], request["Deck"]
            deck_id, name = summary["DeckId"], summary["Name"]
            if not isinstance(deck_id, str) or not isinstance(name, str):
                raise ValueError("Deck identity must be text")
            attributes = {v["name"]: v["value"] for v in summary.get("Attributes", [])}
            decks[deck_id] = {
                "deck_id": deck_id, "deck_name": name,
                "format_in_client": attributes.get("Format"),
                "main_deck": normalize_cards(deck["MainDeck"]),
                "sideboard": normalize_cards(deck.get("Sideboard", [])),
                "command_zone": normalize_cards(deck.get("CommandZone", [])),
                "companions": normalize_cards(deck.get("Companions", [])),
                "tail_line": line_number,
            }
        except (KeyError, ValueError, TypeError, IndexError):
            malformed += 1
    return list(decks.values()), malformed


def select_deck(decks, name=None, deck_id=None):
    if not name and not deck_id:
        raise ValueError("Select the UI-confirmed deck by name or ID")
    matches = [d for d in decks if (name is None or d["deck_name"] == name)
               and (deck_id is None or d["deck_id"] == deck_id)]
    if len(matches) != 1:
        raise ValueError(f"Expected one exact deck match; found {len(matches)}. Use --deck-id or UI export.")
    return matches[0]


def new_directory(root, prefix):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    label = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    destination = root / (prefix + "_" + label)
    destination.mkdir(exist_ok=False)
    return destination


def prepare(args):
    from card_model import CardCatalog
    decks, malformed = parse_decks(read_log_tail(args.player_log, args.tail_mib))
    selected = select_deck(decks, args.deck_name, args.deck_id)
    if args.translation_db and not Path(args.translation_db).is_file():
        raise ValueError("Explicit translation database does not exist")
    catalog = CardCatalog(args.game_root, args.translation_db)
    zones = ["main_deck", "sideboard", "command_zone", "companions"]
    main_quantities = {c["cardId"]: c["quantity"] for c in selected["main_deck"]}
    ids = {c["cardId"] for zone in zones for c in selected[zone]}
    if not ids:
        raise ValueError("Selected deck has no cards")
    related = set(catalog.linked_closure(ids))
    pending = list(related)
    while pending:
        gid = pending.pop()
        source = catalog.cards[gid]
        token_ids = [int(m) for m in re.findall(r"(?:^|,)\d+:(\d+)", source.get("AbilityIdToLinkedTokenGrpId") or "")]
        for token_id in token_ids:
            for linked in catalog.linked_closure([token_id]):
                if linked not in related:
                    related.add(linked)
                    pending.append(linked)
        if len(related) > 1024:
            raise ValueError("Unexpectedly large card linkage; inspect database encoding")
    records = []
    for gid in sorted(ids):
        record = dict(catalog.record(gid))
        record["deck_quantity"] = main_quantities.get(gid, 0)
        record["deck_sections"] = {zone: sum(c["quantity"] for c in selected[zone] if c["cardId"] == gid) for zone in zones}
        records.append(record)
    related_records = [catalog.record(gid) for gid in sorted(related - ids)]
    destination = new_directory(args.output_root, "match")
    session = {
        "schema_version": 2, "session_id": destination.name, "created_at": utc_now(),
        **selected, "main_deck_count": sum(main_quantities.values()),
        "mode": args.mode, "status": "preparing",
        "turn_index_definition": "Own turn, 1-based; mulligan is 0.",
        "observation_policy": "Player-visible state only; no hidden opponent hand/library used.",
        "sources": {"deck": str(Path(args.player_log).resolve()), "cards": str(catalog.database),
                    "translation": str(catalog.zh_database) if catalog.zh_database else None,
                    "tail_mib": args.tail_mib, "ignored_malformed_deck_lines": malformed},
        "related_record_count": len(related_records),
    }
    write_json(destination / "session.json", session)
    write_jsonl(destination / "card_records.jsonl", records)
    write_jsonl(destination / "related_cards.jsonl", related_records)
    (destination / "actions.jsonl").touch(exist_ok=False)
    lines = ["Deck"]
    for zone in ("main_deck", "sideboard"):
        if zone == "sideboard" and selected[zone]:
            lines += ["", "Sideboard"]
        for entry in selected[zone]:
            card = catalog.record(entry["cardId"])
            lines.append(f"{entry['quantity']} {card['name_en']} ({card['set_code'].upper()}) {card['collector_number']}")
    (destination / "deck.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"session_directory": str(destination), "deck_name": selected["deck_name"],
            "main_deck_count": session["main_deck_count"], "card_records": len(records),
            "related_records": len(related_records), "status": "preparing"}


def artifact_stats(item, artifact_count):
    """Evaluate only the explicitly supplied static count model, not a game engine."""
    basis = item.get("stats_basis", {})
    if basis.get("kind") != "artifact_count":
        return None
    return (basis.get("base_power", 0) + artifact_count + basis.get("power_modifier", 0),
            basis.get("base_toughness", 0) + artifact_count + basis.get("toughness_modifier", 0))


def state_issues(observation):
    if not isinstance(observation, dict):
        return []
    issues, battlefield = [], observation.get("self_battlefield", {})
    if not isinstance(battlefield, dict):
        return ["self_battlefield must be an object"]
    artifacts = battlefield.get("artifacts", [])
    if not isinstance(artifacts, list) or any(not isinstance(a, dict) for a in artifacts):
        return ["artifacts must be a list of objects"]
    count = observation.get("artifact_count")
    if count is not None and (type(count) is not int or count < 0):
        return ["artifact_count must be a nonnegative integer"]
    if battlefield.get("artifact_list_complete") or observation.get("artifact_list_complete"):
        actual_count = 0
        for item in artifacts:
            quantity = item.get("quantity", 1)
            if type(quantity) is not int or quantity <= 0:
                issues.append("Invalid artifact quantity")
                continue
            actual_count += quantity
        if count is not None and count != actual_count:
            issues.append("Artifact count disagrees with complete artifact list")
        count = actual_count
    if count is not None:
        for item in artifacts:
            try:
                expected = artifact_stats(item, count)
            except (TypeError, ValueError, AttributeError):
                issues.append("Invalid dynamic stats formula")
                continue
            if expected and (item.get("power"), item.get("toughness")) != expected:
                issues.append("Dynamic artifact stats disagree with explicit count model")
    return issues


FORBIDDEN_OBSERVATION_KEYS = {"verified_result", "game_outcome", "final_result", "future_draw", "future_opponent_action"}


def future_keys(value):
    if isinstance(value, dict):
        return [key for key in value if key in FORBIDDEN_OBSERVATION_KEYS] + [key for v in value.values() for key in future_keys(v)]
    if isinstance(value, list):
        return [key for v in value for key in future_keys(v)]
    return []


def eligible(record, accept_legacy=False):
    evidence, quality = record.get("evidence", {}), record.get("quality", {})
    confirmed = evidence.get("effect_status") == "confirmed"
    legacy = (accept_legacy and "effect_status" not in evidence
              and quality.get("label") == "reviewed_self_play_candidate"
              and quality.get("expert_gold") is False)
    reviewed = quality.get("label") == "reviewed_self_play_candidate" and quality.get("expert_gold") is False
    return (reviewed and record.get("training_inclusion") is True and (confirmed or legacy)
            and bool(record.get("verified_result")) and bool(record.get("reason_summary"))
            and bool(evidence.get("action_event_sequences")))


def build_training(records, session_id, accept_legacy=False):
    rows, metadata, skipped = [], [], []
    for record in records:
        if record.get("session_id") != session_id:
            raise ValueError("Decision belongs to a different session")
        if not eligible(record, accept_legacy):
            skipped.append(record.get("sample_id"))
            continue
        observation = record["observation"]
        if future_keys(observation):
            raise ValueError("Future/outcome fields found inside a decision observation")
        if state_issues(observation):
            raise ValueError("Inconsistent decision state: " + record["sample_id"])
        user = {"turn": record["turn"], "phase": record["phase"], "observation": observation,
                "candidate_actions": record.get("candidate_actions", []), "request": "选择当前动作。"}
        if record.get("local_card_reference"):
            user["local_card_reference"] = record["local_card_reference"]
        answer = {"action": record["chosen_action"], "reason_summary": record["reason_summary"]}
        rows.append({"messages": [
            {"role": "system", "content": "你是MTGA决策助手。仅依据当前玩家可见信息与卡牌规则选择合法动作；不假设对手隐藏手牌或牌序。turn为我方回合编号，调度为0。输出JSON，包含action与简短reason_summary。"},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)},
        ]})
        metadata.append({"sample_id": record["sample_id"], "session_id": session_id,
                         "verified_result": record["verified_result"], "evidence": record["evidence"],
                         "quality": record.get("quality", {}), "game_outcome": record.get("game_outcome")})
    return rows, metadata, skipped


def validate_session(directory):
    directory = Path(directory)
    session = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    names = ["actions.jsonl", "card_records.jsonl", "related_cards.jsonl", "token_records.jsonl",
             "decision_records.jsonl", "training.jsonl", "training_metadata.jsonl", "corrections.jsonl"]
    data = {name: read_jsonl(directory / name, optional=True) for name in names}
    issues, warnings = [], []
    events, decisions = data["actions.jsonl"], data["decision_records.jsonl"]
    event_ids = {e.get("seq") for e in events}
    if events and [e.get("seq") for e in events] != list(range(1, len(events) + 1)):
        issues.append("Raw event sequences must be unique and contiguous")
    if any(e.get("session_id") != session["session_id"] for e in events):
        issues.append("Raw event belongs to a different session")
    total = sum(c["quantity"] for c in session.get("main_deck", []))
    if total != session.get("main_deck_count"):
        issues.append("Main-deck count mismatch")
    card_records = {c["card_id"]: c for c in data["card_records.jsonl"]}
    for entry in session.get("main_deck", []):
        card = card_records.get(entry["cardId"])
        if not card or card.get("deck_quantity") != entry["quantity"]:
            issues.append("Main-deck card reference/quantity mismatch")
    by_id = {d["sample_id"]: d for d in decisions}
    if len(by_id) != len(decisions):
        issues.append("Duplicate sample IDs")
    legacy_count = 0
    for record in decisions:
        if record.get("session_id") != session["session_id"]:
            issues.append("Decision belongs to another session")
        refs = record.get("evidence", {}).get("action_event_sequences", [])
        if not refs or not all(seq in event_ids for seq in refs):
            issues.append("Decision has missing event evidence: " + record["sample_id"])
        if "effect_status" not in record.get("evidence", {}):
            legacy_count += 1
        issues += state_issues(record.get("observation"))
        if future_keys(record.get("observation")):
            issues.append("Future information in decision observation")
    if legacy_count:
        warnings.append(f"{legacy_count} legacy records lack explicit effect_status; curation requires explicit legacy acceptance")
    training, metadata = data["training.jsonl"], data["training_metadata.jsonl"]
    if metadata and len(metadata) != len(training):
        issues.append("Training/metadata row counts disagree")
    aligned = [by_id.get(m.get("sample_id")) for m in metadata] if metadata else [d for d in decisions if d.get("training_inclusion") is True]
    if training and len(aligned) != len(training):
        issues.append("Training/decision row counts disagree")
    for index, row in enumerate(training):
        try:
            messages = row["messages"]
            if list(row) != ["messages"] or [m["role"] for m in messages] != ["system", "user", "assistant"]:
                raise ValueError("Invalid SFT messages")
            user, answer = json.loads(messages[1]["content"]), json.loads(messages[2]["content"])
            if future_keys(user):
                raise ValueError("Future result in SFT input")
            record = aligned[index]
            if not record or record.get("training_inclusion") is not True:
                raise ValueError("Training row has no included source decision")
            if record.get("evidence", {}).get("effect_status") in {"failed", "uncertain"}:
                raise ValueError("Unconfirmed effect included as positive sample")
            if answer["action"] != record["chosen_action"] or answer["reason_summary"] != record["reason_summary"]:
                raise ValueError("Training answer differs from reviewed decision")
            if user["turn"] != record["turn"] or user["phase"] != record["phase"] or user["observation"] != record["observation"]:
                raise ValueError("Training input differs from source state")
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            issues.append(f"training row {index + 1}: {exc}")
    if session.get("status") == "complete" and not session.get("result"):
        issues.append("Complete session lacks a result")
    final_events = [e for e in events if e.get("type") == "game_result"]
    if session.get("status") == "complete" and final_events:
        result, final = session.get("result", {}), final_events[-1]
        if result.get("outcome") != final.get("result"):
            issues.append("Session outcome disagrees with recorded game result")
        for key in ("self_life", "opponent_life"):
            if key in result and key in final and result[key] != final[key]:
                issues.append("Final life total disagrees with game result")
    return {"passed": not issues, "session_id": session["session_id"], "counts": {n: len(v) for n, v in data.items()},
            "main_deck_count": total, "issues": issues, "warnings": warnings,
            "scope": "Structural consistency only; not strategic optimality or independent visual verification."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    default_log = Path.home() / "AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log"
    listing = commands.add_parser("list-decks")
    preparation = commands.add_parser("prepare")
    for command in (listing, preparation):
        command.add_argument("--player-log", type=Path, default=default_log)
        command.add_argument("--tail-mib", type=int, default=8)
    preparation.add_argument("--game-root", type=Path, required=True)
    preparation.add_argument("--output-root", type=Path, required=True)
    preparation.add_argument("--translation-db", type=Path)
    selection = preparation.add_mutually_exclusive_group(required=True)
    selection.add_argument("--deck-name")
    selection.add_argument("--deck-id")
    preparation.add_argument("--mode", default="Sparky Bot Match", help="Metadata label only; does not start a game")
    validation = commands.add_parser("validate")
    validation.add_argument("--session", type=Path, required=True)
    validation.add_argument("--report", type=Path)
    curation = commands.add_parser("curate")
    curation.add_argument("--session", type=Path, required=True)
    curation.add_argument("--output-root", type=Path, required=True)
    curation.add_argument("--accept-reviewed-legacy", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "list-decks":
            decks, malformed = parse_decks(read_log_tail(args.player_log, args.tail_mib))
            result = {"decks": [{"deck_id": d["deck_id"], "deck_name": d["deck_name"], "main_deck_count": sum(c["quantity"] for c in d["main_deck"]), "format": d["format_in_client"]} for d in decks], "ignored_malformed_lines": malformed}
        elif args.command == "prepare":
            result = prepare(args)
        elif args.command == "validate":
            result = validate_session(args.session)
            if args.report:
                write_json(args.report, result)
        else:
            check = validate_session(args.session)
            if not check["passed"]:
                raise ValueError("Source validation failed: " + "; ".join(check["issues"]))
            session = json.loads((args.session / "session.json").read_text(encoding="utf-8"))
            if session.get("status") not in {"complete", "interrupted"}:
                raise ValueError("Curate only after recording the actual session end state")
            records = read_jsonl(args.session / "decision_records.jsonl")
            rows, metadata, skipped = build_training(records, session["session_id"], args.accept_reviewed_legacy)
            destination = new_directory(args.output_root, "curated")
            write_jsonl(destination / "training.jsonl", rows)
            write_jsonl(destination / "training_metadata.jsonl", metadata)
            result = {"output_directory": str(destination), "included": len(rows), "skipped": skipped,
                      "quality": "reviewed_self_play_candidate", "model_weights_updated": False}
            write_json(destination / "curation.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("passed", True) else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
