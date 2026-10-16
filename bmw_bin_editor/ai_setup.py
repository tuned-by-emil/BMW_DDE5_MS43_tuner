"""Small arithmetic-only hardware memory, sourced from user text in order.

The full conversation stays in the model prompt. These recognised facts only
choose deterministic tyre/differential/gearbox assumptions; they grant no patch
authority and never include earlier assistant answers.
"""
from __future__ import annotations
import re

FIELDS = ("tyres", "ratios", "gearboxes")
_TYRE = re.compile(r"\b(\d{3})[ \t]*/[ \t]*(\d{2})[ \t]*r[ \t]*(\d{2})\b", re.I)
_GEARBOX = re.compile(r"\b([4-9])[ \t]*hp[ \t]*(\d{2,3})\b", re.I)
_LABEL = r"(?:diff(?:erential)?(?:[ \t]+ratio)?|rear(?:[ \t-]+(?:end|axle))?(?:[ \t]+ratio)?|final[ \t]+drive(?:[ \t]+ratio)?)"
_NUMBER = r"(?P<ratio>[+-]?\d[.,]\d{1,3})(?!\d|[.,]\d)"
_DIFF = tuple(re.compile(p, re.I) for p in (
    r"\b"+_LABEL+r"\b[ \t]*(?:(?:is|of)[ \t]+|[=:][ \t]*)?"+_NUMBER,
    r"(?<![\w.,+-])"+_NUMBER+r"[ \t]*"+_LABEL+r"\b"))


def fact_matches(text):
    """Recognised values in source order, with identical grammar everywhere."""
    text = str(text or "")
    matches = {field: [] for field in FIELDS}
    for match in _TYRE.finditer(text):
        matches["tyres"].append((match.start(), tuple(map(int, match.groups()))))
    for pattern in _DIFF:
        for match in pattern.finditer(text):
            matches["ratios"].append((match.start(), float(match["ratio"].replace(",", "."))))
    for match in _GEARBOX.finditer(text):
        matches["gearboxes"].append((match.start(), match[1]+"hp"+match[2]))
    for field in FIELDS:
        matches[field].sort(key=lambda item: item[0])
    return matches


def setup_facts(text):
    return {field: {value for _, value in items} for field, items in fact_matches(text).items()}


def latest_facts(sources):
    """Sources are newest first; missing fields retain older still-current facts."""
    result = {field: None for field in FIELDS}
    for text in sources:
        matches = fact_matches(text)
        for field in FIELDS:
            if result[field] is None and matches[field]:
                result[field] = matches[field][0][1]
    return result


def compact_setup(sources):
    facts = latest_facts(sources)
    parts = []
    if facts["gearboxes"] is not None:
        parts.append(facts["gearboxes"].upper())
    if facts["ratios"] is not None:
        parts.append("final drive "+str(facts["ratios"]))
    if facts["tyres"] is not None:
        width, aspect, rim = facts["tyres"]
        parts.append(f"{width}/{aspect}R{rim}")
    return "; ".join(parts)


def resolve_conversation_setup(question, current_setup, captured_setup, conversation, initial_question=""):
    """Current question, edited field, recent corrections, then old fallbacks."""
    current_setup, captured_setup = str(current_setup or ""), str(captured_setup or "")
    prior = [turn.get("question", "") for turn in reversed(conversation) if isinstance(turn, dict)]
    sources = [question]
    if current_setup.strip() != captured_setup.strip():
        sources.append(current_setup)
    sources.extend(prior)
    sources.extend((initial_question, current_setup, captured_setup))
    return compact_setup(sources)


def calculation_override(setup, question, override):
    """Select current facts or a scenario explicitly present in this user turn."""
    requested = setup_facts(override)
    if not any(requested.values()):
        raise ValueError("Setup override needs a declared tyre size, differential ratio or gearbox; otherwise use null.")
    current = latest_facts([setup])
    explicit = setup_facts(question)
    for field, name in (("tyres", "tyre size"), ("ratios", "differential ratio"), ("gearboxes", "gearbox")):
        if len(requested[field]) > 1:
            raise ValueError("Select one "+name+" per calculation scenario.")
        allowed = set(explicit[field])
        if current[field] is not None:
            allowed.add(current[field])
        if not requested[field].issubset(allowed):
            raise ValueError("Setup override includes an undeclared or superseded "+name+". Use the current user facts or a scenario explicitly stated in this question.")
    return compact_setup([override, setup])
