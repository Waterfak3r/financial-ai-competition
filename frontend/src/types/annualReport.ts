export type AnnualReportRecord = Record<string, unknown>;

export interface AnnualAnalysisManifest {
  run_id: string;
  status: string;
  company_id: string;
  report_year: number;
  document_id: string;
  source_pdf_archive_path: string;
  report_json_path: string;
  source_pdf_sha256: string;
  report_sha256: string;
}

export interface PdfBox {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface EvidenceRegion {
  text?: string;
  page: number;
  bbox: PdfBox;
}

export interface VerificationEvidence {
  evidence_id: string;
  document_id: string;
  source_sha256: string;
  pdf_page: number;
  table_title?: string | null;
  row_label?: string | null;
  column_label?: string | null;
  value_raw?: string | null;
  value_normalized?: string | null;
  unit?: string | null;
  currency?: string | null;
  value_region: EvidenceRegion;
}

export interface MetricVerification {
  target_type: string;
  target_id: string;
  status: string;
  evidence_ids: string[];
}

export interface ConfirmedMetric {
  fact_id: string;
  metric_id?: string;
  company_id?: string;
  label_raw: string;
  raw_value: string;
  normalized_value: string;
  currency: string;
  unit_multiplier: string;
  unit: string | null;
  report_year: number;
  period_start: string | null;
  period_end: string | null;
  period_type?: string;
  statement_type?: string;
  scope: string;
  comparison_role: string;
  restatement_status: string;
  source_document_id: string;
  source_sha256: string;
  verification_evidences: VerificationEvidence[];
  verifications: MetricVerification[];
  limitations: string[];
}

export interface CalculationVerification {
  status: string;
  recomputed_value?: string | number | null;
  reason?: string | null;
  checks?: string[];
}

export interface ClaimVerification {
  claim_id: string;
  status: string;
  reason: string | null;
  expected_text: string | null;
}

export interface ConfirmedCalculation {
  calculation_id: string;
  formula_id: string;
  formula_expression: string;
  input_fact_ids: string[];
  output_value: string | number | null;
  unit: string | null;
  status: string;
  failure_reason: string | null;
  independent_verification: CalculationVerification | null;
}

export interface VerifiedClaim {
  claim_id: string;
  claim_type: string;
  text: string;
  supporting_fact_ids: string[];
  supporting_evidence_ids: string[];
  calculation_ids: string[];
  verification_status: string;
  limitations: string[];
  alternative_explanations: string[];
  follow_up_items: string[];
  independent_verification: ClaimVerification | null;
}

export interface CandidateSignal {
  signal_id: string;
  status: string;
  reason: string | null;
  input_fact_ids: string[];
  calculation_ids: string[];
  left_difference: string | number | null;
  right_difference: string | number | null;
  fraud_conclusion: string | null;
  placement_reasons: string[];
}

export interface M3AnnualScreeningRule {
  rule_id: string;
  rule_version: string;
  formula: string;
  threshold: string | null;
  points_if_triggered: number;
  points: number | null;
  status: string;
  triggered: boolean | null;
  calculated_value: string | null;
  issues: string[];
}

export interface M3AnnualScreening {
  kind: string;
  rule_version: string;
  status: string;
  screening_status: string;
  total_score: number | null;
  maximum_score: number;
  rules: M3AnnualScreeningRule[];
  limitations: string[];
}

export interface AnnualAnalysisReport {
  kind: "fintrace_annual_analysis_report";
  title?: string;
  run_id: string;
  company_id: string;
  report_year: number;
  source_document_id: string;
  source_sha256: string;
  fraud_conclusion: string | null;
  scope: {
    note: string;
    gaps: string[];
  };
  comparability: {
    status: string;
    restatement_status: string;
    limitations: string[];
  } | null;
  confirmed: {
    metrics: ConfirmedMetric[];
    analyses: ConfirmedCalculation[];
  };
  verified_claims: VerifiedClaim[];
  pending_review: {
    facts: AnnualReportRecord[];
    calculations: AnnualReportRecord[];
    claims: AnnualReportRecord[];
    candidate_signals: CandidateSignal[];
    uncited_evidences: AnnualReportRecord[];
  };
  limitations: string[];
  model_called: boolean;
  m3_screening?: M3AnnualScreening | null;
  model_investigation?: AnnualReportRecord | null;
}

export interface AnnualAnalysisResponse {
  report: AnnualAnalysisReport;
  manifest: AnnualAnalysisManifest;
  verification_evidences: VerificationEvidence[];
}
