// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

// The auth context on its own, with no `@/lib/firebase` import (which throws
// without NEXT_PUBLIC_* env at module load). `AuthProvider` in `@/lib/auth`
// feeds it; components that only *read* auth — and their tests — import from
// here. `useAuth` is re-exported from `@/lib/auth`, so existing import sites
// keep working.
import type { User } from "firebase/auth"; // type-only: erased, no runtime import
import { createContext, useContext } from "react";

export type AuthState = { user: User | null; loading: boolean };

export const AuthContext = createContext<AuthState>({ user: null, loading: true });

export function useAuth(): AuthState {
  return useContext(AuthContext);
}
