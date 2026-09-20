// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { apiUpload } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Profile } from "@/lib/types";
import { CARD, SERIF } from "@/components/warm/styles";

type Phase = "idle" | "uploading" | "parsing";
type FileMeta = { name: string; size?: number; kind: string };

const ACCEPT = ".pdf,.docx,.doc,.txt";

// Dev-only affordance: the sample-resume button runs a real extraction and
// overwrites the signed-in user's profile, so it's hidden in production. On
// automatically under `next dev`; opt in for a deployed dev instance with
// NEXT_PUBLIC_DEV_TOOLS=1 (inlined at build time).
const DEV_TOOLS =
  process.env.NODE_ENV !== "production" ||
  process.env.NEXT_PUBLIC_DEV_TOOLS === "1";

// Exactly five: the interval cap and the progress maths read its length, so
// the count sets the visible timing.
const PARSE_STEPS = [
  "Reading your resume",
  "Your work history",
  "Skills & expertise",
  "Where and how you want to work",
  "Saving your profile",
];

// A canned resume so a user can try the flow without their own file. Runs the
// real extraction (it just supplies the text).
const SAMPLE_RESUME = `Alex Rivera
Austin, TX · alex.rivera@example.com · github.com/alexrivera

SUMMARY
Senior Backend Engineer with 8 years building distributed systems and payment
infrastructure. Open to remote or hybrid roles.

EXPERIENCE
Staff Backend Engineer — Stripe (2021 – Present)
- Led payments ledger re-architecture handling 4M+ transactions/day across 30 services.
- Built an async event pipeline that cut settlement latency by 60%.
- Mentored 6 engineers; drove the team's migration to gRPC.

Senior Software Engineer — Plaid (2018 – 2021)
- Owned the bank-integration platform powering 200+ institution connections.
- Reduced p99 API latency by 40% via connection pooling and caching.

Software Engineer — Datadog (2016 – 2018)
- Built ingestion services in Go processing 1M events/sec.

SKILLS
Go, Python, Distributed systems, Kubernetes, gRPC, PostgreSQL, LLM integration

EDUCATION
B.S. Computer Science — University of Texas at Austin (2012 – 2016)`;

function fileKind(name: string): string {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  if (ext === "pdf") return "PDF";
  if (ext === "docx" || ext === "doc") return "DOC";
  return "TXT";
}

export default function OnboardingPage() {
  const { user, loading } = useAuth();
  const router = useRouter();

  const [phase, setPhase] = useState<Phase>("idle");
  const [fileMeta, setFileMeta] = useState<FileMeta | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pasteOpen, setPasteOpen] = useState(false);
  const [pasteText, setPasteText] = useState("");
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!loading && !user) router.push("/login");
  }, [loading, user, router]);

  const submit = useCallback(
    async (body: FormData, meta: FileMeta) => {
      setError(null);
      setFileMeta(meta);
      setPhase("uploading");
      // The upload + extraction is one request; show the "uploading" beat, then
      // flip to "parsing" while the (longer) extraction finishes.
      const toParsing = setTimeout(() => setPhase("parsing"), 1000);
      try {
        await apiUpload<{ profile: Profile }>("/profile/extract", body);
        clearTimeout(toParsing);
        router.push("/onboarding/review");
      } catch (e) {
        clearTimeout(toParsing);
        setError(
          e instanceof Error
            ? e.message.replace(/^\d+:\s*/, "")
            : "Something went wrong reading your resume.",
        );
        setPhase("idle");
      }
    },
    [router],
  );

  const onFile = useCallback(
    (file: File) => {
      const body = new FormData();
      body.append("file", file);
      submit(body, { name: file.name, size: file.size, kind: fileKind(file.name) });
    },
    [submit],
  );

  const onSample = useCallback(() => {
    const body = new FormData();
    body.append("text", SAMPLE_RESUME);
    submit(body, {
      name: "sample-resume.txt",
      size: SAMPLE_RESUME.length,
      kind: "TXT",
    });
  }, [submit]);

  const onPaste = useCallback(() => {
    if (!pasteText.trim()) return;
    const body = new FormData();
    body.append("text", pasteText);
    submit(body, { name: "Pasted resume", kind: "TXT" });
  }, [pasteText, submit]);

  if (loading || !user) {
    return <div className="p-8" style={{ color: "var(--ink-4)" }}>Loading…</div>;
  }

  return (
    <main className="flex flex-1 items-center justify-center p-6">
      <div style={{ ...CARD, width: "min(560px, 100%)", padding: 36 }}>
        <StepRail step={1} />

        {phase === "idle" && (
          <div className="h-phase">
            <h1
              className="mt-6 text-center text-[34px] font-normal"
              style={{ fontFamily: SERIF, lineHeight: 1.15, color: "var(--ink)" }}
            >
              Start with your resume
            </h1>
            <p
              className="mt-3 text-center text-[14.5px]"
              style={{ color: "var(--ink-4)", lineHeight: 1.6, textWrap: "pretty" }}
            >
              We read it once to learn your experience, your skills, and the kind
              of work you actually want &mdash; then go find and rank jobs for you.
            </p>

            {error && (
              <div
                className="mt-4 rounded-[14px] border px-3.5 py-[11px] text-[13.5px]"
                style={{
                  background: "#fdeeea",
                  borderColor: "#f2cfc3",
                  color: "var(--brick)",
                }}
              >
                {error}
              </div>
            )}

            <label
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragging(false);
                const f = e.dataTransfer.files?.[0];
                if (f) onFile(f);
              }}
              className="wm-drop mt-[26px] block cursor-pointer rounded-[20px] border-[1.5px] border-dashed px-[26px] py-9 text-center transition-colors"
              style={{
                background: dragging ? "var(--terracotta-tint)" : "#fdf7ee",
                // Border colour lives on .wm-drop (rest + hover); inline only
                // while dragging, or the inline value would beat :hover.
                ...(dragging ? { borderColor: "var(--terracotta)" } : {}),
              }}
            >
              <input
                ref={inputRef}
                type="file"
                accept={ACCEPT}
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) onFile(f);
                }}
              />
              <span
                className="mx-auto flex h-[54px] w-[54px] items-center justify-center rounded-2xl border text-[22px]"
                style={{
                  background: "var(--honey-tint)",
                  borderColor: "#f4dfb4",
                  color: "#9a6216",
                }}
              >
                ⬆
              </span>
              <span
                className="mt-4 block text-base font-semibold"
                style={{ color: "var(--ink)" }}
              >
                Drop your resume here
              </span>
              <span
                className="mt-[5px] block text-[13px]"
                style={{ color: "#a3927f" }}
              >
                PDF or DOCX &middot; up to 10&nbsp;MB
              </span>
              <span className="wm-cta mt-5 inline-flex h-[42px] items-center rounded-xl px-[22px] text-sm font-semibold">
                Browse files
              </span>
            </label>

            {DEV_TOOLS && (
              <div className="mt-4 text-center">
                <button
                  onClick={onSample}
                  className="wm-link text-[13px] font-semibold"
                >
                  Use a sample resume →
                </button>
              </div>
            )}

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

            {pasteOpen ? (
              <div>
                <textarea
                  value={pasteText}
                  onChange={(e) => setPasteText(e.target.value)}
                  rows={8}
                  placeholder="Paste the full text of your resume here…"
                  className="wm-input w-full rounded-[13px] p-3 text-[14px] outline-none"
                />
                <div className="mt-2.5 flex items-center justify-between">
                  <button
                    onClick={() => setPasteOpen(false)}
                    className="text-[13px] font-medium"
                    style={{ color: "var(--ink-4)" }}
                  >
                    ← Back to upload
                  </button>
                  <button
                    onClick={onPaste}
                    disabled={!pasteText.trim()}
                    className="wm-cta h-[42px] rounded-xl px-[22px] text-sm font-semibold"
                  >
                    Build my profile →
                  </button>
                </div>
              </div>
            ) : (
              <button
                onClick={() => setPasteOpen(true)}
                className="wm-ghost flex h-[42px] w-full items-center justify-center gap-2 rounded-[13px] border text-[13.5px] font-semibold"
                style={{ borderColor: "#e8dacb", color: "var(--ink-2)" }}
              >
                ¶ Paste resume text
              </button>
            )}

            <p
              className="mt-[18px] text-center text-[12.5px]"
              style={{ color: "#a3927f" }}
            >
              Kept private. Used only to match you to jobs &mdash; never shared.
            </p>
          </div>
        )}

        {phase === "uploading" && <UploadingView file={fileMeta} />}
        {phase === "parsing" && <ParsingView file={fileMeta} />}
      </div>
    </main>
  );
}

function StepRail({ step }: { step: 1 | 2 }) {
  return (
    <div className="flex items-center justify-center gap-2.5 text-[11.5px] font-bold">
      <RailItem n={1} label="Upload" active={step === 1} done={step > 1} />
      <span className="h-px w-[30px]" style={{ background: "#dfd0bd" }} />
      <RailItem n={2} label="Review" active={step === 2} done={false} />
    </div>
  );
}

function RailItem({
  n,
  label,
  active,
  done,
}: {
  n: number;
  label: string;
  active: boolean;
  done: boolean;
}) {
  const on = active || done;
  return (
    <span
      className="inline-flex items-center gap-[7px]"
      style={{ color: on ? "var(--ink)" : "#b0a08d" }}
    >
      <span
        className="flex h-5 w-5 items-center justify-center rounded-full border text-[11px]"
        style={
          on
            ? { background: "var(--terracotta)", color: "#fff9f2", borderColor: "var(--terracotta)" }
            : { borderColor: "#dfd0bd", color: "#b0a08d" }
        }
      >
        {done ? "✓" : n}
      </span>
      {label}
    </span>
  );
}

function fmtSize(bytes?: number): string {
  if (!bytes) return "";
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function FileChip({
  file,
  status,
}: {
  file: FileMeta | null;
  status: { text: string; color: string };
}) {
  return (
    <div
      className="flex items-center gap-[13px] rounded-2xl border px-[17px] py-3.5"
      style={{ background: "#fbf6ef", borderColor: "var(--border-warm-hair)" }}
    >
      <span
        className="flex h-11 w-9 flex-none items-center justify-center rounded-lg border text-[10px] font-bold"
        style={{
          background: "#fdeeea",
          borderColor: "#f2cfc3",
          color: "var(--terracotta)",
          letterSpacing: "0.06em",
        }}
      >
        {file?.kind ?? "DOC"}
      </span>
      <div className="min-w-0 flex-1">
        <div className="truncate text-[14.5px] font-semibold" style={{ color: "var(--ink)" }}>
          {file?.name ?? "resume"}
        </div>
        <div className="mt-0.5 text-[12.5px]" style={{ color: "#a3927f" }}>
          {fmtSize(file?.size) || "resume"}
        </div>
      </div>
      <span className="text-[11.5px] font-bold" style={{ color: status.color }}>
        {status.text}
      </span>
    </div>
  );
}

function ProgressBar({ pct, duration }: { pct: number; duration: string }) {
  return (
    <div
      className="mt-5 h-2 overflow-hidden rounded-full"
      style={{ background: "#f0e3d3" }}
    >
      <div
        className="h-full rounded-full"
        style={{
          width: `${pct}%`,
          background: "linear-gradient(90deg,#d9873f,var(--terracotta))",
          transition: `width ${duration}`,
        }}
      />
    </div>
  );
}

function UploadingView({ file }: { file: FileMeta | null }) {
  const [pct, setPct] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setPct((p) => Math.min(100, p + 9)), 90);
    return () => clearInterval(t);
  }, []);
  return (
    <div className="mt-6 h-phase">
      <FileChip file={file} status={{ text: `${pct}%`, color: "var(--terracotta-d)" }} />
      <h1
        className="mt-7 text-center text-[30px] font-normal"
        style={{ fontFamily: SERIF, lineHeight: 1.15, color: "var(--ink)" }}
      >
        Sending your resume…
      </h1>
      <p className="mt-2 text-center text-[14.5px]" style={{ color: "var(--ink-4)" }}>
        Securely transferring your resume.
      </p>
      <ProgressBar pct={pct} duration="0.12s linear" />
    </div>
  );
}

function ParsingView({ file }: { file: FileMeta | null }) {
  // The extraction is one atomic call; advance the visible step on a timer so
  // the wait reads as progress. The final step lands when we navigate away.
  const [active, setActive] = useState(0);
  useEffect(() => {
    const t = setInterval(
      () => setActive((i) => Math.min(i + 1, PARSE_STEPS.length - 1)),
      820,
    );
    return () => clearInterval(t);
  }, []);
  const pct = Math.round(((active + 1) / PARSE_STEPS.length) * 100);

  return (
    <div className="mt-6 h-phase">
      <FileChip file={file} status={{ text: "uploaded ✓", color: "var(--sage)" }} />
      <h1
        className="mt-7 text-center text-[30px] font-normal"
        style={{ fontFamily: SERIF, lineHeight: 1.15, color: "var(--ink)" }}
      >
        Getting to know you…
      </h1>
      <p className="mt-2 text-center text-[14.5px]" style={{ color: "var(--ink-4)" }}>
        Hermes is extracting your experience and saving it to your account.
      </p>
      <ProgressBar pct={pct} duration="0.4s cubic-bezier(0.22,0.61,0.36,1)" />

      <div className="mt-6 flex flex-col gap-[13px]">
        {PARSE_STEPS.map((label, i) => {
          const state = i < active ? "done" : i === active ? "active" : "todo";
          return (
            <div
              key={label}
              className="flex items-center gap-[13px]"
              style={{ opacity: state === "todo" ? 0.5 : 1 }}
            >
              {state === "done" ? (
                <span
                  className="h-pop flex h-6 w-6 items-center justify-center rounded-full border text-xs"
                  style={{
                    background: "var(--sage-tint)",
                    borderColor: "#cfe0c8",
                    color: "var(--sage)",
                  }}
                >
                  ✓
                </span>
              ) : state === "active" ? (
                <span
                  className="inline-block h-6 w-6 rounded-full"
                  style={{
                    border: "2px solid var(--terracotta)",
                    borderTopColor: "transparent",
                    animation: "hspin 0.8s linear infinite",
                  }}
                />
              ) : (
                <span
                  className="h-6 w-6 rounded-full border"
                  style={{ background: "var(--surface-warm)", borderColor: "#dfd0bd" }}
                />
              )}
              <span
                className="flex-1 text-[14.5px]"
                style={{
                  color: state === "todo" ? "var(--ink-4)" : "var(--ink)",
                  fontWeight: state === "active" ? 600 : 400,
                }}
              >
                {label}
              </span>
              {state === "active" && (
                <span
                  className="text-[12.5px] font-semibold"
                  style={{ color: "var(--terracotta-d)" }}
                >
                  reading…
                </span>
              )}
            </div>
          );
        })}
      </div>

      <p className="mt-[22px] text-center text-[12.5px]" style={{ color: "#a3927f" }}>
        About fifteen seconds. Feel free to stretch.
      </p>
    </div>
  );
}
