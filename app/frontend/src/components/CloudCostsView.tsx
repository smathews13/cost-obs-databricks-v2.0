import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import awsLogo from "@/assets/aws.png";
import azureLogo from "@/assets/azure.png";
import gcpLogo from "@/assets/gcp.svg";
import { KPITrendModal } from "./KPITrendModal";
import { KPICard } from "@/components/ui/KPICard";
import {
  BarChart,
  Bar,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from "recharts";
import { format, parseISO } from "date-fns";
import type {
  AWSCostsResponse,
  TimeseriesResponse,
  AWSActualDashboardBundle,
  AzureActualDashboardBundle,
  GCPActualDashboardBundle,
  InfraCostsResponse,
  InfraCostsTimeseriesResponse,
  InfraBillingSummary,
} from "@/types/billing";
import { formatCurrency, workspaceUrl } from "@/utils/formatters";
import { StatusIndicator } from "./StatusIndicator";
import { AzureActualView } from "./AzureActualView";
import { GCPActualView } from "./GCPActualView";
import { VirtualizedList } from "./VirtualizedList";
import { AWSActualView } from "./AWSActualView";
import { CloudIntegrationWizard } from "./CloudIntegrationWizard";
import type { CloudIntegration } from "./CloudIntegrationWizard";
import { C } from "@/theme";
import { PageHero, InfoPanel } from "@/components/brand";

type CostMode = "estimated" | "actual";

interface CloudCostsViewProps {
  data: AWSCostsResponse | undefined;
  isLoading: boolean;
  timeseriesData: TimeseriesResponse | undefined;
  timeseriesLoading: boolean;
  host: string | null | undefined;
  actualData?: AWSActualDashboardBundle;
  actualLoading?: boolean;
  azureActualData?: AzureActualDashboardBundle;
  azureActualLoading?: boolean;
  gcpActualData?: GCPActualDashboardBundle;
  gcpActualLoading?: boolean;
  infraData?: InfraCostsResponse;
  infraLoading?: boolean;
  infraTimeseriesData?: InfraCostsTimeseriesResponse;
  infraTimeseriesLoading?: boolean;
  startDate?: string;
  endDate?: string;
  detectedCloud?: string;
  workspaceNameMap?: Record<string, string>;
  workspaceIds?: string[];
  accountPricingApplied?: boolean;
  loadError?: string;
  partialReasons?: Record<string, string>;
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);
}

type SortField = "cluster_name" | "estimated_aws_cost" | "total_dbu_hours" | "days_active";
type SortDirection = "asc" | "desc";

const FAMILY_PALETTE = [
  C.s2, C.s5, C.s3, C.s3, C.s4,
  C.s5, C.s1, C.lava, C.slate,
];

const INSTANCE_COLORS: Record<string, string> = {
  i3: C.s2, i3en: C.s2, i4i: C.s5,
  m4: C.s3, m5: C.s3, m5d: C.s3, m5n: C.s3,
  m6i: C.s3, m6id: C.s3, m7g: C.s3, m7gd: C.s3, m7i: C.s3,
  r5: C.s4, r5d: C.s4, r6id: C.s4, r6gd: C.s4, r8gd: C.s4,
  c5: C.s5, c5d: C.s5, c6gd: C.s5,
  g4dn: C.s1, g5: C.s1,
  p3: C.lava,
  "rd-fleet": C.s5, "rgd-fleet": C.s5,
  Standard_D: C.s2, Standard_DS: C.s2,
  Standard_E: C.s3, Standard_ES: C.s3,
  Standard_F: C.s5, Standard_FS: C.s5,
  Standard_L: C.s4, Standard_LS: C.s4,
  Standard_M: C.lava,
  Standard_NC: C.s1, Standard_ND: C.s1, Standard_NV: C.s3,
  unknown: C.slate,
};

function getInstanceColor(name: string, index: number): string {
  return INSTANCE_COLORS[name] || FAMILY_PALETTE[index % FAMILY_PALETTE.length];
}

function formatDate(dateStr: string): string {
  try {
    return format(parseISO(dateStr), "MMM d");
  } catch {
    return dateStr;
  }
}

function getClusterUrl(host: string | null | undefined, clusterId: string | null, workspaceId: string | null): string | null {
  if (!host || !clusterId) return null;
  const workspaceParam = workspaceId ? `?o=${workspaceId}` : '';
  return workspaceUrl(host, `/compute/interactive${workspaceParam}`);
}

function getInstanceFamily(instanceType: string | null | undefined): string {
  if (!instanceType) return 'unknown';
  if (instanceType.startsWith('Standard_')) {
    const m = instanceType.match(/^(Standard_[A-Z]+)/);
    return m ? m[1] : 'unknown';
  }
  const dotIdx = instanceType.indexOf('.');
  return dotIdx > 0 ? instanceType.slice(0, dotIdx) : instanceType;
}

function getInstancePricingUrl(instanceType: string | null, isAzure: boolean = false): string {
  if (isAzure) {
    if (!instanceType) return "https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/";
    const seriesMatch = instanceType.match(/^Standard_([A-Z]+)/i);
    const series = seriesMatch ? seriesMatch[1].toUpperCase() : null;
    const azureFamilyUrls: Record<string, string> = {
      'D': 'https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/#d-series',
      'E': 'https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/#e-series',
      'F': 'https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/#f-series',
      'L': 'https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/#l-series',
      'M': 'https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/#m-series',
      'NC': 'https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/#nc-series',
      'ND': 'https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/#nd-series',
    };
    return series && azureFamilyUrls[series]
      ? azureFamilyUrls[series]
      : "https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/";
  }
  if (!instanceType) return "https://aws.amazon.com/ec2/pricing/on-demand/";
  const family = instanceType.split('.')[0];
  const familyUrls: Record<string, string> = {
    'i3': 'https://aws.amazon.com/ec2/instance-types/i3/',
    'i3en': 'https://aws.amazon.com/ec2/instance-types/i3en/',
    'm5': 'https://aws.amazon.com/ec2/instance-types/m5/',
    'm5d': 'https://aws.amazon.com/ec2/instance-types/m5/',
    'm6i': 'https://aws.amazon.com/ec2/instance-types/m6i/',
    'r5': 'https://aws.amazon.com/ec2/instance-types/r5/',
    'r5d': 'https://aws.amazon.com/ec2/instance-types/r5/',
    'c5': 'https://aws.amazon.com/ec2/instance-types/c5/',
    'c5d': 'https://aws.amazon.com/ec2/instance-types/c5/',
    'g4dn': 'https://aws.amazon.com/ec2/instance-types/g4/',
    'g5': 'https://aws.amazon.com/ec2/instance-types/g5/',
    'p3': 'https://aws.amazon.com/ec2/instance-types/p3/',
  };
  return familyUrls[family] || "https://aws.amazon.com/ec2/pricing/on-demand/";
}

export function CloudCostsView({
  data,
  isLoading,
  timeseriesData,
  timeseriesLoading,
  host: _host,
  actualData,
  actualLoading,
  azureActualData,
  azureActualLoading,
  gcpActualData,
  gcpActualLoading,
  infraData,
  infraLoading,
  infraTimeseriesData,
  infraTimeseriesLoading,
  startDate,
  endDate,
  detectedCloud,
  workspaceNameMap,
  workspaceIds,
  accountPricingApplied = false,
  loadError,
  partialReasons,
}: CloudCostsViewProps) {
  const [sortField, setSortField] = useState<SortField>("estimated_aws_cost");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const [currentPage, setCurrentPage] = useState(1);
  const [showHistoricalClusters, setShowHistoricalClusters] = useState(false);
  const [selectedKPI, setSelectedKPI] = useState<{kpi: string; label: string} | null>(null);
  const [selectedFamilies, setSelectedFamilies] = useState<Set<string>>(new Set());
  const [prevSelectedFamilies, setPrevSelectedFamilies] = useState(selectedFamilies);
  if (prevSelectedFamilies !== selectedFamilies) {
    setPrevSelectedFamilies(selectedFamilies);
    setCurrentPage(1);
  }
  const [tableFamily, setTableFamily] = useState<string[]>([]);
  const tableFamilySeen = useRef<Set<string>>(new Set());
  const [tableWorkspace, setTableWorkspace] = useState<string[]>([]);
  const tableWorkspaceSeen = useRef<Set<string>>(new Set());
  const [tableWorkspaceSearch, setTableWorkspaceSearch] = useState("");
  const [clusterSearch, setClusterSearch] = useState("");
  const [familyFilterOpen, setFamilyFilterOpen] = useState(false);
  const [workspaceFilterOpen, setWorkspaceFilterOpen] = useState(false);
  const familyFilterRef = useRef<HTMLDivElement>(null);
  const workspaceFilterRef = useRef<HTMLDivElement>(null);

  const itemsPerPage = 10;

  const [activeActualCloud, setActiveActualCloud] = useState<"AWS" | "AZURE" | "GCP">(() => {
    const c = (detectedCloud || "AWS").toUpperCase();
    if (c === "AZURE") return "AZURE";
    if (c === "GCP") return "GCP";
    return "AWS";
  });

  const INTEGRATIONS_KEY = "cost-obs-cloud-integrations";
  const [cloudIntegrations, setCloudIntegrations] = useState<CloudIntegration[]>(() => {
    try { return JSON.parse(localStorage.getItem(INTEGRATIONS_KEY) || "[]"); } catch { return []; }
  });
  const [showIntegrationWizard, setShowIntegrationWizard] = useState(false);
  const [wizardCloud, setWizardCloud] = useState<"azure" | "aws" | "gcp" | null>(null);
  const [wizardExpandedStep, setWizardExpandedStep] = useState<number | null>(null);
  const [viewingIntegration, setViewingIntegration] = useState<CloudIntegration | null>(null);

  const addIntegration = (cloud: "azure" | "aws" | "gcp") => {
    if (cloudIntegrations.length >= 3) return;
    const newInt: CloudIntegration = { id: Date.now().toString(), cloud, label: cloud === "azure" ? "Azure" : cloud === "gcp" ? "GCP" : "AWS" };
    const updated = [...cloudIntegrations, newInt];
    setCloudIntegrations(updated);
    localStorage.setItem(INTEGRATIONS_KEY, JSON.stringify(updated));
  };

  const removeIntegration = (id: string) => {
    const updated = cloudIntegrations.filter(i => i.id !== id);
    setCloudIntegrations(updated);
    localStorage.setItem(INTEGRATIONS_KEY, JSON.stringify(updated));
  };

  const openWizardForExisting = (integration: CloudIntegration) => {
    setWizardCloud(integration.cloud);
    setWizardExpandedStep(null);
    setShowIntegrationWizard(true);
    setViewingIntegration(integration);
  };

  useEffect(() => {
    if (!familyFilterOpen && !workspaceFilterOpen) return;
    const handler = (e: MouseEvent) => {
      if (familyFilterRef.current && !familyFilterRef.current.contains(e.target as Node)) {
        setFamilyFilterOpen(false);
      }
      if (workspaceFilterRef.current && !workspaceFilterRef.current.contains(e.target as Node)) {
        setWorkspaceFilterOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [familyFilterOpen, workspaceFilterOpen]);

  const queryClient = useQueryClient();
  const wsKey = workspaceIds?.join(",") ?? "";
  useEffect(() => {
    if (!startDate || !endDate) return;
    for (const kpi of ["infra_cost", "infra_clusters", "infra_dbu_hours", "avg_cost_per_cluster"]) {
      queryClient.prefetchQuery({
        queryKey: ["kpi-trend", kpi, startDate, endDate, "daily", wsKey],
        queryFn: async () => {
          const params = new URLSearchParams({ kpi, start_date: startDate, end_date: endDate, granularity: "daily" });
          if (wsKey) params.set("workspace_ids", wsKey);
          const res = await fetch(`/api/billing/kpi-trend?${params}`);
          if (!res.ok) throw new Error("prefetch failed");
          return res.json();
        },
        staleTime: 5 * 60 * 1000,
      });
    }
  }, [startDate, endDate, wsKey, queryClient]);

  const MINIMIZE_KEY = "cost-obs-minimize-infra-info";
  const [infoMinimized, setInfoMinimized] = useState(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem(MINIMIZE_KEY) === "true";
    }
    return false;
  });

  const handleMinimizeToggle = (checked: boolean) => {
    setInfoMinimized(checked);
    if (checked) {
      localStorage.setItem(MINIMIZE_KEY, "true");
    } else {
      localStorage.removeItem(MINIMIZE_KEY);
    }
  };

  const cloud = infraData?.cloud || detectedCloud || "AWS";
  const cloudDisplayName = cloud.toUpperCase() === "AZURE" ? "Azure" : cloud.toUpperCase() === "GCP" ? "GCP" : "AWS";
  const isAzure = cloud.toUpperCase() === "AZURE";
  const isGCP = cloud.toUpperCase() === "GCP";
  const daysCount = startDate && endDate
    ? Math.round((new Date(endDate).getTime() - new Date(startDate).getTime()) / 86400000) + 1
    : null;

  const awsActualAvailable = actualData?.available === true;
  const azureActualAvailable = azureActualData?.available === true;
  const gcpActualAvailable = gcpActualData?.available === true;
  const multipleActualAvailable = [awsActualAvailable, azureActualAvailable, gcpActualAvailable].filter(Boolean).length > 1;
  const actualAvailable = awsActualAvailable || azureActualAvailable || gcpActualAvailable;

  const cloudTabs: Array<{ key: "AWS" | "AZURE" | "GCP"; label: string; logo: string; activeClass: string; available: boolean }> = [
    { key: "AWS",   label: "AWS",   logo: awsLogo,   activeClass: "text-orange-600", available: awsActualAvailable },
    { key: "AZURE", label: "Azure", logo: azureLogo, activeClass: "text-blue-600",   available: azureActualAvailable },
    { key: "GCP",   label: "GCP",   logo: gcpLogo,   activeClass: "text-blue-500",   available: gcpActualAvailable },
  ];
  const CloudTabSwitcher = multipleActualAvailable ? (
    <div className="flex items-center gap-1 rounded-lg bg-gray-100 p-1">
      {cloudTabs.filter(t => t.available).map(t => (
        <button
          key={t.key}
          onClick={() => setActiveActualCloud(t.key)}
          className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
            activeActualCloud === t.key ? `bg-white shadow ${t.activeClass}` : "text-gray-600 hover:text-gray-900"
          }`}
        >
          <img src={t.logo} className="h-3.5 w-3.5 object-contain" alt={t.label} />
          {t.label}
        </button>
      ))}
    </div>
  ) : null;
  const [costMode, setCostMode] = useState<CostMode>("estimated");

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDirection(sortDirection === "asc" ? "desc" : "asc");
    } else {
      setSortField(field);
      setSortDirection("desc");
      setCurrentPage(1);
    }
  };

  const sortIcon = (field: SortField) => {
    if (sortField !== field) {
      return <span className="ml-1 text-gray-300">↕</span>;
    }
    return <span className="ml-1">{sortDirection === "asc" ? "↑" : "↓"}</span>;
  };

  const showLoading = isLoading || infraLoading || (costMode === "actual" && (
    activeActualCloud === "AZURE" ? azureActualLoading :
    activeActualCloud === "GCP"   ? gcpActualLoading :
    actualLoading
  ));

  const billingSummary: InfraBillingSummary | undefined = infraData?.billing_summary;

  const cloudSummary = useMemo(() => {
    if (!data?.clusters) return { totalCost: 0, totalDBUHours: 0, avgActiveClustersPerDay: 0, avgCostPerCluster: 0 };
    const bs = billingSummary;
    const totalDBUHours = data.clusters.reduce((sum, c) => sum + (c.total_dbu_hours || 0), 0);
    const clustersWithTypes = data.clusters.filter(c => c.driver_instance_type || c.worker_instance_type);
    const estimatedTotal = clustersWithTypes.reduce((sum, c) => sum + (c.estimated_aws_cost || 0), 0);

    const avgCostPerCluster = bs?.avg_cost_per_cluster || (() => {
      const tsPoints = infraTimeseriesData?.timeseries || [];
      const activeTsPoints = tsPoints.filter(p => (p["Infrastructure Cost"] || 0) > 0);
      if (activeTsPoints.length > 0) {
        const tsTotal = activeTsPoints.reduce((s, p) => s + (p["Infrastructure Cost"] || 0), 0);
        return tsTotal / activeTsPoints.length;
      }
      return 0;
    })();

    if (bs && bs.total_cost != null && bs.total_cost > 0) {
      return {
        totalCost: bs.total_cost,
        totalDBUHours,
        avgActiveClustersPerDay: bs.avg_clusters_per_day ?? 0,
        avgCostPerCluster,
      };
    }

    return { totalCost: estimatedTotal, totalDBUHours, avgActiveClustersPerDay: clustersWithTypes.length, avgCostPerCluster };
  }, [data, infraTimeseriesData, billingSummary]);

  const allClusters = data?.clusters;
  const filteredClusters = useMemo(
    () => {
      const clusters = allClusters ?? [];
      return showHistoricalClusters ? clusters : clusters.filter(c => c.driver_instance_type || c.worker_instance_type);
    },
    [allClusters, showHistoricalClusters],
  );

  const familyFilteredClusters = useMemo(
    () => selectedFamilies.size === 0
      ? filteredClusters
      : filteredClusters.filter(c => {
          const df = getInstanceFamily(c.driver_instance_type);
          const wf = getInstanceFamily(c.worker_instance_type);
          return selectedFamilies.has(df) || selectedFamilies.has(wf);
        }),
    [filteredClusters, selectedFamilies],
  );

  const instanceFamilies = data?.instance_families;
  const availableTableFamilies = useMemo(() => {
    const families = new Set<string>();
    (instanceFamilies || []).forEach(f => {
      if (f.instance_family && f.instance_family !== 'unknown') families.add(f.instance_family);
    });
    return [...families].sort();
  }, [instanceFamilies]);

  useEffect(() => {
    const seen = tableFamilySeen.current;
    const fresh = availableTableFamilies.filter(x => !seen.has(x));
    if (fresh.length === 0) return;
    setTableFamily(prev => Array.from(new Set([...prev, ...fresh])));
    fresh.forEach(x => seen.add(x));
  }, [availableTableFamilies]);

  const availableTableWorkspaces = useMemo(() => {
    const ws = new Set<string>();
    familyFilteredClusters.forEach(c => { if (c.workspace_id) ws.add(c.workspace_id); });
    return [...ws].sort();
  }, [familyFilteredClusters]);

  // Fallback name lookup from cluster rows (populated via backend LEFT JOIN on
  // system.access.workspaces_latest). Used when workspaceNameMap is missing an id.
  const clusterWorkspaceNames = useMemo(() => {
    const map: Record<string, string> = {};
    for (const c of familyFilteredClusters) {
      const wsId = c.workspace_id;
      const wsName = c.workspace_name;
      if (wsId && wsName && !map[wsId]) map[wsId] = wsName;
    }
    return map;
  }, [familyFilteredClusters]);

  useEffect(() => {
    const seen = tableWorkspaceSeen.current;
    const fresh = availableTableWorkspaces.filter(x => !seen.has(x));
    if (fresh.length === 0) return;
    setTableWorkspace(prev => Array.from(new Set([...prev, ...fresh])));
    fresh.forEach(x => seen.add(x));
  }, [availableTableWorkspaces]);

  const filteredTimeseriesData = useMemo(() => {
    if (!timeseriesData?.timeseries) return null;
    if (selectedFamilies.size === 0) return timeseriesData.timeseries;
    return timeseriesData.timeseries.map((point) => {
      let filteredCost = 0;
      for (const family of selectedFamilies) {
        filteredCost += (point[family] as number) || 0;
      }
      return { ...point, "AWS Cost": filteredCost };
    });
  }, [timeseriesData, selectedFamilies]);

  if (showLoading) {
    return (
      <div className="flex h-64 flex-col items-center justify-center gap-3">
        <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-200" style={{ borderTopColor: C.lava }} />
        <p className="text-sm text-gray-500">Loading cloud costs...</p>
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="space-y-6">
        <div className="rounded-lg bg-white p-6 border" style={{ borderColor: C.hairline }}>
          <h3 className="text-lg font-semibold text-gray-900">
            Cloud cost data could not be loaded
          </h3>
          <p className="mt-2 text-sm text-gray-600">
            Required Databricks usage totals did not complete after automatic retries.
            Wait for current SQL work to finish, then refresh Cloud Costs.
          </p>
          <p className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-800">
            {loadError}
          </p>
        </div>
      </div>
    );
  }

  if (costMode === "actual" && activeActualCloud === "AZURE" && azureActualData?.available) {
    return (
      <AzureActualView
        azureActualData={azureActualData}
        cloudTabSwitcher={CloudTabSwitcher}
        onSwitchToEstimated={() => setCostMode("estimated")}
      />
    );
  }

  if (costMode === "actual" && activeActualCloud === "GCP" && gcpActualData?.available) {
    return (
      <GCPActualView
        gcpActualData={gcpActualData}
        cloudTabSwitcher={CloudTabSwitcher}
        onSwitchToEstimated={() => setCostMode("estimated")}
      />
    );
  }

  if (costMode === "actual" && activeActualCloud === "AWS" && actualData?.available) {
    return (
      <AWSActualView
        actualData={actualData}
        cloudTabSwitcher={CloudTabSwitcher}
        onSwitchToEstimated={() => setCostMode("estimated")}
      />
    );
  }

  const ModeToggle = actualAvailable ? (
    <div className="mb-6 flex items-center justify-between">
      <div className="flex items-center gap-2">
        <span className="inline-flex items-center gap-1.5 rounded-full bg-orange-100 px-3 py-1 text-sm font-medium text-orange-800">
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          {multipleActualAvailable ? "Multi-Cloud Cost Data Available" : azureActualAvailable ? "Azure Cost Data Available" : gcpActualAvailable ? "GCP Cost Data Available" : "AWS CUR Data Available"}
        </span>
        {CloudTabSwitcher}
      </div>
      <div className="flex items-center gap-2">
        <div className="flex rounded-lg bg-gray-100 p-1">
          <button
            onClick={() => setCostMode("actual")}
            className={`rounded-md px-4 py-1.5 text-sm font-medium transition-colors ${
              costMode === "actual"
                ? "bg-white text-orange-600 shadow"
                : "text-gray-600 hover:text-gray-900"
            }`}
          >
            Actual Costs
          </button>
          <button
            onClick={() => setCostMode("estimated")}
            className={`rounded-md px-4 py-1.5 text-sm font-medium transition-colors ${
              costMode === "estimated"
                ? "bg-white text-orange-600 shadow"
                : "text-gray-600 hover:text-gray-900"
            }`}
          >
            Estimated
          </button>
        </div>
        {cloudIntegrations.length < 3 && (
          <button
            onClick={() => { setWizardCloud(null); setWizardExpandedStep(null); setViewingIntegration(null); setShowIntegrationWizard(true); }}
            className="inline-flex items-center gap-1.5 rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 transition-colors"
          >
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
            </svg>
            Integrate cloud costs
          </button>
        )}
      </div>
    </div>
  ) : null;

  const EstimationInfoBox = (
    <InfoPanel
      title="Cloud Costs tab methodology"
      minimized={infoMinimized}
      onToggle={handleMinimizeToggle}
    >
      {isAzure ? (
        <ul className="list-inside list-disc space-y-1">
          <li>Shows Databricks DBUs, {accountPricingApplied ? "account-price" : "list-price"} spend, active clusters, and Azure VM instance metadata</li>
          <li>Databricks spend comes from billed DBU usage {accountPricingApplied ? "with the account pricing factor applied" : "joined to the applicable system list price"}</li>
          <li>DBUs are not VM node-hours, so the app does not infer an Azure currency cost from them</li>
          <li>Connect Azure Cost Management for authoritative VM, disk, and network costs</li>
        </ul>
      ) : isGCP ? (
        <ul className="list-inside list-disc space-y-1">
          <li>Shows Databricks DBUs, {accountPricingApplied ? "account-price" : "list-price"} spend, active clusters, and GCP machine-type metadata</li>
          <li>Databricks spend comes from billed DBU usage {accountPricingApplied ? "with the account pricing factor applied" : "joined to the applicable system list price"}</li>
          <li>DBUs are not VM node-hours, so the app does not infer a GCP currency cost from them</li>
          <li>Connect GCP Billing Export for authoritative VM, disk, storage, and network costs</li>
        </ul>
      ) : (
        <ul className="list-inside list-disc space-y-1">
          <li>Shows Databricks DBUs, {accountPricingApplied ? "account-price" : "list-price"} spend, active clusters, and EC2 instance-type metadata</li>
          <li>Databricks spend comes from billed DBU usage {accountPricingApplied ? "with the account pricing factor applied" : "joined to the applicable system list price"}</li>
          <li>DBUs are not VM node-hours, so the app does not infer an AWS currency cost from them</li>
          <li>Connect AWS CUR 2.0 for authoritative EC2, EBS, network, and discount-adjusted costs</li>
        </ul>
      )}
      <p className="mt-3 text-xs italic" style={{ color: C.slate }}>
        For exact costs, integrate {isAzure ? "Azure Cost Management" : isGCP ? "GCP Billing Export" : "AWS CUR 2.0"} below.
      </p>
    </InfoPanel>
  );

  const CurSetupBanner = !actualAvailable ? (
    <div className="mb-6">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex rounded-lg bg-gray-100 p-1">
            <button
              disabled
              className="cursor-not-allowed rounded-md px-4 py-1.5 text-sm font-medium text-gray-500"
              title={`Configure ${isAzure ? "Azure Cost Management Export" : "AWS CUR"} to enable actual costs`}
            >
              Actual Costs
            </button>
            <button className="rounded-md bg-white px-4 py-1.5 text-sm font-medium text-orange-600 shadow">
              Estimated
            </button>
          </div>
          <span className="flex items-center gap-1.5 text-sm text-gray-500">
            <svg className="h-4 w-4 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            Add cloud billing integrations to see actual costs from {isAzure ? "Azure Cost Management" : "AWS CUR"} alongside your estimates.
          </span>
        </div>
        {cloudIntegrations.length < 3 && (
          <button
            onClick={() => { setWizardCloud(null); setWizardExpandedStep(null); setViewingIntegration(null); setShowIntegrationWizard(true); }}
            className="inline-flex items-center gap-1.5 rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 transition-colors"
          >
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
            </svg>
            Integrate cloud costs
          </button>
        )}
      </div>

      {cloudIntegrations.length > 0 && (
        <div className="mt-3 space-y-2">
          <div className="text-xs font-medium uppercase tracking-wider text-gray-500">Additional Cloud Integrations</div>
          {cloudIntegrations.map((integration) => (
            <div key={integration.id} className="flex items-center justify-between rounded-lg border border-gray-200 bg-white px-3 py-2.5">
              <div className="flex items-center gap-2">
                <span
                  className="rounded px-1.5 py-0.5 text-xs font-medium"
                  style={integration.cloud === "azure"
                    ? { backgroundColor: '#0078D420', color: '#0078D4' }
                    : { backgroundColor: '#FF990020', color: '#CC7700' }
                  }
                >
                  {integration.label}
                </span>
                <span className="text-sm text-gray-700">{integration.cloud === "azure" ? "Azure Cost Management Export" : integration.cloud === "gcp" ? "GCP Billing Export (BigQuery)" : "AWS CUR 2.0"}</span>
                <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-700">Setup in progress</span>
              </div>
              <div className="flex items-center gap-2">
                <button onClick={() => openWizardForExisting(integration)} className="text-xs text-blue-600 hover:text-blue-800">
                  View setup guide
                </button>
                <button
                  onClick={() => removeIntegration(integration.id)}
                  className="rounded p-1 text-gray-500 hover:bg-red-50 hover:text-red-500"
                  title="Remove integration"
                >
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  ) : null;

  if (data?.error) {
    return (
      <div className="space-y-6">
        {ModeToggle}
        {CurSetupBanner}
        <div className="rounded-lg bg-white p-6 border" style={{ borderColor: C.hairline }}>
          <h3 className="mb-4 text-lg font-semibold text-gray-900">{cloudDisplayName} Infrastructure Costs</h3>
          <p className="text-sm text-amber-600">{data.error}</p>
        </div>
      </div>
    );
  }

  if (!data || data.clusters.length === 0) {
    const hasBillingSummary = billingSummary != null && billingSummary.total_cost != null && billingSummary.total_cost > 0;
    return (
      <div className="space-y-6">
        {ModeToggle}
        {CurSetupBanner}
        {hasBillingSummary && (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <div className="rounded-lg bg-white p-6 border" style={{ borderColor: C.hairline }}>
              <p className="text-sm font-medium text-gray-500">
                {accountPricingApplied ? "Account-price" : "List-price"} Compute Spend
              </p>
              <p className="text-2xl font-semibold text-gray-900">{formatCurrency(billingSummary!.total_cost ?? 0)}</p>
              <p className="mt-1 text-xs text-gray-500">{billingSummary!.days_in_range ?? 0} days (all-purpose + jobs + DLT)</p>
            </div>
            <div className="rounded-lg bg-white p-6 border" style={{ borderColor: C.hairline }}>
              <p className="text-sm font-medium text-gray-500">Avg Active Clusters / Day</p>
              <p className="text-2xl font-semibold text-gray-900">{formatNumber(billingSummary!.avg_clusters_per_day ?? 0)}</p>
              <p className="mt-1 text-xs text-gray-500">daily average</p>
            </div>
            <div className="rounded-lg bg-white p-6 border" style={{ borderColor: C.hairline }}>
              <p className="text-sm font-medium text-gray-500">Avg Cost / Cluster</p>
              <p className="text-2xl font-semibold text-gray-900">{formatCurrency(billingSummary!.avg_cost_per_cluster ?? 0)}</p>
              <p className="mt-1 text-xs text-gray-500">per cluster per day</p>
            </div>
          </div>
        )}
        <div className="rounded-lg bg-white p-6 border" style={{ borderColor: C.hairline }}>
          <h3 className="mb-4 text-lg font-semibold text-gray-900">{cloudDisplayName} Infrastructure Costs</h3>
          <div className="flex h-32 flex-col items-center justify-center gap-2 text-gray-500">
            <p className="text-base font-medium">No cluster instance data available</p>
            <p className="text-sm text-center max-w-md">Cluster-level VM cost estimates require classic (non-serverless) compute. This workspace may be using serverless jobs, SQL warehouses, or serverless DLT — which don't expose instance types.</p>
          </div>
        </div>
      </div>
    );
  }

  const historicalClusterCount = data.clusters.filter(c => !c.driver_instance_type && !c.worker_instance_type).length;

  const isTableFamilyFilterActive = tableFamily.length > 0 && tableFamily.length < availableTableFamilies.length;

  const resolveWsName = (id: string) => workspaceNameMap?.[id] || clusterWorkspaceNames[id] || id;

  const isTableWorkspaceFilterActive = tableWorkspace.length > 0 && tableWorkspace.length < availableTableWorkspaces.length;

  const tableFilteredClusters = familyFilteredClusters.filter(c => {
    if (isTableFamilyFilterActive) {
      const df = getInstanceFamily(c.driver_instance_type);
      const wf = getInstanceFamily(c.worker_instance_type);
      if (!tableFamily.includes(df) && !tableFamily.includes(wf)) return false;
    }
    if (isTableWorkspaceFilterActive && !tableWorkspace.includes(c.workspace_id || "")) return false;
    return true;
  });

  const searchFilteredClusters = clusterSearch
    ? tableFilteredClusters.filter(c =>
        (c.cluster_name || "").toLowerCase().includes(clusterSearch.toLowerCase()) ||
        (c.cluster_id || "").toLowerCase().includes(clusterSearch.toLowerCase())
      )
    : tableFilteredClusters;

  const sortedClusters = [...searchFilteredClusters].sort((a, b) => {
    const modifier = sortDirection === "asc" ? 1 : -1;
    if (sortField === "cluster_name") {
      return ((a.cluster_name || "").localeCompare(b.cluster_name || "")) * modifier;
    }
    const aVal = (a[sortField] as number) ?? 0;
    const bVal = (b[sortField] as number) ?? 0;
    return (aVal - bVal) * modifier;
  });

  const totalPages = Math.ceil(sortedClusters.length / itemsPerPage);
  const startIndex = (currentPage - 1) * itemsPerPage;
  const endIndex = startIndex + itemsPerPage;
  const paginatedClusters = sortedClusters.slice(startIndex, endIndex);

  const familyChartData = data.instance_families
    .filter((f) => f.instance_family && f.instance_family !== "unknown")
    .slice(0, 10)
    .map((f) => ({
      name: f.instance_family,
      value: f.total_dbu_hours,
    }));

  const timeseriesFamilies: string[] = (timeseriesData as (TimeseriesResponse & { instance_families?: string[] }) | undefined)?.instance_families || [];

  return (
    <div className="animate-fade-in space-y-6">
      {EstimationInfoBox}
      <PageHero
        icon={
          <svg fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 15a4 4 0 004 4h9a5 5 0 10-.1-9.999 5.002 5.002 0 10-9.78 2.096A4.001 4.001 0 003 15z" />
          </svg>
        }
        title="Cloud Costs"
        subtitle="Estimated cloud infrastructure costs and cluster analytics"
        dateRange={{ startDate, endDate }}
        workspaceScope={workspaceIds && workspaceIds.length > 0
          ? workspaceIds.length === 1
            ? (workspaceNameMap?.[workspaceIds[0]] || workspaceIds[0])
            : `${workspaceIds.length} workspaces`
          : "Account-wide"}
      />

      {Object.keys(partialReasons ?? {}).length > 0 && (
        <div
          className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3"
          role="status"
        >
          <p className="text-sm font-semibold text-amber-900">
            Some actual-cost details are temporarily unavailable
          </p>
          <p className="mt-1 text-sm text-amber-800">
            The authoritative total and available sections are shown. Refresh Cloud Costs
            after current SQL work finishes to retry missing detail.
          </p>
        </div>
      )}

      {ModeToggle}
      {CurSetupBanner}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className="relative">
          <span className="absolute top-2 left-2 z-10 inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-wide" style={{ backgroundColor: C.amber, color: C.white }}>est.</span>
          <KPICard
            title="Total Cloud Costs"
            value={formatCurrency(cloudSummary.totalCost)}
            subtitle={startDate && endDate ? (() => {
              const days = Math.round((new Date(endDate).getTime() - new Date(startDate).getTime()) / 86400000) + 1;
              const dailyAvg = cloudSummary.totalCost > 0 ? cloudSummary.totalCost / days : 0;
              return `${formatCurrency(dailyAvg)}/day avg · ${days} days`;
            })() : undefined}
            onActivate={startDate && endDate ? () => setSelectedKPI({ kpi: "infra_cost", label: `Daily ${cloudDisplayName} Cluster Spend` }) : undefined}
            icon={
              <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            }
          />
        </div>
        <KPICard
          title="Total Cluster DBUs"
          value={formatNumber(cloudSummary.totalDBUHours)}
          subtitle={
            billingSummary?.total_cost
              ? `${formatCurrency(billingSummary.total_cost)} ${accountPricingApplied ? "account-price" : "list-price"} DBU spend · ${data.clusters.length} clusters`
              : `across ${data.clusters.length} clusters`
          }
          onActivate={startDate && endDate ? () => setSelectedKPI({ kpi: "infra_dbu_hours", label: "Daily Cluster DBUs" }) : undefined}
          icon={
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          }
        />
        <KPICard
          title="Active Clusters"
          value={formatNumber(cloudSummary.avgActiveClustersPerDay)}
          subtitle="daily average"
          infoText="Average number of distinct clusters with billing activity per day in the selected period. Includes all cluster types (job clusters, interactive clusters)."
          onActivate={startDate && endDate ? () => setSelectedKPI({ kpi: "infra_clusters", label: `Daily Active ${cloudDisplayName} Clusters` }) : undefined}
          icon={
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 00-2-2m-2-4h.01M17 16h.01" />
            </svg>
          }
        />
        <div className="relative">
          <span className="absolute top-2 left-2 z-10 inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-wide" style={{ backgroundColor: C.amber, color: C.white }}>est.</span>
          <KPICard
            title="Cost Per-Cluster"
            value={formatCurrency(cloudSummary.avgCostPerCluster)}
            subtitle={startDate && endDate ? `average over ${Math.round((new Date(endDate).getTime() - new Date(startDate).getTime()) / 86400000) + 1} days` : undefined}
            infoText="Total estimated cloud infrastructure cost divided by the number of distinct clusters with billing activity in the period."
            onActivate={startDate && endDate ? () => setSelectedKPI({ kpi: "avg_cost_per_cluster", label: `Daily ${cloudDisplayName} Cost Per-Cluster` }) : undefined}
            icon={
              <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
              </svg>
            }
          />
        </div>
      </div>

      {selectedKPI && startDate && endDate && (
        <KPITrendModal
          kpi={selectedKPI.kpi}
          kpiLabel={selectedKPI.label}
          isOpen={!!selectedKPI}
          onClose={() => setSelectedKPI(null)}
          startDate={startDate}
          endDate={endDate}
          workspaceIds={workspaceIds}
        />
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {(infraTimeseriesLoading || timeseriesLoading) ? (
          <div className="rounded-lg bg-white p-6 border" style={{ borderColor: C.hairline }}>
            <h3 className="mb-4 text-lg font-semibold text-gray-900">{cloudDisplayName} Cluster Costs{daysCount ? ` over ${daysCount} Days` : ""} <span className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-wide align-middle ml-1" style={{ backgroundColor: C.amber, color: C.white }}>est.</span></h3>
            <div className="h-80 animate-pulse rounded bg-gray-200" />
          </div>
        ) : (infraTimeseriesData?.timeseries && infraTimeseriesData.timeseries.length > 0) ? (
          <div className="rounded-lg bg-white p-6 border" style={{ borderColor: C.hairline }}>
            <h3 className="mb-4 text-lg font-semibold text-gray-900">{cloudDisplayName} Cluster Costs{daysCount ? ` over ${daysCount} Days` : ""} <span className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-wide align-middle ml-1" style={{ backgroundColor: C.amber, color: C.white }}>est.</span></h3>
            <ResponsiveContainer width="100%" height={320}>
              <AreaChart data={infraTimeseriesData.timeseries} margin={{ top: 10, right: 30, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="infraCostGradient" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor={C.s1} stopOpacity={0.3} />
                    <stop offset="95%" stopColor={C.s1} stopOpacity={0.05} />
                  </linearGradient>
                </defs>
                <XAxis dataKey="date" tickFormatter={formatDate} stroke={C.muted} fontSize={12} tickMargin={8} />
                <YAxis tickFormatter={(v) => formatCurrency(v)} stroke={C.muted} fontSize={12} width={80} />
                <Tooltip
                  formatter={(value: number | undefined) => formatCurrency(value ?? 0)}
                  labelFormatter={(label) => format(parseISO(label as string), "MMM d, yyyy")}
                  contentStyle={{ backgroundColor: C.white, border: `1px solid ${C.hairline}`, borderRadius: "8px" }}
                />
                <Area isAnimationActive={false} type="monotone" dataKey="Infrastructure Cost" stroke={C.s1} strokeWidth={2} fill="url(#infraCostGradient)" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        ) : (filteredTimeseriesData && filteredTimeseriesData.length > 0) ? (
          <div className="rounded-lg bg-white p-6 border" style={{ borderColor: C.hairline }}>
            <div className="mb-4 flex items-center justify-between">
              <h3 className="text-lg font-semibold text-gray-900">{cloudDisplayName} Cluster Costs{daysCount ? ` over ${daysCount} Days` : ""} <span className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-wide align-middle ml-1" style={{ backgroundColor: C.amber, color: C.white }}>est.</span></h3>
              {timeseriesFamilies.length > 0 && (
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    onClick={() => setSelectedFamilies(new Set())}
                    className={`rounded-full px-3 py-1 text-xs font-medium ${
                      selectedFamilies.size === 0 ? "text-white" : "bg-blue-50 text-blue-700 hover:bg-blue-100"
                    }`}
                    style={selectedFamilies.size === 0 ? { backgroundColor: C.s2 } : undefined}
                  >
                    All
                  </button>
                  {timeseriesFamilies.filter(f => f !== "unknown").slice(0, 8).map((family, idx) => (
                    <button
                      key={family}
                      onClick={() => {
                        setSelectedFamilies((prev) => {
                          const next = new Set(prev);
                          if (next.has(family)) { next.delete(family); } else { next.add(family); }
                          return next;
                        });
                      }}
                      className={`rounded-full px-3 py-1 text-xs font-medium ${
                        selectedFamilies.has(family) ? "text-white" : "bg-blue-50 text-blue-700 hover:bg-blue-100"
                      }`}
                      style={selectedFamilies.has(family) ? { backgroundColor: getInstanceColor(family, idx) } : undefined}
                    >
                      {family}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <ResponsiveContainer width="100%" height={320}>
              <AreaChart data={filteredTimeseriesData} margin={{ top: 10, right: 30, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="awsCostGradient" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor={C.s1} stopOpacity={0.3} />
                    <stop offset="95%" stopColor={C.s1} stopOpacity={0.05} />
                  </linearGradient>
                </defs>
                <XAxis dataKey="date" tickFormatter={formatDate} stroke={C.muted} fontSize={12} tickMargin={8} />
                <YAxis tickFormatter={(v) => formatCurrency(v)} stroke={C.muted} fontSize={12} width={80} />
                <Tooltip
                  formatter={(value: number | undefined) => formatCurrency(value ?? 0)}
                  labelFormatter={(label) => format(parseISO(label as string), "MMM d, yyyy")}
                  contentStyle={{ backgroundColor: C.white, border: `1px solid ${C.hairline}`, borderRadius: "8px" }}
                />
                <Area isAnimationActive={false} type="monotone" dataKey="AWS Cost" stroke={C.s1} strokeWidth={2} fill="url(#awsCostGradient)" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        ) : null}

        <div className="rounded-lg bg-white p-6 border" style={{ borderColor: C.hairline }}>
          <h3 className="mb-4 text-lg font-semibold text-gray-900">Usage by Instance Family</h3>
          <ResponsiveContainer width="100%" height={320}>
            <BarChart data={familyChartData} layout="vertical" margin={{ left: 10, right: 20 }}>
              <XAxis type="number" tickFormatter={(v) => `${(v / 1000).toFixed(0)}K`} stroke={C.muted} fontSize={12} tickMargin={8} />
              <YAxis type="category" dataKey="name" width={100} fontSize={12} stroke={C.muted} interval={0} />
              <Tooltip
                formatter={(value: number | undefined) => [formatNumber(value ?? 0) + " DBU hours", "Usage"]}
                contentStyle={{ backgroundColor: C.white, border: `1px solid ${C.hairline}`, borderRadius: "8px" }}
              />
              <Bar isAnimationActive={false} dataKey="value" radius={[0, 4, 4, 0]}>
                {familyChartData.map((entry, index) => (
                  <Cell key={entry.name} fill={getInstanceColor(entry.name, index)} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="rounded-lg bg-white p-6 border" style={{ borderColor: C.hairline }}>
        <div className="mb-4 flex items-center justify-between">
          <div>
            <h3 className="flex items-center gap-2 text-lg font-semibold text-gray-900">
              {cloudDisplayName} Cluster Leaderboard
              <span className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-wide" style={{ backgroundColor: C.amber, color: C.white }}>est.</span>
              <span className="inline-flex items-center group relative">
                <svg className="h-3.5 w-3.5 text-gray-500 cursor-help" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <span className="pointer-events-none absolute top-5 left-0 z-[9999] w-72 rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs font-normal text-gray-600 opacity-0 shadow-lg transition-opacity group-hover:opacity-100">
                  Estimated {cloudDisplayName} VM cost for cluster compute nodes, derived from DBU hours × cloud instance pricing ({cloudDisplayName === "GCP" ? "us-central1" : cloudDisplayName === "Azure" ? "East US" : "us-east-1"} on-demand rates). This is separate from Databricks DBU spend shown in the page header.
                </span>
              </span>
            </h3>
            <p className="text-sm text-gray-500">
              {sortedClusters.length} cluster{sortedClusters.length !== 1 ? "s" : ""}{selectedFamilies.size > 0 ? ` · ${[...selectedFamilies].join(", ")} only` : ""}{isTableFamilyFilterActive ? ` · filtered to ${tableFamily.length} families` : ""}{isTableWorkspaceFilterActive ? ` · filtered to ${tableWorkspace.length} workspaces` : ""}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <label className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
              <input
                type="checkbox"
                checked={showHistoricalClusters}
                onChange={(e) => { setShowHistoricalClusters(e.target.checked); setCurrentPage(1); }}
                className="h-4 w-4 rounded border-gray-300 text-orange-600 focus:ring-orange-500"
              />
              <span>Show historical clusters ({historicalClusterCount})</span>
            </label>
            <div className="group relative">
              <svg className="h-4 w-4 cursor-help text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              <div className="invisible absolute right-0 top-6 z-10 w-72 rounded-lg bg-gray-900 p-3 text-xs text-white opacity-0 shadow-xl transition-all group-hover:visible group-hover:opacity-100">
                <p className="font-semibold mb-1.5">Historical Clusters</p>
                <p className="text-gray-200">Historical clusters have no instance type information available. These are typically old or deleted clusters that no longer have detailed configuration data.</p>
              </div>
            </div>
            {availableTableWorkspaces.length > 1 && (
              <div className="relative" ref={workspaceFilterRef}>
                <button
                  onClick={() => { setWorkspaceFilterOpen(o => !o); setFamilyFilterOpen(false); setTableWorkspaceSearch(""); }}
                  className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${isTableWorkspaceFilterActive ? "border-lava text-lava" : "border-gray-300 bg-white text-gray-700 hover:bg-gray-50"}`}
                >
                  {!isTableWorkspaceFilterActive ? "Workspaces" : tableWorkspace.length === 1 ? resolveWsName(tableWorkspace[0]) : `${tableWorkspace.length} Workspaces`}
                  {isTableWorkspaceFilterActive && (
                    <button onClick={(e) => { e.stopPropagation(); setTableWorkspace([...availableTableWorkspaces]); setCurrentPage(1); }} className="ml-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full bg-orange-100 text-orange-600 hover:bg-orange-200">
                      <svg className="h-2 w-2" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M6 18L18 6M6 6l12 12" /></svg>
                    </button>
                  )}
                  <svg className={`h-3 w-3 transition-transform ${workspaceFilterOpen ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
                {workspaceFilterOpen && (
                  <div className="absolute right-0 top-full z-[9999] mt-1 w-72 rounded-lg border border-gray-200 bg-white shadow-lg">
                    <div className="p-2">
                      <input
                        type="text"
                        value={tableWorkspaceSearch}
                        onChange={(e) => setTableWorkspaceSearch(e.target.value)}
                        placeholder="Search workspaces..."
                        className="w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:border-lava focus:outline-none focus:ring-1 focus:ring-lava"
                        autoFocus
                      />
                    </div>
                    <div>
                      <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
                        <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Workspaces</span>
                        <div className="flex items-center gap-2 text-xs">
                          <button onClick={(e) => { e.stopPropagation(); setTableWorkspace([...availableTableWorkspaces]); setCurrentPage(1); }} className="text-gray-500 hover:text-gray-800">All</button>
                          <span className="text-gray-300">·</span>
                          <button onClick={(e) => { e.stopPropagation(); setTableWorkspace([]); setCurrentPage(1); }} className="text-gray-500 hover:text-gray-800">Clear</button>
                        </div>
                      </div>
                      {(() => {
                        const filtered = availableTableWorkspaces.filter(w => {
                          if (!tableWorkspaceSearch) return true;
                          const q = tableWorkspaceSearch.toLowerCase();
                          return resolveWsName(w).toLowerCase().includes(q) || w.toLowerCase().includes(q);
                        });
                        if (filtered.length === 0) {
                          return <div className="px-3 py-2 text-sm text-gray-500">No matching workspaces</div>;
                        }
                        return (
                          <VirtualizedList
                            items={filtered}
                            itemHeight={36}
                            maxHeight={256}
                            getKey={(w) => w}
                            renderItem={(w) => {
                              const label = resolveWsName(w);
                              return (
                                <button
                                  onClick={() => { setTableWorkspace((prev) => prev.includes(w) ? prev.filter(x => x !== w) : [...prev, w]); setCurrentPage(1); }}
                                  className="flex w-full items-center gap-2 px-3 py-2.5 text-xs hover:bg-gray-50"
                                >
                                  <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${tableWorkspace.includes(w) ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
                                    {tableWorkspace.includes(w) && <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>}
                                  </div>
                                  <span className="truncate text-gray-700">{label}</span>
                                </button>
                              );
                            }}
                          />
                        );
                      })()}
                    </div>
                  </div>
                )}
              </div>
            )}
            {availableTableFamilies.length > 0 && (
              <div className="relative" ref={familyFilterRef}>
                <button
                  onClick={() => { setFamilyFilterOpen(o => !o); setWorkspaceFilterOpen(false); }}
                  className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${isTableFamilyFilterActive ? "border-lava text-lava" : "border-gray-300 bg-white text-gray-700 hover:bg-gray-50"}`}
                >
                  {!isTableFamilyFilterActive ? "Families" : tableFamily.length === 1 ? tableFamily[0] : `${tableFamily.length} Families`}
                  {isTableFamilyFilterActive && (
                    <button onClick={(e) => { e.stopPropagation(); setTableFamily([...availableTableFamilies]); setCurrentPage(1); }} className="ml-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full bg-orange-100 text-orange-600 hover:bg-orange-200">
                      <svg className="h-2 w-2" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M6 18L18 6M6 6l12 12" /></svg>
                    </button>
                  )}
                  <svg className={`h-3 w-3 transition-transform ${familyFilterOpen ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
                {familyFilterOpen && (
                  <div className="absolute right-0 top-full z-[9999] mt-1 min-w-[180px] max-h-64 overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-lg">
                    <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
                      <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Families</span>
                      <div className="flex items-center gap-2 text-xs">
                        <button onClick={(e) => { e.stopPropagation(); setTableFamily([...availableTableFamilies]); setCurrentPage(1); }} className="text-gray-500 hover:text-gray-800">All</button>
                        <span className="text-gray-300">·</span>
                        <button onClick={(e) => { e.stopPropagation(); setTableFamily([]); setCurrentPage(1); }} className="text-gray-500 hover:text-gray-800">Clear</button>
                      </div>
                    </div>
                    {availableTableFamilies.map(f => (
                      <button
                        key={f}
                        onClick={() => { setTableFamily((prev) => prev.includes(f) ? prev.filter(x => x !== f) : [...prev, f]); setCurrentPage(1); }}
                        className="flex w-full items-center gap-2 px-3 py-2.5 text-xs hover:bg-gray-50"
                      >
                        <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${tableFamily.includes(f) ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
                          {tableFamily.includes(f) && <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>}
                        </div>
                        <span className="truncate text-gray-700">{f}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
            <div className="relative">
              <svg className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-500" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
              </svg>
              <input
                type="text"
                placeholder="Search clusters..."
                value={clusterSearch}
                onChange={(e) => { setClusterSearch(e.target.value); setCurrentPage(1); }}
                className="w-44 rounded-full border border-gray-200 bg-white py-1.5 pl-9 pr-4 text-xs placeholder:text-gray-400 focus:border-lava focus:outline-none focus:ring-1 focus:ring-lava"
              />
            </div>
          </div>
        </div>

        <div className="overflow-x-hidden">
          <table className="w-full table-fixed divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="cursor-pointer px-3 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500 hover:text-gray-700" onClick={() => handleSort("cluster_name")}>
                  Cluster {sortIcon("cluster_name")}
                </th>
                <th className="w-44 px-3 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Instance Types</th>
                <th className="w-28 px-3 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">
                  <div className="flex items-center justify-end gap-1">
                    <span className="cursor-pointer hover:text-gray-700" onClick={() => handleSort("estimated_aws_cost")}>
                      Cost {sortIcon("estimated_aws_cost")}
                    </span>
                    <div className="group relative">
                      <svg className="h-3.5 w-3.5 cursor-help text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                      </svg>
                      <div className="invisible absolute right-0 top-6 z-10 w-72 rounded-lg bg-gray-900 p-3 text-xs text-white opacity-0 shadow-xl transition-all group-hover:visible group-hover:opacity-100">
                        <p className="font-semibold mb-1.5">Cost Estimate Details</p>
                        <ul className="space-y-1 text-gray-200">
                          <li>• {isAzure ? "Azure VM" : "EC2 instance"} costs only</li>
                          <li>• Based on on-demand pricing</li>
                          <li>• Assumes avg 2-4 workers per cluster</li>
                          <li>• Excludes storage, network, and platform fees</li>
                        </ul>
                      </div>
                    </div>
                  </div>
                </th>
                <th className="w-28 cursor-pointer px-3 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500 hover:text-gray-700" onClick={() => handleSort("total_dbu_hours")}>
                  DBU Hours {sortIcon("total_dbu_hours")}
                </th>
                <th className="w-16 cursor-pointer px-3 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500 hover:text-gray-700" onClick={() => handleSort("days_active")}>
                  Days {sortIcon("days_active")}
                </th>
                <th className="w-14 px-3 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">%</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 bg-white">
              {paginatedClusters.length === 0 && clusterSearch && (
                <tr>
                  <td colSpan={6} className="px-3 py-8 text-center text-sm text-gray-500">
                    No clusters match your search.
                  </td>
                </tr>
              )}
              {paginatedClusters.map((cluster, idx) => {
                const url = getClusterUrl(_host, cluster.cluster_id, cluster.workspace_id);
                return (
                  <tr key={`${cluster.cluster_id}-${idx}`} className="hover:bg-gray-50">
                    <td className="px-3 py-3">
                      {url ? (
                        <div className="flex flex-col gap-1">
                          <a href={url} target="_blank" rel="noopener noreferrer" className="group flex max-w-xs items-center gap-1 truncate text-sm font-medium text-blue-600 hover:text-blue-800">
                            <span className="truncate">{cluster.cluster_name || cluster.cluster_id}</span>
                            <svg className="h-3 w-3 flex-shrink-0 opacity-0 transition-opacity group-hover:opacity-100" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                            </svg>
                          </a>
                          <div className="flex items-center gap-2">
                            {cluster.state && <StatusIndicator status={cluster.state} type="cluster" />}
                            {cluster.cluster_source && (
                              <span className="inline-flex rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-600">{cluster.cluster_source}</span>
                            )}
                            {cluster.cluster_name && cluster.cluster_name !== cluster.cluster_id && (
                              <span className="text-xs text-gray-500">{cluster.cluster_id}</span>
                            )}
                          </div>
                        </div>
                      ) : (
                        <div className="flex flex-col gap-1">
                          <div className="max-w-xs truncate text-sm font-medium text-gray-900">{cluster.cluster_name || cluster.cluster_id}</div>
                          <div className="flex items-center gap-2">
                            {cluster.state && <StatusIndicator status={cluster.state} type="cluster" />}
                            {cluster.cluster_source && (
                              <span className="inline-flex rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-600">{cluster.cluster_source}</span>
                            )}
                            {cluster.cluster_name && cluster.cluster_name !== cluster.cluster_id && (
                              <span className="text-xs text-gray-500">{cluster.cluster_id}</span>
                            )}
                          </div>
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-3">
                      <div className="flex flex-col gap-1">
                        {cluster.driver_instance_type && (
                          <div className="group relative inline-flex items-center gap-1">
                            <span className="inline-flex max-w-full truncate rounded bg-blue-50 px-2 py-0.5 text-xs font-mono text-blue-700" title={`D: ${cluster.driver_instance_type}`}>D: {cluster.driver_instance_type}</span>
                            <a href={getInstancePricingUrl(cluster.driver_instance_type, isAzure)} target="_blank" rel="noopener noreferrer" className="text-blue-500 hover:text-blue-700" title={`View ${isAzure ? "Azure" : "AWS"} pricing for this instance type`}>
                              <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                              </svg>
                            </a>
                          </div>
                        )}
                        {cluster.worker_instance_type && (
                          <div className="group relative inline-flex items-center gap-1">
                            <span className="inline-flex max-w-full truncate rounded bg-green-50 px-2 py-0.5 text-xs font-mono text-green-700" title={`W: ${cluster.worker_instance_type}`}>W: {cluster.worker_instance_type}</span>
                            <a href={getInstancePricingUrl(cluster.worker_instance_type, isAzure)} target="_blank" rel="noopener noreferrer" className="text-green-500 hover:text-green-700" title={`View ${isAzure ? "Azure" : "AWS"} pricing for this instance type`}>
                              <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                              </svg>
                            </a>
                          </div>
                        )}
                        {!cluster.driver_instance_type && !cluster.worker_instance_type && (
                          <div className="group relative inline-flex items-center gap-1">
                            <span className="inline-flex rounded bg-gray-100 px-2 py-0.5 text-xs text-gray-500">Historical cluster</span>
                            <div className="relative">
                              <svg className="h-3 w-3 cursor-help text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                              </svg>
                              <div className="invisible absolute left-0 top-6 z-10 w-64 rounded-lg bg-gray-900 p-3 text-xs text-white opacity-0 shadow-xl transition-all group-hover:visible group-hover:opacity-100">
                                <p className="font-semibold mb-1.5">Instance type unavailable</p>
                                <p className="text-gray-200">This cluster no longer exists in the workspace. Instance type information is only available for currently configured clusters.</p>
                              </div>
                            </div>
                          </div>
                        )}
                      </div>
                    </td>
                    <td className="whitespace-nowrap px-3 py-3 text-right text-sm font-medium text-gray-900">{formatCurrency(cluster.estimated_aws_cost)}</td>
                    <td className="whitespace-nowrap px-3 py-3 text-right text-sm text-gray-600">{formatNumber(cluster.total_dbu_hours)}</td>
                    <td className="whitespace-nowrap px-3 py-3 text-right text-sm text-gray-600">{cluster.days_active}</td>
                    <td className="whitespace-nowrap px-3 py-3 text-right text-sm text-gray-500">{(cluster.percentage ?? 0).toFixed(1)}%</td>
                  </tr>
                );
              })}
            </tbody>
            <tfoot className="bg-gray-50">
              <tr>
                <td colSpan={2} className="px-3 py-3 text-sm font-medium text-gray-700">Total ({sortedClusters.length} clusters)</td>
                <td className="whitespace-nowrap px-3 py-3 text-right text-sm font-bold text-gray-900">
                  {formatCurrency(data.total_estimated_cost)}
                </td>
                <td className="whitespace-nowrap px-3 py-3 text-right text-sm font-medium text-gray-700">{formatNumber(data.total_dbu_hours)}</td>
                <td colSpan={2}></td>
              </tr>
            </tfoot>
          </table>
        </div>
        {totalPages > 1 && (
          <div className="mt-4 flex items-center justify-between border-t border-gray-200 pt-4">
            <p className="text-sm text-gray-700">
              Showing <span className="font-medium">{startIndex + 1}</span> to{" "}
              <span className="font-medium">{Math.min(endIndex, sortedClusters.length)}</span> of{" "}
              <span className="font-medium">{sortedClusters.length}</span> clusters
            </p>
            <div className="flex gap-2">
              <button onClick={() => setCurrentPage(Math.max(1, currentPage - 1))} disabled={currentPage === 1} className="rounded border border-gray-300 px-3 py-1 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50">
                Previous
              </button>
              {Array.from({ length: totalPages }, (_, i) => i + 1)
                .filter((page) => page === 1 || page === totalPages || (page >= currentPage - 1 && page <= currentPage + 1))
                .map((page, idx, arr) => {
                  const prevPage = arr[idx - 1];
                  const showEllipsis = prevPage && page - prevPage > 1;
                  return (
                    <>
                      {showEllipsis && <span key={`ellipsis-${page}`} className="px-2 py-1 text-gray-500">...</span>}
                      <button
                        key={page}
                        onClick={() => setCurrentPage(page)}
                        className={`rounded px-3 py-1 text-sm font-medium ${currentPage === page ? "bg-orange-600 text-white" : "border border-gray-300 text-gray-700 hover:bg-gray-50"}`}
                      >
                        {page}
                      </button>
                    </>
                  );
                })}
              <button onClick={() => setCurrentPage(Math.min(totalPages, currentPage + 1))} disabled={currentPage === totalPages} className="rounded border border-gray-300 px-3 py-1 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50">
                Next
              </button>
            </div>
          </div>
        )}
      </div>

      <div className="fixed" style={{ top: -9999, left: -9999, opacity: 0.01, pointerEvents: "none" }} aria-hidden="true">
        <img src={awsLogo} alt="" style={{ width: 1, height: 1 }} />
        <img src={azureLogo} alt="" style={{ width: 1, height: 1 }} />
        <img src={gcpLogo} alt="" style={{ width: 1, height: 1 }} />
      </div>

      <CloudIntegrationWizard
        show={showIntegrationWizard}
        onClose={() => { setShowIntegrationWizard(false); setViewingIntegration(null); }}
        wizardCloud={wizardCloud}
        setWizardCloud={setWizardCloud}
        wizardExpandedStep={wizardExpandedStep}
        setWizardExpandedStep={setWizardExpandedStep}
        viewingIntegration={viewingIntegration}
        cloudIntegrations={cloudIntegrations}
        addIntegration={addIntegration}
        isAzure={isAzure}
        isGCP={isGCP}
      />
    </div>
  );
}
