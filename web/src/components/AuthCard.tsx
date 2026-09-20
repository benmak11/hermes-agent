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
import { readStored, writeStored } from "@/lib/localStore";
import { APP_HOME, safeNext, SIGNUP_FROM_KEY } from "@/lib/nav";
import { signupOutcome, type SignupResult } from "@/lib/signupFlow";
import { CARD, SERIF } from "@/components/warm/styles";

type Mode = "signin" | "signup";
type Phase = "idle" | "creating" | "checking";
type ErrState = {
  tone: "recover" | "error" | "locked" | "waitlisted";
  title?: string;
  body: string;
};

/** Auth-card strings (not marketing copy — that lives in marketing/copy.ts). */
const LOCKED_PANEL: ErrState = {
  tone: "locked",
  title: "This account isn't on the invite list",
  body: "Your Google sign-in worked, but Hermes is invite-only right now and this address hasn't been added. Ask whoever invited you for access, then try again.",
};
const WAITLISTED_PANEL: ErrState = {
  tone: "waitlisted",
  title: "You're on the list",
  body: "Hermes is invite-only while we're small. Your account and your place in line are saved — when a seat opens for this address, sign in again and you're in.",
};


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

function Spinner({ size = 15, color = "#fff9f2" }: { size?: number; color?: string }) {
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

// Border, background, colour and the focus ring are owned by `.wm-input`.
const inputCls =
  "wm-input h-[46px] w-full rounded-[13px] px-[14px] text-[14.5px] outline-none";

/** Micro-label above each field. */
const labelCls = "block text-[12.5px] font-semibold";
const labelStyle: React.CSSProperties = { color: "var(--ink-3)" };

/** Per-mode h1 (the sub and footer ternaries sit inline below). */
const HEADLINE: Record<Mode, string> = {
  signin: "Welcome back.",
  signup: "Let's get you set up.",
};

/** 18px status dot at the head of a panel. */
const dotCls =
  "flex h-[18px] w-[18px] flex-none items-center justify-center rounded-full text-[11px] font-bold";

/** The 04 file-card treatment, tinted per panel tone. */
function panelStyle(bg: string, border: string): React.CSSProperties {
  return { background: bg, border: `1px solid ${border}`, borderRadius: 16, padding: "14px 17px" };
}

export function AuthCard({ initialMode, next }: { initialMode: Mode; next: string | null }) {
  const router = useRouter();
  const { user, loading } = useAuth();

  const [mode, setMode] = useState<Mode>(initialMode);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<ErrState | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [created, setCreated] = useState(false);
  // Set the instant a sign-in comes back "not allowed" from the admission
  // check (see admit), and never cleared automatically — only a fresh sign-in
  // attempt resets it. This exists as its own flag, separate from `phase`,
  // because `auth.signOut()` clearing `user` via onIdTokenChanged is async:
  // there is a window after we decide "locked out" but before Firebase's
  // listener has actually nulled `user` out, and the redirect effect below
  // must not race that window and let a locked-out session through.
  const [lockedOut, setLockedOut] = useState(false);

  // Already-signed-in visitor, or a successful sign in / Google → home. The
  // create-account flow handles its own handoff (below), so it's excluded.
  // Also held off during "checking" (the post-auth admission check) and once
  // locked out — see admit.
  useEffect(() => {
    if (!loading && user && !created && phase === "idle" && !lockedOut) {
      router.push(safeNext(next) ?? APP_HOME);
    }
  }, [loading, user, created, phase, lockedOut, router, next]);

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

  /** End the session here: flag first, then sign out, then explain. The flag
   *  must land before signOut so the redirect effect can't race the async
   *  onIdTokenChanged null-out (see lockedOut above). */
  async function lockOut(panel: ErrState) {
    setLockedOut(true);
    await auth.signOut();
    setError(panel);
  }

  /** Yesterday's Google pre-flight (GET /profile, 403 == not allowed), kept
   *  only for the minutes between the web and api deploys when
   *  POST /account/signup still 404s. A 403 here is the allowlist refusing
   *  this account; anything else (network error, 500, ...) is "the backend
   *  had a bad moment", which must not be treated as "you are not allowed". */
  async function legacyProbe(): Promise<boolean> {
    try {
      await apiFetch("/profile");
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) {
        await lockOut(LOCKED_PANEL);
        return false;
      }
    }
    return true;
  }

  /** Post-auth admission. True = carry on; false = the session was ended here.
   *  Records the marketing CTA the visitor came in through (stashed by /signup)
   *  and clears it once the outcome is known — kept on fallback/error so the
   *  next attempt still carries it. */
  async function admit(provider: "google" | "email"): Promise<boolean> {
    const source = readStored("session", SIGNUP_FROM_KEY);
    let result: SignupResult;
    try {
      const r = await apiFetch<{ allowed: boolean }>("/account/signup", {
        method: "POST",
        body: JSON.stringify({ source }),
      });
      result = { kind: "ok", allowed: r.allowed };
    } catch (e) {
      result = { kind: "error", status: e instanceof ApiError ? e.status : null };
    }
    switch (signupOutcome(result)) {
      case "continue":
        writeStored("session", SIGNUP_FROM_KEY, null);
        return true;
      case "waitlist":
        writeStored("session", SIGNUP_FROM_KEY, null);
        await lockOut(WAITLISTED_PANEL);
        return false;
      case "locked":
        await lockOut(LOCKED_PANEL);
        return false;
      case "fallback":
        return provider === "google" ? legacyProbe() : true;
    }
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

    // A real Firebase session now exists. Decide admission *before* the
    // redirect effect can act on it; on "not allowed" admit() has already
    // signed the user back out and lockedOut holds the effect off.
    try {
      await admit("google");
    } finally {
      setPhase("idle");
    }
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
      // "checking" goes on before the await, as withGoogle does: the phase
      // guard on the redirect effect must already hold when onIdTokenChanged
      // sets `user`, whatever order the scheduler flushes them in.
      setPhase("checking");
      try {
        await signInWithEmailAndPassword(auth, email.trim(), password);
        // Same admission as Google: a waitlisted address that comes back and
        // signs in gets the panel here, not a raw 403 on /app — and this is
        // the exact path the operator's grant relies on ("sign in again").
        await admit("email");
      } catch (err) {
        setError(describeAuthError(errCode(err)));
      } finally {
        setPhase("idle"); // on "continue" the redirect effect takes it from here
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
    } catch (err) {
      setPhase("idle");
      setError(describeAuthError(errCode(err)));
      return;
    }
    // Phase stays "creating" through the admission check so the redirect
    // effect stays out; a stranger gets the waitlist panel instead of
    // /onboarding and a 403 there.
    if (await admit("email")) setCreated(true);
    else setPhase("idle");
  }

  const card = (
    <div style={{ ...CARD, width: "min(440px, 100%)", padding: 36 }}>
      {created ? (
        <div className="py-4 text-center">
          <span
            className="h-pop mx-auto inline-flex h-11 w-11 items-center justify-center rounded-full text-[22px]"
            style={{
              background: "var(--sage-tint)",
              border: "1px solid #cfe0c8",
              color: "var(--sage)",
            }}
          >
            ✓
          </span>
          <div className="mt-3.5 text-base font-semibold" style={{ color: "var(--ink)" }}>
            Account created
          </div>
          <div className="mt-1.5 text-sm leading-relaxed" style={{ color: "var(--ink-4)" }}>
            {"Welcome to Hermes. Let's build your profile from your résumé."}
          </div>
          <div
            className="mt-4 flex items-center justify-center gap-2 text-[12.5px]"
            style={{ color: "#a3927f" }}
          >
            <Spinner size={13} color="#a3927f" />
            Taking you to upload your résumé…
          </div>
        </div>
      ) : (
        <>
          {/* Brand */}
          <div className="flex items-center gap-[11px]">
            <span
              className="flex h-[34px] w-[34px] items-center justify-center rounded-[11px] text-[17px] font-bold"
              style={{ background: "var(--terracotta)", color: "#fff9f2" }}
            >
              H
            </span>
            <span
              className="text-[19px] font-bold"
              style={{ color: "var(--ink)", letterSpacing: "-0.01em" }}
            >
              Hermes
            </span>
          </div>

          <h1
            className="mt-[22px] text-[30px] font-normal"
            style={{ fontFamily: SERIF, lineHeight: 1.15, color: "var(--ink)" }}
          >
            {HEADLINE[mode]}
          </h1>
          <p
            className="mt-2 text-[14.5px]"
            style={{ color: "var(--ink-4)", lineHeight: 1.55 }}
          >
            {mode === "signin"
              ? "Your matches have been piling up while you were away."
              : "Two minutes now, and the job search runs itself after."}
          </p>

          {/* Sign in / Create account toggle */}
          <div
            className="mt-[22px] flex gap-1 rounded-[14px] border p-1"
            style={{ background: "#f6ede1", borderColor: "var(--border-warm-hair)" }}
          >
            {(["signin", "signup"] as const).map((m) => {
              const active = mode === m;
              return (
                <button
                  key={m}
                  type="button"
                  onClick={() => switchMode(m)}
                  className={`h-9 flex-1 rounded-[11px] text-[13.5px] font-semibold${active ? "" : " wm-seg"}`}
                  style={
                    active
                      ? {
                          background: "var(--surface-warm)",
                          color: "var(--ink)",
                          boxShadow: "0 1px 3px rgba(94,63,39,0.10)",
                        }
                      : { background: "transparent" } // colour lives on .wm-seg so :hover can win
                  }
                >
                  {m === "signin" ? "Sign in" : "Create account"}
                </button>
              );
            })}
          </div>

          {/* Humanized error / notice */}
          {error?.tone === "recover" ? (
            <div className="mt-[22px]" style={panelStyle("#fdeeea", "#f2cfc3")}>
              <div className="flex items-center gap-2">
                <span className={dotCls} style={{ background: "var(--brick)", color: "#fff9f2" }}>
                  !
                </span>
                <span className="text-[13.5px] font-semibold" style={{ color: "var(--brick)" }}>
                  {error.title}
                </span>
              </div>
              <p
                className="mt-2 text-[13.5px]"
                style={{ color: "var(--brick)", lineHeight: 1.55 }}
              >
                {error.body}
              </p>
              <button
                type="button"
                onClick={() => switchMode("signup")}
                className="wm-ghost mt-[11px] h-9 w-full rounded-[11px] border text-[13px] font-semibold"
                style={{ borderColor: "#f2cfc3", color: "var(--brick)" }}
              >
                Create an account →
              </button>
            </div>
          ) : error?.tone === "locked" || error?.tone === "waitlisted" ? (
            <div
              className="mt-[22px]"
              style={
                error.tone === "waitlisted"
                  ? panelStyle("var(--sage-tint)", "#cfe0c8")
                  : panelStyle("var(--honey-tint)", "#f4dfb4")
              }
            >
              <div className="flex items-center gap-2">
                <span
                  className={dotCls}
                  style={
                    error.tone === "waitlisted"
                      ? { background: "var(--sage)", color: "#fff9f2" }
                      : { background: "#9a6216", color: "#fff9f2" }
                  }
                >
                  {error.tone === "waitlisted" ? "✓" : "!"}
                </span>
                <span className="text-[13.5px] font-semibold" style={{ color: "var(--ink)" }}>
                  {error.title}
                </span>
              </div>
              <p
                className="mt-2 text-[13.5px]"
                style={{ color: "var(--ink-3)", lineHeight: 1.55 }}
              >
                {error.body}
              </p>
            </div>
          ) : error ? (
            <p className="mt-[22px] text-[13.5px]" style={{ color: "var(--brick)" }}>
              {error.body}
            </p>
          ) : null}

          {notice && (
            <p className="mt-[22px] text-[13.5px]" style={{ color: "var(--sage)" }}>
              {notice}
            </p>
          )}

          {/* Google */}
          <button
            type="button"
            onClick={withGoogle}
            disabled={phase === "checking"}
            className="wm-ghost mt-5 flex h-[46px] w-full items-center justify-center gap-2.5 rounded-[13px] border text-[14.5px] font-semibold"
            style={{
              borderColor: "#e8dacb",
              color: "var(--ink)",
              cursor: phase === "checking" ? "not-allowed" : "pointer",
            }}
          >
            {phase === "checking" ? (
              <>
                <Spinner size={15} color="#a3927f" />
                Checking access…
              </>
            ) : (
              <>
                <span className="font-bold" style={{ color: "var(--terracotta)" }}>
                  G
                </span>{" "}
                {mode === "signin" ? "Continue with Google" : "Sign up with Google"}
              </>
            )}
          </button>

          <div className="my-[22px] flex items-center gap-3">
            <span className="h-px flex-1" style={{ background: "var(--border-warm-hair)" }} />
            <span
              className="text-[10.5px] font-bold"
              style={{ color: "#b0a08d", letterSpacing: "0.14em" }}
            >
              OR
            </span>
            <span className="h-px flex-1" style={{ background: "var(--border-warm-hair)" }} />
          </div>

          <form onSubmit={onSubmit}>
            <label className={`${labelCls} mb-[7px]`} style={labelStyle}>
              Email
            </label>
            <input
              type="email"
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={phase === "creating"}
              className={inputCls}
            />

            <div className="mb-[7px] mt-4 flex items-center justify-between">
              <label className={labelCls} style={labelStyle}>
                Password
              </label>
              {mode === "signin" && (
                <button
                  type="button"
                  onClick={onForgotPassword}
                  className="wm-link text-[12.5px] font-semibold"
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
            />

            {/* Strength meter (create account only) */}
            {mode === "signup" && password.length > 0 && (
              <div className="mt-2 flex gap-[5px]">
                {[0, 1, 2, 3].map((i) => (
                  <span
                    key={i}
                    className="h-1 flex-1 rounded-[2px]"
                    style={{ background: i < pwStrength ? "var(--sage)" : "#f0e3d3" }}
                  />
                ))}
              </div>
            )}

            {mode === "signin" ? (
              <button
                type="submit"
                disabled={phase === "checking"}
                className="wm-cta mt-[22px] flex h-[48px] w-full items-center justify-center gap-2.5 rounded-[13px] text-[15px] font-semibold"
                style={{
                  cursor: phase === "checking" ? "not-allowed" : "pointer",
                }}
              >
                {phase === "checking" ? (
                  <>
                    <Spinner size={15} color="#b0a08d" />
                    Checking access…
                  </>
                ) : (
                  "Sign in"
                )}
              </button>
            ) : (
              <button
                type="submit"
                disabled={!canCreate}
                className="wm-cta mt-6 flex h-[48px] w-full items-center justify-center gap-2.5 rounded-[13px] text-[15px] font-semibold"
              >
                {phase === "creating" ? (
                  <>
                    <Spinner size={15} color="#b0a08d" />
                    Creating your account…
                  </>
                ) : (
                  "Create my account"
                )}
              </button>
            )}
          </form>

          <p
            className="mt-[18px] text-center text-[12.5px] leading-relaxed"
            style={{ color: "#a3927f" }}
          >
            {mode === "signin"
              ? "Invite-only while we're still small — thanks for being early."
              : "Invite-only while we're still small — create your account and we'll hold your place in line."}
          </p>
        </>
      )}
    </div>
  );

  return <main className="flex flex-1 items-center justify-center p-6">{card}</main>;
}
