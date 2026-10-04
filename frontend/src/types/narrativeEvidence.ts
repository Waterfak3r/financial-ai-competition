import type { PdfBox } from "./annualReport";

export const NARRATIVE_EVIDENCE_INTERPRETATION_LABEL = "原文片段定位/模型观点引用，非独立因果核验";
export const NARRATIVE_EVIDENCE_INTERPRETATION_CODE = "original_text_location_model_opinion_not_independent_causal_verification";

export interface NarrativeReadingSourceBlock {
  block_index: number;
  bbox: PdfBox;
  raw_text: string;
}

export interface NarrativeReadingContext {
  purpose: "display_only";
  source_pdf_sha256: string;
  pdf_page: number;
  text: string;
  bbox: PdfBox;
  expanded: boolean;
  limited: boolean;
  limit_reason: string | null;
  boundary_reason: string | null;
  citation_block_index: number;
  source_blocks: NarrativeReadingSourceBlock[];
}

export interface NarrativeEvidence {
  evidence_id: string;
  source_document_id: string;
  source_sha256: string;
  signal_ids: string[];
  section_label: string;
  pdf_page: number;
  bbox: PdfBox;
  raw_text: string;
  selection_reason: string;
  cited_by_signal_ids: string[];
  preview_url: string;
  reading_context?: NarrativeReadingContext;
}

export interface NarrativeEvidenceResponse {
  run_id: string;
  source_document_id: string;
  source_sha256: string;
  interpretation_label: typeof NARRATIVE_EVIDENCE_INTERPRETATION_LABEL;
  items: NarrativeEvidence[];
}
