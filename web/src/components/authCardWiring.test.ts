import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/** The verify-email wiring, asserted against source: these modules import
 *  Firebase and cannot render in this node-only suite. */
function code(rel: string): string {
  const src = readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
  return src.replace(/\/\/[^\n]*/g, "").replace(/\/\*[\s\S]*?\*\//g, "");
}

/** The body of `name` from its declaration up to the next top-level function. */
function fnBody(src: string, name: string): string {
  const start = src.indexOf(`function ${name}(`);
  expect(start, `${name} not found`).toBeGreaterThan(-1);
  const rest = src.slice(start + 1);
  const end = rest.search(/\n  (async )?function |\n  const card =/);
  return end === -1 ? rest : rest.slice(0, end);
}

function inOrder(src: string, needles: string[]): void {
  let at = -1;
  for (const n of needles) {
    const i = src.indexOf(n, at + 1);
    expect(i, `${n} missing or out of order`).toBeGreaterThan(at);
    at = i;
  }
}

const CARD = code("./AuthCard.tsx");

describe("AuthCard verify-email wiring", () => {
  it("sends a verification email right after creating the account, before admission", () => {
    const submit = fnBody(CARD, "onSubmit");
    const create = submit.slice(submit.indexOf('setPhase("creating")'));
    inOrder(create, [
      "createUserWithEmailAndPassword(",
      "sendEmailVerification(newUser)",
      'admit("email")',
    ]);
  });

  it("does not resend on every sign-in", () => {
    const submit = fnBody(CARD, "onSubmit");
    const signin = submit.slice(0, submit.indexOf('setPhase("creating")'));
    expect(signin).toContain("signInWithEmailAndPassword(");
    expect(signin).not.toContain("sendEmailVerification");
    expect(signin).toContain('openVerify(auth.currentUser?.email ?? email.trim(), "signin")');
  });

  it("refreshes the user and forces a new token before re-admitting on I've verified", () => {
    inOrder(fnBody(CARD, "onCheckVerified"), [
      "await u.reload()",
      "await u.getIdToken(true)",
      'await admit("email")',
    ]);
  });

  it("rate-limits resend through canResend", () => {
    const resend = fnBody(CARD, "onResend");
    inOrder(resend, ["canResend(verifying, now)", "sendEmailVerification(u)"]);
    expect(resend).toContain('type: "send_failed", code: errCode(e)');
  });

  it("signs out on Use a different account", () => {
    expect(fnBody(CARD, "onUseDifferentAccount")).toContain("await auth.signOut()");
  });

  it("holds the redirect off while the verify screen is up", () => {
    expect(CARD).toContain("!lockedOut && !isVerifying");
  });

  it("opens the screen for an app-route arrival without sending anything", () => {
    expect(CARD).toContain('(verify && !verifyDone && user ? openVerify(user.email, "app") : null)');
  });

  it("routes a verify_email admission to the screen, not the waitlist", () => {
    const admit = fnBody(CARD, "admit");
    expect(admit).toMatch(/case "verify":\s*return "verify";/);
  });

  it("wires the three buttons", () => {
    expect(CARD).toContain("I've verified");
    expect(CARD).toContain("Resend email");
    expect(CARD).toContain("Use a different account");
    expect(CARD).toContain("onCheck={onCheckVerified}");
    expect(CARD).toContain("onResend={onResend}");
    expect(CARD).toContain("onSwitch={onUseDifferentAccount}");
  });
});

describe("verify-email routing", () => {
  it("passes ?verify=1 from the login page into the card", () => {
    expect(code("../app/(auth)/login/page.tsx")).toContain(
      'verify={params.get("verify") === "1"}',
    );
  });

  it("sends an app 403 email_unverified to the verify screen before throwing", () => {
    const api = code("../lib/api.ts");
    inOrder(api, ["routeToVerify(res.status, body);", "throw new ApiError(res.status, body, id);"]);
    expect(api).toContain("verifyRedirect(status, body, window.location.pathname");
  });
});

describe("onboarding extract-cap wiring", () => {
  it("renders the upload failure through extractErrorMessage", () => {
    const page = code("../app/(auth)/onboarding/page.tsx");
    expect(page).toContain("setError(extractErrorMessage(e));");
    expect(page).not.toContain("e.message.replace(");
  });
});
