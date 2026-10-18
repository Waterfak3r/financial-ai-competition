export interface QuarterlySourceDocument {
  document_id: string;
  company_id: string;
  source_pdf: string;
  source_sha256: string;
  source_url: string;
  publication_date: string;
  report_year: number;
  report_period: "Q1" | "H1" | "9M" | "FY";
  nature: string;
  use_conditions: string;
}

export interface QuarterlyVerification {
  verification_id: string;
  target_type: string;
  target_id: string;
  status: "verified" | "conflict" | "insufficient_evidence";
  checks: string[];
  conflicts: string[];
  evidence_ids: string[];
  limitations: string[];
  verified_at: string;
  details?: Record<string, unknown>;
}

export interface QuarterlyProof {
  proof_id: string;
  proof_type: string;
  target_id: string;
  source_sha256s: string[];
  evidence_ids: string[];
  binding_sha256: string;
  issued_at: string;
  status: string;
}

export interface QuarterlyDerivedQuarter {
  derived_id: string;
  metric_id: "revenue" | "net_profit_parent" | "operating_cash_flow" | string;
  company_id: string;
  year: number;
  quarter: "Q1" | "Q2" | "Q3" | "Q4";
  normalized_value: string;
  currency: string;
  unit: string;
  unit_multiplier: string;
  statement_type: string;
  scope: string;
  formula_id: string;
  formula_expression: string;
  input_observation_ids: string[];
  input_source_document_ids: string[];
  limitations: string[];
  period_start?: string;
  period_end?: string;
  verification?: QuarterlyVerification;
  proof?: QuarterlyProof;
}

export interface QuarterlyComparison {
  comparison_id: string;
  metric_id: "revenue" | "net_profit_parent" | "operating_cash_flow" | string;
  relation: "year_over_year" | "quarter_over_quarter";
  current_id: string;
  prior_id: string;
  current_value: string | null;
  prior_value: string | null;
  difference: string | null;
  growth_rate: string | null;
  status: "calculated" | "abstained" | "failed";
  failure_reason?: string | null;
  limitations: string[];
  comparability?: QuarterlyVerification;
  verification?: QuarterlyVerification;
  proof?: QuarterlyProof;
}

export interface QuarterlyObservation {
  observation_id: string;
  metric_id: string;
  company_id: string;
  report_year: number;
  report_period: string;
  observation_year: number;
  column_role: string;
  basis: string;
  statement_type: string;
  scope: string;
  unit_multiplier: string;
  source_document_id: string;
  verification?: QuarterlyVerification;
  proof?: QuarterlyProof;
}

export interface QuarterlyAnalysisResult {
  schema_version: string;
  run_id: string;
  created_at: string;
  status: "completed" | "completed_with_abstentions" | "failed";
  company_id: string;
  target_year: number;
  target_quarter: string;
  requested_relations: string[];
  dataset?: {
    dataset_id?: string;
    nature?: string;
    use_conditions?: string;
    manifest_path?: string;
  };
  source_documents: QuarterlySourceDocument[];
  source_observations: QuarterlyObservation[];
  source_issues?: unknown[];
  derived_quarters: QuarterlyDerivedQuarter[];
  comparisons: QuarterlyComparison[];
  limitations: string[];
  provenance?: {
    engine?: string;
    python_version?: string;
  };
}
