"""The capability ("agent") catalog: identity, defaults, and the I/O contract
each capability publishes (used by workflow validation and the Input/Output
panel). This is the single source of truth — routers must import from here
rather than declaring their own copies.
"""

# Capabilities that may start a workflow with no upstream agent feeding them
# (they ingest externally-supplied files/data rather than a prior step's output).
ENTRY_CAPABLE_CAPABILITY_IDS: set[str] = {"cap-doc-intel", "cap-data-intel"}

CAPABILITIES: list[dict] = [
    {
        "id": "cap-doc-intel", "name": "Document Intelligence",
        "description": "Parse and extract structured content from documents (PDF, DOCX, TXT)",
        "category": "core", "default_model": "gpt-4o", "default_tokens": 4000,
        "input_schema": [
            {"key": "source_documents", "type": "file[]", "required": True, "description": "Uploaded PDF/DOCX/TXT files to parse"},
        ],
        "output_schema": [
            {"key": "extracted_text", "type": "string", "required": True, "description": "Structured text extracted from documents"},
            {"key": "document_metadata", "type": "object", "required": False, "description": "Per-document metadata (page count, type, size)"},
        ],
    },
    {
        "id": "cap-data-intel", "name": "Data Intelligence",
        "description": "Parse and analyze structured data (CSV, XLSX)",
        "category": "core", "default_model": "gpt-4o", "default_tokens": 4000,
        "input_schema": [
            {"key": "source_dataset", "type": "file[]", "required": True, "description": "Uploaded CSV/XLSX files or connected data source"},
        ],
        "output_schema": [
            {"key": "structured_records", "type": "object[]", "required": True, "description": "Parsed structured records"},
            {"key": "data_summary", "type": "object", "required": False, "description": "Summary statistics of the dataset"},
        ],
    },
    {
        "id": "cap-policy-extract", "name": "Policy Extraction",
        "description": "Extract policies, rules, conditions, deadlines from documents",
        "category": "core", "default_model": "gpt-4o", "default_tokens": 6000,
        "input_schema": [
            {"key": "extracted_text", "type": "string", "required": True, "description": "Document text to extract policies from"},
        ],
        "output_schema": [
            {"key": "policies", "type": "object[]", "required": True, "description": "Extracted policy rules, conditions, and deadlines"},
        ],
    },
    {
        "id": "cap-control-map", "name": "Control Mapping",
        "description": "Map extracted policies to compliance controls",
        "category": "core", "default_model": "gpt-4o", "default_tokens": 6000,
        "input_schema": [
            {"key": "policies", "type": "object[]", "required": True, "description": "Policies to map to compliance controls"},
        ],
        "output_schema": [
            {"key": "control_mappings", "type": "object[]", "required": True, "description": "Mapping of policies to compliance controls"},
        ],
    },
    {
        "id": "cap-gap-detect", "name": "Gap Detection",
        "description": "Identify gaps between required controls and actual state",
        "category": "core", "default_model": "gpt-4o", "default_tokens": 8000,
        "input_schema": [
            {"key": "control_mappings", "type": "object[]", "required": False, "description": "Mapped controls to evaluate for gaps"},
            {"key": "structured_records", "type": "object[]", "required": False, "description": "Operational data to compare against required state"},
        ],
        "output_schema": [
            {"key": "gaps", "type": "object[]", "required": True, "description": "Identified gaps between required and actual state"},
        ],
    },
    {
        "id": "cap-risk-assess", "name": "Risk Assessment",
        "description": "Score and categorize identified risks",
        "category": "core", "default_model": "gpt-4o", "default_tokens": 6000,
        "input_schema": [
            {"key": "gaps", "type": "object[]", "required": False, "description": "Gaps or findings to score"},
        ],
        "output_schema": [
            {"key": "risk_scores", "type": "object[]", "required": True, "description": "Scored and categorized risks"},
        ],
    },
    {
        "id": "cap-recommend", "name": "Recommendation Engine",
        "description": "Generate actionable recommendations with priority and impact",
        "category": "core", "default_model": "gpt-4o", "default_tokens": 8000,
        "input_schema": [
            {"key": "risk_scores", "type": "object[]", "required": False, "description": "Risk findings to base recommendations on"},
        ],
        "output_schema": [
            {"key": "recommendations", "type": "object[]", "required": True, "description": "Actionable recommendations with priority and impact"},
        ],
    },
    {
        "id": "cap-exec-intel", "name": "Executive Intelligence",
        "description": "Generate executive-level summary with scores and key findings",
        "category": "core", "default_model": "gpt-4o", "default_tokens": 4000,
        "input_schema": [
            {"key": "recommendations", "type": "object[]", "required": False, "description": "Recommendations to summarize for executives"},
        ],
        "output_schema": [
            {"key": "executive_summary", "type": "object", "required": True, "description": "Executive-level summary with scores and key findings"},
        ],
    },
    {
        "id": "cap-cust-intel", "name": "Customer Intelligence",
        "description": "Analyze customer data, sentiment, churn signals",
        "category": "specialist", "default_model": "gpt-4o", "default_tokens": 6000,
        "input_schema": [
            {"key": "structured_records", "type": "object[]", "required": True, "description": "Customer data to analyze"},
        ],
        "output_schema": [
            {"key": "customer_segments", "type": "object[]", "required": True, "description": "Segmented customers by churn risk profile"},
        ],
    },
    {
        "id": "cap-rev-analysis", "name": "Revenue Analysis",
        "description": "Detect revenue leakage patterns and quantify impact",
        "category": "specialist", "default_model": "gpt-4o", "default_tokens": 6000,
        "input_schema": [
            {"key": "structured_records", "type": "object[]", "required": True, "description": "Financial/operational data to analyze"},
        ],
        "output_schema": [
            {"key": "leakage_patterns", "type": "object[]", "required": True, "description": "Detected revenue leakage patterns and quantified impact"},
        ],
    },
    {
        "id": "cap-compliance-check", "name": "Compliance Checker",
        "description": "Verify compliance against specific regulatory frameworks",
        "category": "specialist", "default_model": "gpt-4o", "default_tokens": 8000,
        "input_schema": [
            {"key": "control_mappings", "type": "object[]", "required": True, "description": "Mapped controls to verify"},
        ],
        "output_schema": [
            {"key": "compliance_status", "type": "object", "required": True, "description": "Compliance verification results against regulatory frameworks"},
        ],
    },
    {
        "id": "cap-csat-analyzer", "name": "CSAT Analyzer",
        "description": "Analyze satisfaction scores and identify drivers",
        "category": "specialist", "default_model": "gpt-4o", "default_tokens": 4000,
        "input_schema": [
            {"key": "structured_records", "type": "object[]", "required": True, "description": "Satisfaction survey data"},
        ],
        "output_schema": [
            {"key": "csat_drivers", "type": "object[]", "required": True, "description": "Identified satisfaction drivers and scores"},
        ],
    },
    {
        "id": "cap-offer-strategy", "name": "Offer Strategy",
        "description": "Generate customer retention offers based on risk profile",
        "category": "specialist", "default_model": "gpt-4o", "default_tokens": 4000,
        "input_schema": [
            {"key": "customer_segments", "type": "object[]", "required": True, "description": "Customer segments to target with offers"},
        ],
        "output_schema": [
            {"key": "retention_offers", "type": "object[]", "required": True, "description": "Generated personalized retention offers"},
        ],
    },
    {
        "id": "cap-policy-valid", "name": "Policy Validation",
        "description": "Validate outputs against organizational policies",
        "category": "governance", "default_model": "gpt-4o-mini", "default_tokens": 2000,
        "input_schema": [
            {"key": "retention_offers", "type": "object[]", "required": False, "description": "Proposed offers to validate"},
            {"key": "recommendations", "type": "object[]", "required": False, "description": "Proposed recommendations to validate"},
        ],
        "output_schema": [
            {"key": "validation_result", "type": "object", "required": True, "description": "Pass/fail validation against organizational policies"},
        ],
    },
    {
        "id": "cap-audit-log", "name": "Audit Logger",
        "description": "Generate structured audit entries for compliance",
        "category": "governance", "default_model": "gpt-4o-mini", "default_tokens": 1000,
        "input_schema": [
            {"key": "validation_result", "type": "object", "required": False, "description": "Validation outcome to log"},
        ],
        "output_schema": [
            {"key": "audit_entry", "type": "object", "required": True, "description": "Structured audit entry for compliance"},
        ],
    },
    {
        "id": "cap-decision-rec", "name": "Decision Recorder",
        "description": "Create formal decision records with evidence",
        "category": "governance", "default_model": "gpt-4o", "default_tokens": 4000,
        "input_schema": [
            {"key": "validation_result", "type": "object", "required": False, "description": "Validated decision to record"},
        ],
        "output_schema": [
            {"key": "decision_record", "type": "object", "required": True, "description": "Formal decision record with evidence"},
        ],
    },
    {
        "id": "cap-human-approve", "name": "Human Approval Gate",
        "description": "Pause workflow for explicit human approval",
        "category": "human-review", "default_model": "none", "default_tokens": 0,
        "input_schema": [
            {"key": "upstream_output", "type": "object", "required": True, "description": "Output of the prior step requiring human sign-off"},
        ],
        "output_schema": [
            {"key": "approval_decision", "type": "object", "required": True, "description": "Human approve/reject decision"},
        ],
    },
    {
        "id": "cap-human-review", "name": "Human Review Gate",
        "description": "Show output to human for review and optional editing",
        "category": "human-review", "default_model": "none", "default_tokens": 0,
        "input_schema": [
            {"key": "upstream_output", "type": "object", "required": True, "description": "Output of the prior step requiring human review"},
        ],
        "output_schema": [
            {"key": "reviewed_output", "type": "object", "required": True, "description": "Output after optional human edits"},
        ],
    },
    {
        "id": "cap-escalation", "name": "Escalation Gate",
        "description": "Route to senior reviewer based on risk threshold",
        "category": "human-review", "default_model": "none", "default_tokens": 0,
        "input_schema": [
            {"key": "risk_scores", "type": "object", "required": False, "description": "Risk score used to evaluate escalation threshold"},
        ],
        "output_schema": [
            {"key": "escalation_decision", "type": "object", "required": True, "description": "Routing decision for senior reviewer"},
        ],
    },
    # ── Revenue Leakage vertical slice (use-cases/revenue-leakage/AGENTS.md §2/§4) ──
    # Net-new registrations for stages with no existing catalog match. Named
    # after their WORKFLOW.md stage id (not "cap-" prefixed) to match the
    # spec's own capability ids verbatim. Deterministic, non-LLM execution for
    # these is wired in services/revenue_leakage_pipeline_adapters.py — this
    # catalog entry is only identity/defaults/I-O contract, same as every
    # other capability here.
    {
        "id": "data-intelligence", "name": "Data Intelligence",
        "description": "Ingest and aggregate Revenue Leakage order/refund/chargeback records into a customer-level activity summary",
        "category": "specialist", "default_model": "gpt-4o", "default_tokens": 4000,
        "input_schema": [
            {"key": "orders", "type": "object[]", "required": True, "description": "Order records"},
            {"key": "refunds", "type": "object[]", "required": True, "description": "Refund records"},
            {"key": "chargebacks", "type": "object[]", "required": True, "description": "Chargeback records"},
        ],
        "output_schema": [
            {"key": "at_risk_customers", "type": "object[]", "required": True, "description": "Customers with repeat refund/chargeback activity"},
        ],
    },
    {
        "id": "policy-extraction", "name": "Policy Extraction",
        "description": "Parse the Revenue Leakage refund/discount policy document into structured thresholds",
        "category": "core", "default_model": "gpt-4o", "default_tokens": 6000,
        "input_schema": [
            {"key": "policy_document", "type": "string", "required": True, "description": "Raw policy document text"},
        ],
        "output_schema": [
            {"key": "thresholds", "type": "object", "required": True, "description": "Parsed policy thresholds (return window, refund authorization tiers, discount ceilings, chargeback rate)"},
        ],
    },
    {
        "id": "billing-gap-analysis", "name": "Billing Gap Analysis",
        "description": "Compare billed amounts against contracted/invoiced terms to detect under-billing",
        "category": "specialist", "default_model": "gpt-4o", "default_tokens": 4000,
        "input_schema": [
            {"key": "invoices", "type": "object[]", "required": True, "description": "Invoice/contract billing records"},
        ],
        "output_schema": [
            {"key": "billing_gaps", "type": "object[]", "required": True, "description": "Detected under-billing findings, or an explicit unsupported result when no billing dataset is available"},
        ],
    },
    {
        "id": "pricing-anomaly-detection", "name": "Pricing Anomaly Detection",
        "description": "Flag order-level discounts that exceed the policy-defined ceiling for their category",
        "category": "specialist", "default_model": "gpt-4o", "default_tokens": 4000,
        "input_schema": [
            {"key": "orders", "type": "object[]", "required": True, "description": "Order records with discount_pct/discount_reason"},
            {"key": "thresholds", "type": "object", "required": True, "description": "Parsed policy discount ceilings"},
        ],
        "output_schema": [
            {"key": "pricing_anomalies", "type": "object[]", "required": True, "description": "Discount findings exceeding policy ceilings"},
        ],
    },
    {
        "id": "contract-compliance-check", "name": "Contract Compliance Check",
        "description": "Verify order/refund terms against signed contract terms",
        "category": "specialist", "default_model": "gpt-4o", "default_tokens": 4000,
        "input_schema": [
            {"key": "contracts", "type": "object[]", "required": True, "description": "Contract terms"},
        ],
        "output_schema": [
            {"key": "compliance_findings", "type": "object[]", "required": True, "description": "Detected contract compliance findings, or an explicit unsupported result when no contract dataset is available"},
        ],
    },
    {
        "id": "policy-evaluation", "name": "Policy Evaluation",
        "description": "Evaluate refund/chargeback records against parsed policy thresholds for return-window, authorization-tier and chargeback-rate compliance",
        "category": "governance", "default_model": "none", "default_tokens": 0,
        "input_schema": [
            {"key": "refunds", "type": "object[]", "required": True, "description": "Refund records"},
            {"key": "chargebacks", "type": "object[]", "required": True, "description": "Chargeback records"},
            {"key": "thresholds", "type": "object", "required": True, "description": "Parsed policy thresholds"},
        ],
        "output_schema": [
            {"key": "violations", "type": "object[]", "required": True, "description": "Policy compliance violations (return-window, refund-authorization, chargeback-rate)"},
        ],
    },
    {
        "id": "financial-impact-analysis", "name": "Financial Impact Analysis",
        "description": "Quantify recoverable vs. lost revenue from Policy Evaluation findings",
        "category": "specialist", "default_model": "gpt-4o", "default_tokens": 8000,
        "input_schema": [
            {"key": "violations", "type": "object[]", "required": True, "description": "Policy compliance violations to quantify"},
        ],
        "output_schema": [
            {"key": "estimated_revenue_loss", "type": "number", "required": True, "description": "Total estimated leakage"},
            {"key": "estimated_recoverable_revenue", "type": "number", "required": True, "description": "Portion recoverable via policy enforcement"},
        ],
    },
    # ── Customer Escalation Management vertical slice (uc-5) ──────────────
    # Net-new registrations for the four approved scenarios (Post-Resolution,
    # Repeated Contact, Policy-Driven, SLA/Aging). Deterministic execution is
    # wired in services/escalation_management_pipeline_adapters.py. Note
    # `cap-escalation` (above) is deliberately NOT reused as a detection
    # capability here — it is the platform's generic human-review routing
    # gate (category "human-review"), not a classification/detection engine;
    # these four capabilities are the actual detection/evaluation engines.
    {
        "id": "escalation-detection", "name": "Escalation Detection",
        "description": "Ingest customer cases and compute the structural signals (reopen references, dispute amounts, mandatory-trigger keywords, open-case age) each escalation scenario is evaluated against",
        "category": "specialist", "default_model": "gpt-4o", "default_tokens": 6000,
        "input_schema": [
            {"key": "cases", "type": "object[]", "required": True, "description": "Customer case/ticket records"},
        ],
        "output_schema": [
            {"key": "escalation_candidates", "type": "object[]", "required": True, "description": "Per-case structural signals feeding the Post-Resolution, Repeated Contact, Policy-Driven and SLA/Aging scenarios"},
        ],
    },
    {
        "id": "post-resolution-verification", "name": "Post-Resolution Verification",
        "description": "Detect cases where a prior case was marked resolved but a later case reopens the same issue",
        "category": "specialist", "default_model": "gpt-4o", "default_tokens": 4000,
        "input_schema": [
            {"key": "cases", "type": "object[]", "required": True, "description": "Customer case/ticket records, including related_case_id links"},
        ],
        "output_schema": [
            {"key": "post_resolution_findings", "type": "object[]", "required": True, "description": "Cases that reopen a previously resolved case's issue, with evidence"},
        ],
    },
    {
        "id": "case-aging-tracking", "name": "Case Aging & SLA Tracking",
        "description": "Compute open-case age and evaluate it against the documented resolution SLA threshold for the case's severity tier",
        "category": "specialist", "default_model": "gpt-4o", "default_tokens": 4000,
        "input_schema": [
            {"key": "open_cases", "type": "object[]", "required": True, "description": "Open case records with severity and opened_date"},
            {"key": "sla_reference_document", "type": "string", "required": True, "description": "Raw SLA reference document text"},
        ],
        "output_schema": [
            {"key": "aging_findings", "type": "object[]", "required": True, "description": "Cases whose age exceeds the SLA resolution threshold for their severity tier, or an explicit no-breach result"},
        ],
    },
    {
        "id": "escalation-policy-evaluation", "name": "Escalation Policy Evaluation",
        "description": "Parse the Customer Escalation Management policy document and evaluate escalation candidates against its mandatory triggers, reopen-handling and repeated-contact thresholds",
        "category": "governance", "default_model": "none", "default_tokens": 0,
        "input_schema": [
            {"key": "escalation_candidates", "type": "object[]", "required": True, "description": "Structural escalation signals to evaluate"},
            {"key": "policy_document", "type": "string", "required": True, "description": "Raw escalation policy document text"},
        ],
        "output_schema": [
            {"key": "policy_findings", "type": "object[]", "required": True, "description": "Cases that cross a policy-defined escalation threshold, with the triggering rule cited"},
        ],
    },
    # ── Operational Risk vertical slice (uc-6) ─────────────────────────────
    # Net-new registrations for the four approved scenarios (Process
    # Breakdown, Control Gap, Vendor & Third-Party, Incident & Near-Miss).
    # Deterministic execution is wired in
    # services/operational_risk_pipeline_adapters.py. Generic responsibilities
    # already covered by an existing capability (materiality/risk scoring,
    # decision recording, executive summary, the human-review gate) are
    # reused below via cap-risk-assess / cap-decision-rec / cap-exec-intel /
    # cap-human-review — no duplicate risk/decision/citation engine is
    # registered here.
    {
        "id": "operational-risk-intelligence", "name": "Operational Risk Intelligence",
        "description": "Ingest control matrix, vendor risk, incident, process event, and remediation action records and normalize them into per-scenario structural signals",
        "category": "specialist", "default_model": "gpt-4o", "default_tokens": 6000,
        "input_schema": [
            {"key": "control_matrix", "type": "object[]", "required": True, "description": "Control matrix rows (status, effectiveness, last tested date)"},
            {"key": "process_events", "type": "object[]", "required": True, "description": "Process execution event records"},
            {"key": "vendor_risk", "type": "object[]", "required": True, "description": "Vendor/third-party risk records"},
            {"key": "incidents", "type": "object[]", "required": True, "description": "Incident and near-miss records"},
            {"key": "remediation_actions", "type": "object[]", "required": True, "description": "Remediation register rows (owner, action, due date)"},
        ],
        "output_schema": [
            {"key": "risk_candidates", "type": "object", "required": True, "description": "Normalized per-source records feeding the four Operational Risk scenarios"},
        ],
    },
    {
        "id": "control-framework-evaluation", "name": "Control Framework Evaluation",
        "description": "Parse the Operational Risk control framework document and evaluate control matrix and process event records for Process Breakdown and Control Gap findings",
        "category": "governance", "default_model": "none", "default_tokens": 0,
        "input_schema": [
            {"key": "control_matrix", "type": "object[]", "required": True, "description": "Control matrix rows to evaluate"},
            {"key": "process_events", "type": "object[]", "required": True, "description": "Process events to evaluate"},
            {"key": "control_framework_document", "type": "string", "required": True, "description": "Raw control framework document text"},
        ],
        "output_schema": [
            {"key": "control_findings", "type": "object[]", "required": True, "description": "Process Breakdown and Control Gap findings, each tagged with its scenario and materiality"},
        ],
    },
    {
        "id": "vendor-risk-evaluation", "name": "Vendor & Third-Party Risk Evaluation",
        "description": "Parse the vendor/third-party requirements document and evaluate vendor risk records for expired certifications, missing attestations, and unresolved findings",
        "category": "governance", "default_model": "none", "default_tokens": 0,
        "input_schema": [
            {"key": "vendor_risk", "type": "object[]", "required": True, "description": "Vendor/third-party risk records to evaluate"},
            {"key": "vendor_requirements_document", "type": "string", "required": True, "description": "Raw vendor/third-party requirements document text"},
        ],
        "output_schema": [
            {"key": "vendor_findings", "type": "object[]", "required": True, "description": "Vendor & Third-Party findings with materiality"},
        ],
    },
    {
        "id": "incident-near-miss-evaluation", "name": "Incident & Near-Miss Evaluation",
        "description": "Parse the incident & near-miss policy document and evaluate incident/near-miss records for materiality, repeated failures, and delayed response",
        "category": "governance", "default_model": "none", "default_tokens": 0,
        "input_schema": [
            {"key": "incidents", "type": "object[]", "required": True, "description": "Incident/near-miss records to evaluate"},
            {"key": "incident_policy_document", "type": "string", "required": True, "description": "Raw incident & near-miss policy document text"},
        ],
        "output_schema": [
            {"key": "incident_findings", "type": "object[]", "required": True, "description": "Incident & Near-Miss findings with materiality"},
        ],
    },
    {
        "id": "remediation-tracking", "name": "Remediation Tracking",
        "description": "Attach accountable owner, remediation action, and due date to each governed Operational Risk finding from the remediation register, without inventing ownership where none is recorded",
        "category": "specialist", "default_model": "gpt-4o", "default_tokens": 4000,
        "input_schema": [
            {"key": "control_findings", "type": "object[]", "required": False, "description": "Process Breakdown / Control Gap findings"},
            {"key": "vendor_findings", "type": "object[]", "required": False, "description": "Vendor & Third-Party findings"},
            {"key": "incident_findings", "type": "object[]", "required": False, "description": "Incident & Near-Miss findings"},
            {"key": "remediation_actions", "type": "object[]", "required": True, "description": "Remediation register rows"},
        ],
        "output_schema": [
            {"key": "remediation_plan", "type": "object[]", "required": True, "description": "Per-finding owner/action/due-date, or an explicit unassigned status when no register row matches"},
        ],
    },
]

CAPABILITIES_BY_ID: dict[str, dict] = {c["id"]: c for c in CAPABILITIES}


def get_capability(capability_id: str) -> dict | None:
    return CAPABILITIES_BY_ID.get(capability_id)


def compatible_capabilities(capability_id: str) -> list[dict]:
    """Capabilities considered safe to swap in for the given one — same
    category, since that's what keeps an existing connection's data contract
    reasonable (e.g. one core ingestion step for another)."""
    current = get_capability(capability_id)
    if not current:
        return []
    return [c for c in CAPABILITIES if c["category"] == current["category"] and c["id"] != capability_id]
