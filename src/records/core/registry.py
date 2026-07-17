"""Schema registry — the closed set of document types this system files.

Restored from the old repo's `schema_registry.json` (Phase 2R.1), redesigned
onto the new domain model: frozen dataclasses instead of loose JSON, and an
explicit `quote_like` flag marking the types that go through the shape
classifier (line_count / renewal_status) and line-based extraction — the
MultiCover foundation. Everything else extracts flat canonical fields
against its schema.

Roles (ported semantics):
- fact_source     — extraction yields dated facts (premiums, pay, expiry...).
- proof_artifact  — proves something (certificate, NCD letter); light facts.
- reference_text  — read, not extracted (policy wording → Q&A index, 2R.4).
- raw_evidence    — stored as-is (claim photos, correspondence).

`valid_from_source` / `valid_to_source` / `provider_field` name canonical
fields whose values drive the current-record projections (2R.2) and entity
linking (2R.3) — restored with the registry so those slices port onto data
that is already here.

A document that matches nothing is "unknown" and routes to review:
misfiled is worse than unextracted.
"""

from __future__ import annotations

from dataclasses import dataclass

UNKNOWN_DOC_TYPE = "unknown"

FACT_SOURCE = "fact_source"
PROOF_ARTIFACT = "proof_artifact"
REFERENCE_TEXT = "reference_text"
RAW_EVIDENCE = "raw_evidence"

ROLES = (FACT_SOURCE, PROOF_ARTIFACT, REFERENCE_TEXT, RAW_EVIDENCE)


@dataclass(frozen=True)
class DocTypeSchema:
    """What the system knows about one document type, before ever seeing a
    document: how to recognise it (description), what to extract from it
    (canonical/required fields), and how its facts behave (role, validity)."""

    doc_type: str
    role: str
    description: str
    canonical_fields: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    quote_like: bool = False  # shape-classified + line-extracted (renewal path)
    valid_from_source: str | None = None
    valid_to_source: str | None = None
    provider_field: str | None = None


_SCHEMAS = (
    DocTypeSchema(
        doc_type="policy_schedule",
        role=FACT_SOURCE,
        description=(
            "The motor insurance policy schedule setting out cover, premium, and "
            "vehicle/driver details for the current policy period."
        ),
        canonical_fields=(
            "policy_end_date",
            "annual_premium",
            "policy_number",
            "policyholder_name",
            "provider",
            "period_start_date",
            "vehicle_registration",
            "named_drivers",
            "cover_level",
            "compulsory_excess",
            "voluntary_excess",
            "ncd_years",
        ),
        required=("policy_end_date", "annual_premium", "vehicle_registration"),
        valid_from_source="period_start_date",
        valid_to_source="policy_end_date",
        provider_field="provider",
    ),
    DocTypeSchema(
        doc_type="renewal_quote",
        role=FACT_SOURCE,
        description=(
            "An insurance renewal quote or renewal invitation proposing next period's "
            "premium and terms — a proposed future state, not yet the current policy. "
            "May cover one product or bundle several (e.g. motor + home)."
        ),
        # Extraction for quote_like types is line-based (ProductLine + stated_total,
        # the MultiCover model); these canonical fields are the document-level
        # identifiers entity linking (2R.3) resolves against.
        canonical_fields=(
            "quote_date",
            "period_start_date",
            "policy_number",
            "provider",
            "vehicle_registration",
        ),
        quote_like=True,
        valid_from_source="period_start_date",
        provider_field="provider",
    ),
    DocTypeSchema(
        doc_type="share_contract_note",
        role=FACT_SOURCE,
        description=(
            "A stockbroker contract note confirming a buy or sell trade, including "
            "price, quantity, and settlement details."
        ),
        canonical_fields=(
            "trade_direction",
            "instrument_identifier",
            "instrument_identifier_type",
            "price",
            "settlement_amount",
            "trade_date",
            "settlement_date",
            "quantity",
            "platform",
        ),
        required=("trade_direction", "instrument_identifier", "settlement_amount", "trade_date"),
        valid_from_source="trade_date",
        valid_to_source="trade_date",
        provider_field="platform",
    ),
    DocTypeSchema(
        doc_type="certificate",
        role=PROOF_ARTIFACT,
        description=(
            "A motor insurance certificate of cover proving legal insurance for a "
            "vehicle over a period."
        ),
        canonical_fields=(
            "policy_number",
            "vehicle_registration",
            "period_start_date",
            "policy_end_date",
            "provider",
        ),
        required=("policy_number",),
        valid_from_source="period_start_date",
        valid_to_source="policy_end_date",
        provider_field="provider",
    ),
    DocTypeSchema(
        doc_type="ncd_letter",
        role=PROOF_ARTIFACT,
        description=(
            "A no-claims-discount confirmation letter from an insurer stating years "
            "of NCD earned."
        ),
        canonical_fields=("ncd_years", "provider", "issue_date", "policy_number"),
        required=("ncd_years",),
        valid_from_source="issue_date",
        provider_field="provider",
    ),
    DocTypeSchema(
        doc_type="policy_wording",
        role=REFERENCE_TEXT,
        description=(
            "The full policy wording / IPID document setting out what is and isn't "
            "covered — a reference text, not a record of a specific policy."
        ),
    ),
    DocTypeSchema(
        doc_type="claim_evidence",
        role=RAW_EVIDENCE,
        description=(
            "Raw evidence supporting an insurance claim, e.g. accident photos or "
            "correspondence."
        ),
    ),
    DocTypeSchema(
        doc_type="eye_prescription",
        role=FACT_SOURCE,
        description=(
            "An optician's eye prescription recording lens measurements and "
            "prescription/expiry dates."
        ),
        canonical_fields=(
            "prescription_date",
            "expiry_date",
            "patient_name",
            "right_sphere",
            "right_cylinder",
            "right_axis",
            "left_sphere",
            "left_cylinder",
            "left_axis",
            "pupillary_distance",
            "optician",
        ),
        required=("prescription_date", "patient_name"),
        valid_from_source="prescription_date",
        valid_to_source="expiry_date",
        provider_field="optician",
    ),
    DocTypeSchema(
        doc_type="payslip",
        role=FACT_SOURCE,
        description=(
            "An employer payslip recording gross/net pay and deductions for one pay "
            "period."
        ),
        canonical_fields=(
            "pay_date",
            "pay_period",
            "gross_pay",
            "net_pay",
            "tax_deducted",
            "national_insurance",
            "employee_name",
            "employer_name",
        ),
        required=("pay_date", "gross_pay", "net_pay"),
        valid_from_source="pay_date",
        valid_to_source="pay_date",
        provider_field="employer_name",
    ),
    DocTypeSchema(
        doc_type="council_tax_bill",
        role=FACT_SOURCE,
        description=(
            "A local authority council tax bill for a property, showing the annual "
            "charge and instalments."
        ),
        canonical_fields=(
            "billing_year",
            "annual_amount",
            "property_band",
            "account_number",
            "property_address",
            "instalment_amount",
            "period_start_date",
            "period_end_date",
            "local_authority",
        ),
        required=("billing_year", "annual_amount"),
        valid_from_source="period_start_date",
        valid_to_source="period_end_date",
        provider_field="local_authority",
    ),
    DocTypeSchema(
        doc_type="utility_bill",
        role=FACT_SOURCE,
        description=(
            "A utility bill (gas/electricity/water) showing usage, billing period, "
            "and amount due."
        ),
        canonical_fields=(
            "billing_period_end",
            "amount_due",
            "utility_type",
            "account_number",
            "meter_reading",
            "due_date",
            "billing_period_start",
            "provider",
        ),
        required=("amount_due", "billing_period_end"),
        valid_from_source="billing_period_start",
        valid_to_source="billing_period_end",
        provider_field="provider",
    ),
    DocTypeSchema(
        doc_type="passport",
        role=FACT_SOURCE,
        description=(
            "A passport identity page recording passport number, holder details, and "
            "expiry date."
        ),
        canonical_fields=(
            "passport_number",
            "expiry_date",
            "date_of_issue",
            "full_name",
            "date_of_birth",
            "nationality",
            "issuing_authority",
        ),
        required=("passport_number", "expiry_date"),
        valid_from_source="date_of_issue",
        valid_to_source="expiry_date",
        provider_field="issuing_authority",
    ),
    DocTypeSchema(
        doc_type="vehicle_mot_certificate",
        role=FACT_SOURCE,
        description=(
            "An MOT test certificate recording a vehicle's test result, date, and "
            "expiry."
        ),
        canonical_fields=(
            "test_date",
            "expiry_date",
            "vehicle_registration",
            "test_result",
            "odometer_reading",
            "test_certificate_number",
            "test_station",
        ),
        required=("expiry_date", "vehicle_registration"),
        valid_from_source="test_date",
        valid_to_source="expiry_date",
        provider_field="test_station",
    ),
)

REGISTRY: dict[str, DocTypeSchema] = {s.doc_type: s for s in _SCHEMAS}
