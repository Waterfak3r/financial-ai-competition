import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import {
  clearModelSettings,
  loadModelSettings,
  ModelSettingsApiError,
  saveModelSettings,
  testModelConnection,
} from "../api/modelSettings";
import type { ModelSettings, ModelSettingsDraft } from "../api/modelSettings";

type SettingsAction = "loading" | "saving" | "testing" | "clearing" | null;
type FeedbackTone = "success" | "error" | "info";

interface SettingsFieldErrors {
  baseUrl?: string;
  modelName?: string;
  apiKey?: string;
}

interface SettingsSnapshot {
  baseUrl: string;
  modelName: string;
}

export function SettingsPage({
  onBack,
  onSaved,
}: {
  onBack: () => void;
  onSaved: (message: string) => void;
}) {
  const [settings, setSettings] = useState<ModelSettings | null>(null);
  const [snapshot, setSnapshot] = useState<SettingsSnapshot | null>(null);
  const [baseUrl, setBaseUrl] = useState("");
  const [modelName, setModelName] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [action, setAction] = useState<SettingsAction>("loading");
  const [fieldErrors, setFieldErrors] = useState<SettingsFieldErrors>({});
  const [feedback, setFeedback] = useState<{ tone: FeedbackTone; message: string } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    void readSettings();
  }, []);

  const busy = action !== null;
  const hasDraftKey = apiKey.trim().length > 0;
  const isDirty = snapshot !== null && (
    normalizedBaseUrl(baseUrl) !== normalizedBaseUrl(snapshot.baseUrl) ||
    modelName.trim() !== snapshot.modelName ||
    hasDraftKey
  );

  async function readSettings() {
    setAction("loading");
    setLoadError(null);
    try {
      const loaded = await loadModelSettings();
      setSettings(loaded);
      const nextBaseUrl = loaded.base_url ?? "";
      const nextModelName = loaded.model_name ?? "";
      setBaseUrl(nextBaseUrl);
      setModelName(nextModelName);
      setSnapshot({ baseUrl: nextBaseUrl, modelName: nextModelName });
      setApiKey("");
      setFieldErrors({});
    } catch (error) {
      setLoadError(messageFor(error, "无法读取模型设置，请重试。"));
    } finally {
      setAction(null);
    }
  }

  function updateBaseUrl(value: string) {
    setBaseUrl(value);
    setFieldErrors((current) => ({ ...current, baseUrl: undefined, apiKey: undefined }));
    setFeedback(null);
  }

  function updateModelName(value: string) {
    setModelName(value);
    setFieldErrors((current) => ({ ...current, modelName: undefined }));
    setFeedback(null);
  }

  function updateApiKey(value: string) {
    setApiKey(value);
    setFieldErrors((current) => ({ ...current, apiKey: undefined }));
    setFeedback(null);
  }

  function validate(): SettingsFieldErrors {
    const errors: SettingsFieldErrors = {};
    const normalizedUrl = normalizedBaseUrl(baseUrl);
    if (normalizedUrl.length === 0) {
      errors.baseUrl = "请填写兼容 Chat Completions 的服务根地址。";
    } else {
      try {
        const parsed = new URL(normalizedUrl);
        if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
          errors.baseUrl = "服务地址必须以 http:// 或 https:// 开头。";
        } else if (parsed.username.length > 0 || parsed.password.length > 0 || parsed.search.length > 0 || parsed.hash.length > 0) {
          errors.baseUrl = "服务地址只填写协议、主机和路径，不要放入账号、密钥、查询参数或片段。";
        } else if (parsed.pathname.replace(/\/+$/, "").toLowerCase().endsWith("/chat/completions")) {
          errors.baseUrl = "请填写服务根地址；系统会自动追加 /chat/completions。";
        }
      } catch {
        errors.baseUrl = "请输入完整的 http:// 或 https:// 服务地址。";
      }
    }
    if (modelName.trim().length === 0) {
      errors.modelName = "请填写服务支持的模型名称。";
    }

    const currentBaseUrl = snapshot?.baseUrl ?? "";
    const baseUrlChanged = normalizedBaseUrl(baseUrl) !== normalizedBaseUrl(currentBaseUrl);
    const keyConfigured = settings?.api_key_configured ?? false;
    if (hasDraftKey === false && (settings === null || !keyConfigured || baseUrlChanged)) {
      errors.apiKey = keyConfigured && baseUrlChanged
        ? "更换服务地址后，请填写新服务对应的 API 密钥。"
        : "当前没有可用的已配置密钥，请填写 API 密钥。";
    }
    return errors;
  }

  function validatedDraft(): ModelSettingsDraft | null {
    const errors = validate();
    setFieldErrors(errors);
    const firstError = errors.baseUrl !== undefined ? "model-settings-base-url"
      : errors.modelName !== undefined ? "model-settings-model-name"
        : errors.apiKey !== undefined ? "model-settings-api-key" : null;
    if (firstError !== null) {
      window.requestAnimationFrame(() => document.getElementById(firstError)?.focus());
      return null;
    }
    const draft: ModelSettingsDraft = {
      base_url: normalizedBaseUrl(baseUrl),
      model_name: modelName.trim(),
    };
    if (hasDraftKey) {
      draft.api_key = apiKey;
    }
    return draft;
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || !isDirty) {
      return;
    }
    const draft = validatedDraft();
    if (draft === null) {
      return;
    }
    setAction("saving");
    setFeedback(null);
    try {
      const saved = await saveModelSettings(draft);
      setSettings(saved);
      const nextBaseUrl = saved.base_url ?? draft.base_url;
      const nextModelName = saved.model_name ?? draft.model_name;
      setSnapshot({ baseUrl: nextBaseUrl, modelName: nextModelName });
      setApiKey("");
      onSaved("模型设置已保存，新启动的 AI 分析与评审将使用此配置。");
    } catch (error) {
      setFeedback({ tone: "error", message: messageFor(error, "保存失败，请检查设置后重试。") });
    } finally {
      setAction(null);
    }
  }

  async function onTestConnection() {
    if (busy) {
      return;
    }
    const draft = validatedDraft();
    if (draft === null) {
      return;
    }
    setAction("testing");
    setFeedback(null);
    try {
      const result = await testModelConnection(draft);
      setFeedback({
        tone: result.success ? "success" : "error",
        message: result.success
          ? `${result.message}本次测试使用当前表单内容，未保存设置。`
          : `${result.message}本次测试未保存设置。`,
      });
    } catch (error) {
      setFeedback({ tone: "error", message: `${messageFor(error, "连接测试失败，请检查服务地址、模型名称和密钥。")}本次测试未保存设置。` });
    } finally {
      setAction(null);
    }
  }

  async function onClearSettings() {
    const localSourceConfirmed = settings?.source === "local";
    const localConfigMayBeUnreadable = settings === null && loadError !== null;
    if (busy || (!localSourceConfirmed && !localConfigMayBeUnreadable)) {
      return;
    }
    setAction("clearing");
    setFeedback(null);
    try {
      const cleared = await clearModelSettings();
      const message = cleared.source === "environment"
        ? "本机模型设置已清除；如果服务环境中配置了模型连接信息，将回退到环境配置。"
        : "本机模型设置已清除，AI 服务状态正在重新检查。";
      onSaved(message);
    } catch (error) {
      setFeedback({ tone: "error", message: messageFor(error, "清除本机配置失败，请重试。") });
    } finally {
      setAction(null);
    }
  }

  if (settings === null) {
    return (
      <section className="settings-page" aria-busy={busy}>
        <div className="settings-heading">
          <p className="eyebrow">本机服务设置</p>
          <h1>模型连接</h1>
          <p className="lede">配置兼容 Chat Completions 的模型服务，保存后用于新启动的 AI 分析与评审。</p>
        </div>
        {loadError === null ? (
          <p className="settings-notice" role="status">正在读取本机模型设置…</p>
        ) : (
          <div className="settings-notice settings-notice-error" role="alert">
            <p>{loadError}</p>
            {feedback === null ? null : <p className="settings-load-feedback">{feedback.message}</p>}
            <div className="settings-load-actions">
              <button type="button" className="secondary" onClick={() => void readSettings()} disabled={busy}>重新读取</button>
              <button type="button" className="settings-clear-button" onClick={() => void onClearSettings()} disabled={busy}>
                {action === "clearing" ? "正在清除…" : "尝试清除本机配置"}
              </button>
            </div>
          </div>
        )}
        <button type="button" className="text-button settings-back-link" onClick={onBack}>返回分析年报</button>
      </section>
    );
  }

  return (
    <section className="settings-page" aria-busy={busy}>
      <div className="settings-heading">
        <p className="eyebrow">本机服务设置</p>
        <h1>模型连接</h1>
        <p className="lede">
          配置兼容 Chat Completions 的模型服务，保存后用于新启动的“AI 分析与评审”。
        </p>
      </div>

      <section className="panel settings-source-panel" aria-labelledby="settings-current-title">
        <div className="settings-section-heading">
          <div>
            <p className="eyebrow">当前状态</p>
            <h2 id="settings-current-title">连接信息</h2>
          </div>
          <span className={`settings-source-badge settings-source-${settings.source}`}>
            {sourceLabel(settings.source)}
          </span>
        </div>
        <dl className="settings-current-grid">
          <div><dt>服务地址</dt><dd>{settings.base_url ?? "尚未配置"}</dd></div>
          <div><dt>模型名称</dt><dd>{settings.model_name ?? "尚未配置"}</dd></div>
          <div><dt>API 密钥</dt><dd>{settings.api_key_configured ? "已配置（不显示密钥）" : "未配置"}</dd></div>
        </dl>
        <p className="help settings-security-note">
          密钥保存在本机后端。页面不会回显密钥，也不会把它存入浏览器缓存。
        </p>
      </section>

      <section className="panel settings-form-panel" aria-labelledby="settings-form-title">
        <div className="settings-section-heading">
          <div>
            <p className="eyebrow">服务配置</p>
            <h2 id="settings-form-title">填写连接信息</h2>
          </div>
          {isDirty ? <span className="settings-unsaved" role="status">有未保存修改</span> : null}
        </div>

        <form className="settings-form" onSubmit={(event) => void onSubmit(event)} noValidate>
          <div className="settings-field">
            <label htmlFor="model-settings-base-url">服务地址</label>
            <input
              id="model-settings-base-url"
              type="url"
              value={baseUrl}
              autoComplete="off"
              spellCheck={false}
              disabled={busy}
              aria-invalid={fieldErrors.baseUrl !== undefined}
              aria-describedby={fieldErrors.baseUrl === undefined ? "model-settings-base-url-help" : "model-settings-base-url-help model-settings-base-url-error"}
              placeholder="https://服务地址/v1"
              onChange={(event) => updateBaseUrl(event.target.value)}
            />
            <p id="model-settings-base-url-help" className="help">填写服务根地址；系统会自动追加 <code>/chat/completions</code>。不要把这段路径填入地址栏。</p>
            {fieldErrors.baseUrl === undefined ? null : <p id="model-settings-base-url-error" className="field-error" role="alert">{fieldErrors.baseUrl}</p>}
          </div>

          <div className="settings-field">
            <label htmlFor="model-settings-model-name">模型名称</label>
            <input
              id="model-settings-model-name"
              type="text"
              value={modelName}
              autoComplete="off"
              spellCheck={false}
              disabled={busy}
              aria-invalid={fieldErrors.modelName !== undefined}
              aria-describedby={fieldErrors.modelName === undefined ? "model-settings-model-name-help" : "model-settings-model-name-help model-settings-model-name-error"}
              placeholder="填写服务支持的模型名称"
              onChange={(event) => updateModelName(event.target.value)}
            />
            <p id="model-settings-model-name-help" className="help">名称由你填写的服务提供方支持；这里不预选供应方或型号。</p>
            {fieldErrors.modelName === undefined ? null : <p id="model-settings-model-name-error" className="field-error" role="alert">{fieldErrors.modelName}</p>}
          </div>

          <div className="settings-field">
            <label htmlFor="model-settings-api-key">API 密钥</label>
            <input
              id="model-settings-api-key"
              type="password"
              value={apiKey}
              autoComplete="new-password"
              spellCheck={false}
              disabled={busy}
              aria-invalid={fieldErrors.apiKey !== undefined}
              aria-describedby={fieldErrors.apiKey === undefined ? "model-settings-api-key-help" : "model-settings-api-key-help model-settings-api-key-error"}
              placeholder={settings.api_key_configured ? "留空以保留当前密钥" : "请输入 API 密钥"}
              onChange={(event) => updateApiKey(event.target.value)}
            />
            <p id="model-settings-api-key-help" className="help">
              {settings.api_key_configured
                ? "已配置密钥不会显示。地址未变时可以留空以保留；更换服务地址必须填写新密钥。"
                : "当前尚无可用密钥，请输入该服务提供的 API 密钥。"}
            </p>
            {fieldErrors.apiKey === undefined ? null : <p id="model-settings-api-key-error" className="field-error" role="alert">{fieldErrors.apiKey}</p>}
          </div>

          {feedback === null ? null : (
            <p className={`settings-feedback settings-feedback-${feedback.tone}`} role={feedback.tone === "error" ? "alert" : "status"}>
              {feedback.message}
            </p>
          )}

          <div className="settings-actions">
            <button type="submit" className="primary" disabled={busy || !isDirty}>
              {action === "saving" ? "正在保存…" : "保存设置并返回分析年报"}
            </button>
            <button type="button" className="secondary" onClick={() => void onTestConnection()} disabled={busy}>
              {action === "testing" ? "正在测试连接…" : "测试连接"}
            </button>
            <button type="button" className="settings-clear-button" onClick={() => void onClearSettings()} disabled={busy || settings.source !== "local"}>
              {action === "clearing" ? "正在清除…" : "清除本机配置"}
            </button>
          </div>
          <p className="help settings-clear-help">
              {settings.source === "local"
              ? "清除后，如服务环境变量中已有完整配置，会自动回退到环境配置。"
              : "只有当前配置来自本机设置文件时，才能从这里清除；环境配置由服务启动环境管理。"}
          </p>
          <div className="settings-footer-actions">
            <button type="button" className="text-button" onClick={onBack} disabled={busy}>返回分析年报</button>
          </div>
        </form>
      </section>
    </section>
  );
}

function sourceLabel(source: ModelSettings["source"]): string {
  switch (source) {
    case "local": return "本机已保存";
    case "environment": return "来自服务环境";
    case "none": return "尚未配置";
  }
}

function normalizedBaseUrl(value: string): string {
  return value.trim().replace(/\/+$/, "");
}

function messageFor(error: unknown, fallback: string): string {
  if (error instanceof ModelSettingsApiError) {
    return error.message;
  }
  if (error instanceof Error && error.message.trim().length > 0) {
    return error.message;
  }
  return fallback;
}
