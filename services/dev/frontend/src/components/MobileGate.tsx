// Desktop-only gate. We render this unconditionally; CSS hides it on
// viewports ≥1024px and hides the rest of the app under that threshold.

function devHost(): string {
  if (typeof window === "undefined") return "dev";
  return window.location.host;
}

function chatHomeUrl(): string {
  if (typeof window === "undefined") return "/";
  const host = window.location.hostname;
  if (host.startsWith("dev.")) {
    return `${window.location.protocol}//${host.slice(4)}/`;
  }
  return "/";
}

export function MobileGate() {
  return (
    <div className="mobile-gate">
      <div className="mobile-gate-inner">
        <h1>{devHost()}</h1>
        <p>
          The IDE is desktop-only — open this page on a screen at least
          1024px wide. The companion chat app at{" "}
          <a href={chatHomeUrl()}>{chatHomeUrl().replace(/^https?:\/\//, "").replace(/\/$/, "")}</a>{" "}
          works on mobile if you need a research session on the go.
        </p>
      </div>
    </div>
  );
}
