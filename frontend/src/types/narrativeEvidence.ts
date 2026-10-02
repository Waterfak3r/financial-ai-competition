import type { PdfBox } from "./annualReport";

export const NARRATIVE_EVIDENCE_INTERPRETATION_LABEL = "原文片段定位/模型观点引用，非独立因果核验";
export const NARRATIVE_EVIDENCE_INTERPRETATION_CODE = "original_text_location_model_opinion_not_independent_causal_verification";

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
}

export interface NarrativeEvidenceResponse {
  run_id: string;
  source_document_id: string;
  source_sha256: string;
  interpretation_label: typeof NARRATIVE_EVIDENCE_INTERPRETATION_LABEL;
  items: NarrativeEvidence[];
}
