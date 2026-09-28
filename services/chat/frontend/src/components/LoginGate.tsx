/**
 * Login UI shown when the chat backend returns 401 on /api/me.
 *
 * Two tenant variants, switched by hostname:
 *   * ald3 (chat.ald3.com, auth.ald3.com): password mode — email + password,
 *     plus a "Forgot / set password" code flow that lands on a new password.
 *   * wizerith (auth.wizerith.ai + the apex): passwordless mode — email →
 *     6-digit code from Resend → signed in. No password ever. Backend
 *     enforces ALLOWED_EMAIL_DOMAINS=wizerith.com so only @wizerith.com
 *     emails can request a code.
 *
 * Mode is detected at render time from `window.location.hostname`, so the
 * same compiled SPA bundle ships to both tenants and the UI flips itself.
 */

import { useState } from "react";
import "./LoginGate.css";

// password mode: "signin" = email + password; "signup" = first-time / forgot
//                — sends a code, then sets a NEW password.
// passwordless mode (wizerith): step 1 = email → request code; step 2 = code → signed in.
type Mode = "signin" | "signup";
type Step = 1 | 2;

// Hostname → tenant-specific config. Read once at module load — these
// are immutable per page-view and a single SPA bundle serves both
// hostnames, so no React state is needed.
function _passwordless(): boolean {
  if (typeof window === "undefined") return false;
  const h = window.location.hostname;
  return h === "wizerith.ai" || h.endsWith(".wizerith.ai");
}
function _brand(): string {
  return _passwordless() ? "Wizerith Asset Management" : "KRAK Services";
}
function _domainHint(): string {
  return _passwordless() ? "@wizerith.com" : "";
}

// Wizerith brand element: the company logo, served by Caddy at
// /_theme/logo.png on every wizerith host. ald3 keeps the text title.
function BrandHeading(): JSX.Element {
  if (_passwordless()) {
    return (
      <img
        className="login-gate-logo"
        src="/_theme/logo.png"
        alt="Wizerith Asset Management"
      />
    );
  }
  return <h1 className="login-gate-title">{_brand()}</h1>;
}

interface AuthResponse {
  email: string;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    let detail = "request failed";
    try {
      const j = await r.json();
      if (j && typeof j.detail === "string") detail = j.detail;
    } catch {
      // body wasn't JSON; keep the generic message
    }
    throw new Error(detail);
  }
  return (await r.json()) as T;
}

interface LoginGateProps {
  // "anon" = no cookie / 401 → show the login form.
  // "forbidden" = cookie present but email not on allowlist (403) → show a
  // dedicated permission-required screen with sign-out + sign-in-as-someone
  // -else options.
  reason?: "anon" | "forbidden";
}

async function postLogout(): Promise<void> {
  try {
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
  } catch {
    /* even if the call fails, reload to drop any stale UI state */
  }
}

// After a successful login/reset, return the user to wherever they came
// from. Bet/market bounce here with `?next=https://bet.ald3.com/` when they
// see a 401; we redirect back instead of dropping them into chat. The
// allowlist guards against open-redirect: only same-tenant `.ald3.com`
// hostnames over https. Anything else falls through to a chat reload.
function _isAuthHost(): boolean {
  if (typeof window === "undefined") return false;
  const h = window.location.hostname;
  return h === "auth.ald3.com" || h === "auth.wizerith.ai";
}

function _authHostFallback(): string {
  // On the sign-in-only surfaces, the user has no useful "current page" to
  // return to — staying on auth.* would just re-render the LoginGate. Send
  // them to the tenant apex so the signed-in pill updates immediately.
  return window.location.hostname === "auth.wizerith.ai"
    ? "https://wizerith.ai/"
    : "https://ald3.com/";
}

function nextUrlOrFallback(): string {
  try {
    const params = new URLSearchParams(window.location.search);
    const next = params.get("next");
    if (!next) return _isAuthHost() ? _authHostFallback() : (window.location.pathname || "/");
    const u = new URL(next, window.location.origin);
    const okProto = u.protocol === "https:" || u.protocol === "http:";
    const okHost =
      u.hostname === "ald3.com" || u.hostname.endsWith(".ald3.com") ||
      u.hostname === "wizerith.ai" || u.hostname.endsWith(".wizerith.ai");
    if (okProto && okHost) return u.toString();
  } catch {
    /* malformed next — fall through */
  }
  return _isAuthHost() ? _authHostFallback() : (window.location.pathname || "/");
}

function redirectAfterAuth(): void {
  const target = nextUrlOrFallback();
  // Drop the `?next=` from the address bar even when we end up reloading
  // chat (otherwise a refresh would keep trying to redirect).
  if (target.startsWith(window.location.origin) || target === "/" || target === window.location.pathname) {
    window.location.href = target;
  } else {
    window.location.href = target;
  }
}

export function LoginGate({ reason = "anon" }: LoginGateProps): JSX.Element {
  if (reason === "forbidden") {
    return (
      <div className="login-gate-shell">
        <div className="login-gate-card">
          <BrandHeading />
          <p className="login-gate-info" style={{ marginTop: 4 }}>
            Permission required
          </p>
          <p className="login-gate-footnote" style={{ marginTop: 12 }}>
            You&rsquo;re signed in, but this email isn&rsquo;t authorized to use chat.
            Ask the admin to grant access, or sign in with a different account.
          </p>
          <button
            type="button"
            className="primary"
            onClick={async () => {
              await postLogout();
              window.location.reload();
            }}
          >
            Sign out
          </button>
        </div>
      </div>
    );
  }
  return _passwordless() ? <PasswordlessLoginForm /> : <LoginForm />;
}

// ---------------------------------------------------------------------------
// Passwordless form (wizerith). Email → code → signed in. No password input,
// no sign-up/sign-in toggle (there's only one path), no "set new password"
// step. The backend's /api/auth/request-code rejects non-allowed domains
// before sending; we surface that 403 inline so the user understands why.
// ---------------------------------------------------------------------------

function PasswordlessLoginForm(): JSX.Element {
  const [step, setStep] = useState<Step>(1);
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setInfo(null);
    setBusy(true);
    try {
      if (step === 1) {
        await postJson<{ message: string }>("/api/auth/request-code", { email });
        setInfo(`Code sent to ${email}. Check your inbox.`);
        setStep(2);
        return;
      }
      await postJson<AuthResponse>("/api/auth/login-with-code", { email, code });
      redirectAfterAuth();
    } catch (err) {
      setError(err instanceof Error ? err.message : "request failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-gate-shell">
      <div className="login-gate-card">
        <h1 className="login-gate-title">{_brand()}</h1>
        <p className="login-gate-info" style={{ marginTop: 4, opacity: 0.85 }}>
          Sign in with a one-time code
        </p>

        <form className="login-gate-form" onSubmit={submit}>
          {step === 1 && (
            <label>
              Email
              <input
                type="email"
                required
                autoComplete="email"
                placeholder={_domainHint() ? `you${_domainHint()}` : "you@example.com"}
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </label>
          )}

          {step === 2 && (
            <>
              <label>
                6-digit code (sent to {email})
                <input
                  type="text"
                  inputMode="numeric"
                  pattern="\d{6}"
                  maxLength={6}
                  autoFocus
                  required
                  value={code}
                  onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
                />
              </label>
              <button
                type="button"
                className="link"
                onClick={() => {
                  setStep(1); setCode(""); setError(null); setInfo(null);
                }}
              >
                Use a different email
              </button>
            </>
          )}

          {error && <p className="login-gate-error">{error}</p>}
          {info && <p className="login-gate-info">{info}</p>}

          <button type="submit" className="primary" disabled={busy}>
            {busy ? "…" : step === 1 ? "Send code" : "Sign in"}
          </button>
        </form>

        <p className="login-gate-footnote">
          {_domainHint()
            ? `Access is limited to ${_domainHint()} addresses.`
            : "We'll email a one-time code to sign you in."}
        </p>
      </div>
    </div>
  );
}

function LoginForm(): JSX.Element {
  const [mode, setMode] = useState<Mode>("signin");
  const [step, setStep] = useState<Step>(1);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  function resetFlow() {
    setStep(1);
    setCode("");
    setNewPassword("");
    setError(null);
    setInfo(null);
  }

  function switchMode(next: Mode) {
    setMode(next);
    resetFlow();
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setInfo(null);
    setBusy(true);
    try {
      if (mode === "signin") {
        await postJson<AuthResponse>("/api/auth/login", { email, password });
        // Cookie is now set on .ald3.com. Honor ?next= for cross-site
        // sign-in from bet/market/dev; otherwise reload chat.
        redirectAfterAuth();
        return;
      }
      if (mode === "signup" && step === 1) {
        await postJson<{ message: string }>("/api/auth/request-password-reset", { email });
        setInfo(`If ${email} is on the access list, a 6-digit code is on its way.`);
        setStep(2);
        return;
      }
      if (mode === "signup" && step === 2) {
        await postJson<AuthResponse>("/api/auth/reset-password", {
          email,
          code,
          new_password: newPassword,
        });
        redirectAfterAuth();
        return;
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "request failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-gate-shell">
      <div className="login-gate-card">
        <h1 className="login-gate-title">{_brand()}</h1>
        <div className="login-gate-tabs">
          <button
            type="button"
            className={mode === "signin" ? "active" : ""}
            onClick={() => switchMode("signin")}
          >
            Sign in
          </button>
          <button
            type="button"
            className={mode === "signup" ? "active" : ""}
            onClick={() => switchMode("signup")}
          >
            Sign up
          </button>
        </div>

        <form className="login-gate-form" onSubmit={submit}>
          {step === 1 && (
            <>
              <label>
                Email
                <input
                  type="email"
                  required
                  autoComplete="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </label>
              {mode === "signin" && (
                <label>
                  Password
                  <input
                    type="password"
                    required
                    minLength={8}
                    autoComplete="current-password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                </label>
              )}
              {mode === "signin" && (
                <button
                  type="button"
                  className="link"
                  onClick={() => switchMode("signup")}
                >
                  Forgot password?
                </button>
              )}
            </>
          )}

          {step === 2 && (
            <>
              <label>
                6-digit code (sent to {email})
                <input
                  type="text"
                  inputMode="numeric"
                  pattern="\d{6}"
                  maxLength={6}
                  required
                  value={code}
                  onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
                />
              </label>
              <label>
                New password
                <input
                  type="password"
                  required
                  minLength={8}
                  autoComplete="new-password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                />
              </label>
              <button type="button" className="link" onClick={resetFlow}>
                Use a different email
              </button>
            </>
          )}

          {error && <p className="login-gate-error">{error}</p>}
          {info && <p className="login-gate-info">{info}</p>}

          <button type="submit" className="primary" disabled={busy}>
            {busy
              ? "…"
              : mode === "signin"
                ? "Sign in"
                : step === 1
                  ? "Send code"
                  : "Set password + sign in"}
          </button>
        </form>

        <p className="login-gate-footnote">
          New here? Pick &ldquo;Sign up&rdquo; — we&rsquo;ll email a 6-digit code
          to verify your address and set your password.
        </p>
      </div>
    </div>
  );
}
