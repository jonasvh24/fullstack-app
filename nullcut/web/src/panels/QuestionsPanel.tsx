import { useState, type FormEvent } from "react";
import type { Plan } from "../api/types";
import { ErrorText } from "../components/ErrorText";

export function QuestionsPanel({
  questions,
  busy,
  error,
  onSubmit,
}: {
  questions: Plan["questions"];
  busy: boolean;
  error: string | null;
  onSubmit: (answers: Record<string, string>) => void;
}) {
  const shown = questions.slice(0, 3);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const filled = shown.filter((q) => (answers[q.id] ?? "").trim() !== "").length;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const out: Record<string, string> = {};
    for (const q of shown) {
      const a = (answers[q.id] ?? "").trim();
      if (a) out[q.id] = a;
    }
    onSubmit(out);
  };

  return (
    <form className="questions" onSubmit={submit} aria-labelledby="q-title">
      <div className="panel-head">
        <h3 id="q-title" className="section-title">
          The planner needs answers before rendering
        </h3>
        <span className="mono dim">
          {filled}/{shown.length}
        </span>
      </div>
      {shown.map((q, i) => (
        <label key={q.id} className="field">
          <span className="field-label q-text">
            <span className="mono dim">Q{i + 1}</span> {q.text}
          </span>
          <input
            className="input"
            type="text"
            value={answers[q.id] ?? ""}
            onChange={(e) => setAnswers((a) => ({ ...a, [q.id]: e.target.value }))}
          />
        </label>
      ))}
      <div className="form-row">
        <button type="submit" className="btn btn-primary" disabled={busy || filled === 0}>
          Answer and replan
        </button>
      </div>
      <ErrorText error={error} />
    </form>
  );
}
