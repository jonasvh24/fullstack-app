import { useId } from "react";

export interface SegOption<T extends string> {
  value: T;
  label: string;
  disabled?: boolean;
  hint?: string;
}

/** A radio group drawn as a segmented control. Arrow keys move between options natively. */
export function Segmented<T extends string>({
  legend,
  value,
  options,
  onChange,
}: {
  legend: string;
  value: T;
  options: SegOption<T>[];
  onChange: (v: T) => void;
}) {
  const name = useId();
  return (
    <fieldset className="field seg-field">
      <legend className="field-label">{legend}</legend>
      <div className="seg">
        {options.map((o) => (
          <label key={o.value} className={`seg-opt ${o.disabled ? "is-disabled" : ""}`} title={o.hint}>
            <input
              type="radio"
              name={name}
              value={o.value}
              checked={value === o.value}
              disabled={o.disabled}
              onChange={() => onChange(o.value)}
            />
            <span>
              {o.label}
              {o.hint && <small className="seg-hint">{o.hint}</small>}
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
