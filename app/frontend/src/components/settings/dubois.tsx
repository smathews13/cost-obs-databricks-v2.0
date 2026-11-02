import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ToastCtx } from "./toast";

// DuBois product-UI tokens (spec §3). Scoped to the Settings modal only — the rest of
// the app keeps its marketing-brand orange. Red means danger only; toggles are blue.
//
// Each token is a CSS custom property (`var(--dob-*, <light fallback>)`) so the modal
// is theme-aware: the light values are defined on :root in index.css and overridden
// under .dark-mode, which cascades to this portaled modal. The hex fallback means the
// modal still renders correctly if the stylesheet hasn't loaded. `surface` is the
// panel/input background (was hardcoded #FFFFFF, which couldn't adapt to dark).
export const T = {
  text: "var(--dob-text, #11171C)",
  textSecondary: "var(--dob-text-secondary, #5F7281)",
  textFaint: "var(--dob-text-faint, #8CA0B0)",
  primary: "var(--dob-primary, #2272B4)",
  primaryHover: "var(--dob-primary-hover, #0E538B)",
  // Filled-button background (white text on top). Separate from `primary` so that in
  // dark mode `primary` can lighten for link/accent text while the button fill stays
  // dark enough for white text to pass WCAG AA.
  primaryFill: "var(--dob-primary-fill, #2272B4)",
  primaryFillHover: "var(--dob-primary-fill-hover, #0E538B)",
  borderControl: "var(--dob-border-control, #C4CFD8)",
  borderGroup: "var(--dob-border-group, #E0E5EA)",
  borderRow: "var(--dob-border-row, #E8ECF0)",
  navBg: "var(--dob-nav-bg, #FAFBFC)",
  codeBg: "var(--dob-code-bg, #F2F5F7)",
  surface: "var(--dob-surface, #FFFFFF)",
  successFg: "var(--dob-success-fg, #277C43)",
  successBg: "var(--dob-success-bg, #EFFAF3)",
  successBorder: "var(--dob-success-border, #BFE6CC)",
  warningFg: "var(--dob-warning-fg, #8A5C10)",
  warningBg: "var(--dob-warning-bg, #FCF6E4)",
  warningBorder: "var(--dob-warning-border, #F2DDA5)",
  dangerFg: "var(--dob-danger-fg, #C82D4C)",
  dangerBg: "var(--dob-danger-bg, #FBEBEF)",
  dangerBorder: "var(--dob-danger-border, #EFB8C4)",
  toggleOff: "var(--dob-toggle-off, #B6C2CC)",
  badgeBg: "var(--dob-badge-bg, #FCF3DE)",
  navy: "var(--dob-navy, #1B3139)",
} as const;

export const MONO = '"SF Mono", Menlo, Consolas, "Liberation Mono", monospace';

// ── Section title ─────────────────────────────────────────────────────────
export function SectionTitle({ title, subtitle, badge }: { title: string; subtitle?: string; badge?: ReactNode }) {
  return (
    <div className="mb-4">
      <div className="flex items-center gap-2">
        <h2 style={{ fontSize: 15, fontWeight: 600, color: T.text }}>{title}</h2>
        {badge}
      </div>
      {subtitle && <p style={{ fontSize: 12, color: T.textSecondary, marginTop: 2 }}>{subtitle}</p>}
    </div>
  );
}

// ── Group card ────────────────────────────────────────────────────────────
export function Group({ label, children, danger }: { label?: string; children: ReactNode; danger?: boolean }) {
  return (
    <div className="mb-5">
      {label && <div style={{ fontSize: 13, fontWeight: 600, color: T.text, marginBottom: 8 }}>{label}</div>}
      <div style={{ border: `1px solid ${danger ? T.dangerBorder : T.borderGroup}`, borderRadius: 8, overflow: "hidden", backgroundColor: danger ? T.dangerBg : T.surface }}>
        {children}
      </div>
    </div>
  );
}

// ── Row (label+helper left, control right, top divider between rows) ────────
export function Row({ label, helper, control, first }: { label: ReactNode; helper?: ReactNode; control?: ReactNode; first?: boolean }) {
  return (
    <div
      className="flex items-center justify-between gap-4"
      style={{ padding: "12px 16px", borderTop: first ? "none" : `1px solid ${T.borderRow}` }}
    >
      <div className="min-w-0">
        <div style={{ fontSize: 13, fontWeight: 500, color: T.text }}>{label}</div>
        {helper && <div style={{ fontSize: 12, color: T.textSecondary, marginTop: 2 }}>{helper}</div>}
      </div>
      {control != null && <div className="shrink-0">{control}</div>}
    </div>
  );
}

// ── Toggle (28×16 track, blue on / grey off) ────────────────────────────────
export function Toggle({ checked, onChange, disabled, label }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label?: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => !disabled && onChange(!checked)}
      style={{
        width: 28, height: 16, borderRadius: 999, position: "relative",
        backgroundColor: checked ? T.primary : T.toggleOff,
        opacity: disabled ? 0.5 : 1, cursor: disabled ? "not-allowed" : "pointer",
        transition: "background-color 120ms", flexShrink: 0,
      }}
    >
      <span
        style={{
          position: "absolute", top: 2, left: checked ? 14 : 2, width: 12, height: 12,
          borderRadius: 999, backgroundColor: "#FFFFFF", transition: "left 120ms",
          boxShadow: "0 1px 2px rgba(0,0,0,0.2)",
        }}
      />
    </button>
  );
}

// ── Select ──────────────────────────────────────────────────────────────────
export function Select<V extends string | number>({ value, onChange, options, disabled, ariaLabel, width }: {
  value: V; onChange: (v: V) => void; disabled?: boolean; ariaLabel?: string; width?: number | string;
  options: { value: V; label: string }[];
}) {
  const [focused, setFocused] = useState(false);
  return (
    <span
      style={{
        position: "relative", display: "inline-flex", alignItems: "center", height: 32,
        width: width ?? 160, borderRadius: 4, border: `1px solid ${focused ? T.primary : T.borderControl}`,
        backgroundColor: disabled ? T.codeBg : T.surface,
        boxShadow: focused ? "0 0 0 2px var(--dob-focus-ring, rgba(34, 114, 180, 0.2))" : "none",
        transition: "border-color 120ms, box-shadow 120ms",
      }}
    >
      <select
        value={value}
        disabled={disabled}
        aria-label={ariaLabel}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onChange={(e) => {
          const raw = e.target.value;
          const match = options.find((o) => String(o.value) === raw);
          onChange((match ? match.value : raw) as V);
        }}
        style={{
          appearance: "none", WebkitAppearance: "none", width: "100%", height: "100%",
          border: "none", outline: "none", borderRadius: 4, padding: "0 32px 0 10px",
          fontSize: 13, color: T.text, backgroundColor: "transparent",
          cursor: disabled ? "not-allowed" : "pointer",
        }}
      >
        {options.map((o) => <option key={String(o.value)} value={o.value}>{o.label}</option>)}
      </select>
      <svg
        aria-hidden="true"
        viewBox="0 0 16 16"
        fill="none"
        style={{ position: "absolute", right: 10, width: 14, height: 14, pointerEvents: "none", color: disabled ? T.textFaint : T.textSecondary }}
      >
        <path d="m4 6 4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </span>
  );
}

// ── Text input ────────────────────────────────────────────────────────────
export function TextInput({ value, onChange, placeholder, mono, disabled, width }: {
  value: string; onChange: (v: string) => void; placeholder?: string; mono?: boolean; disabled?: boolean; width?: number | string;
}) {
  return (
    <input
      type="text"
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      style={{
        height: 32, borderRadius: 4, border: `1px solid ${T.borderControl}`, padding: "0 10px",
        fontSize: 13, color: T.text, fontFamily: mono ? MONO : undefined,
        width: width ?? 280, backgroundColor: disabled ? T.codeBg : T.surface,
      }}
    />
  );
}

// ── Unit input (affix % or $ in a code-bg cell, value right-aligned mono) ────
export function UnitInput({ affix, affixSide = "right", value, onChange, min, max, step, width = 120 }: {
  affix: string; affixSide?: "left" | "right"; value: number; onChange: (v: number) => void;
  min?: number; max?: number; step?: number; width?: number;
}) {
  const affixCell = (
    <span style={{ backgroundColor: T.codeBg, color: T.textSecondary, fontFamily: MONO, fontSize: 12, padding: "0 8px", display: "flex", alignItems: "center", height: "100%" }}>{affix}</span>
  );
  return (
    <div style={{ display: "inline-flex", height: 32, borderRadius: 4, border: `1px solid ${T.borderControl}`, overflow: "hidden", width }}>
      {affixSide === "left" && affixCell}
      <input
        type="number" value={value} min={min} max={max} step={step}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ flex: 1, minWidth: 0, border: "none", outline: "none", textAlign: "right", padding: "0 8px", fontFamily: MONO, fontSize: 13, color: T.text }}
      />
      {affixSide === "right" && affixCell}
    </div>
  );
}

// ── Badge (EXPERIMENTAL / ADMIN ONLY) ────────────────────────────────────────
export function Badge({ children }: { children: ReactNode }) {
  return (
    <span style={{ fontSize: 10, fontWeight: 600, color: T.warningFg, backgroundColor: T.badgeBg, borderRadius: 3, padding: "1px 5px", letterSpacing: 0.3, textTransform: "uppercase" }}>
      {children}
    </span>
  );
}

// ── Buttons ──────────────────────────────────────────────────────────────────
export function PrimaryButton({ children, onClick, disabled }: { children: ReactNode; onClick?: () => void; disabled?: boolean }) {
  const [hover, setHover] = useState(false);
  return (
    <button
      type="button" onClick={onClick} disabled={disabled}
      onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      style={{
        height: 32, borderRadius: 4, padding: "0 14px", fontSize: 13, fontWeight: 500, color: "#FFFFFF",
        backgroundColor: disabled ? "#A9C6DC" : hover ? T.primaryFillHover : T.primaryFill,
        cursor: disabled ? "not-allowed" : "pointer", border: "none",
      }}
    >
      {children}
    </button>
  );
}

export function SecondaryButton({ children, onClick, disabled }: { children: ReactNode; onClick?: () => void; disabled?: boolean }) {
  return (
    <button
      type="button" onClick={onClick} disabled={disabled}
      style={{ height: 32, borderRadius: 4, padding: "0 14px", fontSize: 13, fontWeight: 500, color: T.text, backgroundColor: T.surface, border: `1px solid ${T.borderControl}`, cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.5 : 1, whiteSpace: "nowrap" }}
    >
      {children}
    </button>
  );
}

export function DangerOutlineButton({ children, onClick, disabled }: { children: ReactNode; onClick?: () => void; disabled?: boolean }) {
  return (
    <button
      type="button" onClick={onClick} disabled={disabled}
      style={{ height: 32, borderRadius: 4, padding: "0 14px", fontSize: 13, fontWeight: 500, color: disabled ? T.textFaint : T.dangerFg, backgroundColor: T.surface, border: `1px solid ${disabled ? T.borderControl : T.dangerBorder}`, cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.7 : 1, whiteSpace: "nowrap", flexShrink: 0 }}
    >
      {children}
    </button>
  );
}

export function LinkButton({ children, onClick, href }: { children: ReactNode; onClick?: () => void; href?: string }) {
  const [hover, setHover] = useState(false);
  const style = { fontSize: 13, color: hover ? T.primaryHover : T.primary, cursor: "pointer", background: "none", border: "none", padding: 0, textDecoration: hover ? "underline" : "none" } as const;
  if (href) return <a href={href} target="_blank" rel="noopener noreferrer" style={style} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>{children}</a>;
  return <button type="button" onClick={onClick} style={style} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>{children}</button>;
}

// ── Mono chip ────────────────────────────────────────────────────────────────
export function MonoChip({ children }: { children: ReactNode }) {
  return <span style={{ fontFamily: MONO, fontSize: 12, color: T.text, backgroundColor: T.codeBg, borderRadius: 4, padding: "2px 6px" }}>{children}</span>;
}

// ── Callouts ─────────────────────────────────────────────────────────────────
export function Callout({ tone, children }: { tone: "success" | "warning" | "danger"; children: ReactNode }) {
  const map = {
    success: { fg: T.successFg, bg: T.successBg, border: T.successBorder },
    warning: { fg: T.warningFg, bg: T.warningBg, border: T.warningBorder },
    danger: { fg: T.dangerFg, bg: T.dangerBg, border: T.dangerBorder },
  }[tone];
  return (
    <div style={{ border: `1px solid ${map.border}`, backgroundColor: map.bg, color: map.fg, borderRadius: 6, padding: "10px 12px", fontSize: 13 }}>
      {children}
    </div>
  );
}

// ── Toast ────────────────────────────────────────────────────────────────────

export function ToastProvider({ children }: { children: ReactNode }) {
  const [msg, setMsg] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const toast = useCallback((m: string) => {
    setMsg(m);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setMsg(null), 2400);
  }, []);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  return (
    <ToastCtx.Provider value={toast}>
      {children}
      {msg && (
        <div style={{ position: "fixed", bottom: 24, left: "50%", transform: "translateX(-50%)", zIndex: 10000 }}>
          <div style={{ backgroundColor: T.navy, color: "#FFFFFF", borderRadius: 999, padding: "8px 16px", fontSize: 13, boxShadow: "0 4px 12px rgba(0,0,0,0.25)" }}>{msg}</div>
        </div>
      )}
    </ToastCtx.Provider>
  );
}
