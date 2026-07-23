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
import json
import math
import re
from html import unescape

ABILITY_NAMES = {1: "Strength", 2: "Dexterity", 3: "Constitution",
                  4: "Intelligence", 5: "Wisdom", 6: "Charisma"}
ABILITY_SLUGS = {1: "strength", 2: "dexterity", 3: "constitution",
                  4: "intelligence", 5: "wisdom", 6: "charisma"}

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


def total_character_level(data):
    return sum(c.get("level", 0) for c in data.get("classes", []))


def proficiency_bonus(level):
    return 2 + max(0, (level - 1) // 4)


def gather_classes(data):
    classes = []
    for c in data.get("classes", []):
        definition = c.get("definition") or {}
        subclass = c.get("subclassDefinition") or {}
        level = c.get("level", 0)
        features = []
        for feat in c.get("classFeatures", []):
            fdef = feat.get("definition", feat)
            req_level = fdef.get("requiredLevel")
            if req_level is not None and req_level > level:
                continue
            name = fdef.get("name")
            if name:
                features.append(name)
        classes.append({
            "name": definition.get("name"),
            "level": level,
            "subclass": subclass.get("name"),
            "hit_die": f"d{definition.get('hitDice')}" if definition.get("hitDice") else None,
            "is_starting_class": c.get("isStartingClass", False),
            "features_gained": features,
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


def gather_race(data):
    race = data.get("race") or {}
    traits = []
    for t in race.get("racialTraits", []):
        d = t.get("definition", t)
        name = d.get("name")
        if name and name.lower() not in ("size", "speed", "age", "alignment", "languages"):
            traits.append({"name": name, "summary": strip_html(d.get("snippet") or d.get("description"))})
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


def gather_feats(data):
    feats = []
    for f in data.get("feats", []):
        d = f.get("definition", f)
        feats.append({
            "name": d.get("name"),
            "summary": strip_html(d.get("snippet") or d.get("description")),
        })
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


def extract(raw):
    data = raw["data"]
    level = total_character_level(data)
    ability_scores = gather_ability_scores(data)
    proficiencies = gather_proficiencies(data)

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
        "proficiency_bonus": proficiency_bonus(level),
        "experience_points": data.get("currentXp"),
        "inspiration": data.get("inspiration", False),
        "race": gather_race(data),
        "background": gather_background(data),
        "classes": gather_classes(data),
        "ability_scores": ability_scores,
        "hit_points": gather_hit_points(data),
        "speed": gather_speed(data),
        "proficiencies": proficiencies,
        "passive_perception": 10 + ability_scores["Wisdom"]["modifier"] +
            (proficiency_bonus(level) if "Perception" in proficiencies["skills"]
             and proficiencies["skills"]["Perception"]["proficient"] else 0),
        "feats": gather_feats(data),
        "personality": gather_traits(data),
        "currency": data.get("currencies"),
        "inventory": gather_inventory(data),
        "spells": gather_spells(data),
        **gather_spell_slots(data),
        "notes": gather_notes(data),
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
    lines.append(f"HP: {hp['current']}/{hp['max']} (temp {hp['temporary']})")
    lines.append(f"Proficiency Bonus: +{c['proficiency_bonus']}")
    lines.append(f"Speed: {c['speed']}")
    lines.append(f"Passive Perception: {c['passive_perception']}")
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
