import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ReadinessChecks } from "./ReadinessChecks";
import { normalizeReadinessResult } from "./readiness";
import type { ReadinessResult } from "./ReadinessChecks";
import { READINESS_QUERY_KEY } from "@/hooks/useFeatureAvailability";
import { Group, Row, SecondaryButton, LinkButton, MonoChip, Callout, T, MONO } from "./dubois";
import { useToast } from "./toast";
import { organizationForEmail } from "@/utils/domainIcons";
import { InfoPopover } from "@/components/ui/InfoPopover";
import "./settings.css";
import type {
  AuthStatusPayload,
  UserPermissionsPayload,
  UserRole,
} from "./resourceTypes";

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const USER_GRID_STYLE = {
  "--settings-access-grid-columns": "minmax(210px, 1fr) 190px 90px 112px 112px",
} as React.CSSProperties;
const ROLE_OPTIONS = [
  { value: "consumer", label: "Consumer" },
  { value: "admin", label: "Admin" },
] as const;

type UserPermissions = UserPermissionsPayload;
type AuthStatus = AuthStatusPayload;

function OrganizationIdentity({ email }: { email: string }) {
  const organization = organizationForEmail(email);
  const monogram = organization.name
    .split(/[\s-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word[0])
    .join("")
    .toUpperCase();
  return (
    <span
      data-testid="settings-user-organization"
      title={`${organization.name} (${organization.domain})`}
      className="flex min-w-0 items-center gap-2"
    >
      <span
        className="flex h-7 w-7 shrink-0 items-center justify-center overflow-hidden rounded-full text-[9px] font-bold text-white"
        style={{ backgroundColor: organization.icon?.background ?? T.textSecondary }}
      >
        {organization.icon ? (
          <img
            src={organization.icon.src}
            alt=""
            aria-hidden="true"
            className="aspect-square object-contain"
            style={{ width: `${organization.icon.scale}%`, height: `${organization.icon.scale}%` }}
          />
        ) : monogram}
      </span>
      <span className="truncate text-[12.5px]" style={{ color: T.text }}>
        {organization.name}
      </span>
    </span>
  );
}

function RoleMenuSelect({
  value,
  onChange,
  ariaLabel,
  disabled,
}: {
  value: UserRole;
  onChange: (value: UserRole) => void;
  ariaLabel: string;
  disabled?: boolean;
}) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const listboxId = useId();
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(() => ROLE_OPTIONS.findIndex((option) => option.value === value));
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0, width: 180 });
  const selectedIndex = ROLE_OPTIONS.findIndex((option) => option.value === value);
  const selectedOption = ROLE_OPTIONS[selectedIndex];

  const updateMenuPosition = () => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;
    setMenuPosition({ top: rect.bottom + 4, left: rect.left, width: rect.width });
  };

  const openMenu = (index = selectedIndex) => {
    if (disabled) return;
    setActiveIndex(index < 0 ? 0 : index);
    updateMenuPosition();
    setOpen(true);
  };

  const chooseOption = (index: number) => {
    const option = ROLE_OPTIONS[index];
    if (!option) return;
    if (option.value !== value) onChange(option.value);
    setOpen(false);
    triggerRef.current?.focus();
  };

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!triggerRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false);
    };
    const reposition = () => updateMenuPosition();
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    };
  }, [open]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (disabled) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) {
        openMenu(selectedIndex);
        return;
      }
      const direction = event.key === "ArrowDown" ? 1 : -1;
      setActiveIndex((current) => (current + direction + ROLE_OPTIONS.length) % ROLE_OPTIONS.length);
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      if (!open) return;
      event.preventDefault();
      setActiveIndex(event.key === "Home" ? 0 : ROLE_OPTIONS.length - 1);
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (open) chooseOption(activeIndex);
      else openMenu();
      return;
    }
    if (event.key === "Escape" && open) {
      event.preventDefault();
      setOpen(false);
      return;
    }
    if (event.key === "Tab") {
      setOpen(false);
      return;
    }
    if (event.key.length === 1) {
      const matchIndex = ROLE_OPTIONS.findIndex((option) => option.label.toLowerCase().startsWith(event.key.toLowerCase()));
      if (matchIndex >= 0) {
        event.preventDefault();
        if (open) setActiveIndex(matchIndex);
        else chooseOption(matchIndex);
      }
    }
  };

  return (
    <span className="settings-role-select">
      <button
        ref={triggerRef}
        type="button"
        role="combobox"
        className="settings-role-select-trigger"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listboxId}
        aria-activedescendant={open ? `${listboxId}-option-${activeIndex}` : undefined}
        disabled={disabled}
        onClick={() => open ? setOpen(false) : openMenu()}
        onKeyDown={handleKeyDown}
      >
        <span>{selectedOption?.label ?? value}</span>
        <svg aria-hidden="true" viewBox="0 0 16 16" fill="none" className="settings-role-select-chevron">
          <path d="m4 6 4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open && createPortal(
        <div
          ref={menuRef}
          id={listboxId}
          role="listbox"
          aria-label={ariaLabel}
          className="settings-role-select-menu"
          style={{ top: menuPosition.top, left: menuPosition.left, width: menuPosition.width }}
        >
          {ROLE_OPTIONS.map((option, index) => (
            <div
              id={`${listboxId}-option-${index}`}
              key={option.value}
              role="option"
              aria-selected={option.value === value}
              className={`settings-role-select-option${index === activeIndex ? " is-active" : ""}`}
              onMouseEnter={() => setActiveIndex(index)}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => chooseOption(index)}
            >
              <span>{option.label}</span>
              {option.value === value && (
                <svg aria-hidden="true" viewBox="0 0 16 16" fill="none">
                  <path d="m3.5 8.25 2.75 2.75 6.25-6.25" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              )}
            </div>
          ))}
        </div>,
        document.body,
      )}
    </span>
  );
}

function PermissionState({
  state,
  children,
}: {
  state: "verified" | "missing" | "required" | "unverified";
  children: React.ReactNode;
}) {
  const tone = state === "verified"
    ? { fg: T.successFg, bg: T.successBg, border: T.successBorder }
    : state === "missing"
      ? { fg: T.dangerFg, bg: T.dangerBg, border: T.dangerBorder }
      : state === "required"
        ? { fg: T.warningFg, bg: T.warningBg, border: T.warningBorder }
        : { fg: T.textSecondary, bg: T.navBg, border: T.borderGroup };
  return (
    <span style={{ display: "inline-flex", alignItems: "center", borderRadius: 999, border: `1px solid ${tone.border}`, backgroundColor: tone.bg, color: tone.fg, padding: "2px 9px", fontSize: 11, fontWeight: 600 }}>
      {children}
    </span>
  );
}

export function SettingsPermissions() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [readinessOpen, setReadinessOpen] = useState(false);
  // Open by default: the exact GRANT SQL is what an admin needs on hand when the app
  // runs as a service principal (the common case), matching the pre-revamp behavior.
  const [grantsOpen, setGrantsOpen] = useState(true);
  const [sqlCopied, setSqlCopied] = useState(false);

  const { data: permissions, isLoading } = useQuery<UserPermissions>({
    queryKey: ["user-permissions"],
    queryFn: async () => {
      const res = await fetch("/api/settings/user-permissions");
      if (!res.ok) throw new Error("Failed to fetch");
      return res.json();
    },
  });

  const { data: authStatus } = useQuery<AuthStatus>({
    queryKey: ["settings-auth-status"],
    queryFn: () => fetch("/api/settings/auth-status").then(r => r.json()),
    staleTime: 10 * 1000,
    refetchInterval: 30 * 1000,
  });

  // Shared readiness query — same key as useFeatureAvailability so all components read one cache entry.
  const {
    data: readiness,
    isLoading: readinessLoading,
    error: readinessQueryError,
  } = useQuery<ReadinessResult | null>({
    queryKey: READINESS_QUERY_KEY,
    queryFn: () =>
      fetch("/api/setup/readiness")
        .then(r => r.ok ? r.json() : null)
        .then(normalizeReadinessResult)
        .catch(() => null),
    staleTime: 5 * 60 * 1000,
    retry: false,
    refetchOnWindowFocus: false,
  });

  const handleReadinessRecheck = () => {
    queryClient.refetchQueries({ queryKey: READINESS_QUERY_KEY });
  };

  const saveMutation = useMutation({
    mutationFn: async (data: Pick<UserPermissions, "admins" | "consumers">) => {
      const res = await fetch("/api/settings/user-permissions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail ?? "Failed to save");
      }
      return res.json();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["user-permissions"] });
      queryClient.refetchQueries({ queryKey: ["user"] });
      toast("Users updated");
    },
    onError: (e) => toast(e instanceof Error ? e.message : "Save failed"),
  });

  const [newUserEmail, setNewUserEmail] = useState("");
  const [newUserRole, setNewUserRole] = useState<UserRole>("consumer");

  const ownerEmail = permissions?.owner?.email?.toLowerCase() ?? null;
  const allUsers = [
    ...(permissions?.admins ?? []).map(e => ({ email: e, role: "admin" as const })),
    ...(permissions?.consumers ?? []).map(e => ({ email: e, role: "consumer" as const })),
  ].sort((a, b) => {
    const aOwner = a.email.toLowerCase() === ownerEmail;
    const bOwner = b.email.toLowerCase() === ownerEmail;
    if (aOwner !== bOwner) return aOwner ? -1 : 1;
    return a.email.localeCompare(b.email);
  });
  const explicitAdminCount = permissions?.admins?.length ?? 0;
  const currentRole: UserRole | undefined = permissions?.current_role
    ?? (permissions?.current_user && (permissions.admins ?? []).some((email) => email.toLowerCase() === permissions.current_user!.toLowerCase())
      ? "admin"
      : permissions?.current_user && (permissions.consumers ?? []).some((email) => email.toLowerCase() === permissions.current_user!.toLowerCase())
        ? "consumer"
        : undefined);

  const addUser = () => {
    const email = newUserEmail.trim();
    if (!email) return;
    if (!EMAIL_RE.test(email)) { toast("Enter a valid email address"); return; }
    if (allUsers.some((u) => u.email.toLowerCase() === email.toLowerCase())) { toast("That user is already listed"); return; }
    const admins = [
      ...(permissions?.admins ?? []).filter(e => e !== email),
      ...(newUserRole === "admin" ? [email] : []),
    ];
    const consumers = [
      ...(permissions?.consumers ?? []).filter(e => e !== email),
      ...(newUserRole === "consumer" ? [email] : []),
    ];
    saveMutation.mutate({ admins, consumers });
    setNewUserEmail("");
  };

  const removeUser = (email: string) => {
    saveMutation.mutate({
      admins: (permissions?.admins ?? []).filter(e => e !== email),
      consumers: (permissions?.consumers ?? []).filter(e => e !== email),
    });
  };

  const changeRole = (email: string, newRole: UserRole) => {
    const admins = newRole === "admin"
      ? [...(permissions?.admins ?? []).filter(e => e !== email), email]
      : (permissions?.admins ?? []).filter(e => e !== email);
    const consumers = newRole === "consumer"
      ? [...(permissions?.consumers ?? []).filter(e => e !== email), email]
      : (permissions?.consumers ?? []).filter(e => e !== email);
    saveMutation.mutate({ admins, consumers });
  };

  const isSP = authStatus?.identity === "service_principal";
  const noToken = !authStatus?.token_present;

  if (isLoading) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 py-12" style={{ color: T.textSecondary }}>
        <span aria-hidden="true" className="settings-save-spinner" style={{ width: 20, height: 20, borderRadius: 999, border: `2px solid ${T.borderControl}`, borderTopColor: T.primary, display: "inline-block" }} />
        <span className="text-sm">Loading permissions…</span>
      </div>
    );
  }

  const overall = readiness?.overall;
  const ready = overall === "ready";
  const bannerTone = ready ? { fg: T.successFg, bg: T.successBg, border: T.successBorder }
    : overall === "core_ready" ? { fg: T.warningFg, bg: T.warningBg, border: T.warningBorder }
    : overall ? { fg: T.dangerFg, bg: T.dangerBg, border: T.dangerBorder }
    : { fg: T.textSecondary, bg: T.navBg, border: T.borderGroup };
  const bannerLabel = ready ? "System tables access verified — billing.usage · query.history · schema grants"
    : overall === "core_ready" ? "Core system tables verified — some optional grants missing"
    : overall === "needs_action" ? "System-table grants pending — some metrics show unavailable"
    : overall === "not_ready" ? "System tables not accessible — run the grants below"
    : readinessLoading ? "Checking system-table access…" : "System-table access status unknown";

  const spName = authStatus?.sp_display_name || authStatus?.sp_client_id || "<service-principal>";
  const cat = authStatus?.catalog || "<your_catalog>";
  const sch = authStatus?.schema || "<your_schema>";
  const userEmail = authStatus?.user_email;
  const appGrants =
`-- System tables (billing + query history + compute + lakeflow)
-- WHO: Must be run by a metastore admin or account admin.
-- WHEN: Required once when the app is first created (SP is tied to the app, not the code).
GRANT USE CATALOG ON CATALOG system TO \`${spName}\`;
GRANT USE SCHEMA ON SCHEMA system.billing TO \`${spName}\`;
GRANT SELECT ON TABLE system.billing.usage TO \`${spName}\`;
GRANT SELECT ON TABLE system.billing.list_prices TO \`${spName}\`;
GRANT SELECT ON TABLE system.billing.account_prices TO \`${spName}\`;
GRANT USE SCHEMA ON SCHEMA system.query TO \`${spName}\`;
GRANT SELECT ON TABLE system.query.history TO \`${spName}\`;
GRANT USE SCHEMA ON SCHEMA system.compute TO \`${spName}\`;
GRANT SELECT ON TABLE system.compute.clusters TO \`${spName}\`;
GRANT USE SCHEMA ON SCHEMA system.lakeflow TO \`${spName}\`;
GRANT SELECT ON TABLE system.lakeflow.pipelines TO \`${spName}\`;
GRANT USE SCHEMA ON SCHEMA system.serving TO \`${spName}\`;
GRANT SELECT ON TABLE system.serving.served_entities TO \`${spName}\`;

-- App schema (materialized views)
GRANT USE CATALOG ON CATALOG \`${cat}\` TO \`${spName}\`;
GRANT USE SCHEMA ON SCHEMA \`${cat}\`.\`${sch}\` TO \`${spName}\`;
GRANT CREATE TABLE ON SCHEMA \`${cat}\`.\`${sch}\` TO \`${spName}\`;
GRANT SELECT ON SCHEMA \`${cat}\`.\`${sch}\` TO \`${spName}\`;`;

  const preStyle: React.CSSProperties = {
    backgroundColor: T.codeBg,
    color: T.text,
    border: `1px solid ${T.borderGroup}`,
    borderRadius: 8,
    padding: "13px 14px",
    fontFamily: MONO,
    fontSize: 11.5,
    lineHeight: 1.6,
    overflowX: "auto",
    whiteSpace: "pre",
    margin: 0,
  };
  const copyGrantSql = () => {
    navigator.clipboard?.writeText(appGrants);
    setSqlCopied(true);
    toast("GRANT SQL copied");
    window.setTimeout(() => setSqlCopied(false), 1600);
  };
  const copyIcon = (
    <svg aria-hidden width="14" height="14" viewBox="0 0 16 16" fill="none">
      <rect x="5.25" y="5.25" width="7.5" height="7.5" rx="1.25" stroke="currentColor" strokeWidth="1.25" />
      <path d="M3.5 10.5H3A1.5 1.5 0 0 1 1.5 9V3A1.5 1.5 0 0 1 3 1.5h6A1.5 1.5 0 0 1 10.5 3v.5" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" />
    </svg>
  );

  return (
    <div>
      {/* ── Readiness banner ── */}
      <div style={{ marginBottom: 20 }}>
        <div style={{ border: `1px solid ${bannerTone.border}`, backgroundColor: bannerTone.bg, borderRadius: 8, padding: "10px 14px", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
            <span style={{ width: 8, height: 8, borderRadius: 999, backgroundColor: bannerTone.fg, flexShrink: 0 }} />
            <span style={{ fontSize: 13, color: bannerTone.fg, fontWeight: 500 }}>{bannerLabel}</span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 12, flexShrink: 0 }}>
            <LinkButton onClick={handleReadinessRecheck}>Re-check</LinkButton>
            <LinkButton onClick={() => setReadinessOpen(o => !o)}>{readinessOpen ? "Hide checks" : "View checks"}</LinkButton>
          </div>
        </div>
        {readinessOpen && (
          <div style={{ border: `1px solid ${T.borderGroup}`, borderTop: "none", borderRadius: "0 0 8px 8px", padding: "12px 14px" }}>
            <ReadinessChecks
              result={readiness ?? null}
              loading={readinessLoading}
              fetchError={readinessQueryError ? String(readinessQueryError) : null}
              onRecheck={handleReadinessRecheck}
            />
          </div>
        )}
      </div>

      {/* ── Users ── */}
      <div data-testid="settings-users-section" style={{ fontSize: 13, fontWeight: 600, color: T.text, marginBottom: 4, display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <span>Users</span>
        <span style={{ fontSize: 12, fontWeight: 400, color: T.textSecondary }}>
          {explicitAdminCount === 0
            ? "Bootstrap mode: every authenticated user is an implicit Admin"
            : "Anyone not listed is a Consumer (dashboards only)"}
        </span>
      </div>
      {explicitAdminCount === 1 && (
        <p id="last-admin-explanation" role="status" style={{ margin: "0 2px 8px", fontSize: 12, color: T.warningFg }}>
          The only explicit admin cannot be removed or changed to Consumer. Add another admin first.
        </p>
      )}
      <div data-testid="settings-users-table" style={{ border: `1px solid ${T.borderGroup}`, borderRadius: 8, overflowX: "auto" }}>
        <div className="settings-access-user-grid settings-access-user-header" style={{ ...USER_GRID_STYLE, padding: "7px 16px", backgroundColor: T.navBg, borderBottom: `1px solid ${T.borderRow}` }}>
          <span>User</span>
          <span>Organization</span>
          <span>Persona</span>
          <span>Permission role</span>
          <span aria-hidden="true">Action</span>
        </div>
        {allUsers.length === 0 ? (
          <div style={{ padding: "12px 16px", fontSize: 12, color: T.textSecondary, fontStyle: "italic" }}>
            No administrators are explicitly configured. The server currently treats every
            authenticated user{permissions?.current_user ? `, including ${permissions.current_user} (you),` : ""} as
            an implicit Admin so setup cannot lock itself out. Saving the first explicit Admin
            ends bootstrap mode; unlisted users then become Consumers.
          </div>
        ) : (
          allUsers.map(({ email, role }, i) => {
            const isLastAdmin = role === "admin" && explicitAdminCount === 1;
            const isOwner = email.toLowerCase() === ownerEmail;
            const protectedAdmin = isLastAdmin || isOwner;
            return (
              <div data-testid="access-user-row" className="settings-access-user-grid" key={email} style={{ ...USER_GRID_STYLE, minHeight: 52, padding: "9px 16px", borderTop: i === 0 ? "none" : `1px solid ${T.borderRow}` }}>
                <span title={email} style={{ fontSize: 13, color: T.text, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{email}</span>
                <OrganizationIdentity email={email} />
                <span className={`settings-persona settings-persona--${isOwner ? "owner" : "member"}`}>
                  {isOwner ? "Owner" : "User"}
                </span>
                <RoleMenuSelect
                  value={role}
                  onChange={(nextRole) => changeRole(email, nextRole)}
                  disabled={saveMutation.isPending || protectedAdmin}
                  ariaLabel={`Role for ${email}`}
                />
                <span className="settings-user-action-cell">
                  <button
                    type="button"
                    onClick={() => removeUser(email)}
                    disabled={saveMutation.isPending || protectedAdmin}
                    aria-describedby={isLastAdmin ? "last-admin-explanation" : undefined}
                    title={isOwner ? "The Owner must remain an administrator." : isLastAdmin ? "Add another admin before removing this administrator." : undefined}
                    className="settings-user-action settings-user-action--remove"
                  >
                    Remove
                  </button>
                </span>
              </div>
            );
          })
        )}
        <div data-testid="access-add-user-row" className="settings-access-user-grid" style={{ ...USER_GRID_STYLE, padding: "12px 16px", borderTop: `1px solid ${T.borderRow}`, backgroundColor: T.navBg }}>
          <div style={{ minWidth: 0 }}>
            <input
              type="email" placeholder="user@example.com" aria-label="User email" value={newUserEmail}
              onChange={e => setNewUserEmail(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter") addUser(); }}
              style={{ width: "100%", height: 32, borderRadius: 4, border: `1px solid ${T.borderControl}`, padding: "0 10px", fontSize: 13, color: T.text, backgroundColor: T.surface }}
            />
          </div>
          {EMAIL_RE.test(newUserEmail.trim()) ? (
            <OrganizationIdentity email={newUserEmail.trim()} />
          ) : (
            <span style={{ fontSize: 12, color: T.textSecondary }}>Organization</span>
          )}
          <span className="settings-persona settings-persona--member">User</span>
          <RoleMenuSelect value={newUserRole} onChange={setNewUserRole} ariaLabel="Role for new user" />
          <span className="settings-user-action-cell">
            <button type="button" className="settings-user-action settings-user-action--add" onClick={addUser} disabled={!newUserEmail.trim() || saveMutation.isPending}>
              {saveMutation.isPending ? "Saving…" : "Add User"}
            </button>
          </span>
        </div>
      </div>

      {/* ── Permission roles ── */}
      <div data-testid="settings-role-capabilities" style={{ margin: "20px 0" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 7 }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: T.text }}>Permission roles</span>
          <InfoPopover
            size="compact"
            label="Permission role storage details"
            text={
              permissions?.table_location
                ? `Roles are stored in ${permissions.table_location} and persist across deploys. Owner is sourced from the current Databricks Apps deployment creator.`
                : "Roles persist in the app's managed permissions table. Owner is sourced from the current Databricks Apps deployment creator."
            }
          />
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 8 }}>
          {([
            ["admin", "Admin"],
            ["consumer", "Consumer"],
          ] as const).map(([role, label]) => (
            <div key={role} style={{ border: `1px solid ${T.borderGroup}`, borderRadius: 8, padding: "10px 12px", backgroundColor: T.navBg }}>
              <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
                <span style={{ fontSize: 12, fontWeight: 700, color: T.text }}>{label}</span>
                {currentRole === role && <PermissionState state="verified">Your role</PermissionState>}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* ── Future metastore browser ── */}
      <fieldset
        disabled
        aria-disabled="true"
        aria-describedby="metastore-coming-soon"
        style={{
          position: "relative",
          margin: "0 0 20px",
          padding: 0,
          border: `1px solid ${T.borderGroup}`,
          borderRadius: 8,
          backgroundColor: T.navBg,
          color: T.textSecondary,
          filter: "grayscale(1)",
          opacity: 0.68,
          overflow: "hidden",
        }}
      >
        <legend className="sr-only">Add a metastore</legend>
        <div style={{ padding: "12px 16px", paddingRight: 108 }}>
          <div style={{ fontSize: 13, fontWeight: 600 }}>Add a metastore</div>
          <div id="metastore-coming-soon" style={{ marginTop: 2, fontSize: 12 }}>
            Browse and connect another metastore.
          </div>
        </div>
        <span
          aria-hidden="true"
          style={{ position: "absolute", top: 12, right: 14, borderRadius: 999, padding: "2px 8px", backgroundColor: T.borderGroup, color: T.textSecondary, fontSize: 10, fontWeight: 700, letterSpacing: 0.3, textTransform: "uppercase" }}
        >
          Coming soon
        </span>
        <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) auto", gap: 10, padding: "12px 16px", borderTop: `1px solid ${T.borderRow}` }}>
          <input
            type="text"
            disabled
            aria-label="Metastore"
            placeholder="Select a metastore"
            style={{ height: 32, minWidth: 0, borderRadius: 4, border: `1px solid ${T.borderControl}`, padding: "0 10px", fontSize: 13, color: T.textSecondary, backgroundColor: T.codeBg, cursor: "not-allowed" }}
          />
          <SecondaryButton disabled>Browse</SecondaryButton>
        </div>
      </fieldset>

      {/* ── Query authentication ── */}
      <div>
        <Group label="Query authentication">
          <Row first label="Mode" helper="How the app authenticates to the SQL warehouse."
            control={<span style={{ display: "inline-flex", alignItems: "center", gap: 6, borderRadius: 999, backgroundColor: T.successBg, border: `1px solid ${T.successBorder}`, color: T.successFg, padding: "2px 10px", fontSize: 12, fontWeight: 600 }}><span style={{ width: 6, height: 6, borderRadius: 999, backgroundColor: T.successFg }} />Service principal</span>} />
          <Row label="Running as" helper="The service principal this app queries as."
            control={<MonoChip>{authStatus?.sp_display_name || authStatus?.sp_client_id || "Service principal"}</MonoChip>} />
        </Group>

        {(isSP || noToken) && authStatus && (
          <>
            {overall && overall !== "ready" && (
              <div style={{ marginBottom: 12 }}>
                <Callout tone="warning">
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>System-table grants pending</div>
                  <div style={{ marginBottom: 8 }}>Each new app deploy gets a fresh service principal, so grants don't carry over. Until they run, affected metrics show <em>unavailable</em> (never $0.00). Run this as a metastore admin, then re-check.</div>
                  <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 8 }}>
                    <SecondaryButton onClick={copyGrantSql}><span className="inline-flex items-center gap-1.5">{copyIcon}{sqlCopied ? "Copied" : "Copy SQL"}</span></SecondaryButton>
                  </div>
                  <pre style={preStyle}>{appGrants}</pre>
                </Callout>
              </div>
            )}
            <div style={{ border: `1px solid ${T.borderGroup}`, borderRadius: 8, overflow: "hidden" }}>
              <button type="button" onClick={() => setGrantsOpen(o => !o)} aria-expanded={grantsOpen}
                style={{ width: "100%", display: "flex", alignItems: "center", justifyContent: "space-between", padding: "10px 14px", background: "none", border: "none", cursor: "pointer", fontSize: 13, fontWeight: 500, color: T.text }}>
                App runtime grants — exact SQL (run as metastore admin)
                <svg aria-hidden width="16" height="16" viewBox="0 0 16 16" fill="none"
                  style={{ color: T.textSecondary, flexShrink: 0, transform: grantsOpen ? "rotate(180deg)" : "rotate(0deg)", transition: "transform 140ms" }}>
                  <path d="m4 6 4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
              {grantsOpen && (
                <div style={{ borderTop: `1px solid ${T.borderRow}`, padding: "12px 14px", display: "flex", flexDirection: "column", gap: 8 }}>
                  <div style={{ fontSize: 12, color: T.textSecondary }}>Target SP: <MonoChip>{spName}</MonoChip> · Warehouse <strong>CAN_USE</strong> can't be granted via SQL — set it in SQL Warehouses → Permissions (the app also attempts this on startup).</div>
                  <div style={{ display: "flex", justifyContent: "flex-end" }}>
                    <SecondaryButton onClick={copyGrantSql}><span className="inline-flex items-center gap-1.5">{copyIcon}{sqlCopied ? "Copied" : "Copy SQL"}</span></SecondaryButton>
                  </div>
                  <pre style={preStyle}>{appGrants}</pre>
                  {userEmail && userEmail !== spName && (
                    <>
                      <div style={{ fontSize: 12, color: T.textSecondary, marginTop: 4 }}>Optional — user read access for {userEmail}:</div>
                      <pre style={preStyle}>{`GRANT SELECT ON SCHEMA \`${cat}\`.\`${sch}\` TO \`${userEmail}\`;`}</pre>
                    </>
                  )}
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
