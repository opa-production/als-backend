import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api.js";

/**
 * Fetches one endpoint and re-fetches when its query changes.
 *
 * Two behaviours are worth naming, because both are about not losing the
 * reader's place:
 *
 * **`refetching` is separate from `loading`.** The first load shows skeletons;
 * every load after that holds the previous render at reduced opacity. Swapping
 * a filled table for skeletons on each keystroke makes the page jump and
 * throws away the row someone was reading.
 *
 * **Stale responses are dropped.** Typing in a search box fires a request per
 * change and they do not come back in order. Without the sequence check, a slow
 * response for "wan" can land after "wanjiru" and repopulate the table with the
 * wrong rows — which looks like a bug in the filter, not in the fetching.
 */
export function useApi(path, query, options = {}) {
  const { enabled = true } = options;

  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(enabled);
  const [refetching, setRefetching] = useState(false);

  const sequence = useRef(0);
  const hasLoaded = useRef(false);
  // Serialised so a fresh object literal with identical values does not
  // re-trigger the effect on every render.
  const key = JSON.stringify(query ?? {});

  const run = useCallback(async () => {
    if (!enabled || !path) return;

    const ticket = ++sequence.current;
    if (hasLoaded.current) setRefetching(true);
    else setLoading(true);

    try {
      const result = await api.get(path, JSON.parse(key));
      if (ticket !== sequence.current) return;
      setData(result);
      setError(null);
      hasLoaded.current = true;
    } catch (caught) {
      if (ticket !== sequence.current) return;
      setError(caught);
    } finally {
      if (ticket === sequence.current) {
        setLoading(false);
        setRefetching(false);
      }
    }
  }, [path, key, enabled]);

  useEffect(() => {
    run();
  }, [run]);

  return { data, error, loading, refetching, reload: run };
}

/**
 * A debounced mirror of a value, for search inputs.
 *
 * The input stays instant; only the request waits. Debouncing the input itself
 * makes typing feel broken.
 */
export function useDebounced(value, delay = 280) {
  const [settled, setSettled] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);

  return settled;
}

/** Measures an element's width so an SVG chart can be drawn to fit it. */
export function useMeasuredWidth(fallback = 640) {
  const ref = useRef(null);
  const [width, setWidth] = useState(fallback);

  useEffect(() => {
    const node = ref.current;
    if (!node) return undefined;

    const observer = new ResizeObserver(([entry]) => {
      const next = entry.contentRect.width;
      if (next > 0) setWidth(next);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  return [ref, width];
}
