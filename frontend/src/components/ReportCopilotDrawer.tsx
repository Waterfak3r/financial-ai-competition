import { useState, useRef, useEffect, KeyboardEvent } from "react";
import { askAnnualCopilot, CopilotMessage } from "../api/annualCopilot";
import "../styles/copilot-drawer.css";

interface ReportCopilotDrawerProps {
  runId: string;
  companyId?: string;
  reportYear?: number;
}

const DEFAULT_CHIPS = [
  "深入分析经营现金流与净利润背离的具体成因",
  "本期非经常性损益主要由哪些项目构成？具有可持续性吗？",
  "针对当前指标变化，建议审计人员下一步重点调取哪些科目附注与凭证？",
];

export function ReportCopilotDrawer({ runId, companyId, reportYear }: ReportCopilotDrawerProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [messages, setMessages] = useState<CopilotMessage[]>([]);
  const [inputVal, setInputVal] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [chips, setChips] = useState<string[]>(DEFAULT_CHIPS);
  const bodyRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom of chat when new message arrives
  useEffect(() => {
    if (bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [messages, busy]);

  const handleSend = async (questionText: string) => {
    const q = questionText.trim();
    if (!q || busy) return;

    setError(null);
    setInputVal("");
    const newHistory: CopilotMessage[] = [...messages, { role: "user", content: q }];
    setMessages(newHistory);
    setBusy(true);

    try {
      const res = await askAnnualCopilot(runId, q, messages);
      setMessages([...newHistory, { role: "assistant", content: res.answer }]);
      if (Array.isArray(res.suggested_prompts) && res.suggested_prompts.length > 0) {
        setChips(res.suggested_prompts);
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "请求失败，请稍后重试。";
      setError(msg);
      setMessages([
        ...newHistory,
        { role: "assistant", content: `⚠️ 抱歉，探查助手调用遇到异常：${msg}` },
      ]);
    } finally {
      setBusy(false);
    }
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void handleSend(inputVal);
    }
  };

  return (
    <>
      {/* Floating Action Trigger Button */}
      <button
        type="button"
        className="copilot-floating-trigger"
        onClick={() => setIsOpen(true)}
        aria-label="打开 AI 报告探查助手"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
          <path d="M8 10h.01M12 10h.01M16 10h.01" />
        </svg>
        <span>AI 报告探查助手</span>
      </button>

      {/* Slide-over Drawer Modal */}
      {isOpen ? (
        <>
          <div className="copilot-backdrop" onClick={() => setIsOpen(false)} aria-hidden="true" />
          <aside className="copilot-drawer" aria-label="AI 报告探查助手">
            {/* Header */}
            <div className="copilot-header">
              <div>
                <h3 className="copilot-header-title">
                  <span>AI 报告探查助手</span>
                  <span className="copilot-header-badge">资深法务会计视角</span>
                </h3>
                <p className="copilot-header-desc">
                  已绑定：{companyId ?? "当前公司"} · {reportYear ?? 2024} 年报独立核验事实
                </p>
              </div>
              <button
                type="button"
                className="copilot-close-btn"
                onClick={() => setIsOpen(false)}
                aria-label="关闭探查助手"
              >
                ✕
              </button>
            </div>

            {/* Chat Body */}
            <div className="copilot-body" ref={bodyRef}>
              <div className="copilot-welcome-card">
                <strong>专属于本份财报的智能探查助手</strong>
                回答严格限定于当前报告已独立核验的三表指标、背离异常及审计评审意见，支持穿透式业务归因与查账建议。
              </div>

              {/* Initial Suggestions */}
              {messages.length === 0 ? (
                <div className="copilot-suggestions">
                  <p className="copilot-suggestions-title">💡 推荐快捷探查问题</p>
                  {chips.map((chip, idx) => (
                    <button
                      key={idx}
                      type="button"
                      className="copilot-chip"
                      disabled={busy}
                      onClick={() => void handleSend(chip)}
                    >
                      {chip}
                    </button>
                  ))}
                </div>
              ) : null}

              {/* Message List */}
              {messages.map((m, idx) => (
                <div key={idx} className={`copilot-message-row is-${m.role}`}>
                  <span className="copilot-meta-tag">{m.role === "user" ? "分析师提问" : "FINTRACE 专家推理"}</span>
                  <div className="copilot-bubble">{m.content}</div>
                </div>
              ))}

              {/* Thinking Indicator */}
              {busy ? (
                <div className="copilot-message-row is-assistant">
                  <span className="copilot-meta-tag">FINTRACE 专家推理中</span>
                  <div className="copilot-thinking-bubble">
                    <span className="copilot-spinner" aria-hidden="true" />
                    <span>正在比对核验事实与勾稽关系，组织专业审计解答…</span>
                  </div>
                </div>
              ) : null}
            </div>

            {/* Footer / Input */}
            <div className="copilot-footer">
              {messages.length > 0 && !busy ? (
                <div className="copilot-suggestions" style={{ marginBottom: "0.25rem" }}>
                  <button
                    type="button"
                    className="copilot-chip"
                    style={{ fontSize: "0.76rem", padding: "0.25rem 0.5rem" }}
                    onClick={() => void handleSend("建议审计人员下一步重点调取哪些科目附注与凭证？")}
                  >
                    💡 继续追问：下一步重点调取哪些科目凭证？
                  </button>
                </div>
              ) : null}

              <div className="copilot-input-wrap">
                <textarea
                  className="copilot-textarea"
                  placeholder="针对本份财报提出具体财务或审计问题（按 Enter 发送）…"
                  value={inputVal}
                  disabled={busy}
                  onChange={(e) => setInputVal(e.target.value)}
                  onKeyDown={handleKeyDown}
                  rows={2}
                />
                <button
                  type="button"
                  className="copilot-send-btn"
                  disabled={busy || !inputVal.trim()}
                  onClick={() => void handleSend(inputVal)}
                >
                  {busy ? "探查中" : "发送"}
                </button>
              </div>

              <p className="copilot-input-hint">
                <span>基于当前报告已验证数据作答 · 绝不外发无关内容</span>
                {error ? <span style={{ color: "#ef4444" }}>调用异常</span> : null}
              </p>
            </div>
          </aside>
        </>
      ) : null}
    </>
  );
}
