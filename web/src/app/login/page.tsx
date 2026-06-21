// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import {
  GoogleAuthProvider,
  signInWithEmailAndPassword,
  signInWithPopup,
} from "firebase/auth";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { useAuth } from "@/lib/auth";
import { auth } from "@/lib/firebase";

export default function LoginPage() {
  const router = useRouter();
  const { user, loading } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!loading && user) router.push("/");
  }, [loading, user, router]);

  async function withGoogle() {
    setError(null);
    try {
      await signInWithPopup(auth, new GoogleAuthProvider());
      router.push("/");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function withEmail(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await signInWithEmailAndPassword(auth, email, password);
      router.push("/");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const inputCls =
    "h-[42px] w-full rounded-[9px] border px-[13px] text-sm outline-none focus:ring-[3px]";

  return (
    <main className="flex flex-1 items-center justify-center p-6">
      <div
        className="w-[384px] rounded-2xl border p-8 shadow-sm"
        style={{ background: "var(--surface)", borderColor: "var(--border)" }}
      >
        <div className="flex items-center gap-2.5">
          <span
            className="flex h-[30px] w-[30px] items-center justify-center rounded-lg text-base font-bold"
            style={{ background: "var(--text)", color: "var(--surface)" }}
          >
            H
          </span>
          <span
            className="text-lg font-semibold"
            style={{ color: "var(--text)" }}
          >
            Hermes
          </span>
        </div>

        <p className="mt-4 text-sm leading-relaxed" style={{ color: "var(--muted)" }}>
          Sign in to review your matched jobs.
        </p>

        <button
          onClick={withGoogle}
          className="mt-[22px] flex h-[42px] w-full items-center justify-center gap-2.5 rounded-[9px] border text-sm font-semibold"
          style={{
            background: "var(--surface)",
            borderColor: "var(--border)",
            color: "var(--text)",
          }}
        >
          <span className="font-bold" style={{ color: "#4285f4" }}>
            G
          </span>{" "}
          Continue with Google
        </button>

        <div className="my-5 flex items-center gap-3">
          <span className="h-px flex-1" style={{ background: "var(--border)" }} />
          <span
            className="font-mono text-[11px] tracking-wider"
            style={{ color: "var(--subtle)" }}
          >
            OR
          </span>
          <span className="h-px flex-1" style={{ background: "var(--border)" }} />
        </div>

        <form onSubmit={withEmail}>
          <label
            className="mb-1.5 block text-xs font-medium"
            style={{ color: "var(--label)" }}
          >
            Email
          </label>
          <input
            type="email"
            placeholder="you@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className={inputCls}
            style={
              {
                background: "var(--surface)",
                borderColor: "var(--border)",
                color: "var(--text)",
                "--tw-ring-color": "var(--accent)",
              } as React.CSSProperties
            }
          />
          <label
            className="mb-1.5 mt-3.5 block text-xs font-medium"
            style={{ color: "var(--label)" }}
          >
            Password
          </label>
          <input
            type="password"
            placeholder="••••••••••"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className={inputCls}
            style={
              {
                background: "var(--surface)",
                borderColor: "var(--border)",
                color: "var(--text)",
                "--tw-ring-color": "var(--accent)",
              } as React.CSSProperties
            }
          />
          <button
            type="submit"
            className="mt-[18px] h-[42px] w-full rounded-[9px] text-sm font-semibold"
            style={{ background: "var(--text)", color: "var(--surface)" }}
          >
            Sign in with email
          </button>
        </form>

        {error && (
          <p className="mt-4 text-sm" style={{ color: "var(--danger)" }}>
            {error}
          </p>
        )}

        <p
          className="mt-[18px] text-center text-xs"
          style={{ color: "var(--subtle)" }}
        >
          Access is restricted to invited reviewers.
        </p>
      </div>
    </main>
  );
}
