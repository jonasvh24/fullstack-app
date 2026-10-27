// Thin client for the local API. Every write is JSON, which the backend requires.

export type Mode = "PAPER" | "LIVE";
export type EngineState = "STOPPED" | "RUNNING" | "KILLED" | "TARGET_REACHED" | "HALTED";

export interface Position {
  id: number;
  pair: string;
  amount: number;
  entry_price: number;
  stop_loss: number;
  take_profit: number;
  opened_at: string;
  bid: number | null;
  unrealised_pnl: number | null;
  age_hours: number;
}

export interface Verdict {
  action_index: number;
  action: string;
  pair: string | null;
  verdict: "approved" | "clamped" | "rejected";
  reasons: string[];
  size_requested: number | null;
  size_final: number | null;
  confidence: number | null;
  rationale: string | null;
}

export interface OrderRow {
  id: number;
  pair: string;
  side: string;
  type: string;
  purpose: string;
  amount: number;
  price: number | null;
  status: string;
  filled: number;
  average: number | null;
  fee: number;
  error: string | null;
}

export interface Decision {
  id: number;
  mode: Mode;
  ts: string;
  candle_ts: string | null;
  prompt_version: string;
  model: string;
  cache_hit: boolean;
  snapshot_hash: string;
  valid: boolean;
  validation_errors: string[];
  actions: Array<Record<string, unknown>>;
  pairs: string[];
  verdicts: Verdict[];
  orders: OrderRow[];
  has_rejection: boolean;
  model_output?: string;
}

export interface Run {
  id: number;
  created_at: string;
  pairs: string[];
  starting_capital: number;
  target_roi: number;
  target_equity: number;
  equity: number;
  cash: number;
  positions_value: number;
  peak_equity: number;
  drawdown_pct: number;
  progress_pct: number;
  fees_eur: number;
}

export interface Status {
  mode: Mode;
  label: string;
  state: EngineState;
  state_reason: string | null;
  restart_required: boolean;
  warnings: string[];
  secrets: Record<string, boolean>;
  model: string;
  prompt_version: string;
  timeframe: string;
  last_error: string | null;
  last_reconcile: { ts: string; ok: boolean; mismatch_eur: number; issues: string[] } | null;
  run: Run | null;
  positions: Position[];
  last_decision: Decision | null;
}

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method };
  if (method !== "GET") {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body ?? {});
  }
  const r = await fetch(path, init);
  const text = await r.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!r.ok) {
    const d = data as { detail?: unknown; error?: string } | null;
    const msg = typeof d?.detail === "string" ? d.detail : d?.error ?? `HTTP ${r.status}`;
    throw new ApiError(r.status, msg, data);
  }
  return data as T;
}

export const api = {
  get: <T,>(path: string) => request<T>("GET", path),
  post: <T,>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T,>(path: string, body?: unknown) => request<T>("PUT", path, body),
};

// Times are stored in UTC and shown in Europe/Brussels, 24-hour clock.
const fmt = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Brussels",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

export function brussels(iso: string | null | undefined): string {
  if (!iso) return "—";
  const parts = Object.fromEntries(fmt.formatToParts(new Date(iso)).map((p) => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

export function eur(x: number | null | undefined, digits = 2): string {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  return `€${x.toFixed(digits)}`;
}

export function signedEur(x: number | null | undefined, digits = 2): string {
  if (x === null || x === undefined) return "—";
  return `${x >= 0 ? "+" : "−"}€${Math.abs(x).toFixed(digits)}`;
}

export function num(x: number | null | undefined, digits = 6): string {
  if (x === null || x === undefined) return "—";
  return Number(x.toPrecision(digits)).toString();
}
