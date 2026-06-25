// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { apiUpload } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Profile } from "@/lib/types";

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

const PARSE_STEPS = [
  "Read document",
  "Extract work history",
  "Identify skills & expertise",
  "Detect location & work preferences",
  "Save profile to your account",
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
    return <div className="p-8" style={{ color: "var(--muted)" }}>Loading…</div>;
  }

  return (
    <main className="flex flex-1 items-center justify-center p-6">
      <div className="w-[540px] max-w-full">
        <Brand />
        <StepRail step={1} />

        {phase === "idle" && (
          <div className="h-phase">
            <h1
              className="mt-[22px] text-center text-[26px] font-semibold tracking-tight"
              style={{ color: "var(--text)" }}
            >
              Upload your resume
            </h1>
            <p
              className="mx-auto mt-2.5 max-w-[440px] text-center text-sm leading-relaxed"
              style={{ color: "var(--muted)" }}
            >
              Hermes reads it once to learn your experience and skills &mdash; then
              finds and ranks jobs automatically.
            </p>

            {error && (
              <div
                className="mt-4 rounded-[10px] border px-3.5 py-2.5 text-[13px]"
                style={{
                  background: "var(--surface)",
                  borderColor: "var(--danger-border)",
                  color: "var(--danger)",
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
              className="mt-6 block cursor-pointer rounded-[14px] border-[1.5px] border-dashed px-6 py-10 text-center transition-colors"
              style={{
                background: "var(--surface)",
                borderColor: dragging ? "var(--accent)" : "var(--border-strong)",
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
                className="mx-auto flex h-[46px] w-[46px] items-center justify-center rounded-[11px] border text-xl"
                style={{
                  background: "var(--accent-bg)",
                  borderColor: "var(--accent-border)",
                  color: "var(--accent-text)",
                }}
              >
                ↑
              </span>
              <span
                className="mt-3.5 block text-[15px] font-semibold"
                style={{ color: "var(--text)" }}
              >
                Drag &amp; drop your resume
              </span>
              <span
                className="mt-1 block text-[13px]"
                style={{ color: "var(--subtle)" }}
              >
                PDF or DOCX &middot; up to 10&nbsp;MB
              </span>
              <span
                className="mt-4 inline-flex h-[38px] items-center rounded-[9px] px-[18px] text-[13px] font-semibold"
                style={{ background: "var(--text)", color: "var(--surface)" }}
              >
                Browse files
              </span>
            </label>

            {DEV_TOOLS && (
              <div className="mt-4 text-center">
                <button
                  onClick={onSample}
                  className="text-[13px] font-semibold"
                  style={{ color: "var(--accent)" }}
                >
                  Use a sample resume →
                </button>
              </div>
            )}

            <div className="my-[18px] flex items-center gap-3">
              <span className="h-px flex-1" style={{ background: "var(--border)" }} />
              <span
                className="font-mono text-[11px]"
                style={{ color: "var(--subtle)" }}
              >
                OR
              </span>
              <span className="h-px flex-1" style={{ background: "var(--border)" }} />
            </div>

            {pasteOpen ? (
              <div>
                <textarea
                  value={pasteText}
                  onChange={(e) => setPasteText(e.target.value)}
                  rows={8}
                  placeholder="Paste the full text of your resume here…"
                  className="w-full rounded-[10px] border p-3 text-sm outline-none focus:ring-[3px]"
                  style={
                    {
                      background: "var(--surface)",
                      borderColor: "var(--border)",
                      color: "var(--text)",
                      "--tw-ring-color": "var(--ring)",
                    } as React.CSSProperties
                  }
                />
                <div className="mt-2.5 flex items-center justify-between">
                  <button
                    onClick={() => setPasteOpen(false)}
                    className="text-[13px] font-medium"
                    style={{ color: "var(--muted)" }}
                  >
                    ← Back to upload
                  </button>
                  <button
                    onClick={onPaste}
                    disabled={!pasteText.trim()}
                    className="h-[38px] rounded-[9px] px-[18px] text-[13px] font-semibold disabled:opacity-40"
                    style={{ background: "var(--text)", color: "var(--surface)" }}
                  >
                    Build my profile →
                  </button>
                </div>
              </div>
            ) : (
              <button
                onClick={() => setPasteOpen(true)}
                className="flex h-10 w-full items-center justify-center gap-2 rounded-[9px] border text-[13px] font-semibold"
                style={{
                  background: "var(--surface)",
                  borderColor: "var(--border)",
                  color: "var(--label)",
                }}
              >
                ¶ Paste resume text
              </button>
            )}

            <p
              className="mt-[18px] text-center text-xs"
              style={{ color: "var(--subtle)" }}
            >
              🔒 Stored privately in your account. Used only to match you to jobs.
            </p>
          </div>
        )}

        {phase === "uploading" && <UploadingView file={fileMeta} />}
        {phase === "parsing" && <ParsingView file={fileMeta} />}
      </div>
    </main>
  );
}

function Brand() {
  return (
    <div className="flex items-center justify-center gap-2.5">
      <span
        className="flex h-7 w-7 items-center justify-center rounded-lg text-[15px] font-bold"
        style={{ background: "var(--text)", color: "var(--surface)" }}
      >
        H
      </span>
      <span className="text-base font-semibold" style={{ color: "var(--text)" }}>
        Hermes
      </span>
    </div>
  );
}

function StepRail({ step }: { step: 1 | 2 }) {
  return (
    <div className="mt-5 flex items-center justify-center gap-2 font-mono text-[11px] font-semibold">
      <RailItem n={1} label="Upload" active={step === 1} done={step > 1} />
      <span className="h-px w-[26px]" style={{ background: "var(--border)" }} />
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
      className="inline-flex items-center gap-1.5"
      style={{ color: on ? "var(--text)" : "var(--subtle)" }}
    >
      <span
        className="flex h-[18px] w-[18px] items-center justify-center rounded-full border text-[10px]"
        style={
          on
            ? { background: "var(--text)", color: "var(--surface)", borderColor: "var(--text)" }
            : { borderColor: "var(--border-strong)", color: "var(--subtle)" }
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
      className="flex items-center gap-3 rounded-xl border px-4 py-3"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      <span
        className="flex h-[42px] w-[34px] flex-none items-center justify-center rounded-md border font-mono text-[10px] font-bold"
        style={{
          background: "var(--surface-2)",
          borderColor: "var(--danger-border)",
          color: "var(--danger)",
        }}
      >
        {file?.kind ?? "DOC"}
      </span>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-semibold" style={{ color: "var(--text)" }}>
          {file?.name ?? "resume"}
        </div>
        <div className="mt-0.5 font-mono text-xs" style={{ color: "var(--subtle)" }}>
          {fmtSize(file?.size) || "resume"}
        </div>
      </div>
      <span className="font-mono text-[11px] font-semibold" style={{ color: status.color }}>
        {status.text}
      </span>
    </div>
  );
}

function ProgressBar({ pct, duration }: { pct: number; duration: string }) {
  return (
    <div
      className="mt-[18px] h-1.5 overflow-hidden rounded-full"
      style={{ background: "var(--surface-2)" }}
    >
      <div
        className="h-full rounded-full"
        style={{
          width: `${pct}%`,
          background: "var(--accent)",
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
      <FileChip file={file} status={{ text: `${pct}%`, color: "var(--accent)" }} />
      <h1
        className="mt-[26px] text-center text-[22px] font-semibold tracking-tight"
        style={{ color: "var(--text)" }}
      >
        Uploading…
      </h1>
      <p className="mt-2 text-center text-[13px]" style={{ color: "var(--muted)" }}>
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
      <FileChip file={file} status={{ text: "uploaded ✓", color: "var(--good)" }} />
      <h1
        className="mt-6 text-center text-[22px] font-semibold tracking-tight"
        style={{ color: "var(--text)" }}
      >
        Building your profile…
      </h1>
      <p className="mt-2 text-center text-[13px]" style={{ color: "var(--muted)" }}>
        Hermes is extracting your experience and saving it to your account.
      </p>
      <ProgressBar pct={pct} duration="0.4s cubic-bezier(0.22,0.61,0.36,1)" />

      <div className="mt-5 flex flex-col gap-[11px]">
        {PARSE_STEPS.map((label, i) => {
          const state = i < active ? "done" : i === active ? "active" : "todo";
          return (
            <div
              key={label}
              className="flex items-center gap-3"
              style={{ opacity: state === "todo" ? 0.5 : 1 }}
            >
              {state === "done" ? (
                <span
                  className="h-pop flex h-[22px] w-[22px] items-center justify-center rounded-full border text-xs"
                  style={{
                    background: "var(--good-bg)",
                    borderColor: "var(--good-border)",
                    color: "var(--good)",
                  }}
                >
                  ✓
                </span>
              ) : state === "active" ? (
                <span
                  className="inline-block h-[22px] w-[22px] rounded-full"
                  style={{
                    border: "2px solid var(--accent)",
                    borderTopColor: "transparent",
                    animation: "hspin 0.8s linear infinite",
                  }}
                />
              ) : (
                <span
                  className="h-[22px] w-[22px] rounded-full border"
                  style={{ background: "var(--surface)", borderColor: "var(--border-strong)" }}
                />
              )}
              <span
                className="flex-1 text-sm"
                style={{
                  color: state === "active" ? "var(--text)" : "var(--label)",
                  fontWeight: state === "active" ? 600 : 400,
                }}
              >
                {label}
              </span>
              {state === "active" && (
                <span className="font-mono text-xs" style={{ color: "var(--accent)" }}>
                  scanning…
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
