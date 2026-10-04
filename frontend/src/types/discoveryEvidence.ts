import type { PdfBox } from "./annualReport";

export type AnnualDiscoveryStatus = "completed" | "partial" | "failed" | "not_called";
export type AnnualDiscoveryPriority = "high" | "medium" | "low";

export interface AnnualDiscoveryBatchResult {
  batch_index: number;
  status: "succeeded" | "failed" | "unattempted";
  reason: string | null;
  pages: number[];
  raw_chars: number;
}

export interface AnnualDiscoveryCoverage {
  total_pages: number;
  text_pages: number;
  total_blocks: number;
  total_chars: number;
  planned_batches: number;
  processed_batches: number;
  successful_batches: number;
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
  prompt_version: "annual_discovery_v1";
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
}

export interface AnnualDiscoveryAuditArtifact {
  batch_index: number;
  audit_dir: string | null;
  request_path: string | null;
  response_path: string | null;
  failure_path: string | null;
  status: "started" | "succeeded" | "failed" | "incomplete";
  prompt_version: "annual_discovery_v1";
}
