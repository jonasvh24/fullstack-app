import { useEffect, useState } from "react";

function readHash(): string {
  const h = window.location.hash.replace(/^#/, "");
  return h.startsWith("/") ? h : "/" + h;
}

/** Current hash path, e.g. "/p/abc/brief". */
export function useHashPath(): string {
  const [path, setPath] = useState(readHash);
  useEffect(() => {
    const onChange = () => setPath(readHash());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return path;
}

export function navigate(path: string): void {
  window.location.hash = path;
}

export function href(path: string): string {
  return "#" + path;
}

export type Route =
  | { name: "projects" }
  | { name: "project"; id: string; step: string | null }
  | { name: "notFound"; path: string };

export function matchRoute(path: string): Route {
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  if (parts.length === 0) return { name: "projects" };
  if (parts[0] === "p" && parts[1] && parts.length <= 3) {
    return { name: "project", id: parts[1], step: parts[2] ?? null };
  }
  return { name: "notFound", path };
}
