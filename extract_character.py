#!/usr/bin/env python3
"""
Harvest the important, human-relevant fields from a DNDBeyond character JSON export
and write a condensed JSON (and optional plain-text summary), discarding the bulk
of the export (rules text, HTML descriptions, UI/config metadata, source book
references, etc.).

Usage:
    python3 extract_character.py Horns_EvoWizard.json
    python3 extract_character.py Horns_EvoWizard.json -o out.json --text summary.txt
"""

import argparse
import ast
import json
import math
import operator
import re
from html import unescape

ABILITY_NAMES = {1: "Strength", 2: "Dexterity", 3: "Constitution",
                  4: "Intelligence", 5: "Wisdom", 6: "Charisma"}
ABILITY_SLUGS = {1: "strength", 2: "dexterity", 3: "constitution",
                  4: "intelligence", 5: "wisdom", 6: "charisma"}
ABILITY_ABBR = {"str": "Strength", "dex": "Dexterity", "con": "Constitution",
                 "int": "Intelligence", "wis": "Wisdom", "cha": "Charisma"}

ALIGNMENTS = {
    1: "Lawful Good", 2: "Neutral Good", 3: "Chaotic Good",
    4: "Lawful Neutral", 5: "Neutral", 6: "Chaotic Neutral",
    7: "Lawful Evil", 8: "Neutral Evil", 9: "Chaotic Evil",
}

SKILL_ABILITY = {
    "acrobatics": "Dexterity", "animal-handling": "Wisdom", "arcana": "Intelligence",
    "athletics": "Strength", "deception": "Charisma", "history": "Intelligence",
    "insight": "Wisdom", "intimidation": "Charisma", "investigation": "Intelligence",
    "medicine": "Wisdom", "nature": "Intelligence", "perception": "Wisdom",
    "performance": "Charisma", "persuasion": "Charisma", "religion": "Intelligence",
    "sleight-of-hand": "Dexterity", "stealth": "Dexterity", "survival": "Wisdom",
}


def strip_html(text):
    """Collapse DDB's HTML-formatted rules text into plain, readable text."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_SAFE_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
}


def safe_eval_arithmetic(expr):
    """Evaluate a whitelisted arithmetic expression (+ - * / and parens on numeric
    literals only). Returns None instead of raising if the expression isn't purely
    arithmetic, so callers can fall back to leaving text unrendered."""
    try:
        node = ast.parse(expr, mode="eval").body
    except SyntaxError:
        return None

    def _eval(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _SAFE_OPS:
            return _SAFE_OPS[type(n.op)](_eval(n.left), _eval(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -_eval(n.operand)
        raise ValueError("disallowed expression")

    try:
        return _eval(node)
    except Exception:
        return None


def render_templates(text, proficiency, classlevel=None, ability_scores=None):
    """Render DDB's inline template placeholders, e.g. {{proficiency#unsigned}},
    {{13+proficiency#unsigned}}, {{modifier:cha#unsigned}}, {{(classlevel/2)@roundup}}.
    If a placeholder references a variable that isn't available in the current context
    (e.g. classlevel while rendering race/feat text with no specific class), the
    placeholder is left untouched rather than guessed at."""
    if not text or "{{" not in text:
        return text

    def replace(m):
        raw = m.group(1)
        func = None
        if "@" in raw:
            raw, func = raw.split("@", 1)
        if "#" in raw:
            raw, _fmt = raw.split("#", 1)  # only "unsigned" (bare number) observed; nothing else to special-case
        content = raw.strip()
        failed = False

        def sub_modifier(mm):
            nonlocal failed
            name = ABILITY_ABBR.get(mm.group(1).lower())
            mod = (ability_scores or {}).get(name, {}).get("modifier") if name else None
            if mod is None:
                failed = True
                return mm.group(0)
            return str(mod)

        content = re.sub(r"modifier:([a-zA-Z]+)", sub_modifier, content)

        if re.search(r"\bclasslevel\b", content):
            if classlevel is None:
                failed = True
            else:
                content = re.sub(r"\bclasslevel\b", str(classlevel), content)

        content = re.sub(r"\bproficiency\b", str(proficiency), content)

        if failed:
            return m.group(0)

        value = safe_eval_arithmetic(content)
        if value is None:
            return m.group(0)

        if func == "roundup":
            value = math.ceil(value)
        elif func == "rounddown":
            value = math.floor(value)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return str(value)

    return re.sub(r"\{\{([^}]+)\}\}", replace, text)


def modifier(score):
    return math.floor((score - 10) / 2)


def fmt_mod(m):
    return f"+{m}" if m >= 0 else str(m)


def iter_modifiers(data):
    """All modifier entries across every category (race/class/background/item/feat/condition)."""
    for category, mods in (data.get("modifiers") or {}).items():
        for m in mods:
            yield category, m


def gather_ability_scores(data):
    base = {s["id"]: s["value"] for s in data.get("stats", [])}
    bonus = {s["id"]: s["value"] for s in data.get("bonusStats", [])}
    override = {s["id"]: s["value"] for s in data.get("overrideStats", [])}

    # Ability-score bumps granted via feats/backgrounds show up as "bonus" modifiers,
    # e.g. subType "intelligence-score" with a value.
    extra_bonus = {slug: 0 for slug in ABILITY_SLUGS.values()}
    for _category, m in iter_modifiers(data):
        if m.get("type") == "bonus" and m.get("subType", "").endswith("-score"):
            ability_slug = m["subType"][: -len("-score")]
            if ability_slug in extra_bonus:
                extra_bonus[ability_slug] += m.get("value") or 0

    scores = {}
    for aid, name in ABILITY_NAMES.items():
        slug = ABILITY_SLUGS[aid]
        if override.get(aid) is not None:
            total = override[aid]
        else:
            total = (base.get(aid) or 0) + (bonus.get(aid) or 0) + extra_bonus[slug]
        scores[name] = {"score": total, "modifier": modifier(total)}
    return scores


def build_choice_index(data):
    """Cross-reference data.choices.<category> (which records a componentId + the
    optionValue actually picked) against data.options.<category> (per-category resolved
    option definitions) and, as a fallback, data.choices.choiceDefinitions (a global
    id->label pool keyed by "<componentTypeId>-<type>", used for plain skill/ability/
    tool/language/size picks that DDB doesn't emit a category-specific option row for).

    The lookup key (componentId) is the *defining entity's own id* - a racial trait's
    definition id, a feat's definition id, or a class feature's id - not the id of the
    granted instance itself (e.g. a feat's own componentId in data.feats is unrelated;
    it's definition.id that lines up with componentId here).

    Returns: dict[componentId] -> list of {"resolved_name": str|None, "label": str|None,
    "source_category": category, "resolution_source": "options"|"choiceDefinitions"|None}
    A None resolved_name means DDB recorded a choice slot but no resolvable value for it
    (optionValue is null, or it points at an id absent from both fallback tables) -
    genuinely unresolved in source, not something to guess at.
    """
    choices = data.get("choices") or {}
    options = data.get("options") or {}

    options_index = {}
    for category, opts in options.items():
        if not opts:
            continue
        for o in opts:
            cid = o.get("componentId")
            defn = o.get("definition") or {}
            options_index.setdefault(cid, {})[defn.get("id")] = defn.get("name")

    definitions_index = {}
    for grp in choices.get("choiceDefinitions") or []:
        definitions_index[grp.get("id")] = {o["id"]: o["label"] for o in grp.get("options", [])}

    index = {}
    for category in ("race", "class", "background", "item", "feat"):
        for c in (choices.get(category) or []):
            cid = c.get("componentId")
            option_value = c.get("optionValue")
            resolved_name = None
            source = None
            if option_value is not None:
                by_def_id = options_index.get(cid, {})
                if option_value in by_def_id:
                    resolved_name = by_def_id[option_value]
                    source = "options"
                else:
                    pool = definitions_index.get(f"{c.get('componentTypeId')}-{c.get('type')}", {})
                    if option_value in pool:
                        resolved_name = pool[option_value]
                        source = "choiceDefinitions"
            index.setdefault(cid, []).append({
                "label": c.get("label"),
                "resolved_name": resolved_name,
                "source_category": category,
                "resolution_source": source,
            })
    return index


def resolve_choice(index, component_id):
    """Look up every choice recorded against a component id. Returns (status, picks):
      ("resolved", [names])            - at least one recorded choice has a resolved name
      ("unresolved_in_source", [])     - choice slot(s) exist but no name could be resolved
      (None, [])                       - no choice at all recorded for this component
                                          (nothing to pick - not every trait/feature/feat has one)
    Callers that know a feature hasn't been reached yet (e.g. a level-3 subclass pick on a
    level-1 character) should relabel an "unresolved_in_source" result as "not_yet_available"
    themselves, since this function has no notion of character level."""
    entries = index.get(component_id)
    if not entries:
        return None, []
    picks = [e["resolved_name"] for e in entries if e["resolved_name"] is not None]
    if picks:
        return "resolved", picks
    return "unresolved_in_source", []


def total_character_level(data):
    return sum(c.get("level", 0) for c in data.get("classes", []))


def proficiency_bonus(level):
    return 2 + max(0, (level - 1) // 4)


def gather_classes(data, choice_index, proficiency, ability_scores):
    classes = []
    for c in data.get("classes", []):
        definition = c.get("definition") or {}
        subclass = c.get("subclassDefinition") or {}
        level = c.get("level", 0)

        features_gained = []
        pending_choices = []
        for feat in c.get("classFeatures", []):
            fdef = feat.get("definition", feat)
            name = fdef.get("name")
            if not name:
                continue
            req_level = fdef.get("requiredLevel")
            status, picks = resolve_choice(choice_index, fdef.get("id"))

            if req_level is not None and req_level > level:
                # Not gained yet at the character's current level. Still surface a
                # feature with a pending choice (e.g. a level-3 subclass pick) instead
                # of letting it silently vanish, so it reads as "not chosen yet" rather
                # than looking like a gap in the extraction.
                if status is not None:
                    pending_choices.append({
                        "name": name, "required_level": req_level,
                        "status": "not_yet_available", "picks": picks,
                    })
                continue

            summary = render_templates(strip_html(fdef.get("snippet") or fdef.get("description")),
                                        proficiency, classlevel=level, ability_scores=ability_scores)
            entry = {"name": name, "summary": summary}
            if status is not None:
                entry["choice"] = {"status": status, "picks": picks}
            features_gained.append(entry)

        classes.append({
            "name": definition.get("name"),
            "level": level,
            "subclass": subclass.get("name"),
            "hit_die": f"d{definition.get('hitDice')}" if definition.get("hitDice") else None,
            "is_starting_class": c.get("isStartingClass", False),
            "features_gained": features_gained,
            "pending_choices": pending_choices,
        })
    return classes


def gather_proficiencies(data):
    """Split proficiency modifiers into saving throws, skills, tools, weapons, armor."""
    saving_throws, skills, tools, weapons, armor, languages, resistances = ([] for _ in range(7))
    expertise_skills = set()

    for _category, m in iter_modifiers(data):
        mtype = m.get("type")
        sub = m.get("subType") or ""
        if mtype == "proficiency":
            if sub.endswith("-saving-throws"):
                saving_throws.append(sub.replace("-saving-throws", "").replace("-", " ").title())
            elif sub in SKILL_ABILITY:
                skills.append(sub)
            elif "weapon" in sub or sub.endswith("-weapons"):
                weapons.append(m.get("friendlySubtypeName") or sub)
            elif "armor" in sub or "shield" in sub:
                armor.append(m.get("friendlySubtypeName") or sub)
            else:
                tools.append(m.get("friendlySubtypeName") or sub)
        elif mtype == "expertise" and sub in SKILL_ABILITY:
            expertise_skills.add(sub)
        elif mtype == "language":
            languages.append(m.get("friendlySubtypeName") or sub.title())
        elif mtype == "resistance":
            resistances.append(m.get("friendlySubtypeName") or sub.title())

    skills_out = {}
    for slug in sorted(set(skills) | expertise_skills):
        skills_out[slug.replace("-", " ").title()] = {
            "ability": SKILL_ABILITY.get(slug, "?"),
            "proficient": slug in skills or slug in expertise_skills,
            "expertise": slug in expertise_skills,
        }

    return {
        "saving_throws": sorted(set(saving_throws)),
        "skills": skills_out,
        "tools": sorted(set(tools)),
        "weapons": sorted(set(weapons)),
        "armor": sorted(set(armor)),
        "languages": sorted(set(languages)),
        "damage_resistances": sorted(set(resistances)),
    }


def gather_hit_points(data):
    base = data.get("baseHitPoints") or 0
    bonus = data.get("bonusHitPoints") or 0
    override = data.get("overrideHitPoints")
    removed = data.get("removedHitPoints") or 0
    max_hp = override if override is not None else base + bonus
    return {
        "max": max_hp,
        "current": max_hp - removed,
        "temporary": data.get("temporaryHitPoints") or 0,
    }


def gather_speed(data):
    race = data.get("race") or {}
    speeds = ((race.get("weightSpeeds") or {}).get("normal")) or {}
    return {k: v for k, v in speeds.items() if v}


def gather_size(data):
    race = data.get("race") or {}
    if race.get("size"):
        return race["size"]
    for _category, m in iter_modifiers(data):
        if m.get("type") == "size":
            return m.get("friendlySubtypeName") or (m.get("subType") or "").title()
    return None


def gather_race(data, choice_index, proficiency, ability_scores):
    race = data.get("race") or {}
    traits = []
    for t in race.get("racialTraits", []):
        d = t.get("definition", t)
        name = d.get("name")
        if not name or name.lower() in ("size", "speed", "age", "alignment", "languages"):
            continue
        summary = render_templates(strip_html(d.get("snippet") or d.get("description")),
                                    proficiency, ability_scores=ability_scores)
        trait = {"name": name, "summary": summary}
        status, picks = resolve_choice(choice_index, d.get("id"))
        if status is not None:
            trait["choice"] = {"status": status, "picks": picks}
        traits.append(trait)
    return {
        "name": race.get("fullName") or race.get("baseRaceName"),
        "size": gather_size(data),
        "speed": gather_speed(data),
        "traits": traits,
    }


def gather_background(data):
    bg = (data.get("background") or {}).get("definition") or {}
    return {
        "name": bg.get("name"),
        "skill_proficiencies": bg.get("skillProficienciesDescription"),
        "tool_proficiencies": bg.get("toolProficienciesDescription"),
        "languages": bg.get("languagesDescription"),
        "feature_name": bg.get("featureName"),
    }


def gather_feats(data, choice_index, proficiency, ability_scores):
    feats = []
    for f in data.get("feats", []):
        d = f.get("definition", f)
        summary = render_templates(strip_html(d.get("snippet") or d.get("description")),
                                    proficiency, ability_scores=ability_scores)
        tags = [c.get("tagName") for c in (d.get("categories") or []) if c.get("tagName")]
        feat = {
            "name": d.get("name"),
            "summary": summary,
            "is_origin_feat": "Origin" in tags,
            "tags": tags,
        }
        status, picks = resolve_choice(choice_index, d.get("id"))
        if status is not None:
            feat["choice"] = {"status": status, "picks": picks}
        feats.append(feat)
    return feats


def gather_inventory(data):
    items = []
    for item in data.get("inventory", []):
        d = item.get("definition") or {}
        items.append({
            "name": d.get("name"),
            "quantity": item.get("quantity"),
            "equipped": item.get("equipped", False),
            "attuned": item.get("isAttuned", False),
            "weight": d.get("weight"),
            "cost_gp": d.get("cost"),
            "type": d.get("type"),
            "rarity": d.get("rarity"),
            "damage": (d.get("damage") or {}).get("diceString"),
            "damage_type": d.get("damageType"),
            "armor_class": d.get("armorClass"),
        })
    return items


def gather_spells(data):
    """Combine every spell source (race/class/background/item/feat + per-class spell lists)."""
    result = {}

    def add(source_name, spell_entries):
        bucket = result.setdefault(source_name, [])
        for s in spell_entries:
            d = s.get("definition") or {}
            bucket.append({
                "name": d.get("name"),
                "level": d.get("level"),
                "school": d.get("school"),
                "concentration": d.get("concentration", False),
                "ritual": d.get("ritual", False),
                "prepared": s.get("prepared", s.get("alwaysPrepared", False)),
            })

    spells = data.get("spells") or {}
    for source_name, spell_entries in spells.items():
        if spell_entries:
            add(source_name, spell_entries)

    class_id_to_name = {c["id"]: c["definition"]["name"] for c in data.get("classes", [])}
    for entry in data.get("classSpells", []):
        class_name = class_id_to_name.get(entry.get("characterClassId"), "Class")
        add(class_name, entry.get("spells", []))

    for source_name in result:
        result[source_name].sort(key=lambda x: (x["level"] or 0, x["name"] or ""))

    return result


def gather_spell_slots(data):
    slots = {s["level"]: s["available"] for s in data.get("spellSlots", []) if s.get("available")}
    pact = {s["level"]: s["available"] for s in data.get("pactMagic", []) if s.get("available")}
    out = {}
    if slots:
        out["spell_slots"] = slots
    if pact:
        out["pact_magic_slots"] = pact
    return out


def gather_traits(data):
    t = data.get("traits") or {}
    return {k: v for k, v in t.items() if v}


def gather_notes(data):
    notes = data.get("notes") or {}
    return {k: v for k, v in notes.items() if v}


def gather_computed_stats(data, ability_scores, proficiency, proficiencies):
    """Derived combat/spellcasting numbers, kept separate from the raw source fields
    above them. Everything here is *computed* by this script, not read verbatim from
    the export - flag accordingly rather than presenting it as DDB-confirmed."""
    dex_mod = ability_scores["Dexterity"]["modifier"]

    equipped_armor = None
    equipped_shield = False
    for item in data.get("inventory", []):
        if not item.get("equipped"):
            continue
        d = item.get("definition") or {}
        item_type = d.get("type") or ""
        filter_type = d.get("filterType") or ""
        if "Shield" in item_type or filter_type == "Shield":
            equipped_shield = True
        elif "Armor" in item_type or filter_type == "Armor":
            equipped_armor = d

    armor_class = {}
    if equipped_armor is None:
        armor_class = {"value": 10 + dex_mod, "basis": "unarmored"}
    else:
        base_ac = equipped_armor.get("armorClass") or 10
        # armorTypeId (1=light, 2=medium, 3=heavy) is the reliable signal here - the
        # "type" string is only populated for heavy armor in observed exports and is
        # empty for both light and medium, so it can't distinguish the two.
        armor_type_id = equipped_armor.get("armorTypeId")
        if armor_type_id == 3:
            value = base_ac
        elif armor_type_id == 2:
            value = base_ac + min(dex_mod, 2)
        elif armor_type_id == 1:
            value = base_ac + dex_mod
        else:
            value = base_ac + dex_mod
        armor_class = {"value": value, "basis": equipped_armor.get("name")}
        if armor_type_id not in (1, 2, 3):
            armor_class["verified"] = False
            armor_class["note"] = (
                f"Equipped armor ({equipped_armor.get('name')!r}) has no recognized "
                f"armorTypeId ({armor_type_id!r}); fell back to an uncapped Dex modifier, "
                "which is only correct for light armor. Recheck this item's data before "
                "trusting this AC value."
            )
    if equipped_shield:
        armor_class["value"] += 2
        armor_class["shield"] = True

    initiative = dex_mod
    for _category, m in iter_modifiers(data):
        if m.get("type") == "bonus" and m.get("subType") == "initiative":
            initiative += m.get("value") or 0

    spellcasting = []
    for c in data.get("classes", []):
        definition = c.get("definition") or {}
        ability_id = definition.get("spellCastingAbilityId")
        if not ability_id:
            continue
        ability_name = ABILITY_NAMES.get(ability_id)
        mod = ability_scores.get(ability_name, {}).get("modifier", 0)
        spellcasting.append({
            "class": definition.get("name"),
            "ability": ability_name,
            "spell_save_dc": 8 + proficiency + mod,
            "spell_attack_bonus": proficiency + mod,
        })

    perception = proficiencies["skills"].get("Perception")
    passive_perception = 10 + ability_scores["Wisdom"]["modifier"] + (proficiency if perception and perception["proficient"] else 0)

    return {
        "passive_perception": passive_perception,
        "armor_class": armor_class,
        "initiative": initiative,
        "spellcasting": spellcasting,
    }


def extract(raw):
    data = raw["data"]
    level = total_character_level(data)
    proficiency = proficiency_bonus(level)
    ability_scores = gather_ability_scores(data)
    proficiencies = gather_proficiencies(data)
    choice_index = build_choice_index(data)

    character = {
        "name": data.get("name"),
        "gender": data.get("gender"),
        "alignment": ALIGNMENTS.get(data.get("alignmentId")),
        "faith": data.get("faith") or None,
        "age": data.get("age"),
        "appearance": {
            "hair": data.get("hair"),
            "eyes": data.get("eyes"),
            "skin": data.get("skin"),
            "height": data.get("height"),
            "weight": data.get("weight"),
        },
        "level": level,
        "proficiency_bonus": proficiency,
        "experience_points": data.get("currentXp"),
        "inspiration": data.get("inspiration", False),
        "race": gather_race(data, choice_index, proficiency, ability_scores),
        "background": gather_background(data),
        "classes": gather_classes(data, choice_index, proficiency, ability_scores),
        "ability_scores": ability_scores,
        "hit_points": gather_hit_points(data),
        "speed": gather_speed(data),
        "proficiencies": proficiencies,
        "feats": gather_feats(data, choice_index, proficiency, ability_scores),
        "personality": gather_traits(data),
        "currency": data.get("currencies"),
        "inventory": gather_inventory(data),
        "spells": gather_spells(data),
        **gather_spell_slots(data),
        "notes": gather_notes(data),
        "computed_stats": gather_computed_stats(data, ability_scores, proficiency, proficiencies),
    }
    return character


def to_text_summary(c):
    lines = []
    lines.append(f"{c['name']}  —  Level {c['level']} " +
                 "/".join(f"{cl['name']}" + (f" ({cl['subclass']})" if cl['subclass'] else "") for cl in c["classes"]))
    lines.append(f"{c['race']['name']}, {c['background']['name']} background, {c['alignment']}")
    lines.append("")
    lines.append("Ability Scores:")
    for name, s in c["ability_scores"].items():
        lines.append(f"  {name:<13} {s['score']:>2} ({fmt_mod(s['modifier'])})")
    lines.append("")
    hp = c["hit_points"]
    stats = c["computed_stats"]
    lines.append(f"HP: {hp['current']}/{hp['max']} (temp {hp['temporary']})")
    lines.append(f"Proficiency Bonus: +{c['proficiency_bonus']}")
    lines.append(f"Speed: {c['speed']}")
    ac = stats["armor_class"]
    ac_note = "" if ac.get("verified", True) else "  (! unverified capping logic, see JSON)"
    lines.append(f"Armor Class: {ac['value']} ({ac['basis']}){ac_note}")
    lines.append(f"Initiative: {fmt_mod(stats['initiative'])}")
    lines.append(f"Passive Perception: {stats['passive_perception']}")
    for sc in stats["spellcasting"]:
        lines.append(f"Spell Save DC ({sc['class']}): {sc['spell_save_dc']}  |  Spell Attack: {fmt_mod(sc['spell_attack_bonus'])}")
    lines.append("")
    lines.append("Saving Throw Proficiencies: " + ", ".join(c["proficiencies"]["saving_throws"]))
    prof_skills = [k for k, v in c["proficiencies"]["skills"].items() if v["proficient"]]
    lines.append("Skill Proficiencies: " + ", ".join(prof_skills))
    if c["feats"]:
        lines.append("")
        lines.append("Feats: " + ", ".join(f["name"] for f in c["feats"]))
    lines.append("")
    lines.append(f"Currency: {c['currency']}")
    lines.append("")
    lines.append("Inventory:")
    for item in c["inventory"]:
        flag = " (equipped)" if item["equipped"] else ""
        lines.append(f"  {item['quantity']}x {item['name']}{flag}")
    if c["spells"]:
        lines.append("")
        lines.append("Spells:")
        for source, spell_list in c["spells"].items():
            lines.append(f"  From {source}:")
            for s in spell_list:
                lines.append(f"    [{s['level']}] {s['name']}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Path to a DNDBeyond character JSON export")
    parser.add_argument("-o", "--output", help="Path to write condensed JSON (default: <input>_extracted.json)")
    parser.add_argument("--text", help="Optional path to also write a plain-text summary")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        raw = json.load(f)

    character = extract(raw)

    out_path = args.output or re.sub(r"\.json$", "", args.input) + "_extracted.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(character, f, indent=2)
    print(f"Wrote {out_path}")

    if args.text:
        with open(args.text, "w", encoding="utf-8") as f:
            f.write(to_text_summary(character))
        print(f"Wrote {args.text}")


if __name__ == "__main__":
    main()
