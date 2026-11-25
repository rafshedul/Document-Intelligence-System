"use client";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { ApiError, API_URL, UNAUTHORIZED_EVENT, api, setToken } from "@/lib/api";
import type { Info } from "@/lib/types";
import { ErrorState, LoadingBlock } from "./ui";

const InfoContext = createContext<Info | null>(null);
export const useInfo = (): Info | null => useContext(InfoContext);

/** Loads server settings once. If the API wants a token, shows a sign-in box instead of the app. */
export function AuthGate({ children }: { children: ReactNode }) {
  const [info, setInfo] = useState<Info | null>(null);
  const [state, setState] = useState<"loading" | "login" | "error" | "ready">("loading");
  const [error, setError] = useState<unknown>(null);
  const [token, setTokenInput] = useState("");
  const [loginError, setLoginError] = useState("");

  const load = useCallback(async () => {
    setState("loading");
    try {
      setInfo(await api.info());
      setState("ready");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setState("login");
      else { setError(e); setState("error"); }
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const onUnauthorized = () => setState("login");
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  async function signIn(e: React.FormEvent) {
    e.preventDefault();
    setLoginError("");
    setToken(token.trim());
    try {
      setInfo(await api.info());
      setTokenInput("");
      setState("ready");
    } catch (err) {
      sessionStorage.removeItem("docintel_token");
      setLoginError(err instanceof ApiError && err.status === 401 ? "That token is not right." : err instanceof Error ? err.message : "Sign-in failed.");
    }
  }

  if (state === "loading") return <LoadingBlock label="Connecting…" />;
  if (state === "error") return <div className="mx-auto max-w-lg p-6"><ErrorState title={`Cannot load the app (API: ${API_URL})`} error={error} onRetry={load} /></div>;
  if (state === "login") {
    return (
      <div className="mx-auto mt-16 max-w-sm px-4">
        <form onSubmit={signIn} className="card space-y-3">
          <h1 className="text-lg font-semibold">Sign in</h1>
          <p className="text-sm text-slate-600">Enter the API token (<code className="rounded bg-slate-100 px-1">API_AUTH_TOKEN</code> on the server).</p>
          <label className="label" htmlFor="token">Token</label>
          <input id="token" className="input" type="password" autoFocus autoComplete="off" value={token} onChange={(e) => setTokenInput(e.target.value)} />
          {loginError && <p className="text-sm text-red-700" role="alert">{loginError}</p>}
          <button className="btn btn-primary w-full" type="submit" disabled={!token.trim()}>Sign in</button>
        </form>
      </div>
    );
  }
  return <InfoContext.Provider value={info}>{children}</InfoContext.Provider>;
}
