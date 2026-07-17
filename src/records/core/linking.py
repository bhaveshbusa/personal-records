"""Entity linking — the pipeline's Link stage, deterministic and zero-LLM.

Ported from the old repo's `entity_linker.py` (2R.3). Matches a document's
extracted identifiers to the policy entity it evidences, so a renewal quote
or NCD letter lands on the same entity as the policy schedule it relates
to. Precedence kept from the old repo: exact policy_number match, then
normalised vehicle_registration match, then — only for document types that
establish a *new* entity (policy_schedule) — mint one.

Routing policy differs deliberately from the old repo in ONE case: a
no-match document is accepted UNLINKED rather than parked for review. In
the old model an unlinked record corrupted the entity table; in the new
event model an unlinked RenewalProposed is harmless (renewal_offers just
flags it not comparable), and the Phase 2 golden flow — a first clean
quote with no policy on file yet — must keep working. Ambiguity still
routes to review: misfiled is worse than unlinked. The linker itself is
pure and reports every status; routing lives in `review.rules`.

known_entities are built from the `current_policies` projection — linking
consumes the event log's read side, never the store.
"""

from __future__ import annotations

from dataclasses import dataclass

from records.core.model import Field
from records.core.projections import current_policies

LINK_POLICY_NUMBER = "policy_number"
LINK_VEHICLE_REG = "vehicle_reg"
LINK_NEW_ENTITY = "new_entity"

# LinkResult statuses.
LINKED = "linked"                  # exact match to one known entity
NEW_ENTITY = "new_entity"          # establishing doc_type minted an entity
NO_MATCH = "no_match"              # identifiers present, nothing matched
NO_IDENTIFIERS = "no_identifiers"  # nothing to match on
AMBIGUOUS = "ambiguous"            # multiple entities share the identifier

# Document types whose fact establishes a brand-new entity when no existing
# one matches. Everything else references an existing policy or stays
# unlinked.
NEW_ENTITY_DOC_TYPES = frozenset({"policy_schedule"})


@dataclass(frozen=True)
class LinkResult:
    status: str
    entity_id: str | None = None
    method: str | None = None
    reason: str = ""

    @property
    def linked(self) -> bool:
        return self.status in (LINKED, NEW_ENTITY)


def _raw(value):
    """Identifiers may arrive as typed `Field`s (extraction output) or bare
    values (tests, future callers) — compare on the raw value either way."""
    return value.value if isinstance(value, Field) else value


def _normalise_reg(reg) -> str | None:
    """Uppercase, strip spaces — 'ab12 cde' and 'AB12CDE' must compare equal."""
    reg = _raw(reg)
    if not reg:
        return None
    return str(reg).upper().replace(" ", "")


def known_entities(events) -> list[dict]:
    """The entities a document can link to, folded from the event log:
    one row per current policy with its matchable identifiers."""
    rows = []
    for policy in current_policies(events):
        fields = policy.get("fields", {})
        rows.append(
            {
                "entity_id": policy["entity_id"],
                "policy_number": _raw(fields.get("policy_number")),
                "vehicle_registration": _raw(fields.get("vehicle_registration")),
            }
        )
    return rows


def link_document(
    identifiers: dict, entities: list[dict], doc_type: str | None = None
) -> LinkResult:
    """Match a document's identifiers to a known entity. `identifiers` is a
    mapping with (optionally) policy_number / vehicle_registration, values
    either typed `Field`s or bare strings. Pure — no I/O, no LLM."""
    policy_number = _raw(identifiers.get("policy_number"))
    if policy_number:
        matches = [e for e in entities if e.get("policy_number") == policy_number]
        if len(matches) == 1:
            return LinkResult(LINKED, matches[0]["entity_id"], LINK_POLICY_NUMBER)
        if len(matches) > 1:
            return LinkResult(
                AMBIGUOUS,
                reason="multiple known entities share this policy_number",
            )

    reg = _normalise_reg(identifiers.get("vehicle_registration"))
    if reg:
        matches = [
            e for e in entities if _normalise_reg(e.get("vehicle_registration")) == reg
        ]
        if len(matches) == 1:
            return LinkResult(LINKED, matches[0]["entity_id"], LINK_VEHICLE_REG)
        if len(matches) > 1:
            return LinkResult(
                AMBIGUOUS,
                reason="multiple known entities share this vehicle_registration",
            )

    if not policy_number and not reg:
        return LinkResult(NO_IDENTIFIERS, reason="no identifiers to match on")

    if doc_type in NEW_ENTITY_DOC_TYPES:
        return LinkResult(NEW_ENTITY, str(policy_number or reg), LINK_NEW_ENTITY)

    return LinkResult(NO_MATCH, reason="no matching entity found")
