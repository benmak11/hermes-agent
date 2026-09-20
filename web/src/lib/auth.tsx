// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { onIdTokenChanged, type User } from "firebase/auth";
import { useEffect, useState } from "react";

import { AuthContext } from "@/lib/authContext";
import { auth } from "@/lib/firebase";

export { useAuth } from "@/lib/authContext";

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  // Use onIdTokenChanged (not onAuthStateChanged) so token refreshes are seen.
  useEffect(
    () =>
      onIdTokenChanged(auth, (u) => {
        setUser(u);
        setLoading(false);
      }),
    [],
  );

  return (
    <AuthContext.Provider value={{ user, loading }}>
      {children}
    </AuthContext.Provider>
  );
}
