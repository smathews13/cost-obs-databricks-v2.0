import type { ExportSections } from "@/components/ExportDialog";
import type { DBSQLDashboardBundle } from "@/types/billing";

function fmt(n: unknown, decimals = 2): string {
  return Number(n ?? 0).toFixed(decimals);
}

function escXml(v: unknown): string {
  const s = v == null ? "" : String(v);
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function cell(value: unknown, type: "String" | "Number" = "String"): string {
  const t = type === "Number" && !isNaN(Number(value)) ? "Number" : "String";
  return `<Cell><Data ss:Type="${t}">${escXml(value)}</Data></Cell>`;
}

function headerCell(value: unknown): string {
  return `<Cell ss:StyleID="header"><Data ss:Type="String">${escXml(value)}</Data></Cell>`;
}

function sheet(name: string, rows: string[]): string {
  if (rows.length === 0) return "";
  return `<Worksheet ss:Name="${escXml(name)}"><Table>${rows.join("")}</Table></Worksheet>`;
}

function row(...cells: string[]): string {
  return `<Row>${cells.join("")}</Row>`;
}

function metaSheet(dateRange: { start: string; end: string }, workspaceFilter?: WorkspaceFilter): string {
  const rows = [
    row(headerCell("Cost Observability Report")),
    row(cell("Date Range"), cell(`${dateRange.start} to ${dateRange.end}`)),
    row(
      cell("Workspace Filter"),
      cell(
        workspaceFilter?.ids.length
          ? workspaceFilter.names?.length
            ? workspaceFilter.names.join(", ")
            : workspaceFilter.ids.join(", ")
          : "All workspaces (account-wide)"
      )
    ),
    row(cell("Generated"), cell(new Date().toLocaleString())),
  ];
  return sheet("Report Info", rows);
}

export interface WorkspaceFilter {
  ids: string[];
  names?: string[];
}

type ExportField =
  | "total_dbus" | "total_spend" | "total_cost" | "workspace_count" | "category"
  | "workspace_id" | "workspace_name" | "sku_name"
  | "object_id" | "object_name" | "object_type" | "cluster_id" | "cluster_name"
  | "user_email" | "user_name" | "run_as_user"
  | "app_name" | "app_id" | "tag_key" | "tag_value";

type ExportRow = { [K in ExportField]?: unknown };

export interface CostExportData {
  summary?: ExportRow | null;
  products?: { products?: ExportRow[] | null } | null;
  workspaces?: { workspaces?: ExportRow[] | null } | null;
  skus?: { skus?: ExportRow[] | null } | null;
  pipelineObjects?: { objects?: ExportRow[] | null } | null;
  interactiveBreakdown?: { items?: ExportRow[] | null } | null;
  query360?: DBSQLDashboardBundle | null;
  aiml?: { categories?: { categories?: ExportRow[] | null } | null } | null;
  apps?: { apps?: { apps?: ExportRow[] | null } | null } | null;
  tagging?: { cost_by_tag?: { tags?: ExportRow[] | null } | null } | null;
  users?: { top_users?: ExportRow[] | null } | null;
}

export function generateCostCSV(
  data: CostExportData,
  sections: ExportSections,
  dateRange: { start: string; end: string },
  workspaceFilter?: WorkspaceFilter
): void {
  const sheets: string[] = [metaSheet(dateRange, workspaceFilter)];

  // === DBU Overview tab ===
  const dbuRows: string[] = [];

  if (sections.summary && data.summary) {
    const s = data.summary;
    dbuRows.push(row(headerCell("Executive Summary")));
    dbuRows.push(row(headerCell("Metric"), headerCell("Value")));
    dbuRows.push(row(cell("Total DBUs"), cell(fmt(s.total_dbus), "Number")));
    dbuRows.push(row(cell("Total Spend ($)"), cell(fmt(s.total_spend), "Number")));
    dbuRows.push(row(cell("Workspace Count"), cell(s.workspace_count ?? 0, "Number")));
    dbuRows.push(row(cell("Date Range"), cell(`${dateRange.start} to ${dateRange.end}`)));
    dbuRows.push(row());
  }

  if (sections.products && data.products?.products?.length) {
    dbuRows.push(row(headerCell("Product Breakdown")));
    dbuRows.push(row(headerCell("Category"), headerCell("Total DBUs"), headerCell("Total Spend ($)")));
    for (const p of data.products.products) {
      dbuRows.push(row(cell(p.category), cell(fmt(p.total_dbus), "Number"), cell(fmt(p.total_spend), "Number")));
    }
    dbuRows.push(row());
  }

  if (sections.workspaces && data.workspaces?.workspaces?.length) {
    dbuRows.push(row(headerCell("Workspace Breakdown")));
    dbuRows.push(row(headerCell("Workspace ID"), headerCell("Workspace Name"), headerCell("Total Spend ($)"), headerCell("Total DBUs")));
    for (const w of data.workspaces.workspaces) {
      dbuRows.push(row(cell(w.workspace_id), cell(w.workspace_name ?? w.workspace_id), cell(fmt(w.total_spend), "Number"), cell(fmt(w.total_dbus), "Number")));
    }
    dbuRows.push(row());
  }

  if (sections.skus && data.skus?.skus?.length) {
    dbuRows.push(row(headerCell("SKU Breakdown")));
    dbuRows.push(row(headerCell("SKU Name"), headerCell("Total DBUs"), headerCell("Total Spend ($)")));
    for (const s of data.skus.skus) {
      dbuRows.push(row(cell(s.sku_name), cell(fmt(s.total_dbus), "Number"), cell(fmt(s.total_spend), "Number")));
    }
    dbuRows.push(row());
  }

  if (sections.pipelines && data.pipelineObjects?.objects?.length) {
    dbuRows.push(row(headerCell("Jobs & Pipelines (Top 100)")));
    dbuRows.push(row(headerCell("Object ID"), headerCell("Object Name"), headerCell("Type"), headerCell("Total Spend ($)"), headerCell("Total DBUs")));
    for (const j of data.pipelineObjects.objects.slice(0, 100)) {
      dbuRows.push(row(cell(j.object_id), cell(j.object_name ?? j.object_id), cell(j.object_type ?? ""), cell(fmt(j.total_spend), "Number"), cell(fmt(j.total_dbus), "Number")));
    }
    dbuRows.push(row());
  }

  if (sections.interactive && data.interactiveBreakdown?.items?.length) {
    dbuRows.push(row(headerCell("Interactive Compute (Top 100)")));
    dbuRows.push(row(headerCell("Cluster ID"), headerCell("Cluster Name"), headerCell("Total Spend ($)"), headerCell("Total DBUs")));
    for (const c of data.interactiveBreakdown.items.slice(0, 100)) {
      dbuRows.push(row(cell(c.cluster_id), cell(c.cluster_name ?? c.cluster_id), cell(fmt(c.total_spend), "Number"), cell(fmt(c.total_dbus), "Number")));
    }
    dbuRows.push(row());
  }

  if (dbuRows.length) sheets.push(sheet("DBU Overview", dbuRows));

  // === SQL / Query 360 tab ===
  const sqlRows: string[] = [];

  if (sections.query360 && data.query360) {
    const q = data.query360;
    if (q.by_source?.sources?.length) {
      sqlRows.push(row(headerCell("Query Cost by Source")));
      sqlRows.push(row(headerCell("Source"), headerCell("Total Spend ($)"), headerCell("Query Count")));
      for (const s of q.by_source.sources) {
        sqlRows.push(row(cell(s.query_source_type ?? ""), cell(fmt(s.total_spend), "Number"), cell(s.query_count ?? 0, "Number")));
      }
      sqlRows.push(row());
    }
    if (q.by_warehouse?.warehouses?.length) {
      sqlRows.push(row(headerCell("Query Cost by Warehouse")));
      sqlRows.push(row(headerCell("Warehouse Name"), headerCell("Total Spend ($)"), headerCell("Query Count")));
      for (const w of q.by_warehouse.warehouses) {
        sqlRows.push(row(cell(w.warehouse_name ?? w.warehouse_id ?? ""), cell(fmt(w.total_spend), "Number"), cell(w.query_count ?? 0, "Number")));
      }
      sqlRows.push(row());
    }
    if (q.by_user?.users?.length) {
      sqlRows.push(row(headerCell("Query Cost by User (Top 50)")));
      sqlRows.push(row(headerCell("User"), headerCell("Total Spend ($)"), headerCell("Query Count")));
      for (const u of q.by_user.users.slice(0, 50)) {
        sqlRows.push(row(cell(u.executed_by ?? ""), cell(fmt(u.total_spend), "Number"), cell(u.query_count ?? 0, "Number")));
      }
      sqlRows.push(row());
    }
    if (q.top_queries?.queries?.length) {
      sqlRows.push(row(headerCell("Most Expensive Queries (Top 50)")));
      sqlRows.push(row(headerCell("Query Text"), headerCell("Cost ($)"), headerCell("Duration (s)"), headerCell("Warehouse")));
      for (const q2 of q.top_queries.queries.slice(0, 50)) {
        const text = (q2.statement_preview || "").slice(0, 200);
        sqlRows.push(row(cell(text), cell(fmt(q2.cost), "Number"), cell(fmt(q2.duration_seconds ?? 0), "Number"), cell(q2.warehouse_id ?? "")));
      }
      sqlRows.push(row());
    }
  }

  if (sqlRows.length) sheets.push(sheet("SQL", sqlRows));

  // === AI/ML tab ===
  const aimlRows: string[] = [];

  if (sections.aiml && data.aiml?.categories?.categories?.length) {
    aimlRows.push(row(headerCell("AI/ML by Category")));
    aimlRows.push(row(headerCell("Category"), headerCell("Total Spend ($)"), headerCell("Total DBUs")));
    for (const c of data.aiml.categories.categories) {
      aimlRows.push(row(cell(c.category), cell(fmt(c.total_spend), "Number"), cell(fmt(c.total_dbus), "Number")));
    }
    aimlRows.push(row());
  }

  if (aimlRows.length) sheets.push(sheet("AI-ML", aimlRows));

  // === Apps tab ===
  const appsRows: string[] = [];

  if (sections.apps && data.apps?.apps?.apps?.length) {
    appsRows.push(row(headerCell("Apps Cost")));
    appsRows.push(row(headerCell("App Name"), headerCell("Total Spend ($)"), headerCell("Total DBUs")));
    for (const a of data.apps.apps.apps) {
      appsRows.push(row(cell(a.app_name ?? a.app_id), cell(fmt(a.total_spend), "Number"), cell(fmt(a.total_dbus), "Number")));
    }
    appsRows.push(row());
  }

  if (appsRows.length) sheets.push(sheet("Apps", appsRows));

  // === Tagging tab ===
  const taggingRows: string[] = [];

  if (sections.tagging && data.tagging?.cost_by_tag?.tags?.length) {
    taggingRows.push(row(headerCell("Cost by Tag (Top 200)")));
    taggingRows.push(row(headerCell("Tag Key"), headerCell("Tag Value"), headerCell("Total Spend ($)")));
    for (const t of data.tagging.cost_by_tag.tags.slice(0, 200)) {
      taggingRows.push(row(cell(t.tag_key), cell(t.tag_value), cell(fmt(t.total_spend), "Number")));
    }
    taggingRows.push(row());
  }

  if (taggingRows.length) sheets.push(sheet("Tagging", taggingRows));

  // === Users tab ===
  const userRows: string[] = [];

  if (sections.users && data.users?.top_users?.length) {
    userRows.push(row(headerCell("Top Users by Spend (Top 100)")));
    userRows.push(row(headerCell("User Email"), headerCell("Total Spend ($)"), headerCell("Total DBUs")));
    for (const u of data.users.top_users.slice(0, 100)) {
      userRows.push(row(cell(u.user_email ?? u.run_as_user ?? u.user_name), cell(fmt(u.total_spend), "Number"), cell(fmt(u.total_dbus), "Number")));
    }
    userRows.push(row());
  }

  if (userRows.length) sheets.push(sheet("Users", userRows));

  // === KPIs & Trends tab ===
  // (no structured table data in platformKPIs currently — skip or add when available)

  // Build SpreadsheetML workbook
  const styles = `<Styles>
    <Style ss:ID="header">
      <Font ss:Bold="1" ss:Color="#FFFFFF"/>
      <Interior ss:Color="#1B3139" ss:Pattern="Solid"/>
    </Style>
  </Styles>`;

  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<?mso-application progid="Excel.Sheet"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
  xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"
  xmlns:x="urn:schemas-microsoft-com:office:excel">
  ${styles}
  ${sheets.join("\n")}
</Workbook>`;

  const blob = new Blob([xml], { type: "application/vnd.ms-excel;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = url;
    a.download = `cost-obs-${dateRange.start}-to-${dateRange.end}.xls`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  } finally {
    URL.revokeObjectURL(url);
  }
}
