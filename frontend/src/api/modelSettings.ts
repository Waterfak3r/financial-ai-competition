export type ModelSettingsSource = "local" | "environment" | "none";

export interface ModelSettings {
  base_url: string | null;
  model_name: string | null;
  api_key_configured: boolean;
  configured: boolean;
  source: ModelSettingsSource;
}

export interface ModelSettingsDraft {
  base_url: string;
  model_name: string;
  api_key?: string;
}

export interface ModelConnectionTestResult {
  success: boolean;
  code: string;
  message: string;
}

const MODEL_SETTINGS_PATH = "/api/v1/model-settings";
const MODEL_SETTINGS_TEST_PATH = `${MODEL_SETTINGS_PATH}/test`;

export class ModelSettingsApiError extends Error {
  constructor(message: string, readonly code: string | null = null) {
    super(message);
    this.name = "ModelSettingsApiError";
  }
}

export async function loadModelSettings(): Promise<ModelSettings> {
  const response = await fetch(MODEL_SETTINGS_PATH, {
    method: "GET",
    credentials: "omit",
    cache: "no-store",
  });
  return readSettingsResponse(response);
}

export async function saveModelSettings(draft: ModelSettingsDraft): Promise<ModelSettings> {
  const response = await fetch(MODEL_SETTINGS_PATH, {
    method: "PUT",
    credentials: "omit",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(draft),
  });
  return readSettingsResponse(response, draft.api_key);
}

export async function clearModelSettings(): Promise<ModelSettings> {
  const response = await fetch(MODEL_SETTINGS_PATH, {
    method: "DELETE",
    credentials: "omit",
    cache: "no-store",
  });
  return readSettingsResponse(response);
}

export async function testModelConnection(draft: ModelSettingsDraft): Promise<ModelConnectionTestResult> {
  const response = await fetch(MODEL_SETTINGS_TEST_PATH, {
    method: "POST",
    credentials: "omit",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(draft),
  });
  const payload = await readJson(response);
  if (!response.ok) {
    throw apiError(payload, "连接测试失败。", draft.api_key);
  }
  if (!isConnectionResult(payload)) {
    throw new ModelSettingsApiError("服务返回了无法识别的连接测试结果。", "invalid_response");
  }
  return {
    success: payload.success,
    code: payload.code,
    message: redactSecret(payload.message, draft.api_key),
  };
}

async function readSettingsResponse(response: Response, secret?: string): Promise<ModelSettings> {
  const payload = await readJson(response);
  if (!response.ok) {
    throw apiError(payload, "读取模型设置失败。", secret);
  }
  if (!isModelSettings(payload)) {
    throw new ModelSettingsApiError("服务返回了无法识别的模型设置。", "invalid_response");
  }
  return {
    base_url: payload.base_url,
    model_name: payload.model_name,
    api_key_configured: payload.api_key_configured,
    configured: payload.configured,
    source: payload.source,
  };
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new ModelSettingsApiError("服务暂时无法读取，请稍后重试。", "invalid_response");
  }
}

function apiError(payload: unknown, fallback: string, secret?: string): ModelSettingsApiError {
  if (isRecord(payload)) {
    const detail = payload.detail;
    if (isRecord(detail)) {
      const message = typeof detail.message === "string" ? detail.message : fallback;
      const code = typeof detail.code === "string" ? detail.code : null;
      return new ModelSettingsApiError(redactSecret(message, secret), code);
    }
    if (typeof detail === "string") {
      return new ModelSettingsApiError(redactSecret(detail, secret));
    }
  }
  return new ModelSettingsApiError(fallback);
}

function redactSecret(message: string, secret?: string): string {
  if (secret === undefined || secret.length === 0) {
    return message;
  }
  return message.split(secret).join("[已隐藏]");
}

function isModelSettings(value: unknown): value is ModelSettings {
  if (!isRecord(value)) {
    return false;
  }
  return (
    (value.base_url === null || typeof value.base_url === "string") &&
    (value.model_name === null || typeof value.model_name === "string") &&
    typeof value.api_key_configured === "boolean" &&
    typeof value.configured === "boolean" &&
    (value.source === "local" || value.source === "environment" || value.source === "none")
  );
}

function isConnectionResult(value: unknown): value is ModelConnectionTestResult {
  return (
    isRecord(value) &&
    typeof value.success === "boolean" &&
    typeof value.code === "string" &&
    typeof value.message === "string"
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
