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

import { useAuth } from "@/lib/auth";
import { auth } from "@/lib/firebase";

type Mode = "signin" | "signup";
type Phase = "idle" | "creating";
type ErrState = { tone: "recover" | "error"; title?: string; body: string };

// Invite codes are a SOFT, client-side gate (see globals note): they ship in the
// public bundle, so this restricts a casual visitor, not a determined one. Real
// enforcement would gate account creation server-side. Comma-separated, matched
// case-insensitively. When unset, account creation is treated as not enabled.
const INVITE_CODES = (process.env.NEXT_PUBLIC_INVITE_CODES ?? "")
  .split(",")
  .map((c) => c.trim().toUpperCase())
  .filter(Boolean);

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
  const [inviteCode, setInviteCode] = useState("");
  const [error, setError] = useState<ErrState | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [created, setCreated] = useState(false);

  // Already-signed-in visitor, or a successful sign in / Google → home. The
  // create-account flow handles its own handoff (below), so it's excluded.
  useEffect(() => {
    if (!loading && user && !created && phase !== "creating") router.push("/");
  }, [loading, user, created, phase, router]);

  // New account created → show the success beat, then hand off to onboarding.
  useEffect(() => {
    if (!created) return;
    const t = setTimeout(() => router.push("/onboarding"), 1500);
    return () => clearTimeout(t);
  }, [created, router]);

  const inviteEntered = inviteCode.trim().length > 0;
  const inviteConfigured = INVITE_CODES.length > 0;
  const inviteValid =
    inviteConfigured && INVITE_CODES.includes(inviteCode.trim().toUpperCase());
  const pwStrength = strength(password);
  const canCreate =
    inviteValid && !!email.trim() && password.length >= 6 && phase !== "creating";

  function switchMode(next: Mode) {
    setMode(next);
    setError(null);
    setNotice(null);
  }

  async function withGoogle() {
    setError(null);
    setNotice(null);
    try {
      await signInWithPopup(auth, new GoogleAuthProvider());
    } catch (e) {
      setError(describeAuthError(errCode(e)));
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

    if (mode === "signin") {
      try {
        await signInWithEmailAndPassword(auth, email.trim(), password);
        // The redirect effect takes it from here.
      } catch (err) {
        setError(describeAuthError(errCode(err)));
      }
      return;
    }

    // Create account (invite-gated).
    if (!inviteValid) {
      setError({
        tone: "error",
        body: inviteConfigured
          ? "Enter a valid invite code to continue."
          : "Account creation isn't enabled yet. Ask for an invite.",
      });
      return;
    }
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
            className="mt-[18px] flex h-[42px] w-full items-center justify-center gap-2.5 rounded-[9px] border text-sm font-semibold"
            style={{
              background: "var(--surface)",
              borderColor: "var(--border)",
              color: "var(--text)",
            }}
          >
            <span className="font-bold" style={{ color: "#4285f4" }}>
              G
            </span>{" "}
            {mode === "signin" ? "Continue with Google" : "Sign up with Google"}
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
            {/* Invite code (create account only) */}
            {mode === "signup" && (
              <>
                <div className="mb-1.5 flex items-center justify-between">
                  <label className="text-xs font-medium" style={{ color: "var(--label)" }}>
                    Invite code
                  </label>
                  {inviteEntered && (
                    <span
                      className="text-[11px]"
                      style={{
                        color: inviteValid ? "var(--good)" : "var(--danger)",
                        fontFamily: "var(--font-mono)",
                      }}
                    >
                      {inviteValid ? "✓ valid" : "not recognized"}
                    </span>
                  )}
                </div>
                <input
                  type="text"
                  placeholder="HERMES-XXXX"
                  value={inviteCode}
                  onChange={(e) => setInviteCode(e.target.value)}
                  disabled={phase === "creating"}
                  className={`${inputCls} uppercase`}
                  style={fieldStyle(
                    inviteEntered
                      ? inviteValid
                        ? "var(--good-border)"
                        : "var(--danger-border)"
                      : "var(--border)",
                    true,
                  )}
                />
                {inviteEntered && !inviteValid && (
                  <p className="mt-1.5 text-xs leading-relaxed" style={{ color: "var(--danger)" }}>
                    {"This code isn't valid or has already been used. Use the code from your invite email."}
                  </p>
                )}
              </>
            )}

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
              : "Don't have a code? Access is invite-only right now."}
          </p>
        </>
      )}
    </div>
  );

  return <main className="flex flex-1 items-center justify-center p-6">{card}</main>;
}
