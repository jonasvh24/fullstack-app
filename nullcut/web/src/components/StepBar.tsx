import { href } from "../lib/router";

export const STEPS = ["clips", "brief", "storyboard", "preview", "deliver"] as const;
export type Step = (typeof STEPS)[number];

export const STEP_LABEL: Record<Step, string> = {
  clips: "Clips",
  brief: "Brief",
  storyboard: "Storyboard",
  preview: "Preview",
  deliver: "Deliver",
};

export interface StepState {
  available: boolean;
  reason?: string; // why a step is unavailable
  busy?: boolean; // a job for this step is running
}

export function StepBar({
  projectId,
  current,
  states,
}: {
  projectId: string;
  current: Step;
  states: Record<Step, StepState>;
}) {
  return (
    <nav className="steps" aria-label="Project steps">
      <ol>
        {STEPS.map((s, i) => {
          const st = states[s];
          const isCurrent = s === current;
          const body = (
            <>
              <span className="step-n mono">{i + 1}</span>
              <span className="step-label">{STEP_LABEL[s]}</span>
              {st.busy && <span className="step-busy" aria-label="running" />}
              {!st.available && st.reason && <span className="step-reason">{st.reason}</span>}
            </>
          );
          return (
            <li key={s} className={`step ${isCurrent ? "is-current" : ""} ${st.available ? "" : "is-locked"}`}>
              {st.available || isCurrent ? (
                <a href={href(`/p/${encodeURIComponent(projectId)}/${s}`)} aria-current={isCurrent ? "step" : undefined}>
                  {body}
                </a>
              ) : (
                <span className="step-disabled" aria-disabled="true">
                  {body}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
