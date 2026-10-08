export type AnnualAnalysisJobMode =
  | "deterministic"
  | "m3_screening"
  | "model_investigation"
  | "integrated_analysis";

export type AnnualAnalysisJobStatus =
  | "queued"
  | "running"
  | "completed"
  | "completed_with_issues"
  | "failed"
  | "interrupted"
  | "cancelled";

export type AnnualAnalysisSnapshotState =
  | "running"
  | "cancelled"
  | "interrupted"
  | "failed"
  | "completed"
  | "completed_with_issues";

export interface AnnualAnalysisJobRequest {
  company_id: string;
  report_year: number;
  document_id: string;
  source_pdf_path: string;
  sha256: string;
  mode: AnnualAnalysisJobMode;
}

export interface AnnualAnalysisJobErrorDetail {
  code: string;
  message: string;
}

export interface AnnualAnalysisResultAvailability {
  status: "available" | "unavailable";
  category:
    | "archive_readable_after_failure"
    | "partial_checkpoint_available"
    | "completed_report_available"
    | "source_validation_failed"
    | "archive_incompatible"
    | "no_recovery_projection";
  run_id: string | null;
  result_url: string | null;
  message: string | null;
  snapshot_state?: AnnualAnalysisSnapshotState | null;
  is_partial?: boolean;
  snapshot_sequence?: number;
}

export type AnnualAnalysisRecoveryCapabilityReason =
  | "recoverable_batches"
  | "recovery_not_available"
  | "binding_missing"
  | "source_invalid"
  | "parent_active"
  | "already_has_active_child";

export interface AnnualAnalysisRecoveryCapability {
  can_resume: boolean;
  reason: AnnualAnalysisRecoveryCapabilityReason;
}

export interface AnnualAnalysisRecovery {
  contract_version: "fintrace_annual_recovery_v1";
  parent_run_id: string;
  reused_batches: number;
  current_attempts: number;
}

export interface AnnualAnalysisRecoveryProgress {
  contract_version: "fintrace_annual_recovery_v1";
  parent_run_id: string | null;
  parent_provider_admission_count: number;
  current_provider_admission_count: number;
  cumulative_provider_admission_count: number;
  reused_batch_count: number;
  updated_at: string;
}

export type AnnualAnalysisBudgetScope = "new_run" | "resume";

export type AnnualAnalysisBudgetStageStatus =
  | "not_started"
  | "planned"
  | "running"
  | "completed"
  | "partial"
  | "budget_exhausted"
  | "reserved";

export interface AnnualAnalysisStageBudget {
  time_limit_seconds: number;
  attempt_limit: number;
  planned_attempts: number | null;
  admitted_attempts: number;
  status: AnnualAnalysisBudgetStageStatus;
}

export interface AnnualAnalysisProviderAttemptLimits {
  task_default_new_run: 30;
  task_default_resume: 20;
  task_effective: 30 | 20;
  shared_goal_limit: number | null;
  shared_goal_remaining_at_start: number | null;
}

export interface AnnualAnalysisBudget {
  schema_version: "annual_analysis_budget_v1";
  scope: AnnualAnalysisBudgetScope;
  main_report_limit_seconds: number;
  work_deadline_seconds: number;
  finalization_reserve_seconds: 15;
  elapsed_running_seconds: number;
  queue_seconds: number;
  provider_attempt_limits: AnnualAnalysisProviderAttemptLimits;
  provider_admission_count: number;
  provider_attempts_remaining: number;
  stages: {
    annual_discovery: AnnualAnalysisStageBudget;
    annual_investigation: AnnualAnalysisStageBudget;
    annual_issue_grouping: AnnualAnalysisStageBudget;
    annual_review: AnnualAnalysisStageBudget;
  };
}

export interface AnnualAnalysisDiscoveryProgress {
  event: "planned" | "batch_started" | "batch_finished" | "budget_exhausted";
  status: "not_called" | "running" | "completed" | "partial" | "failed" | "budget_exhausted";
  batch_index: number;
  planned_batches: number;
  processed_batches: number;
  successful_batches: number;
  partial_batches: number;
  failed_batches: number;
  total_pages: number;
  total_chars: number;
  updated_at: string;
}

export interface AnnualAnalysisJob {
  job_id: string;
  status: AnnualAnalysisJobStatus;
  stage: string;
  created_at: string;
  updated_at: string;
  request: AnnualAnalysisJobRequest;
  run_id: string | null;
  result_url: string | null;
  error: AnnualAnalysisJobErrorDetail | null;
  result_availability?: AnnualAnalysisResultAvailability;
  discovery_progress?: AnnualAnalysisDiscoveryProgress;
  recovery?: AnnualAnalysisRecovery;
  recovery_progress?: AnnualAnalysisRecoveryProgress;
  recovery_capability?: AnnualAnalysisRecoveryCapability;
  analysis_budget?: AnnualAnalysisBudget;
}
