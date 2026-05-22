"use client";

import {
  GoogleAuthProvider,
  onAuthStateChanged,
  signInWithPopup,
  signOut as firebaseSignOut,
  type User as FirebaseUser,
} from "firebase/auth";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { firebaseConfigured, getFirebaseAuth } from "./firebase";

export interface MeProfile {
  id: string;
  email: string | null;
  display_name: string | null;
  photo_url: string | null;
  is_super_admin: boolean;
}

interface AuthCtx {
  ready: boolean;
  firebaseConfigured: boolean;
  firebaseUser: FirebaseUser | null;
  me: MeProfile | null;
  loadingMe: boolean;
  error: string | null;
  signInWithGoogle: () => Promise<void>;
  signOut: () => Promise<void>;
  refresh: () => Promise<void>;
}

const Ctx = createContext<AuthCtx | null>(null);

// Identity provider — Beli Aman BAP is the network IdP for the seller +
// onboarding stack. /api/bap/* is a Next rewrite to the BAP host.
const IDENTITY_BASE = "/api/bap";

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [firebaseUser, setFirebaseUser] = useState<FirebaseUser | null>(null);
  const [ready, setReady] = useState(false);
  const [me, setMe] = useState<MeProfile | null>(null);
  const [loadingMe, setLoadingMe] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchMe = useCallback(async () => {
    const auth = getFirebaseAuth();
    if (!auth?.currentUser) {
      setMe(null);
      return;
    }
    setLoadingMe(true);
    setError(null);
    try {
      const token = await auth.currentUser.getIdToken();
      const res = await fetch(`${IDENTITY_BASE}/api/v1/me`, {
        headers: { Authorization: `Bearer ${token}` },
        cache: "no-store",
      });
      if (!res.ok) {
        setMe(null);
        setError(`Identity lookup failed (${res.status})`);
        return;
      }
      const j = await res.json();
      setMe({
        id: j.id,
        email: j.email ?? null,
        display_name: j.display_name ?? null,
        photo_url: j.photo_url ?? null,
        is_super_admin: !!j.is_super_admin,
      });
    } catch (e) {
      setMe(null);
      setError(e instanceof Error ? e.message : "Identity lookup failed");
    } finally {
      setLoadingMe(false);
    }
  }, []);

  useEffect(() => {
    if (!firebaseConfigured) {
      setReady(true);
      return;
    }
    const auth = getFirebaseAuth();
    if (!auth) {
      setReady(true);
      return;
    }
    const unsub = onAuthStateChanged(auth, async (u) => {
      setFirebaseUser(u);
      setReady(true);
      if (u) {
        await fetchMe();
      } else {
        setMe(null);
        setError(null);
      }
    });
    return () => unsub();
  }, [fetchMe]);

  const signInWithGoogle = useCallback(async () => {
    const auth = getFirebaseAuth();
    if (!auth) throw new Error("Firebase not configured");
    const provider = new GoogleAuthProvider();
    await signInWithPopup(auth, provider);
  }, []);

  const signOut = useCallback(async () => {
    const auth = getFirebaseAuth();
    if (auth) await firebaseSignOut(auth);
    setMe(null);
    setError(null);
  }, []);

  const value: AuthCtx = useMemo(
    () => ({
      ready,
      firebaseConfigured,
      firebaseUser,
      me,
      loadingMe,
      error,
      signInWithGoogle,
      signOut,
      refresh: fetchMe,
    }),
    [ready, firebaseUser, me, loadingMe, error, signInWithGoogle, signOut, fetchMe]
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth must be used inside AuthProvider");
  return v;
}
