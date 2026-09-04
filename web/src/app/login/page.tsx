// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import {
  createUserWithEmailAndPassword,
  GoogleAuthProvider,
  sendPasswordResetEmail,
  signInWithEmailAndPassword,
  signInWithPopup,
} from "firebase/auth";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { apiFetch, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { auth } from "@/lib/firebase";

type Mode = "signin" | "signup";
type Phase = "idle" | "creating" | "checking";
type ErrState = { tone: "recover" | "error" | "locked"; title?: string; body: string };


/** Map a Firebase auth error code to copy a human can act on. */
function describeAuthError(code: string): ErrState {
  switch (code) {
    case "auth/invalid-credential":
    case "auth/wrong-password":
    case "auth/user-not-found":
      return {
        tone: "recover",
        title: "We couldn't sign you in",
        body: "That email and password don't match an account. New to Hermes? Create one with your invite.",
      };
    case "auth/invalid-email":
      return { tone: "error", body: "That doesn't look like a valid email address." };
    case "auth/too-many-requests":
      return {
        tone: "error",
        body: "Too many attempts. Please wait a moment and try again.",
      };
    case "auth/email-already-in-use":
      return {
        tone: "error",
        body: "An account with this email already exists — try signing in instead.",
      };
    case "auth/weak-password":
      return { tone: "error", body: "Choose a stronger password (at least 6 characters)." };
    case "auth/popup-closed-by-user":
    case "auth/cancelled-popup-request":
      return { tone: "error", body: "Google sign-in was cancelled." };
    default:
      return { tone: "error", body: "Something went wrong. Please try again." };
  }
}

function errCode(e: unknown): string {
  return (e as { code?: string })?.code ?? "";
}

/** 0–4 rough password strength for the create-account meter. */
function strength(pw: string): number {
  let s = 0;
  if (pw.length >= 8) s++;
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) s++;
  if (/\d/.test(pw)) s++;
  if (/[^A-Za-z0-9]/.test(pw)) s++;
  return s;
}

function Spinner({ size = 15, color = "var(--surface)" }: { size?: number; color?: string }) {
  return (
    <span
      className="inline-block rounded-full border-2"
      style={{
        width: size,
        height: size,
        borderColor: color,
        borderTopColor: "transparent",
        animation: "hspin 0.8s linear infinite",
      }}
    />
  );
}

const inputCls =
  "h-[42px] w-full rounded-[9px] border px-[13px] text-sm outline-none focus:ring-[3px]";

function fieldStyle(borderColor = "var(--border)", mono = false): React.CSSProperties {
  return {
    background: "var(--surface)",
    borderColor,
    color: "var(--text)",
    "--tw-ring-color": "var(--accent)",
    ...(mono ? { fontFamily: "var(--font-mono)", letterSpacing: "1px" } : {}),
  } as React.CSSProperties;
}

export default function LoginPage() {
  const router = useRouter();
  const { user, loading } = useAuth();

  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<ErrState | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [created, setCreated] = useState(false);
  // Set the instant a Google sign-in comes back 403 from the allowlist
  // pre-flight, and never cleared automatically — only a fresh sign-in
  // attempt resets it. This exists as its own flag, separate from `phase`,
  // because `auth.signOut()` clearing `user` via onIdTokenChanged is async:
  // there is a window after we decide "locked out" but before Firebase's
  // listener has actually nulled `user` out, and the redirect effect below
  // must not race that window and let a locked-out session through.
  const [lockedOut, setLockedOut] = useState(false);

  // Already-signed-in visitor, or a successful sign in / Google → home. The
  // create-account flow handles its own handoff (below), so it's excluded.
  // Also held off during "checking" (the post-Google allowlist pre-flight)
  // and once locked out — see withGoogle.
  useEffect(() => {
    if (!loading && user && !created && phase === "idle" && !lockedOut) {
      router.push("/");
    }
  }, [loading, user, created, phase, lockedOut, router]);

  // New account created → show the success beat, then hand off to onboarding.
  useEffect(() => {
    if (!created) return;
    const t = setTimeout(() => router.push("/onboarding"), 1500);
    return () => clearTimeout(t);
  }, [created, router]);

  const pwStrength = strength(password);
  const canCreate =
    !!email.trim() && password.length >= 6 && phase !== "creating";

  function switchMode(next: Mode) {
    setMode(next);
    setError(null);
    setNotice(null);
    setLockedOut(false);
  }

  async function withGoogle() {
    setError(null);
    setNotice(null);
    setLockedOut(false);
    setPhase("checking");
    try {
      await signInWithPopup(auth, new GoogleAuthProvider());
    } catch (e) {
      setError(describeAuthError(errCode(e)));
      setPhase("idle");
      return;
    }

    // A real Firebase session now exists. Probe the allowlist with an
    // endpoint we'd call right after landing anyway (GET /profile is the
    // first-run gate) *before* the redirect effect can act on it — moving an
    // existing, cheap, single-Firestore-read call earlier, not adding a new
    // one. A 403 here is the allowlist refusing this account; anything else
    // (network error, 500, ...) is "the backend had a bad moment", which
    // must not be treated as "you are not allowed" — those need different
    // messages, and only one of them is grounds for signing the user back
    // out of a session they otherwise validly hold.
    try {
      await apiFetch("/profile");
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) {
        setLockedOut(true);
        await auth.signOut();
        setError({
          tone: "locked",
          title: "This account isn't on the invite list",
          body: "Your Google sign-in worked, but Hermes is invite-only right now and this address hasn't been added. Ask whoever invited you for access, then try again.",
        });
        setPhase("idle");
        return;
      }
      // Network error, 500, etc. — the backend, not the allowlist. Let the
      // sign-in stand; don't conflate "unreachable" with "not allowed".
    }
    setPhase("idle");
  }

  async function onForgotPassword() {
    setNotice(null);
    if (!email.trim()) {
      setError({
        tone: "error",
        body: "Enter your email above first, then tap Forgot password.",
      });
      return;
    }
    try {
      await sendPasswordResetEmail(auth, email.trim());
      setError(null);
      // Firebase has *accepted* the request here; it does not confirm delivery.
      // The default sender (noreply@<project>.firebaseapp.com) is often filtered,
      // so steer the user to spam rather than over-promising "sent".
      setNotice(
        `If an account exists for ${email.trim()}, a reset link is on its way — ` +
          `check your spam/Promotions folder if it doesn't arrive in a minute.`,
      );
    } catch (e) {
      setError(describeAuthError(errCode(e)));
    }
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setNotice(null);
    setLockedOut(false);

    if (mode === "signin") {
      try {
        await signInWithEmailAndPassword(auth, email.trim(), password);
        // The redirect effect takes it from here.
      } catch (err) {
        setError(describeAuthError(errCode(err)));
      }
      return;
    }

    // Access is decided server-side by the allowlist: a create that succeeds
    // here still gets a 403 on the first authenticated call unless the address
    // holds a seat. The client-side code field this replaced only ever gated
    // this form, never Google sign-in, and shipped its codes in the bundle.
    setPhase("creating");
    try {
      await createUserWithEmailAndPassword(auth, email.trim(), password);
      setCreated(true); // keep phase "creating" so the redirect effect stays out
    } catch (err) {
      setPhase("idle");
      setError(describeAuthError(errCode(err)));
    }
  }

  const card = (
    <div
      className="w-[384px] rounded-2xl border p-8 shadow-sm"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      {created ? (
        <div className="py-4 text-center">
          <span
            className="h-pop mx-auto inline-flex h-11 w-11 items-center justify-center rounded-full text-[22px]"
            style={{
              background: "var(--good-bg)",
              border: "1px solid var(--good-border)",
              color: "var(--good)",
            }}
          >
            ✓
          </span>
          <div className="mt-3.5 text-base font-semibold" style={{ color: "var(--text)" }}>
            Account created
          </div>
          <div className="mt-1.5 text-sm leading-relaxed" style={{ color: "var(--muted)" }}>
            {"Welcome to Hermes. Let's build your profile from your résumé."}
          </div>
          <div
            className="mt-4 flex items-center justify-center gap-2 text-xs"
            style={{ color: "var(--subtle)", fontFamily: "var(--font-mono)" }}
          >
            <Spinner size={13} color="var(--subtle)" />
            Taking you to upload your résumé…
          </div>
        </div>
      ) : (
        <>
          {/* Brand */}
          <div className="flex items-center gap-2.5">
            <span
              className="flex h-[30px] w-[30px] items-center justify-center rounded-lg text-base font-bold"
              style={{ background: "var(--text)", color: "var(--surface)" }}
            >
              H
            </span>
            <span className="text-lg font-semibold" style={{ color: "var(--text)" }}>
              Hermes
            </span>
          </div>

          <p className="mt-4 text-sm leading-relaxed" style={{ color: "var(--muted)" }}>
            {mode === "signin"
              ? "Sign in to review your matched jobs."
              : "Create your reviewer account."}
          </p>

          {/* Sign in / Create account toggle */}
          <div
            className="mt-[18px] flex gap-[3px] rounded-[10px] border p-[3px]"
            style={{ background: "var(--bg)", borderColor: "var(--border)" }}
          >
            {(["signin", "signup"] as const).map((m) => {
              const active = mode === m;
              return (
                <button
                  key={m}
                  type="button"
                  onClick={() => switchMode(m)}
                  className="h-8 flex-1 rounded-[7px] text-[13px] font-semibold"
                  style={
                    active
                      ? {
                          background: "var(--surface)",
                          color: "var(--text)",
                          boxShadow: "0 1px 2px rgba(0,0,0,0.07)",
                        }
                      : { background: "transparent", color: "var(--muted)" }
                  }
                >
                  {m === "signin" ? "Sign in" : "Create account"}
                </button>
              );
            })}
          </div>

          {/* Humanized error / notice */}
          {error?.tone === "recover" ? (
            <div
              className="mt-[18px] rounded-[10px] border px-3.5 py-[13px]"
              style={{
                borderColor: "var(--danger-border)",
                background: "color-mix(in srgb, var(--danger) 9%, var(--surface))",
              }}
            >
              <div className="flex items-center gap-2">
                <span
                  className="flex h-[18px] w-[18px] items-center justify-center rounded-full text-xs font-bold"
                  style={{ background: "var(--danger)", color: "var(--surface)" }}
                >
                  !
                </span>
                <span className="text-[13px] font-semibold" style={{ color: "var(--danger)" }}>
                  {error.title}
                </span>
              </div>
              <p className="mt-2 text-[13px] leading-relaxed" style={{ color: "var(--danger)" }}>
                {error.body}
              </p>
              <button
                type="button"
                onClick={() => switchMode("signup")}
                className="mt-[11px] h-[34px] w-full rounded-lg border text-[13px] font-semibold"
                style={{
                  borderColor: "var(--danger-border)",
                  background: "var(--surface)",
                  color: "var(--danger)",
                }}
              >
                Create an account →
              </button>
            </div>
          ) : error?.tone === "locked" ? (
            <div
              className="mt-[18px] rounded-[10px] border px-3.5 py-[13px]"
              style={{
                borderColor: "var(--border)",
                background: "color-mix(in srgb, var(--accent) 7%, var(--surface))",
              }}
            >
              <div className="flex items-center gap-2">
                <span
                  className="flex h-[18px] w-[18px] items-center justify-center rounded-full text-xs"
                  style={{ background: "var(--accent)", color: "var(--surface)" }}
                >
                  🔒
                </span>
                <span className="text-[13px] font-semibold" style={{ color: "var(--text)" }}>
                  {error.title}
                </span>
              </div>
              <p className="mt-2 text-[13px] leading-relaxed" style={{ color: "var(--muted)" }}>
                {error.body}
              </p>
            </div>
          ) : error ? (
            <p className="mt-[18px] text-sm" style={{ color: "var(--danger)" }}>
              {error.body}
            </p>
          ) : null}

          {notice && (
            <p className="mt-[18px] text-sm" style={{ color: "var(--good)" }}>
              {notice}
            </p>
          )}

          {/* Google */}
          <button
            type="button"
            onClick={withGoogle}
            disabled={phase === "checking"}
            className="mt-[18px] flex h-[42px] w-full items-center justify-center gap-2.5 rounded-[9px] border text-sm font-semibold"
            style={{
              background: "var(--surface)",
              borderColor: "var(--border)",
              color: "var(--text)",
              cursor: phase === "checking" ? "not-allowed" : "pointer",
            }}
          >
            {phase === "checking" ? (
              <>
                <Spinner size={15} color="var(--muted)" />
                Checking access…
              </>
            ) : (
              <>
                <span className="font-bold" style={{ color: "#4285f4" }}>
                  G
                </span>{" "}
                {mode === "signin" ? "Continue with Google" : "Sign up with Google"}
              </>
            )}
          </button>

          <div className="my-5 flex items-center gap-3">
            <span className="h-px flex-1" style={{ background: "var(--border)" }} />
            <span
              className="text-[11px] tracking-wider"
              style={{ color: "var(--subtle)", fontFamily: "var(--font-mono)" }}
            >
              OR
            </span>
            <span className="h-px flex-1" style={{ background: "var(--border)" }} />
          </div>

          <form onSubmit={onSubmit}>
            <label
              className="mb-1.5 mt-3.5 block text-xs font-medium"
              style={{ color: "var(--label)" }}
            >
              Email
            </label>
            <input
              type="email"
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={phase === "creating"}
              className={inputCls}
              style={fieldStyle()}
            />

            <div className="mb-1.5 mt-3.5 flex items-center justify-between">
              <label className="text-xs font-medium" style={{ color: "var(--label)" }}>
                Password
              </label>
              {mode === "signin" && (
                <button
                  type="button"
                  onClick={onForgotPassword}
                  className="text-xs font-medium"
                  style={{ color: "var(--accent)" }}
                >
                  Forgot password?
                </button>
              )}
            </div>
            <input
              type="password"
              placeholder="••••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={phase === "creating"}
              className={inputCls}
              style={fieldStyle()}
            />

            {/* Strength meter (create account only) */}
            {mode === "signup" && password.length > 0 && (
              <div className="mt-2 flex gap-[5px]">
                {[0, 1, 2, 3].map((i) => (
                  <span
                    key={i}
                    className="h-1 flex-1 rounded-[2px]"
                    style={{ background: i < pwStrength ? "var(--good)" : "var(--border)" }}
                  />
                ))}
              </div>
            )}

            {mode === "signin" ? (
              <button
                type="submit"
                className="mt-[18px] h-[42px] w-full rounded-[9px] text-sm font-semibold"
                style={{ background: "var(--text)", color: "var(--surface)" }}
              >
                Sign in
              </button>
            ) : (
              <button
                type="submit"
                disabled={!canCreate}
                className="mt-5 flex h-[42px] w-full items-center justify-center gap-2.5 rounded-[9px] text-sm font-semibold"
                style={{
                  background: canCreate ? "var(--text)" : "var(--skeleton)",
                  color: canCreate ? "var(--surface)" : "var(--subtle)",
                  cursor: canCreate ? "pointer" : "not-allowed",
                }}
              >
                {phase === "creating" ? (
                  <>
                    <Spinner size={15} />
                    Creating your account…
                  </>
                ) : (
                  "Create account"
                )}
              </button>
            )}
          </form>

          <p
            className="mt-[18px] text-center text-xs leading-relaxed"
            style={{ color: "var(--subtle)" }}
          >
            {mode === "signin"
              ? "Access is restricted to invited reviewers."
              : "Hermes is invite-only: you can create an account, but you'll need an invited address to get in."}
          </p>
        </>
      )}
    </div>
  );

  return <main className="flex flex-1 items-center justify-center p-6">{card}</main>;
}
