import { useEffect, useRef } from "react";

/**
 * Call `fn` every `intervalMs` while `active` is true. The next tick is scheduled
 * only after the previous call settles, so slow responses never pile up.
 * Nothing runs while `active` is false.
 */
export function usePoll(fn: () => unknown | Promise<unknown>, active: boolean, intervalMs = 1000): void {
  const fnRef = useRef(fn);
  useEffect(() => {
    fnRef.current = fn;
  });

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      try {
        await fnRef.current();
      } catch {
        // The caller surfaces its own errors; keep polling.
      } finally {
        if (!cancelled) timer = window.setTimeout(tick, intervalMs);
      }
    };
    timer = window.setTimeout(tick, intervalMs);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [active, intervalMs]);
}
