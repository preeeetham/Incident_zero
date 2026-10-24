import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { get } from "./api";
import type { Event, State } from "./types";

// One EventSource for the whole app. Every event bumps `version`; pages refetch on it (debounced), so the room
// stays live without each page holding its own stream.

interface Live {
  state: State | null;
  version: number;
  connected: boolean;
  lastEvents: Event[];
  refreshState: () => void;
  approver: string;
  setApprover: (name: string) => void;
}

const Ctx = createContext<Live | null>(null);
const NAME_KEY = "iz.approver";

function readName(): string {
  try {
    return sessionStorage.getItem(NAME_KEY) ?? "";
  } catch {
    return "";
  }
}

export function LiveProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<State | null>(null);
  const [version, setVersion] = useState(0);
  const [connected, setConnected] = useState(false);
  const [lastEvents, setLastEvents] = useState<Event[]>([]);
  const [approver, setApproverState] = useState(readName);
  const bump = useRef<number | undefined>(undefined);

  const refreshState = useCallback(() => {
    get<State>("/api/state").then(setState).catch(() => setState(null));
  }, []);

  const setApprover = useCallback((name: string) => {
    setApproverState(name);
    try {
      sessionStorage.setItem(NAME_KEY, name);
    } catch {
      /* private mode: the name lives for this page only */
    }
  }, []);

  useEffect(() => {
    refreshState();
    let es: EventSource | null = null;
    let retry: number | undefined;
    const open = () => {
      es = new EventSource("/api/stream");
      es.onopen = () => setConnected(true);
      es.onerror = () => {
        setConnected(false);
        es?.close();
        retry = window.setTimeout(open, 3000);
      };
      es.addEventListener("event", (msg) => {
        const ev = JSON.parse((msg as MessageEvent).data) as Event;
        setLastEvents((prev) => [...prev.slice(-49), ev]);
        notify(ev);
        window.clearTimeout(bump.current);
        bump.current = window.setTimeout(() => {
          setVersion((v) => v + 1);
          refreshState();
        }, 250);
      });
    };
    open();
    const poll = window.setInterval(refreshState, 15000);
    return () => {
      es?.close();
      window.clearTimeout(retry);
      window.clearInterval(poll);
    };
  }, [refreshState]);

  return (
    <Ctx.Provider value={{ state, version, connected, lastEvents, refreshState, approver, setApprover }}>
      {children}
    </Ctx.Provider>
  );
}

export function useLive(): Live {
  const v = useContext(Ctx);
  if (!v) throw new Error("useLive outside LiveProvider");
  return v;
}

// Data that refetches whenever anything happens.
export function useLiveData<T>(path: string | null): { data: T | null; error: string | null; reload: () => void } {
  const { version } = useLive();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!path) return;
    let alive = true;
    get<T>(path)
      .then((d) => {
        if (alive) {
          setData(d);
          setError(null);
        }
      })
      .catch((e: Error) => alive && setError(e.message));
    return () => {
      alive = false;
    };
  }, [path, version, tick]);
  return { data, error, reload: () => setTick((t) => t + 1) };
}

function notify(ev: Event) {
  if (ev.kind !== "approval" || !ev.title.startsWith("approval needed")) return;
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  try {
    const n = new Notification("Approval needed", { body: ev.title, tag: ev.incident_id ?? "iz" });
    n.onclick = () => {
      window.focus();
      if (ev.incident_id) window.location.assign(`/incidents/${ev.incident_id}/plan`);
    };
  } catch {
    /* some browsers only allow notifications from a service worker */
  }
}
