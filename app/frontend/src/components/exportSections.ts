import type { TabVisibility } from "@/components/settings/appSettings";

export interface ExportSections {
  summary: boolean;
  products: boolean;
  workspaces: boolean;
  skus: boolean;
  pipelines: boolean;
  interactive: boolean;
  query360: boolean;
  aiml: boolean;
  apps: boolean;
  tagging: boolean;
  users: boolean;
  platformKPIs: boolean;
  anomalies: boolean;
  awsCosts: boolean;
  optimize: boolean;
}

export type ExportFormat = "pdf" | "csv";

export const SECTION_TO_TAB: Record<keyof ExportSections, keyof TabVisibility | null> = {
  summary: "dbu",
  products: "dbu",
  workspaces: "dbu",
  skus: "dbu",
  pipelines: "dbu",
  interactive: "dbu",
  query360: "sql",
  aiml: "ai-gateway",
  apps: "apps",
  tagging: "tagging",
  users: "users-groups",
  platformKPIs: "kpis",
  anomalies: "kpis",
  awsCosts: "infra",
  optimize: "optimizer",
};

export function getRequiredExportTabs(
  sections: ExportSections,
  visibility: TabVisibility,
): Array<keyof TabVisibility> {
  return Array.from(new Set(
    (Object.keys(SECTION_TO_TAB) as Array<keyof ExportSections>)
      .filter((section) => sections[section])
      .map((section) => SECTION_TO_TAB[section])
      .filter((tab): tab is keyof TabVisibility => Boolean(tab && visibility[tab])),
  ));
}
