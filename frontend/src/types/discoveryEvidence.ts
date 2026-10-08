import type { PdfBox } from "./annualReport";

export type AnnualDiscoveryStatus = "completed" | "partial" | "failed" | "not_called";
export type AnnualDiscoveryPriority = "high" | "medium" | "low";

export interface AnnualDiscoveryBatchResult {
  batch_index: number;
  status: "succeeded" | "partial" | "failed" | "unattempted";
  reason: string | null;
  pages: number[];
  raw_chars: number;
  accepted_findings?: number;
  rejected_findings?: number;
  restored_quotes?: number;
  inherited_findings?: number;
  resolution?: "executed" | "reused" | "unattempted";
}

export interface AnnualDiscoveryExecutionAttempt {
  attempt_index: number;
  audit_id: string;
  admission_id: string | null;
  provider_admitted: boolean;
  status: "succeeded" | "failed" | "rejected";
  response_accepted: boolean;
  accepted_findings: number;
  reason: string | null;
}

export interface AnnualDiscoveryExecutionBatch {
  batch_index: number;
  state: "succeeded" | "partial" | "failed" | "unattempted";
  resolution: "executed" | "reused" | "unattempted";
  reason: string | null;
  attempts: AnnualDiscoveryExecutionAttempt[];
}

export interface AnnualDiscoveryExecutionBindingV3 {
  source_sha256: string;
  company_id: string;
  report_year: number;
  source_document_id: string;
  analysis_mode: "model_investigation" | "integrated_analysis";
  provider_model_fingerprint: string;
  model: string;
  prompts_sha256: string;
  parser_version: string;
  plan_sha256: string;
  m3_binding_sha256: string | null;
}

export interface AnnualDiscoveryExecutionBindingV4 extends AnnualDiscoveryExecutionBindingV3 {
  plan_version: "annual_discovery_plan_v3";
  execution_version: "execution_v4";
}

export interface AnnualDiscoveryPhaseBudget {
  schema_version: "annual_analysis_budget_v1";
  scope: "new_run" | "resume";
  main_report_limit_seconds: 300;
  work_deadline_seconds: 285;
  finalization_reserve_seconds: 15;
  shared_goal_limit: number | null;
  shared_goal_remaining_at_start: number | null;
  task_provider_attempt_limit: 30 | 20;
  stage_time_limits_seconds: {
    annual_discovery: 150;
    annual_investigation: 60;
    annual_issue_grouping: 35;
    annual_review: 25;
  };
  stage_provider_attempt_limits: {
    annual_discovery: 18 | 14;
    annual_investigation: 2;
    annual_issue_grouping: 8 | 2;
    annual_review: 2;
  };
  stage_provider_attempt_limits_status: "configured";
}

export interface AnnualDiscoveryExecutionContractV3 {
  kind: "fintrace_annual_discovery_execution_v3";
  plan_sha256: string;
  identity_sha256: string;
  binding: AnnualDiscoveryExecutionBindingV3;
  parent_run_id: string | null;
  provider_admission_count: number;
  parent_provider_admission_count: number;
  cumulative_provider_admission_count: number;
  response_count: number;
  reused_batches: number[];
  batches: AnnualDiscoveryExecutionBatch[];
}

export interface AnnualDiscoveryExecutionContractV4 {
  kind: "fintrace_annual_discovery_execution_v4";
  plan_sha256: string;
  identity_sha256: string;
  binding: AnnualDiscoveryExecutionBindingV4;
  phase_budget: AnnualDiscoveryPhaseBudget;
  parent_run_id: string | null;
  provider_admission_count: number;
  parent_provider_admission_count: number;
  cumulative_provider_admission_count: number;
  response_count: number;
  reused_batches: number[];
  batches: AnnualDiscoveryExecutionBatch[];
  scheduler: AnnualDiscoveryExecutionScheduler;
  timing: AnnualDiscoveryExecutionTiming;
}

export interface AnnualDiscoveryExecutionScheduler {
  requested_max_concurrency: number;
  effective_max_concurrency: number;
  inflight_peak: number;
  checkpoint_coordinator_count: 1;
  checkpoint_publications: number;
  dispatch_closed_reason: string | null;
  time_deadline_reason: "discovery_stage_budget_exhausted" | "workflow_time_budget_exhausted" | null;
  inflight_drained: boolean;
  inflight_count: number;
}

export interface AnnualDiscoveryExecutionTiming {
  source_parse_seconds: number;
  planning_seconds: number;
  source_parse_reused: boolean;
  discovery_elapsed_seconds: number;
  main_work_remaining_seconds: number | null;
}

export type AnnualDiscoveryExecutionContract =
  | AnnualDiscoveryExecutionContractV3
  | AnnualDiscoveryExecutionContractV4;

export interface AnnualDiscoveryCoverage {
  total_pages: number;
  text_pages: number;
  total_blocks: number;
  total_chars: number;
  planned_batches: number;
  processed_batches: number;
  successful_batches: number;
  partial_batches?: number;
  failed_batches: number;
  batch_results: AnnualDiscoveryBatchResult[];
  processed_chars: number;
  attempted_chars: number;
  unattempted_batches: number;
  unattempted_chars: number;
  planned_chars: number;
  omitted_chars: number;
  unread_pages: number[];
  failed_pages: number[];
  no_text_pages: number[];
  image_pages: number[];
  unread_image_pages: number[];
  truncated: boolean;
  complete: boolean;
  source_status: "parsed" | "unavailable";
  content_status: "text_available" | "no_extractable_text" | "unavailable";
  time_budget_exhausted: boolean;
  text_only_scope: string;
}

export interface AnnualDiscoveryCitation {
  evidence_id: string;
  quote: string;
}

export interface AnnualDiscoveryFinding {
  finding_id: string;
  batch_index?: number;
  title: string;
  observation: string;
  why_it_matters: string;
  follow_up: string;
  priority: AnnualDiscoveryPriority;
  classification: "unverified_model_finding";
  verification_status: "unverified";
  evidence_ids: string[];
  citations: AnnualDiscoveryCitation[];
}

export interface AnnualDiscoveryEvidence {
  evidence_id: string;
  source_document_id: string;
  source_sha256: string;
  pdf_page: number;
  block_index: number;
  start_char: number;
  bbox: PdfBox;
  raw_text: string;
}

export interface AnnualDiscoveryEvidenceCitation {
  finding_id: string;
  quote: string;
}

export interface AnnualDiscoveryEvidenceIndexItem extends AnnualDiscoveryEvidence {
  finding_ids: string[];
  citations: AnnualDiscoveryEvidenceCitation[];
  preview_url: string;
}

export interface AnnualDiscoveryEvidenceIndex {
  interpretation_label: "年报原文定位，模型发现未经独立语义核验";
  items: AnnualDiscoveryEvidenceIndexItem[];
}

export interface AnnualModelDiscovery {
  kind: "fintrace_annual_discovery";
  run_id: string;
  company_id: string;
  report_year: number;
  source_document_id: string;
  source_sha256: string;
  status: AnnualDiscoveryStatus;
  prompt_version: "annual_discovery_v1" | "annual_discovery_v2" | "annual_discovery_v3";
  model_called: boolean;
  model_call_count: number;
  model_call_attempt_count: number;
  coverage: AnnualDiscoveryCoverage;
  findings: AnnualDiscoveryFinding[];
  evidences: AnnualDiscoveryEvidence[];
  audit_artifacts: AnnualDiscoveryAuditArtifact[];
  limitations: string[];
  reason: string | null;
  fraud_conclusion: null;
  execution_contract?: AnnualDiscoveryExecutionContract;
}

export interface AnnualDiscoveryAuditArtifact {
  batch_index: number;
  audit_dir: string | null;
  request_path: string | null;
  response_path: string | null;
  failure_path: string | null;
  status: "started" | "succeeded" | "failed" | "incomplete";
  prompt_version: "annual_discovery_v1" | "annual_discovery_v2" | "annual_discovery_v3";
}
