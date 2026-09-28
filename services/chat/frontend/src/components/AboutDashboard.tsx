/**
 * About panel — sidebar overlay (same `.dashboard` shell as the others).
 * Read-only feature overview of the chat web AI + the terminal.
 * Intentionally omits security details and sibling apps.
 */
import { useT } from "../i18n";

export interface AboutDashboardProps {
  onClose: () => void;
  visible: boolean;
}

interface FeatureRow {
  name: string;
  desc: string;
}

/** Derive the workspace (dev.*) hostname for this tenant by transforming
 *  the current chat host. Mirrors Sidebar.tsx + Artifact.tsx so the
 *  three cross-product paths agree on the URL shape without hardcoding
 *  any specific tenant. The {terminalHost} i18n token name is preserved
 *  for back-compat with translated strings, but it now resolves to the
 *  dev.<host> URL — the Terminal panel lives inside the dev workspace. */
function terminalHostFromLocation(): string {
  if (typeof window === "undefined") return "";
  const host = window.location.host;
  if (host.startsWith("chat.")) return host.replace(/^chat\./, "dev.");
  if (host.split(".").length === 2) return "dev." + host;
  return "dev." + host.replace(/^[^.]+\./, "");
}

/** Replace `{terminalHost}` tokens in an i18n string with the dynamic
 *  hostname. We use this rather than introducing a generic-vars
 *  templating layer on `t()` because the only token in the dictionary
 *  today is `{terminalHost}`; expand if more arrive. */
function expandHostPlaceholders(s: string, terminalHost: string): string {
  return s.replace(/\{terminalHost\}/g, terminalHost);
}

export function AboutDashboard({ onClose, visible }: AboutDashboardProps): JSX.Element {
  const t = useT();
  const terminalHost = terminalHostFromLocation();
  const tt = (key: string) => expandHostPlaceholders(t(key), terminalHost);

  if (!visible) return <></>;

  const chatFeatures: FeatureRow[] = [
    { name: t("about.chat.attachments.name"),  desc: t("about.chat.attachments.desc") },
    { name: t("about.chat.imagegen.name"),     desc: t("about.chat.imagegen.desc") },
    { name: t("about.chat.artifacts.name"),    desc: t("about.chat.artifacts.desc") },
    { name: t("about.chat.memory.name"),       desc: t("about.chat.memory.desc") },
    { name: t("about.chat.persona.name"),      desc: t("about.chat.persona.desc") },
    { name: t("about.chat.filepicker.name"),   desc: t("about.chat.filepicker.desc") },
    { name: t("about.chat.model.name"),        desc: t("about.chat.model.desc") },
    { name: t("about.chat.languages.name"),    desc: t("about.chat.languages.desc") },
    { name: t("about.chat.theme.name"),        desc: t("about.chat.theme.desc") },
    { name: t("about.chat.notify.name"),       desc: t("about.chat.notify.desc") },
    { name: t("about.chat.organize.name"),     desc: t("about.chat.organize.desc") },
    { name: t("about.chat.search.name"),       desc: t("about.chat.search.desc") },
  ];

  const termFeatures: FeatureRow[] = [
    { name: t("about.term.terminal.name"),  desc: t("about.term.terminal.desc") },
    { name: t("about.term.browser.name"),   desc: t("about.term.browser.desc") },
    { name: t("about.term.editor.name"),    desc: t("about.term.editor.desc") },
    { name: t("about.term.preview.name"),   desc: t("about.term.preview.desc") },
    { name: t("about.term.upload.name"),    desc: t("about.term.upload.desc") },
    { name: t("about.term.layout.name"),    desc: t("about.term.layout.desc") },
  ];

  return (
    <div className="dashboard" role="dialog" aria-label={t("about.title")}>
      <header className="dashboard-header">
        <button
          type="button"
          className="dashboard-back"
          onClick={onClose}
          aria-label={t("about.close_aria")}
          title={t("about.close_title")}
        >
          ←
        </button>
        <h2 className="dashboard-title">{t("about.title")}</h2>
      </header>
      <div className="dashboard-body about-body">
        <p className="about-lede">{tt("about.lede")}</p>

        <section className="dashboard-section">
          <h3 className="dashboard-section-title">{t("about.chat.heading")}</h3>
          <p className="about-section-blurb">{t("about.chat.blurb")}</p>
          <dl className="about-feature-list">
            {chatFeatures.map((f) => (
              <div className="about-feature-row" key={f.name}>
                <dt>{f.name}</dt>
                <dd>{f.desc}</dd>
              </div>
            ))}
          </dl>
        </section>

        <section className="dashboard-section">
          <h3 className="dashboard-section-title">{tt("about.term.heading")}</h3>
          <p className="about-section-blurb">{t("about.term.blurb")}</p>
          <dl className="about-feature-list">
            {termFeatures.map((f) => (
              <div className="about-feature-row" key={f.name}>
                <dt>{f.name}</dt>
                <dd>{f.desc}</dd>
              </div>
            ))}
          </dl>
        </section>
      </div>
    </div>
  );
}
