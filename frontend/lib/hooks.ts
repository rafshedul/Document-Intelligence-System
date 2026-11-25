import { useEffect, useRef } from "react";

/** Call `callback` every `delayMs` while `delayMs` is a number (pass null to stop). */
export function useInterval(callback: () => void, delayMs: number | null): void {
  const saved = useRef(callback);
  useEffect(() => { saved.current = callback; }, [callback]);
  useEffect(() => {
    if (delayMs === null) return;
    const id = setInterval(() => saved.current(), delayMs);
    return () => clearInterval(id);
  }, [delayMs]);
}

export const POLL_MS = 2000;
