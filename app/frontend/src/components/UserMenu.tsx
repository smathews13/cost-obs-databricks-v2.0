import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowUpRight,
  ChevronDown,
  ChevronRight,
  Github,
  LayoutGrid,
  LogOut,
  Mail,
  Slack,
} from "lucide-react";
import { FloatingMenu } from "@/components/ui/FloatingMenu";
import { DEFAULT_DOMAIN_ICON, domainIconForEmail } from "@/utils/domainIcons";

interface UserMenuProps {
  name: string;
  email: string;
  isAdmin: boolean;
  workspaceHost: string | null | undefined;
}

type MenuItemElement = HTMLAnchorElement | HTMLButtonElement;

interface FeedbackTargets {
  github_issue_url: string;
  email_href: string | null;
  slack: { url: string; fallback_url: string | null } | null;
}

const DEFAULT_FEEDBACK_TARGETS: FeedbackTargets = {
  github_issue_url: "https://github.com/smathews13/cost-obs-databricks-v2.0/issues/new",
  email_href: null,
  slack: null,
};

function workspaceBaseUrl(host: string | null | undefined): string | null {
  const trimmed = host?.trim();
  if (!trimmed) return null;
  return (trimmed.startsWith("http://") || trimmed.startsWith("https://")
    ? trimmed
    : `https://${trimmed}`
  ).replace(/\/+$/, "");
}

function safeSlackTarget(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    if (
      parsed.protocol === "slack:"
      && parsed.hostname === "user"
      && parsed.pathname.replaceAll("/", "") === ""
      && /^T[A-Z0-9]{8,}$/.test(parsed.searchParams.get("team") ?? "")
      && /^[UW][A-Z0-9]{8,}$/.test(parsed.searchParams.get("id") ?? "")
      && [...parsed.searchParams.keys()].every((key) => key === "team" || key === "id")
    ) {
      return value;
    }
    if (
      parsed.protocol === "https:"
      && parsed.hostname.endsWith(".slack.com")
      && parsed.hostname !== "hooks.slack.com"
      && parsed.pathname.startsWith("/team/")
      && !parsed.username
      && !parsed.password
      && !parsed.search
      && !parsed.hash
    ) {
      return value;
    }
  } catch {
    return null;
  }
  return null;
}

function readableUserName(name: string, email: string): string {
  const candidate = name.trim();
  const opaqueIdentity = (
    !candidate
    || /^\d/.test(candidate)
    || /^[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(candidate)
  );
  if (!opaqueIdentity) return candidate;

  const localPart = email.split("@")[0]?.trim();
  if (!localPart) return email;
  return localPart
    .split(/[._-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function UserMenu({ name, email, isAdmin, workspaceHost }: UserMenuProps) {
  const [open, setOpen] = useState(false);
  const [chooserOpen, setChooserOpen] = useState(false);
  const [feedbackTargets, setFeedbackTargets] = useState(DEFAULT_FEEDBACK_TARGETS);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const feedbackRef = useRef<HTMLButtonElement>(null);
  const mainItemRefs = useRef<Array<MenuItemElement | null>>([]);
  const chooserItemRefs = useRef<Array<MenuItemElement | null>>([]);
  const openFocusIndexRef = useRef(0);
  const hoverCloseTimerRef = useRef<number | null>(null);
  const baseUrl = useMemo(() => workspaceBaseUrl(workspaceHost), [workspaceHost]);
  const displayName = useMemo(() => readableUserName(name, email), [name, email]);
  const preferredIcon = useMemo(() => domainIconForEmail(email), [email]);
  // Customer logos are not published to the public mirror; fall back when one is missing.
  const [failedIconSrc, setFailedIconSrc] = useState<string | null>(null);
  const domainIcon = failedIconSrc === preferredIcon.src ? DEFAULT_DOMAIN_ICON : preferredIcon;
  const onIconError = useCallback(() => setFailedIconSrc(preferredIcon.src), [preferredIcon.src]);

  const closeMenu = useCallback((returnFocus = true) => {
    setOpen(false);
    setChooserOpen(false);
    if (returnFocus) {
      window.setTimeout(() => triggerRef.current?.focus(), 0);
    }
  }, []);

  const openChooser = useCallback((moveFocus: boolean) => {
    if (hoverCloseTimerRef.current !== null) {
      window.clearTimeout(hoverCloseTimerRef.current);
      hoverCloseTimerRef.current = null;
    }
    setChooserOpen(true);
    if (moveFocus) {
      window.setTimeout(() => chooserItemRefs.current[0]?.focus(), 0);
    }
  }, []);

  const scheduleChooserClose = () => {
    hoverCloseTimerRef.current = window.setTimeout(() => {
      setChooserOpen(false);
      hoverCloseTimerRef.current = null;
    }, 150);
  };

  useEffect(() => () => {
    if (hoverCloseTimerRef.current !== null) window.clearTimeout(hoverCloseTimerRef.current);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/user/feedback-targets", { signal: controller.signal })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("feedback config unavailable")))
      .then((targets: FeedbackTargets) => {
        const slackUrl = safeSlackTarget(targets.slack?.url);
        const fallbackUrl = safeSlackTarget(targets.slack?.fallback_url);
        setFeedbackTargets({
          github_issue_url: targets.github_issue_url || DEFAULT_FEEDBACK_TARGETS.github_issue_url,
          email_href: targets.email_href || null,
          slack: slackUrl ? { url: slackUrl, fallback_url: fallbackUrl } : null,
        });
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => {
      mainItemRefs.current[openFocusIndexRef.current]?.focus();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [open]);

  useEffect(() => {
    if (!open) return;

    const onPointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) closeMenu();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeMenu();
      }
    };
    const onRouteChange = () => closeMenu();

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("popstate", onRouteChange);
    window.addEventListener("hashchange", onRouteChange);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("popstate", onRouteChange);
      window.removeEventListener("hashchange", onRouteChange);
    };
  }, [closeMenu, open]);

  const moveMainFocus = (direction: 1 | -1) => {
    const items = mainItemRefs.current.filter((item): item is MenuItemElement => item !== null);
    if (items.length === 0) return;
    const currentIndex = items.indexOf(document.activeElement as MenuItemElement);
    const nextIndex = currentIndex < 0
      ? (direction === 1 ? 0 : items.length - 1)
      : (currentIndex + direction + items.length) % items.length;
    setChooserOpen(false);
    items[nextIndex].focus();
  };

  const onMainMenuKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      moveMainFocus(event.key === "ArrowDown" ? 1 : -1);
    } else if (event.key === "ArrowRight" && document.activeElement === feedbackRef.current) {
      event.preventDefault();
      openChooser(true);
    } else if (event.key === "ArrowLeft" && chooserOpen) {
      event.preventDefault();
      setChooserOpen(false);
      feedbackRef.current?.focus();
    }
  };

  const onChooserKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      event.stopPropagation();
      const items = chooserItemRefs.current.filter((item): item is MenuItemElement => item !== null);
      const currentIndex = items.indexOf(document.activeElement as MenuItemElement);
      const direction = event.key === "ArrowDown" ? 1 : -1;
      const nextIndex = currentIndex < 0
        ? (direction === 1 ? 0 : items.length - 1)
        : (currentIndex + direction + items.length) % items.length;
      items[nextIndex]?.focus();
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      event.stopPropagation();
      setChooserOpen(false);
      feedbackRef.current?.focus();
    }
  };

  const openSlack = () => {
    if (!feedbackTargets.slack) return;
    const { url, fallback_url: fallbackUrl } = feedbackTargets.slack;
    if (url.startsWith("https://")) {
      window.open(url, "_blank", "noopener,noreferrer");
      return;
    }
    let blurred = false;
    const onBlur = () => {
      blurred = true;
    };
    window.addEventListener("blur", onBlur, { once: true });
    window.location.href = url;
    window.setTimeout(() => {
      window.removeEventListener("blur", onBlur);
      if (fallbackUrl && !blurred && document.hasFocus()) {
        window.open(fallbackUrl, "_blank", "noopener,noreferrer");
      }
    }, 800);
  };

  const itemClass = "user-menu-item flex h-[40px] w-full items-center gap-[11px] rounded-[6px] px-[10px] text-left text-[13.5px] font-medium text-[#1B3139] hover:bg-[#FBF9F6] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FF3621]/35";
  const unavailableClass = !baseUrl ? " pointer-events-none opacity-50" : "";

  return (
    <div ref={rootRef} className="relative shrink-0">
      <button
        ref={triggerRef}
        type="button"
        aria-label={`User menu for ${email}`}
        aria-haspopup="menu"
        aria-expanded={open}
        data-state={open ? "open" : "closed"}
        onClick={() => {
          if (open) {
            closeMenu();
          } else {
            openFocusIndexRef.current = 0;
            setOpen(true);
          }
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            openFocusIndexRef.current = event.key === "ArrowDown" ? 0 : 2;
            setOpen(true);
          }
        }}
        className={`rail-user-trigger rail-control-border flex h-[28px] min-w-0 items-center gap-0 rounded-[7px] border px-[3px] text-[11.5px] font-medium text-[#E9EFED] transition-[background-color,border-color,box-shadow] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FF3621]/35 focus-visible:ring-offset-1 focus-visible:ring-offset-[#1B3139] min-[900px]:gap-[7px] min-[900px]:pl-[3px] min-[900px]:pr-[8px] ${open ? "bg-[#294A56] shadow-[0_1px_3px_rgba(4,18,23,.28),inset_0_1px_0_rgba(117,157,170,.12)]" : "bg-white/[.07] hover:bg-[#243F49]"}`}
      >
        <span
          data-testid="user-menu-domain-icon"
          className="flex h-[20px] w-[20px] items-center justify-center overflow-hidden rounded-full"
        >
          <img
            src={domainIcon.src}
            onError={onIconError}
            alt=""
            aria-hidden="true"
            className="aspect-square object-contain"
            style={{ width: `${domainIcon.scale}%`, height: `${domainIcon.scale}%` }}
          />
        </span>
        <span className="hidden max-w-[88px] truncate min-[900px]:block min-[1280px]:max-w-[160px] min-[1536px]:max-w-[220px]">{email}</span>
        <ChevronDown size={12} strokeWidth={2} className={`hidden shrink-0 text-[#B8CCD2] transition-transform min-[900px]:block ${open ? "rotate-180" : ""}`} aria-hidden="true" />
      </button>

      {open && (
        <div
          role="menu"
          aria-label="User menu"
          onKeyDown={onMainMenuKeyDown}
          className="user-menu-panel animate-fade-in absolute right-0 top-full z-50 mt-[8px] w-[300px] rounded-[10px] border border-[#E4E2DD] bg-white p-[8px] text-[#1B3139] shadow-[0_8px_28px_rgba(11,32,38,.16)]"
        >
          <div className="flex items-center gap-[10px] px-[8px] py-[7px]">
            <span
              data-testid="user-menu-expanded-domain-icon"
              className="flex h-[36px] w-[36px] shrink-0 items-center justify-center overflow-hidden rounded-full"
              style={{ backgroundColor: domainIcon.background }}
            >
              <img
                src={domainIcon.src}
                onError={onIconError}
                alt=""
                aria-hidden="true"
                className="aspect-square object-contain"
                style={{ width: `${domainIcon.scale}%`, height: `${domainIcon.scale}%` }}
              />
            </span>
            <span className="min-w-0 flex-1">
              <span className="user-menu-name block truncate text-[13.5px] font-semibold text-[#1B3139]">{displayName}</span>
              <span className="user-menu-secondary block truncate text-[11.5px] text-[#618794]">{email}</span>
            </span>
            {isAdmin && (
              <span className="user-menu-badge rounded-full bg-[#EEEDE9] px-[8px] py-[3px] text-[11px] font-semibold text-[#618794]">
                Admin
              </span>
            )}
          </div>

          <div className="user-menu-divider my-[6px] h-px bg-[#EFECE6]" />

          <a
            ref={(element) => { mainItemRefs.current[0] = element; }}
            role="menuitem"
            tabIndex={-1}
            href={baseUrl ? `${baseUrl}/apps` : undefined}
            aria-disabled={!baseUrl}
            onClick={() => closeMenu(false)}
            className={`${itemClass}${unavailableClass}`}
          >
            <LayoutGrid size={15} className="text-[#618794]" aria-hidden="true" />
            <span className="flex-1">Back to Apps</span>
            <ArrowUpRight size={13} className="text-[#618794]" aria-hidden="true" />
          </a>

          <div
            className="relative"
            onMouseEnter={() => openChooser(false)}
            onMouseLeave={scheduleChooserClose}
          >
            <button
              ref={(element) => {
                feedbackRef.current = element;
                mainItemRefs.current[1] = element;
              }}
              type="button"
              role="menuitem"
              tabIndex={-1}
              aria-haspopup="menu"
              aria-expanded={chooserOpen}
              onClick={() => openChooser(false)}
              className={itemClass}
            >
              <img src="/brand/databricks-symbol-red.svg" alt="" className="h-[15px] w-[14px] object-contain" />
              <span className="flex-1">Report Feedback</span>
              <ChevronRight size={13} className="text-[#618794]" aria-hidden="true" />
            </button>

            {chooserOpen && (
              <FloatingMenu
                anchorRef={feedbackRef}
                side="right"
                gap={8}
                role="menu"
                aria-label="Send via"
                onKeyDown={onChooserKeyDown}
                onMouseEnter={() => openChooser(false)}
                className="user-menu-panel animate-fade-in w-[250px] rounded-[10px] border border-[#E4E2DD] bg-white p-[8px] shadow-[0_8px_28px_rgba(11,32,38,.16)]"
              >
                <div className="user-menu-secondary px-[10px] pb-[5px] pt-[3px] text-[10.5px] font-bold tracking-[.07em] text-[#618794]">
                  SEND VIA
                </div>
                <a
                  ref={(element) => { chooserItemRefs.current[0] = element; }}
                  role="menuitem"
                  tabIndex={-1}
                  href={feedbackTargets.github_issue_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="user-menu-item flex h-[36px] w-full items-center gap-[10px] rounded-[6px] px-[10px] text-[13px] font-medium text-[#1B3139] hover:bg-[#FBF9F6] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FF3621]/35"
                >
                  <Github size={14} className="text-[#618794]" aria-hidden="true" />
                  GitHub issue
                </a>
                {feedbackTargets.slack && (
                  <button
                    ref={(element) => { chooserItemRefs.current[1] = element; }}
                    type="button"
                    role="menuitem"
                    tabIndex={-1}
                    onClick={openSlack}
                    className="user-menu-item flex h-[36px] w-full items-center gap-[10px] rounded-[6px] px-[10px] text-[13px] font-medium text-[#1B3139] hover:bg-[#FBF9F6] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FF3621]/35"
                  >
                    <Slack size={14} className="text-[#618794]" aria-hidden="true" />
                    Slack message
                  </button>
                )}
                {feedbackTargets.email_href && (
                  <a
                    ref={(element) => { chooserItemRefs.current[2] = element; }}
                    role="menuitem"
                    tabIndex={-1}
                    href={feedbackTargets.email_href}
                    className="user-menu-item flex h-[36px] w-full items-center gap-[10px] rounded-[6px] px-[10px] text-[13px] font-medium text-[#1B3139] hover:bg-[#FBF9F6] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FF3621]/35"
                  >
                    <Mail size={14} className="text-[#618794]" aria-hidden="true" />
                    Email
                  </a>
                )}
              </FloatingMenu>
            )}
          </div>

          <div className="user-menu-divider my-[6px] h-px bg-[#EFECE6]" />

          <a
            ref={(element) => { mainItemRefs.current[2] = element; }}
            role="menuitem"
            tabIndex={-1}
            href={baseUrl ?? undefined}
            aria-disabled={!baseUrl}
            onClick={() => closeMenu(false)}
            className={`${itemClass} text-[#D82A18]${unavailableClass}`}
          >
            <LogOut size={15} aria-hidden="true" />
            Logout
          </a>
        </div>
      )}
    </div>
  );
}
