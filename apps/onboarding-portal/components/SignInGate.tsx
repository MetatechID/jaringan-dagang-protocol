"use client";

import { useAuth } from "@/lib/auth-context";

export default function SignInGate({ children }: { children: React.ReactNode }) {
  const {
    ready,
    firebaseConfigured,
    firebaseUser,
    me,
    loadingMe,
    error,
    signInWithGoogle,
    signOut,
  } = useAuth();

  // No Firebase config → portal is open (dev/preview only — production
  // always has the env baked in).
  if (!firebaseConfigured) return <>{children}</>;

  if (!ready || (firebaseUser && loadingMe)) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-surface-900">
        <div className="text-sm text-slate-400">Verifying access…</div>
      </div>
    );
  }

  // Authenticated AND super admin → let them in.
  if (firebaseUser && me?.is_super_admin) {
    return <>{children}</>;
  }

  // Authenticated but not super admin → 403 screen with sign-out.
  if (firebaseUser && me && !me.is_super_admin) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-surface-900 px-4">
        <div className="w-full max-w-md rounded-2xl border border-slate-700/60 bg-slate-900/60 p-8 backdrop-blur">
          <div className="text-center mb-6">
            <div className="w-12 h-12 mx-auto mb-3 rounded-xl bg-rose-600 grid place-items-center text-white text-xl font-bold">!</div>
            <h1 className="text-xl font-semibold text-slate-100">
              Network admin only
            </h1>
            <p className="text-sm text-slate-400 mt-2">
              You're signed in as <span className="text-slate-200">{me.email}</span>, but
              this portal is restricted to Jaringan Dagang network administrators.
              Contact the network team if you believe this is a mistake.
            </p>
          </div>
          <button
            onClick={() => signOut().catch(() => {})}
            className="w-full px-4 py-3 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-lg text-sm font-medium text-slate-200"
          >
            Sign out
          </button>
        </div>
      </div>
    );
  }

  // Authenticated but identity lookup failed.
  if (firebaseUser && !me) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-surface-900 px-4">
        <div className="w-full max-w-md rounded-2xl border border-slate-700/60 bg-slate-900/60 p-8 backdrop-blur text-center">
          <h1 className="text-lg font-semibold text-slate-100 mb-2">
            Couldn't verify your identity
          </h1>
          <p className="text-sm text-slate-400 mb-6">
            {error || "Identity service is unreachable. Please try again."}
          </p>
          <button
            onClick={() => signOut().catch(() => {})}
            className="w-full px-4 py-3 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-lg text-sm font-medium text-slate-200"
          >
            Sign out
          </button>
        </div>
      </div>
    );
  }

  // Not signed in → Google sign-in card.
  return (
    <div className="min-h-screen flex items-center justify-center bg-surface-900 px-4">
      <div className="w-full max-w-md rounded-2xl border border-slate-700/60 bg-slate-900/60 p-8 backdrop-blur">
        <div className="text-center mb-6">
          <div className="w-12 h-12 mx-auto mb-3 rounded-xl bg-indigo-600 grid place-items-center text-white text-xl font-bold">
            JD
          </div>
          <h1 className="text-xl font-semibold text-slate-100">
            Jaringan Dagang Network
          </h1>
          <p className="text-sm text-slate-400 mt-2">
            Sign in to manage the open commerce network. Super-admin access only.
          </p>
        </div>

        <button
          onClick={() =>
            signInWithGoogle().catch((e) =>
              alert(`Sign-in failed: ${e.message || e}`)
            )
          }
          className="w-full flex items-center justify-center gap-3 px-4 py-3 bg-white hover:bg-slate-100 rounded-lg text-sm font-medium text-slate-900"
        >
          <svg className="w-5 h-5" viewBox="0 0 24 24">
            <path
              fill="#4285F4"
              d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"
            />
            <path
              fill="#34A853"
              d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.99.66-2.25 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"
            />
            <path
              fill="#FBBC05"
              d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"
            />
            <path
              fill="#EA4335"
              d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"
            />
          </svg>
          Sign in with Google
        </button>

        <p className="mt-6 text-xs text-center text-slate-500">
          Identity provided by Beli Aman — same login as the seller dashboard.
        </p>
      </div>
    </div>
  );
}
