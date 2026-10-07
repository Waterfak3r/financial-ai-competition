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
}
