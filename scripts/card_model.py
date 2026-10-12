"""Lossless MTGA records plus explicit presentation semantics (schema 2).

LinkedFaceType below is the *SQLite* representation, verified against linked
titles, IsPrimaryCard and characteristics. It is NOT the similarly named GRE
protobuf enum; those numeric values differ in this installed client.
"""

import html
import re
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from export_cards import ability_pairs, localizations, read_db, type_line

SCHEMA_VERSION = 2
# Intrinsic mana abilities granted by basic land subtypes (rule 305.6).
# These may be absent from Cards.AbilityIds even when the land produces mana.
BASIC_LAND_MANA = {54: ("W", 1001), 43: ("U", 1002), 69: ("B", 1003),
                   49: ("R", 1004), 29: ("G", 1005)}
LINK_TYPES = {
    0: ("single", "single"), 1: ("transform", "back"), 2: ("transform", "front"),
    3: ("meld", "result"), 4: ("meld", "component"),
    5: ("split", "half"), 6: ("split", "aggregate"),
    7: ("adventure", "spell"), 8: ("adventure", "permanent"),
    9: ("modal_dfc", "back"), 10: ("modal_dfc", "front"),
    11: ("specialize", "specialization"), 12: ("specialize", "base"),
    13: ("prototype", "prototype"), 14: ("prototype", "normal"),
    15: ("room", "door"), 16: ("room", "aggregate"),
    17: ("omen", "spell"), 18: ("omen", "permanent"),
    19: ("prepare", "permanent"), 20: ("prepare", "spell"),
}
FAMILY_LABELS = {
    "single": "单面牌", "transform": "转化双面牌", "modal_dfc": "模式双面牌",
    "meld": "合体牌", "split": "拼合牌", "aftermath": "余响拼合牌",
    "adventure": "历险牌", "specialize": "精进／专精牌", "prototype": "试作牌",
    "room": "房间牌", "omen": "兆咒牌", "prepare": "备咒牌",
}
ROLE_LABELS = {
    "single": "单面", "front": "正面", "back": "背面", "result": "合体结果",
    "component": "合体组件", "half": "拼合部分", "aggregate": "总记录",
    "permanent": "永久物部分", "spell": "咒语部分", "specialization": "精进形态",
    "base": "基础形态", "prototype": "试作特征", "normal": "通常特征", "door": "房间门",
}
LAYOUT_LABELS = {
    "creature": "生物", "artifact_creature": "神器生物", "enchantment_creature": "结界生物",
    "instant": "瞬间", "sorcery": "法术", "artifact": "神器", "equipment": "武具",
    "vehicle": "载具", "enchantment": "结界", "aura": "灵气", "land": "地",
    "planeswalker": "鹏洛客", "battle": "战役", "saga": "传纪", "class": "职业",
    "case": "案件", "station": "蓄势／太空船／星球", "dungeon": "地城",
    "token_creature": "生物衍生物", "token_other": "非生物衍生物", "emblem": "徽记",
    "auxiliary": "辅助状态牌", "aggregate": "多部分牌总记录", "unsupported": "待适配",
}


def numbers(value):
    return [int(x) for x in (value or "").split(",") if x]


def strip_markup(value):
    value = re.sub(r"<br\s*/?>", "\n", value or "", flags=re.I)
    # Rebalanced Arena title glyph is a marker, not a mana symbol.
    value = re.sub(r'<sprite[^>]*name=[\"\']arena_a[\"\'][^>]*>', "A-", value, flags=re.I)
    value = re.sub(r'<sprite[^>]*name=[\"\']([^\"\']+)[\"\'][^>]*>',
                   lambda m: "{o" + m[1][1:] + "}" if m[1].startswith("x") else "{" + m[1] + "}",
                   value, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", value)).replace("\r", "").strip()


def classify(card, abilities, en):
    types, subtypes = set(numbers(card["Types"])), set(numbers(card["Subtypes"]))
    family, role = LINK_TYPES.get(card["LinkedFaceType"], ("unsupported", "unknown"))
    if family == "split" and ("aftermath" in (card["AdditionalFrameDetails"] or "").lower()
                              or any(a["ability_id"] == 159 for a in abilities)):
        family = "aftermath"
    if role == "aggregate":
        layout = "aggregate"
    elif 13 in types:
        layout = "dungeon"
    elif 8 in types:
        layout = "planeswalker"
    elif ("emblem" in (en.get(card["TypeTextId"]) or "").lower()
          or strip_markup(en.get(card["TitleId"])) == "The Ring"):
        layout = "emblem"
    elif 14 in types:
        layout = "battle"
    elif 347 in subtypes:
        layout = "saga"
    elif 400 in subtypes:
        layout = "class"
    elif 424 in subtypes:
        layout = "case"
    elif 458 in subtypes or 456 in subtypes or any(a["ability_id"] == 373 for a in abilities):
        layout = "station"
    elif card["IsToken"]:
        layout = "token_creature" if 2 in types else "token_other" if types else "auxiliary"
    elif 331 in subtypes:
        layout = "vehicle"
    elif 28 in subtypes:
        layout = "equipment"
    elif 6 in subtypes:
        layout = "aura"
    elif 2 in types:
        layout = "artifact_creature" if 1 in types else "enchantment_creature" if 3 in types else "creature"
    elif 5 in types:
        layout = "land"
    elif 4 in types:
        layout = "instant"
    elif 10 in types:
        layout = "sorcery"
    elif 1 in types:
        layout = "artifact"
    elif 3 in types:
        layout = "enchantment"
    else:
        layout = "unsupported"
    return family, role, layout


class CardCatalog:
    def __init__(self, game_root, zh_database=None):
        self.root = Path(game_root).resolve()
        raw = self.root / "MTGA_Data" / "Downloads" / "Raw"
        self.database = max(raw.glob("Raw_CardDatabase_*.mtga"), key=lambda p: p.stat().st_mtime)
        self.zh_database = Path(zh_database) if zh_database else None
        with closing(read_db(self.database)) as db:
            self.en = localizations(db, "Localizations_enUS")
            self.cards = {r["GrpId"]: dict(r) for r in db.execute("SELECT * FROM Cards")}
            self.abilities = {r["Id"]: dict(r) for r in db.execute("SELECT * FROM Abilities")}
            self.alt_base = {r["AltGrpId"]: r["BaseGrpId"] for r in db.execute("SELECT * FROM AltToBasePrintings")}
        self.zh = {}
        if self.zh_database is not None and self.zh_database.is_file():
            with closing(read_db(self.zh_database)) as db:
                self.zh = localizations(db, "Localizations_jaJP")
        self.reverse_links = defaultdict(set)
        for gid, c in self.cards.items():
            for other in numbers(c["LinkedFaceGrpIds"]):
                self.reverse_links[other].add(gid)
        self._records, self._groups = {}, {}

    def linked_closure(self, ids):
        found, pending = set(ids), list(ids)
        while pending:
            gid = pending.pop()
            if gid not in self.cards:
                raise KeyError(f"Missing linked card {gid}")
            c = self.cards[gid]
            linked = set(numbers(c["LinkedFaceGrpIds"])) | self.reverse_links[gid]
            for other in linked - found:
                found.add(other)
                pending.append(other)
        return sorted(found)

    def group(self, gid):
        if gid in self._groups:
            return self._groups[gid]
        ids = self.linked_closure([gid])
        c = self.cards[gid]
        family, _ = LINK_TYPES.get(c["LinkedFaceType"], ("unsupported", "unknown"))
        if family == "single":
            ids = [gid]
        root_type = {"transform": 2, "modal_dfc": 10, "meld": 3, "split": 6,
                     "room": 16, "adventure": 8, "specialize": 12, "prototype": 14,
                     "omen": 18, "prepare": 19}.get(family, 0)
        roots = [i for i in ids if self.cards[i]["LinkedFaceType"] == root_type]
        root = min(roots or ids)
        r = self.cards[root]
        if family == "split" and ("aftermath" in (r["AdditionalFrameDetails"] or "").lower()
                                  or any(a == 159 for a, t in ability_pairs(r["AbilityIds"]))):
            family = "aftermath"
        if family in ("split", "aftermath", "room"):
            displays = numbers(r["LinkedFaceGrpIds"])
        elif family == "meld":
            displays = sorted(i for i in ids if i != root) + [root]
        else:
            displays = [root] + [i for i in numbers(r["LinkedFaceGrpIds"]) if i != root]
            displays += [i for i in ids if i not in displays]
        result = {"group_id": f"{family}:{root}", "family": family, "root_card_id": root,
                  "record_ids": ids, "display_card_ids": displays,
                  "relationships": [{"from": i, "to": j, "family": family}
                                    for i in ids for j in numbers(self.cards[i]["LinkedFaceGrpIds"])]}
        for i in ids:
            self._groups[i] = result
        return result

    def _ability(self, aid, tid):
        data = self.abilities.get(aid, {})
        return {"ability_id": aid, "text_id": tid, "text_en": self.en.get(tid),
                "text_zh": self.zh.get(tid), "source_ability": data}

    def display_sections(self, card, abilities):
        hidden = {a for a, t in ability_pairs(card["HiddenAbilityIds"])}
        by_id = {a["ability_id"]: a for a in abilities}
        sections = []
        for a in abilities:
            if a["ability_id"] in hidden:
                continue
            data = a["source_ability"]
            section = {"ability_ids": [a["ability_id"]], "text_en": a["text_en"],
                       "text_zh": a["text_zh"], "kind": "rules", "label": ""}
            if data.get("LoyaltyCost") is not None:
                section.update(kind="loyalty", label=data["LoyaltyCost"])
            elif data.get("BaseId") == 166 and data.get("BaseIdNumeral"):
                section.update(kind="chapter", label=str(data["BaseIdNumeral"]),
                               chapters=[data["BaseIdNumeral"]])
            elif data.get("SubCategory") == 27:
                section["kind"] = "case_condition"
            elif data.get("SubCategory") == 28:
                section["kind"] = "case_effect"
            wrapper = re.match(r"^(CLASSLEVEL|STATIONLEVEL)\s*\[([^]]*)\]\s*\[([^]]*)\]", a["text_en"] or "")
            if wrapper:
                wrapper_type, threshold, pt = wrapper.groups()
                section.update(kind="class_level" if wrapper_type == "CLASSLEVEL" else "station_level",
                               label=threshold, level_requirement=threshold, power_toughness=pt or None)
                children = numbers(data.get("HiddenAbilityIds"))
                # These wrapper records often contain untranslated English in the Chinese mod.
                # Resolve their exact underlying ability localizations when available.
                if children:
                    child_abilities = []
                    for child in children:
                        child_record = by_id.get(child)
                        if not child_record:
                            child_data = self.abilities.get(child)
                            if not child_data:
                                raise ValueError(f"Missing wrapper child {child} on {card['GrpId']}")
                            child_record = self._ability(child, child_data["TextId"])
                        child_abilities.append(child_record)
                    section["ability_ids"] += children
                    for lang in ("en", "zh"):
                        texts = [ch[f"text_{lang}"] for ch in child_abilities]
                        section[f"text_{lang}"] = "\n".join(texts) if all(texts) else None
                else:
                    for lang in ("en", "zh"):
                        text = a[f"text_{lang}"] or ""
                        match = re.match(r"^(?:CLASSLEVEL|STATIONLEVEL)\s*\[[^]]*\]\s*\[[^]]*\]\s*(.*)$", text, re.S)
                        section[f"text_{lang}"] = re.sub(r"^\[|\]$", "", match[1]).replace("] [", "\n") if match else None
            # Multiple chapters sharing identical text use one row with all chapter labels.
            prior = sections[-1] if sections else None
            if (section["kind"] == "chapter" and prior and prior["kind"] == "chapter"
                    and section["text_en"] == prior["text_en"] and section["text_zh"] == prior["text_zh"]):
                prior["chapters"].extend(section["chapters"])
                prior["ability_ids"].extend(section["ability_ids"])
                prior["label"] = ",".join(map(str, prior["chapters"]))
            else:
                sections.append(section)
        if strip_markup(self.en.get(card["TitleId"])) == "The Ring":
            for index, section in enumerate(sections, 1):
                section.update(kind="ring_level", label=str(index), ring_level=index)
        return sections

    def record(self, gid):
        if gid in self._records:
            return self._records[gid]
        card = self.cards[gid]
        abilities = [self._ability(a, t) for a, t in ability_pairs(card["AbilityIds"])]
        family, role, layout = classify(card, abilities, self.en)
        group = self.group(gid)
        sections = self.display_sections(card, abilities)
        intrinsic_mana = []
        if 5 in numbers(card["Types"]):
            for subtype in numbers(card["Subtypes"]):
                if subtype not in BASIC_LAND_MANA:
                    continue
                color, aid = BASIC_LAND_MANA[subtype]
                mana = self._ability(aid, self.abilities[aid]["TextId"])
                mana.update(origin="basic_land_type_rule_305.6", subtype_id=subtype, mana_color=color)
                intrinsic_mana.append(mana)
        if not sections:
            sections.extend({"kind": "intrinsic_mana", "label": "", "ability_ids": [a["ability_id"]],
                             "text_en": a["text_en"], "text_zh": a["text_zh"], "origin": a["origin"]}
                            for a in intrinsic_mana)
        stats = {"power": None, "toughness": None, "starting_loyalty": None, "defense": None}
        if layout == "planeswalker":
            # A planeswalker-to-planeswalker transforming back face may have no
            # printed starting loyalty: inherit counters, never manufacture a 0.
            stats["starting_loyalty"] = card["Toughness"] or None
        elif layout == "battle":
            stats["defense"] = card["Toughness"]
        elif card["Power"] or card["Toughness"]:
            stats.update(power=card["Power"] or None, toughness=card["Toughness"] or None)
        record = {
            "schema_version": SCHEMA_VERSION, "card_id": gid, "art_id": card["ArtId"],
            "name_en": self.en.get(card["TitleId"]), "name_zh": self.zh.get(card["TitleId"]),
            "type_line_en": type_line(card, self.en), "type_line_zh": type_line(card, self.zh),
            "flavor_en": self.en.get(card["FlavorTextId"]), "flavor_zh": self.zh.get(card["FlavorTextId"]),
            "mana_cost_raw": card["OldSchoolManaText"],
            "power": card["Power"], "toughness": card["Toughness"], "stats": stats,
            "set_code": card["ExpansionCode"], "collector_number": card["CollectorNumber"],
            "abilities": abilities, "display_sections": sections,
            "intrinsic_mana_abilities": intrinsic_mana,
            "rules_en": "\n".join(a["text_en"] for a in abilities if a["text_en"]),
            "rules_zh": "\n".join(a["text_zh"] for a in abilities if a["text_zh"]),
            "display_rules_en": "\n".join(s["text_en"] for s in sections if s["text_en"]),
            "display_rules_zh": "\n".join(s["text_zh"] for s in sections if s["text_zh"]),
            "presentation": {"family": group["family"], "face_role": role, "layout": layout,
                             "is_physical_face": role not in ("spell", "prototype", "half", "door"),
                             "is_aggregate": role == "aggregate"},
            "group": group,
            "identity": {"base_printing_id": self.alt_base.get(gid, gid),
                         "title_id": card["InterchangeableTitleId"] or card["TitleId"],
                         "rebalanced_card_id": card["RebalancedCardGrpId"] or None},
            "source_card": card,
        }
        self._records[gid] = record
        return record

    def preferred_language(self, records, requested="zh"):
        if requested != "zh":
            return "en"
        for r in records:
            if (not r["name_zh"] or (r["presentation"]["layout"] not in ("auxiliary", "emblem")
                                     and not r["type_line_zh"])
                    or any(not s["text_zh"] for s in r["display_sections"])):
                return "en"
        return "zh"

    def audit(self):
        layouts, families, issues = Counter(), Counter(), []
        for gid, c in self.cards.items():
            if not c["ArtId"]:
                continue  # Wildcards have no card characteristics and are not playable cards.
            try:
                r = self.record(gid)
                layouts[r["presentation"]["layout"]] += 1
                families[r["group"]["family"]] += 1
                if r["presentation"]["layout"] == "unsupported":
                    issues.append({"card_id": gid, "issue": "unsupported_layout"})
                if r["group"]["family"] == "unsupported":
                    issues.append({"card_id": gid, "issue": "unknown_link_type"})
                if any(not s["text_en"] for s in r["display_sections"]):
                    issues.append({"card_id": gid, "issue": "missing_english_rules"})
            except Exception as exc:
                issues.append({"card_id": gid, "issue": type(exc).__name__, "message": str(exc)})
        return {"schema_version": SCHEMA_VERSION, "database": str(self.database),
                "total_rows": len(self.cards), "card_rows_with_art_id": sum(bool(c["ArtId"]) for c in self.cards.values()),
                "excluded_wildcards": [gid for gid, c in self.cards.items() if not c["ArtId"]],
                "linked_type_counts": dict(Counter(c["LinkedFaceType"] for c in self.cards.values())),
                "layouts": dict(layouts), "families": dict(families), "issues": issues,
                "scope": "Metadata and template classification only; this is not proof that every card image has rendered successfully."}
