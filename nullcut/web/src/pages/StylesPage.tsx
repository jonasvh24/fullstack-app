import { useCallback, useEffect, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { CompareRow, Style, ThumbnailProfile } from "../api/types";
import { CompareChart, Glyph, SERIES, type ChartSeries } from "../components/CompareChart";
import { ErrorText } from "../components/ErrorText";
import { ExtractForm } from "../components/ExtractForm";
import { LearnedHighlights } from "../components/LearnedHighlights";
import { flattenParams, humanise } from "../lib/format";

const MIN_COMPARE = 2;
const MAX_COMPARE = SERIES.length; // 4

function KindTag({ kind }: { kind: string | undefined }) {
  return <span className={`tag ${kind === "extracted" ? "tag-broll" : "tag-none"}`}>{kind ?? "shipped"}</span>;
}

function DeleteButton({ style, onDeleted }: { style: Style; onDeleted: (id: string) => void }) {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!armed) return;
    const t = window.setTimeout(() => setArmed(false), 4000);
    return () => window.clearTimeout(t);
  }, [armed]);

  const run = async () => {
    if (!armed) {
      setArmed(true);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.deleteStyle(style.id);
      onDeleted(style.id);
    } catch (e) {
      setError(errorMessage(e));
      setArmed(false);
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className="del-cell">
      <button
        type="button"
        className={`btn btn-xs ${armed ? "btn-danger-armed" : "btn-danger"}`}
        onClick={() => void run()}
        disabled={busy}
        aria-label={armed ? `Confirm deleting ${style.name}` : `Delete ${style.name}`}
      >
        {armed ? "Confirm delete" : "Delete"}
      </button>
      {error && (
        <span className="err-inline small-text" role="alert">
          {error}
        </span>
      )}
    </span>
  );
}

function StylesTable({
  styles,
  slots,
  onToggle,
  onDeleted,
}: {
  styles: Style[];
  slots: Record<string, number>;
  onToggle: (id: string) => void;
  onDeleted: (id: string) => void;
}) {
  const full = Object.keys(slots).length >= MAX_COMPARE;
  return (
    <div className="table-wrap">
      <table className="table styles-table">
        <thead>
          <tr>
            <th scope="col">Compare</th>
            <th scope="col">Name</th>
            <th scope="col">Kind</th>
            <th scope="col">Description</th>
            <th scope="col" className="num">
              Sources
            </th>
            <th scope="col">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {styles.map((s) => {
            const slot = slots[s.id];
            const on = slot != null;
            return (
              <tr key={s.id}>
                <td>
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={on}
                      disabled={!on && full}
                      onChange={() => onToggle(s.id)}
                      aria-label={`Compare ${s.name}`}
                    />
                    {on && <Glyph slot={slot} />}
                  </label>
                </td>
                <td>
                  <span className="style-name">{s.name}</span> <span className="mono dim">{s.id}</span>
                </td>
                <td>
                  <KindTag kind={s.kind} />
                </td>
                <td className="desc-cell">{s.description}</td>
                <td className="num mono" title={(s.sources ?? []).join("\n")}>
                  {s.kind === "extracted" ? (s.sources ?? []).length : "–"}
                </td>
                <td className="num">{s.kind === "extracted" && <DeleteButton style={s} onDeleted={onDeleted} />}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Comparison({ styles, slots }: { styles: Style[]; slots: Record<string, number> }) {
  const ids = Object.keys(slots).sort((a, b) => slots[a] - slots[b]);
  const key = ids.join(",");
  const [rows, setRows] = useState<CompareRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (ids.length < MIN_COMPARE) {
      setRows(null);
      setError(null);
      return;
    }
    let cancelled = false;
    api
      .compareStyles(ids)
      .then((r) => {
        if (cancelled) return;
        setRows(r);
        setError(null);
      })
      .catch((e) => {
        if (!cancelled) setError(errorMessage(e));
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const series: ChartSeries[] = ids.map((id) => ({
    id,
    name: styles.find((s) => s.id === id)?.name ?? id,
    slot: slots[id],
  }));

  return (
    <section className="panel" aria-labelledby="cmp-title">
      <div className="panel-head">
        <h2 id="cmp-title" className="section-title">
          Comparison
        </h2>
        <span className="mono dim">
          {ids.length}/{MAX_COMPARE} selected
        </span>
      </div>
      {ids.length < MIN_COMPARE ? (
        <p className="empty small">Tick {MIN_COMPARE} to {MAX_COMPARE} styles in the table to compare their parameters.</p>
      ) : (
        <>
          <ErrorText error={error} />
          {rows && rows.length === 0 && <p className="empty small">No comparable parameters.</p>}
          {rows && rows.length > 0 && <CompareChart rows={rows} series={series} />}
          {!rows && !error && <p className="dim">Loading comparison</p>}
        </>
      )}
    </section>
  );
}

function ProfilesSection({ profiles, error }: { profiles: ThumbnailProfile[] | null; error: string | null }) {
  return (
    <>
      <ErrorText error={error} />
      {profiles && profiles.length === 0 && <p className="empty small">No thumbnail profiles.</p>}
      {profiles && profiles.length > 0 && (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Kind</th>
                <th scope="col">Description</th>
                <th scope="col">Parameters</th>
              </tr>
            </thead>
            <tbody>
              {profiles.map((p) => {
                const params = flattenParams(p.params ?? {});
                return (
                  <tr key={p.id}>
                    <td>
                      <span className="style-name">{p.name}</span> <span className="mono dim">{p.id}</span>
                    </td>
                    <td>
                      <KindTag kind={p.kind} />
                    </td>
                    <td className="desc-cell">{p.description}</td>
                    <td>
                      {params.length === 0 ? (
                        <span className="dim">–</span>
                      ) : (
                        <details className="param-fold">
                          <summary>
                            <span className="mono">{params.length}</span> parameters
                          </summary>
                          <dl className="kv param-kv">
                            {params.map(([k, v]) => (
                              <div key={k} className="kv-pair">
                                <dt title={k}>{humanise(k)}</dt>
                                <dd className="mono">{v}</dd>
                              </div>
                            ))}
                          </dl>
                        </details>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

export function StylesPage() {
  const [styles, setStyles] = useState<Style[] | null>(null);
  const [stylesError, setStylesError] = useState<string | null>(null);
  const [profiles, setProfiles] = useState<ThumbnailProfile[] | null>(null);
  const [profilesError, setProfilesError] = useState<string | null>(null);
  // style id -> colour slot; a slot stays with its style until it is unticked.
  const [slots, setSlots] = useState<Record<string, number>>({});

  const loadStyles = useCallback(async () => {
    try {
      setStyles(await api.styles());
      setStylesError(null);
    } catch (e) {
      setStylesError(errorMessage(e));
    }
  }, []);
  const loadProfiles = useCallback(async () => {
    try {
      setProfiles(await api.thumbnailProfiles());
      setProfilesError(null);
    } catch (e) {
      setProfilesError(errorMessage(e));
    }
  }, []);

  useEffect(() => {
    void loadStyles();
    void loadProfiles();
  }, [loadStyles, loadProfiles]);

  const toggle = (id: string) =>
    setSlots((prev) => {
      if (id in prev) {
        const next = { ...prev };
        delete next[id];
        return next;
      }
      if (Object.keys(prev).length >= MAX_COMPARE) return prev;
      const used = new Set(Object.values(prev));
      let slot = 0;
      while (used.has(slot)) slot++;
      return { ...prev, [id]: slot };
    });

  const onDeleted = (id: string) => {
    setSlots((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
    setStyles((list) => (list ? list.filter((s) => s.id !== id) : list));
  };

  return (
    <div className="page">
      <div className="page-head">
        <h1 className="page-title">Styles</h1>
        <span className="dim mono">{styles ? styles.length : ""}</span>
      </div>

      <section className="panel" aria-labelledby="styles-title">
        <h2 id="styles-title" className="sr-only">
          All styles
        </h2>
        <ErrorText error={stylesError} />
        {styles && styles.length > 0 && (
          <StylesTable styles={styles} slots={slots} onToggle={toggle} onDeleted={onDeleted} />
        )}
        {styles && styles.length === 0 && <p className="empty">No styles.</p>}
        {!styles && !stylesError && <p className="dim">Loading</p>}
        <ExtractForm
          id="style-extract"
          title="Extract a style"
          folderLabel="Folder of 3–10 reference videos (on the server)"
          folderPlaceholder="/home/me/reference/creator-x"
          submitLabel="Extract style"
          start={api.extractStyle}
          onDone={() => void loadStyles()}
        />
      </section>

      {styles && <Comparison styles={styles} slots={slots} />}

      <section className="panel" aria-labelledby="tp-title">
        <div className="panel-head">
          <h2 id="tp-title" className="section-title">
            Thumbnail profiles
          </h2>
          <span className="mono dim">{profiles ? profiles.length : ""}</span>
        </div>
        <ProfilesSection profiles={profiles} error={profilesError} />
        <ExtractForm
          id="profile-extract"
          title="Extract a thumbnail profile"
          folderLabel="Folder of 20–50 reference thumbnails (on the server)"
          folderPlaceholder="/home/me/reference/thumbs"
          submitLabel="Extract profile"
          start={api.extractThumbnailProfile}
          onDone={() => void loadProfiles()}
        />
      </section>

      <LearnedHighlights />
    </div>
  );
}
