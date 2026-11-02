export interface DomainIconDefinition {
  src: string;
  background: string;
  scale: number;
}

export interface OrganizationDefinition {
  id: string;
  name: string;
  domain: string;
  icon: DomainIconDefinition | null;
}

export const DEFAULT_DOMAIN_ICON: DomainIconDefinition = {
  src: "/brand/databricks-symbol-white.svg",
  background: "#ff5f46",
  scale: 62,
};

// Opaque domain hashes and asset IDs keep customer names out of filenames and
// source identifiers. The shared registry can replace this local map directly.
const DOMAIN_ICONS: Record<string, DomainIconDefinition> = {
  "0bceb744": DEFAULT_DOMAIN_ICON,
  "36ca1751": { src: "/brand/domain-icons/t01.png", background: "#ef2029", scale: 100 },
  "2100abe5": { src: "/brand/domain-icons/t02.png", background: "#e30613", scale: 100 },
  "0a85c5c0": { src: "/brand/domain-icons/t03.png", background: "#ffffff", scale: 82 },
  "19520464": { src: "/brand/domain-icons/t04.svg", background: "#000000", scale: 72 },
  "d1a298e4": { src: "/brand/domain-icons/t05.png", background: "#302e2e", scale: 100 },
};

const ORGANIZATIONS: Record<string, Omit<OrganizationDefinition, "domain">> = {
  "0bceb744": { id: "databricks", name: "Databricks", icon: DEFAULT_DOMAIN_ICON },
};

function domainKey(value: string): string {
  let hash = 2166136261;
  for (const character of value.trim().toLowerCase()) {
    hash ^= character.charCodeAt(0);
    hash = Math.imul(hash, 16777619) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

export function domainIconForEmail(email: string): DomainIconDefinition {
  const domain = email.split("@").at(-1)?.trim();
  if (!domain) return DEFAULT_DOMAIN_ICON;
  return DOMAIN_ICONS[domainKey(domain)] ?? DEFAULT_DOMAIN_ICON;
}

export function organizationForEmail(email: string): OrganizationDefinition {
  const domain = email.split("@").at(-1)?.trim().toLowerCase() ?? "";
  const organization = ORGANIZATIONS[domainKey(domain)];
  if (organization) return { ...organization, domain };
  return {
    id: domain ? `domain:${domain}` : "unknown",
    name: domain || "Unknown organization",
    domain,
    icon: null,
  };
}
