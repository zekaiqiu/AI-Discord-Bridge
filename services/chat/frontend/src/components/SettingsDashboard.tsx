/**
 * Settings panel — sidebar overlay (same `.dashboard` shell as
 * AgentsDashboard / UsageDashboard). Reads the user's settings from
 * /api/settings on mount, debounces saves through PUT /api/settings,
 * and reports the merged result back to the App via onChange so other
 * UI (theme attr, composer defaults, notification toggles) updates
 * without a page reload.
 */
import { useCallback, useEffect, useState } from "react";
import {
  DEFAULT_USER_SETTINGS,
  LANGUAGE_LABELS,
  LanguageChoice,
  ModelChoice,
  ThemeChoice,
  UserSettings,
  WindowLayout,
  getMemory,
  getSettings,
  putMemory,
  putSettings,
} from "../api";
import { useT } from "../i18n";

const MEMORY_MAX_CHARS = 8000;

export interface SettingsDashboardProps {
  onClose: () => void;
  visible: boolean;
  /** Called whenever a save succeeds with the freshly-coerced server result. */
  onChange?: (next: UserSettings) => void;
}

export function SettingsDashboard({
  onClose,
  visible,
  onChange,
}: SettingsDashboardProps): JSX.Element {
  const t = useT();
  const [settings, setSettings] = useState<UserSettings>(DEFAULT_USER_SETTINGS);
  const [loading, setLoading] = useState<boolean>(true);
  const [saving, setSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // Load on first reveal
  useEffect(() => {
    if (!visible) return;
    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        const s = await getSettings();
        if (!cancelled) {
          setSettings(s);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [visible]);

  // Patch helper — optimistically updates local state, then PUTs.
  // On failure restores the previous value and surfaces the error.
  const patch = useCallback(
    async (delta: Partial<UserSettings>) => {
      const prev = settings;
      const optimistic = { ...prev, ...delta };
      setSettings(optimistic);
      setSaving(true);
      try {
        const result = await putSettings(delta);
        setSettings(result);
        setError(null);
        onChange?.(result);
      } catch (err) {
        setSettings(prev);
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setSaving(false);
      }
    },
    [settings, onChange],
  );

  // Persona is a textarea — we want to debounce so we're not PUT-ing
  // on every keystroke. Hold local value until blur, then save.
  const [personaDraft, setPersonaDraft] = useState<string>("");
  useEffect(() => {
    setPersonaDraft(settings.persona);
  }, [settings.persona]);

  // Cross-session memory. Loaded separately from settings (different
  // endpoint, larger payload). Editable by the user; auto-updated by
  // the model via the <memory_update> protocol after each turn. We
  // refetch on every panel open so a model-driven update from another
  // tab/turn shows up correctly.
  const [memoryText, setMemoryText] = useState<string>("");
  const [memoryDraft, setMemoryDraft] = useState<string>("");
  const [memorySaving, setMemorySaving] = useState<boolean>(false);
  const [memoryError, setMemoryError] = useState<string | null>(null);
  useEffect(() => {
    if (!visible) return;
    let cancelled = false;
    (async () => {
      try {
        const m = await getMemory();
        if (!cancelled) {
          setMemoryText(m.text);
          setMemoryDraft(m.text);
          setMemoryError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setMemoryError(err instanceof Error ? err.message : String(err));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [visible]);

  const saveMemory = useCallback(
    async (next: string) => {
      if (next === memoryText) return;
      const prev = memoryText;
      setMemoryText(next);
      setMemorySaving(true);
      try {
        const result = await putMemory(next);
        setMemoryText(result.text);
        setMemoryDraft(result.text);
        setMemoryError(null);
      } catch (err) {
        setMemoryText(prev);
        setMemoryDraft(prev);
        setMemoryError(err instanceof Error ? err.message : String(err));
      } finally {
        setMemorySaving(false);
      }
    },
    [memoryText],
  );

  return (
    <div className="dashboard" role="dialog" aria-label={t("settings.title")}>
      <header className="dashboard-header">
        <button
          type="button"
          className="dashboard-back"
          onClick={onClose}
          aria-label={t("settings.close_aria")}
          title={t("settings.close_title")}
        >
          ←
        </button>
        <h2 className="dashboard-title">{t("settings.title")}</h2>
        {saving && (
          <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--text-secondary)" }}>
            {t("settings.saving")}
          </span>
        )}
      </header>
      <div className="dashboard-body">
        {loading ? (
          <div className="dashboard-empty">{t("settings.loading")}</div>
        ) : (
          <>
            {error && <div className="dashboard-error">{error}</div>}

            <section className="dashboard-section">
              <h3 className="dashboard-section-title">{t("settings.section.model")}</h3>
              <label className="settings-label">
                <span>{t("settings.label.default_model")}</span>
                <select
                  className="settings-select"
                  value={settings.default_model}
                  onChange={(e) => patch({ default_model: e.target.value as ModelChoice })}
                >
                  <option value="kimi">{t("settings.model.kimi")}</option>
                  <option value="glm">{t("settings.model.glm")}</option>
                  <option value="mimo">{t("settings.model.mimo")}</option>
                  <option value="mimo-flash">{t("settings.model.mimo_flash")}</option>
                  <option value="qwen">{t("settings.model.qwen")}</option>
                  <option value="deepseek">{t("settings.model.deepseek")}</option>
                  <option value="minimax">{t("settings.model.minimax")}</option>
                  <option value="gemma4-local">Gemma4 (Local)</option>
                </select>
              </label>
            </section>

            <section className="dashboard-section">
              <h3 className="dashboard-section-title">{t("settings.section.composer")}</h3>
              <label className="settings-label settings-label--row">
                <input
                  type="checkbox"
                  checked={settings.send_on_enter}
                  onChange={(e) => patch({ send_on_enter: e.target.checked })}
                />
                <span>
                  <strong>{t("settings.send_on_enter.label")}</strong>
                  <small>
                    {settings.send_on_enter
                      ? t("settings.send_on_enter.help_on")
                      : t("settings.send_on_enter.help_off")}
                  </small>
                </span>
              </label>
            </section>

            <section className="dashboard-section">
              <h3 className="dashboard-section-title">{t("settings.section.persona")}</h3>
              <p style={{ fontSize: 12, color: "var(--text-secondary)", margin: 0 }}>
                {t("settings.persona.help")}
              </p>
              <textarea
                className="settings-textarea"
                rows={5}
                placeholder={t("settings.persona.placeholder")}
                maxLength={4000}
                value={personaDraft}
                onChange={(e) => setPersonaDraft(e.target.value)}
                onBlur={() => {
                  if (personaDraft !== settings.persona) {
                    patch({ persona: personaDraft });
                  }
                }}
              />
            </section>

            {/* UI display language is now a site-wide setting in the shared
                top bar (gear → Language), not a chat-local one. Only the
                model's response language remains here. */}
            <section className="dashboard-section">
              <h3 className="dashboard-section-title">{t("settings.section.output_language")}</h3>
              <p style={{ fontSize: 12, color: "var(--text-secondary)", margin: 0 }}>
                {t("settings.output_language.help")}
              </p>
              <label className="settings-label">
                <span>{t("settings.output_language.field")}</span>
                <select
                  className="settings-select"
                  value={settings.output_language}
                  onChange={(e) => patch({ output_language: e.target.value as LanguageChoice })}
                >
                  {(Object.keys(LANGUAGE_LABELS) as LanguageChoice[]).map((code) => (
                    <option key={code} value={code}>
                      {LANGUAGE_LABELS[code]}
                    </option>
                  ))}
                </select>
              </label>
            </section>

            <section className="dashboard-section">
              <h3 className="dashboard-section-title">
                {t("settings.section.memory")}
                {memorySaving && (
                  <span style={{ marginLeft: 8, fontSize: 12, color: "var(--text-secondary)", fontWeight: "normal" }}>
                    {t("settings.saving")}
                  </span>
                )}
              </h3>
              <p style={{ fontSize: 12, color: "var(--text-secondary)", margin: 0 }}>
                {t("settings.memory.help", {
                  tag: "<memory_update>",
                  max: MEMORY_MAX_CHARS.toLocaleString(),
                })}
              </p>
              {memoryError && <div className="dashboard-error">{memoryError}</div>}
              <textarea
                className="settings-textarea"
                rows={8}
                placeholder={t("settings.memory.placeholder")}
                maxLength={MEMORY_MAX_CHARS}
                value={memoryDraft}
                onChange={(e) => setMemoryDraft(e.target.value)}
                onBlur={() => {
                  if (memoryDraft !== memoryText) {
                    void saveMemory(memoryDraft);
                  }
                }}
              />
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <button
                  type="button"
                  className="settings-button"
                  onClick={() => {
                    if (memoryText && window.confirm(t("settings.memory.clear_confirm"))) {
                      void saveMemory("");
                    }
                  }}
                  disabled={!memoryText || memorySaving}
                >
                  {t("settings.memory.clear")}
                </button>
                <span style={{ fontSize: 11, color: "var(--text-secondary)" }}>
                  {memoryDraft.length.toLocaleString()} / {MEMORY_MAX_CHARS.toLocaleString()}
                </span>
              </div>
            </section>

            <section className="dashboard-section">
              <h3 className="dashboard-section-title">{t("settings.section.notifications")}</h3>
              <label className="settings-label settings-label--row">
                <input
                  type="checkbox"
                  checked={settings.notify_on_complete}
                  onChange={async (e) => {
                    const on = e.target.checked;
                    if (on && "Notification" in window) {
                      // Ask for permission up front; if the user denies,
                      // turn the toggle back off so they're not lied to.
                      const perm = await Notification.requestPermission();
                      if (perm !== "granted") {
                        setError(t("settings.notify.denied"));
                        return;
                      }
                    }
                    patch({ notify_on_complete: on });
                  }}
                />
                <span>
                  <strong>{t("settings.notify.label")}</strong>
                  <small>{t("settings.notify.help")}</small>
                </span>
              </label>
            </section>

            <section className="dashboard-section">
              <h3 className="dashboard-section-title">{t("settings.section.appearance")}</h3>
              <label className="settings-label">
                <span>{t("settings.theme.label")}</span>
                <select
                  className="settings-select"
                  value={settings.theme}
                  onChange={(e) => patch({ theme: e.target.value as ThemeChoice })}
                >
                  <option value="dark">{t("settings.theme.dark")}</option>
                  <option value="light">{t("settings.theme.light")}</option>
                  <option value="system">{t("settings.theme.system")}</option>
                </select>
              </label>
              <label className="settings-label">
                <span>{t("settings.window_layout.label")}</span>
                <select
                  className="settings-select"
                  value={settings.window_layout}
                  onChange={(e) => patch({ window_layout: e.target.value as WindowLayout })}
                >
                  <option value="columns">{t("settings.window_layout.columns")}</option>
                  <option value="grid">{t("settings.window_layout.grid")}</option>
                </select>
                <small style={{ fontSize: 12, color: "var(--text-secondary)", display: "block", marginTop: 4 }}>
                  {t("settings.window_layout.help")}
                </small>
              </label>
            </section>

            <section className="dashboard-section">
              <h3 className="dashboard-section-title">{t("settings.section.tokens")}</h3>
              <label className="settings-label settings-label--row">
                <input
                  type="checkbox"
                  checked={settings.show_token_costs}
                  onChange={(e) => patch({ show_token_costs: e.target.checked })}
                />
                <span>
                  <strong>{t("settings.tokens.label")}</strong>
                  <small>{t("settings.tokens.help")}</small>
                </span>
              </label>
            </section>

            <section className="dashboard-section">
              <h3 className="dashboard-section-title">{t("settings.section.tidiness")}</h3>
              <label className="settings-label">
                <span>{t("settings.tidiness.field")}</span>
                <input
                  type="number"
                  className="settings-input"
                  min={0}
                  max={3650}
                  value={settings.auto_archive_days}
                  onChange={(e) => {
                    const n = Math.max(0, Math.min(3650, parseInt(e.target.value || "0", 10) || 0));
                    patch({ auto_archive_days: n });
                  }}
                />
              </label>
              <p style={{ fontSize: 12, color: "var(--text-secondary)", margin: 0 }}>
                {t("settings.tidiness.help")}
              </p>
            </section>
          </>
        )}
      </div>
    </div>
  );
}
