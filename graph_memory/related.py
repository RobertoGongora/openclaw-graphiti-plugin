"""Bounded name-based recall expansion, shared by live and historical reads."""

import re

from .store import normalized

KINDS = frozenset({"decision", "topic"})
LIMIT = 8
SUBJECT_LIMIT = 12


def label(text):
    return normalized(text.replace("_", " ").replace("-", " "))


def root_names(roots):
    from .retrieval import ANCHOR_KINDS, GENERIC_NAMES

    # Ambiguous roots must not silently expand every interpretation.
    if len(roots) != 1 or roots[0]["kind"] not in ANCHOR_KINDS:
        return []
    root = roots[0]
    return sorted(
        {
            name
            for name in (label(root["name"]), label(root["key"].partition(":")[2]))
            if name not in GENERIC_NAMES
            and (len(name) >= 3 or (len(name) >= 2 and any(c.isdigit() for c in name)))
        }
    )


def decision_question(question):
    from .retrieval import tokens

    return bool(
        tokens(question or "")
        & {
            "decide",
            "decision",
            "decisions",
            "defer",
            "postpone",
            "postponed",
            "proceed",
            "approve",
            "approved",
            "approval",
            "cancel",
            "cancelled",
            "should",
            "plan",
            "plans",
            "planned",
            "planning",
            "decisión",
            "decisiones",
            "decidir",
            "aplazar",
            "posponer",
        }
    )


def select(roots, candidates, question):
    """Consider canonical names/keys only, never aliases or recursive neighbors.

    Selection bounds extra entities, not the result budget. Complete temporal
    roles still resolve before the ordinary shared ranker selects output facts.
    """
    from .retrieval import tokens

    names = root_names(roots)
    if not names or not decision_question(question):
        return []
    root_ids = {e["id"] for e in roots}
    terms = tokens(question) - tokens(" ".join(names))
    if not terms:
        return []
    matches = []
    for entity in candidates:
        if entity["id"] in root_ids or entity.get("merged_into") or entity["kind"] not in KINDS:
            continue
        fields = [label(entity["name"]), label(entity["key"].partition(":")[2])]
        if any(
            re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", field)
            for name in names
            for field in fields
        ):
            matches.append((len(terms & tokens(" ".join(fields))), entity))
    matches.sort(key=lambda item: (-item[0], item[1]["key"]))
    return [entity for _, entity in matches[:LIMIT]]


def subjects(seeds, question):
    """Load decision nodes, never every subject connected to a popular topic.

    A person's `decided` edge leads to its decision target, not all the person's
    unrelated preferences. A decision's outgoing state roles load completely.
    """
    from .retrieval import tokens

    terms = tokens(question)
    scores = {}
    for fact in seeds:
        score = len(
            terms
            & tokens(
                " ".join(
                    str(fact.get(k) or "")
                    for k in ("summary", "subject", "target", "relation", "slot")
                )
            )
        )
        if fact.get("subject_kind") == "decision":
            key = fact["subject_id"]
        elif fact.get("relation") == "decided" and fact.get("target_kind") == "decision":
            key = fact["target_id"]
        else:
            continue
        scores[key] = max(scores.get(key, 0), score)
    return sorted(scores, key=lambda key: (-scores[key], key))[:SUBJECT_LIMIT]
