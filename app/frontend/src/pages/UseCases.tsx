import { useState, useEffect, useCallback, useRef } from "react";
import { createPortal } from "react-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Package, TrendingUp, Users, X, Tag, Trash2, Calendar, PlayCircle, Server, Database, Workflow, Bot, BarChart3 } from "lucide-react";
import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip, BarChart, Bar, XAxis, YAxis, ReferenceLine, Legend } from "recharts";
import { formatCurrency, formatNumber } from "@/utils/formatters";
import { C } from "@/theme";
import { PageHero } from "@/components/brand";

type UseCaseStage = 'Live' | 'Development' | 'Planned' | 'Inactive';

interface UseCase {
  use_case_id: string;
  name: string;
  description: string;
  owner: string;
  status: string;
  created_at: string;
  updated_at: string;
  tags: Record<string, string>;
  stage: UseCaseStage;
  start_date: string | null;
  end_date: string | null;
  live_date: string | null;
}

interface UseCaseSummary extends UseCase {
  total_spend: number;
  total_dbus: number;
  object_count: number;
  percentage: number;
}

interface UseCasesListResponse {
  use_cases: UseCase[];
  count: number;
}

interface UseCasesSummaryResponse {
  use_cases: UseCaseSummary[];
  total_spend: number;
  count: number;
}

interface MonthlyConsumptionResponse {
  months: { month: string; total_spend: number; total_dbus: number }[];
  live_events: { month: string; use_case_id: string; use_case_name: string; live_date: string }[];
  date_range: { start: string; end: string };
}

interface AvailableObject {
  object_id: string;
  object_name: string;
  workspace_id: string | null;
  object_type: string;
}

interface AvailableObjectsResponse {
  objects: AvailableObject[];
  count: number;
  object_type: string;
}

interface AssignedObject {
  mapping_id: string;
  object_type: string;
  object_id: string;
  object_name: string | null;
  workspace_id: string | null;
  assigned_at: string | null;
  custom_start_date: string | null;
  custom_end_date: string | null;
}

interface UseCaseDetailResponse {
  use_case_id: string;
  name: string;
  objects: AssignedObject[];
  object_count: number;
}

interface CreateUseCaseModalProps {
  isOpen: boolean;
  onClose: () => void;
  existingTags: Record<string, string[]>;
}

interface EditUseCaseModalProps {
  useCase: UseCaseSummary | null;
  isOpen: boolean;
  onClose: () => void;
  existingTags: Record<string, string[]>;
}

const STAGE_COLORS: Record<UseCaseStage, { bg: string; text: string; border: string }> = {
  Live: { bg: 'bg-green-100', text: 'text-green-800', border: 'border-green-200' },
  Development: { bg: 'bg-blue-100', text: 'text-blue-800', border: 'border-blue-200' },
  Planned: { bg: 'bg-amber-50', text: 'text-amber-700', border: 'border-amber-200' },
  Inactive: { bg: 'bg-gray-100', text: 'text-gray-600', border: 'border-gray-200' },
};

function StageBadge({ stage }: { stage: UseCaseStage }) {
  const colors = STAGE_COLORS[stage] || STAGE_COLORS.Development;
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium border ${colors.bg} ${colors.text} ${colors.border}`}>
      {stage === 'Live' && <PlayCircle className="h-3 w-3" />}
      {stage}
    </span>
  );
}

const COLORS = [C.lava, C.lavaHover, C.busy, C.coral, C.s1, C.coralBrd];

interface UserInfo {
  email: string;
  name: string;
}

function CreateUseCaseModal({ isOpen, onClose, existingTags }: CreateUseCaseModalProps) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [owner, setOwner] = useState("");
  const [stage, setStage] = useState<UseCaseStage>("Development");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [startToday, setStartToday] = useState(false);
  const [tags, setTags] = useState<Record<string, string>>({});
  const [newTagKey, setNewTagKey] = useState("");
  const [newTagValue, setNewTagValue] = useState("");
  const [showKeyDropdown, setShowKeyDropdown] = useState(false);
  const [showValueDropdown, setShowValueDropdown] = useState(false);
  const keyDropdownRef = useRef<HTMLDivElement>(null);
  const valueDropdownRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedObjectType, setSelectedObjectType] = useState<string>("cluster");
  const [showObjectPicker, setShowObjectPicker] = useState(false);
  const [pendingObjects, setPendingObjects] = useState<AvailableObject[]>([]);
  const queryClient = useQueryClient();

  // Escape key handler
  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    if (e.key === "Escape") onClose();
  }, [onClose]);

  useEffect(() => {
    if (isOpen) {
      document.addEventListener("keydown", handleKeyDown);
      document.body.style.overflow = "hidden";
    }
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = "";
    };
  }, [isOpen, handleKeyDown]);

  // Fetch current user to auto-populate owner
  const { data: currentUser } = useQuery<UserInfo>({
    queryKey: ["user"],
    queryFn: async () => {
      const response = await fetch("/api/user/me");
      if (!response.ok) throw new Error("Failed to fetch user");
      return response.json();
    },
  });

  // Auto-populate owner with current user's email when modal opens
  if (isOpen && currentUser?.email && !owner) {
    setOwner(currentUser.email);
  }

  // Fetch available objects for the object picker
  const { data: availableObjects, isLoading: objectsLoading } = useQuery<AvailableObjectsResponse>({
    queryKey: ["available-objects", selectedObjectType],
    queryFn: async () => {
      const response = await fetch(`/api/use-cases/available-objects?object_type=${selectedObjectType}`);
      if (!response.ok) throw new Error("Failed to fetch available objects");
      return response.json();
    },
    enabled: isOpen && showObjectPicker,
  });

  const addPendingObject = (obj: AvailableObject) => {
    if (!pendingObjects.some(o => o.object_id === obj.object_id && o.object_type === obj.object_type)) {
      setPendingObjects(prev => [...prev, obj]);
    }
  };

  const removePendingObject = (objectId: string, objectType: string) => {
    setPendingObjects(prev => prev.filter(o => !(o.object_id === objectId && o.object_type === objectType)));
  };

  const createMutation = useMutation({
    mutationFn: async (data: { name: string; description: string; owner: string; stage: UseCaseStage; start_date: string | null; end_date: string | null; tags: Record<string, string> | null }) => {
      setError(null);
      const response = await fetch("/api/use-cases/use-cases", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.detail || "Failed to create use case");
      }
      const result = await response.json();

      // Assign pending objects to the newly created use case
      if (pendingObjects.length > 0 && result.use_case_id) {
        await Promise.all(
          pendingObjects.map(obj =>
            fetch(`/api/use-cases/use-cases/${result.use_case_id}/objects`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                object_type: obj.object_type,
                object_id: obj.object_id,
                object_name: obj.object_name,
                workspace_id: obj.workspace_id,
              }),
            })
          )
        );
      }

      return result;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["use-cases"] });
      queryClient.invalidateQueries({ queryKey: ["use-cases-summary"] });
      queryClient.invalidateQueries({ queryKey: ["monthly-consumption"] });
      setName("");
      setDescription("");
      setOwner("");
      setStage("Development");
      setStartDate("");
      setEndDate("");
      setStartToday(false);
      setTags({});
      setNewTagKey("");
      setNewTagValue("");
      setPendingObjects([]);
      setShowObjectPicker(false);
      setError(null);
      onClose();
    },
    onError: (err: Error) => {
      setError(err.message);
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (name.trim()) {
      createMutation.mutate({
        name,
        description,
        owner,
        stage,
        start_date: startDate || null,
        end_date: endDate || null,
        tags: Object.keys(tags).length > 0 ? tags : null,
      });
    }
  };

  const addTag = () => {
    if (newTagKey.trim() && newTagValue.trim()) {
      setTags(prev => ({ ...prev, [newTagKey.trim()]: newTagValue.trim() }));
      setNewTagKey("");
      setNewTagValue("");
    }
  };

  const removeTag = (key: string) => {
    setTags(prev => {
      const newTags = { ...prev };
      delete newTags[key];
      return newTags;
    });
  };

  const tagKeys = Object.keys(existingTags);
  const tagValues = newTagKey ? existingTags[newTagKey] || [] : [];

  if (!isOpen) return null;

  return createPortal(
    <div
      className="animate-backdrop fixed inset-0 z-50 overflow-y-auto bg-black/50"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="flex min-h-full items-center justify-center p-4" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="animate-dialog w-full max-w-4xl rounded-lg bg-white p-6 shadow-xl" onClick={(e) => e.stopPropagation()}>
        <h2 className="mb-4 text-xl font-semibold text-gray-900">Create New Use Case</h2>

        {error && (
          <div className="mb-4 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-600">
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700">Name *</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-400 focus:outline-none"
              placeholder="e.g., Customer Analytics"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700">Description</label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-400 focus:outline-none"
              placeholder="Describe the purpose and scope of this use case"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700">Owner</label>
            <input
              type="text"
              value={owner}
              onChange={(e) => setOwner(e.target.value)}
              className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-400 focus:outline-none"
              placeholder="e.g., data-team@example.com"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700">Stage</label>
            <select
              value={stage}
              onChange={(e) => setStage(e.target.value as UseCaseStage)}
              className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-400 focus:outline-none"
            >
              <option value="Planned">Planned</option>
              <option value="Development">Development</option>
              <option value="Live">Live</option>
              <option value="Inactive">Inactive</option>
            </select>
          </div>

          <div className="flex items-center gap-2">
            <input
              type="checkbox"
              id="start-today-create"
              checked={startToday}
              onChange={(e) => {
                setStartToday(e.target.checked);
                if (e.target.checked) {
                  setStartDate(new Date().toISOString().split('T')[0]);
                }
              }}
              className="h-4 w-4 rounded border-gray-300 text-orange-600 focus:ring-orange-500"
            />
            <label htmlFor="start-today-create" className="text-sm font-medium text-gray-700">
              Start use case today
            </label>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700">
                <Calendar className="inline h-4 w-4 mr-1" />
                Start Date
              </label>
              <input
                type="date"
                value={startDate}
                onChange={(e) => {
                  setStartDate(e.target.value);
                  setStartToday(false);
                }}
                disabled={startToday}
                className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-400 focus:outline-none disabled:bg-gray-100 disabled:text-gray-500"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700">
                <Calendar className="inline h-4 w-4 mr-1" />
                End Date
              </label>
              <input
                type="date"
                value={endDate}
                onChange={(e) => setEndDate(e.target.value)}
                className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-400 focus:outline-none"
                placeholder="Leave empty for ongoing"
              />
              <p className="mt-1 text-xs text-gray-500">Leave empty for ongoing use cases</p>
            </div>
          </div>

          {/* Tags Section */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              <Tag className="inline h-4 w-4 mr-1" />
              Tags
            </label>

            {/* Existing Tags */}
            {Object.keys(tags).length > 0 && (
              <div className="flex flex-wrap gap-2 mb-3">
                {Object.entries(tags).map(([key, value]) => (
                  <span
                    key={key}
                    className="inline-flex items-center gap-1 rounded-full bg-orange-100 px-3 py-1 text-sm text-orange-800"
                  >
                    <span className="font-medium">{key}:</span> {value}
                    <button
                      type="button"
                      onClick={() => removeTag(key)}
                      className="ml-1 text-orange-600 hover:text-orange-800"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </span>
                ))}
              </div>
            )}

            {/* Add New Tag */}
            <div className="flex gap-2">
              <div className="flex-1 relative" ref={keyDropdownRef}>
                <input
                  type="text"
                  value={newTagKey}
                  onChange={(e) => {
                    setNewTagKey(e.target.value);
                    setShowKeyDropdown(true);
                    if (e.target.value !== newTagKey) {
                      setNewTagValue("");
                    }
                  }}
                  onFocus={() => setShowKeyDropdown(true)}
                  onBlur={() => setTimeout(() => setShowKeyDropdown(false), 150)}
                  placeholder="Tag key (e.g., team)"
                  className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-400 focus:outline-none focus:ring-1 focus:ring-blue-400"
                />
                {showKeyDropdown && tagKeys.filter(k => !newTagKey || k.toLowerCase().includes(newTagKey.toLowerCase())).length > 0 && (
                  <div className="absolute z-50 mt-1 w-full max-h-40 overflow-y-auto rounded-md border border-gray-200 bg-white shadow-lg">
                    {tagKeys.filter(k => !newTagKey || k.toLowerCase().includes(newTagKey.toLowerCase())).map(key => (
                      <button
                        key={key}
                        type="button"
                        className="w-full px-3 py-2 text-left text-sm hover:bg-blue-50 text-gray-700"
                        onMouseDown={(e) => {
                          e.preventDefault();
                          setNewTagKey(key);
                          setNewTagValue("");
                          setShowKeyDropdown(false);
                        }}
                      >
                        {key}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <div className="flex-1 relative" ref={valueDropdownRef}>
                <input
                  type="text"
                  value={newTagValue}
                  onChange={(e) => {
                    setNewTagValue(e.target.value);
                    setShowValueDropdown(true);
                  }}
                  onFocus={() => setShowValueDropdown(true)}
                  onBlur={() => setTimeout(() => setShowValueDropdown(false), 150)}
                  placeholder="Tag value"
                  className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-400 focus:outline-none focus:ring-1 focus:ring-blue-400"
                />
                {showValueDropdown && tagValues.filter(v => !newTagValue || v.toLowerCase().includes(newTagValue.toLowerCase())).length > 0 && (
                  <div className="absolute z-50 mt-1 w-full max-h-40 overflow-y-auto rounded-md border border-gray-200 bg-white shadow-lg">
                    {tagValues.filter(v => !newTagValue || v.toLowerCase().includes(newTagValue.toLowerCase())).map(val => (
                      <button
                        key={val}
                        type="button"
                        className="w-full px-3 py-2 text-left text-sm hover:bg-blue-50 text-gray-700"
                        onMouseDown={(e) => {
                          e.preventDefault();
                          setNewTagValue(val);
                          setShowValueDropdown(false);
                        }}
                      >
                        {val}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <button
                type="button"
                onClick={addTag}
                disabled={!newTagKey.trim() || !newTagValue.trim()}
                className="rounded-md bg-gray-100 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-200 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Add
              </button>
            </div>
            <p className="mt-2 text-xs text-gray-500">
              Select from existing tags or add custom ones
            </p>
          </div>

          {/* Objects Section */}
          <div className="rounded-lg border border-gray-200 p-4 space-y-3 bg-gray-50">
            <div className="flex items-center justify-between">
              <label className="block text-sm font-medium text-gray-700">
                <Package className="inline h-4 w-4 mr-1" />
                Objects ({pendingObjects.length})
              </label>
              <button
                type="button"
                onClick={() => setShowObjectPicker(!showObjectPicker)}
                className="inline-flex items-center gap-1 rounded-md bg-white border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50"
              >
                <Plus className="h-4 w-4" />
                Add Object
              </button>
            </div>

            {/* Object Picker */}
            {showObjectPicker && (
              <div className="rounded-lg border border-gray-300 bg-white p-3 space-y-3">
                <div className="flex gap-2">
                  <select
                    value={selectedObjectType}
                    onChange={(e) => setSelectedObjectType(e.target.value)}
                    className="rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-400 focus:outline-none"
                  >
                    {OBJECT_TYPE_OPTIONS.map(opt => (
                      <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                  </select>
                  <button
                    type="button"
                    onClick={() => setShowObjectPicker(false)}
                    className="ml-auto text-gray-500 hover:text-gray-600"
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>

                {objectsLoading ? (
                  <div className="flex justify-center py-4">
                    <div className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-orange-600" />
                  </div>
                ) : (
                  <div className="max-h-48 overflow-y-auto space-y-1">
                    {availableObjects?.objects?.length === 0 ? (
                      <p className="text-sm text-gray-500 py-2">No {selectedObjectType}s found</p>
                    ) : (
                      availableObjects?.objects?.map(obj => {
                        const isAdded = pendingObjects.some(
                          o => o.object_id === obj.object_id && o.object_type === obj.object_type
                        );
                        return (
                          <div
                            key={`${obj.object_type}-${obj.object_id}`}
                            className={`flex items-center justify-between p-2 rounded-md ${isAdded ? 'bg-green-50' : 'hover:bg-gray-50'}`}
                          >
                            <div className="flex-1 min-w-0">
                              <p className="text-sm font-medium text-gray-900 truncate">{obj.object_name}</p>
                              <p className="text-xs text-gray-500 truncate">{obj.object_id}</p>
                            </div>
                            {isAdded ? (
                              <span className="text-xs text-green-600 font-medium">Added</span>
                            ) : (
                              <button
                                type="button"
                                onClick={() => addPendingObject(obj)}
                                className="rounded-md bg-orange-100 px-2 py-1 text-xs font-medium text-orange-700 hover:bg-orange-200"
                              >
                                Add
                              </button>
                            )}
                          </div>
                        );
                      })
                    )}
                  </div>
                )}
              </div>
            )}

            {/* List of pending objects */}
            {pendingObjects.length > 0 ? (
              <div className="space-y-2">
                {pendingObjects.map(obj => {
                  const typeOption = OBJECT_TYPE_OPTIONS.find(o => o.value === obj.object_type);
                  const Icon = typeOption?.icon || Package;
                  return (
                    <div
                      key={`${obj.object_type}-${obj.object_id}`}
                      className="flex items-center justify-between p-2 rounded-md bg-white border border-gray-200"
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        <Icon className="h-4 w-4 text-gray-500 flex-shrink-0" />
                        <div className="min-w-0">
                          <p className="text-sm font-medium text-gray-900 truncate">
                            {obj.object_name || obj.object_id}
                          </p>
                          <p className="text-xs text-gray-500">{obj.object_type}</p>
                        </div>
                      </div>
                      <button
                        type="button"
                        onClick={() => removePendingObject(obj.object_id, obj.object_type)}
                        className="text-gray-500 hover:text-red-600 p-1"
                        title="Remove"
                      >
                        <X className="h-4 w-4" />
                      </button>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="text-sm text-gray-500 text-center py-2">
                No objects selected. Click "Add Object" to associate resources.
              </p>
            )}
          </div>

          <div className="flex items-center justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={createMutation.isPending}
              className="btn-brand rounded-lg px-4 py-2 text-sm font-medium text-white transition-colors disabled:opacity-50"
            >
              {createMutation.isPending ? "Creating..." : "Create Use Case"}
            </button>
          </div>
        </form>
      </div>
      </div>
    </div>,
    document.body
  );
}

const OBJECT_TYPE_OPTIONS = [
  { value: 'cluster', label: 'Clusters', icon: Server },
  { value: 'warehouse', label: 'Warehouses', icon: Database },
  { value: 'pipeline', label: 'Pipelines', icon: Workflow },
  { value: 'job', label: 'Jobs', icon: BarChart3 },
  { value: 'endpoint', label: 'Endpoints', icon: Bot },
];

function EditUseCaseModal({ useCase, isOpen, onClose, existingTags }: EditUseCaseModalProps) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [owner, setOwner] = useState("");
  const [tags, setTags] = useState<Record<string, string>>({});
  const [newTagKey, setNewTagKey] = useState("");
  const [newTagValue, setNewTagValue] = useState("");
  const [showKeyDropdown, setShowKeyDropdown] = useState(false);
  const [showValueDropdown, setShowValueDropdown] = useState(false);
  const keyDropdownRef = useRef<HTMLDivElement>(null);
  const valueDropdownRef = useRef<HTMLDivElement>(null);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [stage, setStage] = useState<UseCaseStage>("Development");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [liveDate, setLiveDate] = useState("");
  const [startToday, setStartToday] = useState(false);
  const [selectedObjectType, setSelectedObjectType] = useState<string>("cluster");
  const [showObjectPicker, setShowObjectPicker] = useState(false);
  const [addingObjectId, setAddingObjectId] = useState<string | null>(null);
  const [updateError, setUpdateError] = useState<string | null>(null);
  const queryClient = useQueryClient();

  // Fetch use case details including assigned objects
  const { data: useCaseDetails, refetch: refetchDetails } = useQuery<UseCaseDetailResponse>({
    queryKey: ["use-case-details", useCase?.use_case_id],
    queryFn: async () => {
      const response = await fetch(`/api/use-cases/use-cases/${useCase?.use_case_id}`);
      if (!response.ok) throw new Error("Failed to fetch use case details");
      return response.json();
    },
    enabled: isOpen && !!useCase?.use_case_id,
  });

  // Fetch available objects based on selected type
  const { data: availableObjects, isLoading: objectsLoading } = useQuery<AvailableObjectsResponse>({
    queryKey: ["available-objects", selectedObjectType],
    queryFn: async () => {
      const response = await fetch(`/api/use-cases/available-objects?object_type=${selectedObjectType}`);
      if (!response.ok) throw new Error("Failed to fetch available objects");
      return response.json();
    },
    enabled: isOpen && showObjectPicker,
  });

  // Assign object mutation
  const assignObjectMutation = useMutation({
    mutationFn: async (obj: AvailableObject) => {
      setAddingObjectId(obj.object_id);
      const response = await fetch(`/api/use-cases/use-cases/${useCase?.use_case_id}/objects`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          object_type: obj.object_type,
          object_id: obj.object_id,
          object_name: obj.object_name,
          workspace_id: obj.workspace_id,
        }),
      });
      if (!response.ok) throw new Error("Failed to assign object");
      return response.json();
    },
    onSuccess: () => {
      setAddingObjectId(null);
      refetchDetails();
      queryClient.invalidateQueries({ queryKey: ["use-cases-summary"] });
    },
    onError: () => {
      setAddingObjectId(null);
    },
  });

  // Remove object mutation
  const removeObjectMutation = useMutation({
    mutationFn: async (mappingId: string) => {
      const response = await fetch(`/api/use-cases/use-cases/${useCase?.use_case_id}/objects/${mappingId}`, {
        method: "DELETE",
      });
      if (!response.ok) throw new Error("Failed to remove object");
      return response.json();
    },
    onSuccess: () => {
      refetchDetails();
      queryClient.invalidateQueries({ queryKey: ["use-cases-summary"] });
    },
  });

  // Reset form when use case changes
  useEffect(() => {
    if (useCase) {
      setName(useCase.name || "");
      setDescription(useCase.description || "");
      setOwner(useCase.owner || "");
      setTags(useCase.tags || {});
      setStage(useCase.stage || "Development");
      setStartDate(useCase.start_date || "");
      setEndDate(useCase.end_date || "");
      setLiveDate(useCase.live_date || "");
      setStartToday(false);
      setShowDeleteConfirm(false);
    }
  }, [useCase]);

  // Handle escape key
  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    if (e.key === "Escape") onClose();
  }, [onClose]);

  useEffect(() => {
    if (isOpen) {
      document.addEventListener("keydown", handleKeyDown);
      document.body.style.overflow = "hidden";
    }
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = "";
    };
  }, [isOpen, handleKeyDown]);

  const updateMutation = useMutation({
    mutationFn: async (data: {
      name: string;
      description: string;
      owner: string;
      tags: Record<string, string>;
      stage: UseCaseStage;
      start_date: string | null;
      end_date: string | null;
      live_date: string | null;
    }) => {
      setUpdateError(null);
      const response = await fetch(`/api/use-cases/use-cases/${useCase?.use_case_id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.detail || "Failed to update use case");
      }
      return response.json();
    },
    onSuccess: () => {
      setUpdateError(null);
      queryClient.invalidateQueries({ queryKey: ["use-cases"] });
      queryClient.invalidateQueries({ queryKey: ["use-cases-summary"] });
      queryClient.invalidateQueries({ queryKey: ["monthly-consumption"] });
      onClose();
    },
    onError: (err: Error) => {
      setUpdateError(err.message);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: async () => {
      const response = await fetch(`/api/use-cases/use-cases/${useCase?.use_case_id}`, {
        method: "DELETE",
      });
      if (!response.ok) throw new Error("Failed to delete use case");
      return response.json();
    },
    onSuccess: async () => {
      const deletedId = useCase?.use_case_id;
      // Optimistically remove from caches so it disappears immediately
      queryClient.setQueryData<UseCasesListResponse>(["use-cases"], (old) =>
        old ? { ...old, use_cases: old.use_cases.filter((uc) => uc.use_case_id !== deletedId), count: old.count - 1 } : old
      );
      queryClient.setQueryData<UseCasesSummaryResponse>(["use-cases-summary"], (old) =>
        old ? { ...old, use_cases: old.use_cases.filter((uc) => uc.use_case_id !== deletedId), count: old.count - 1 } : old
      );
      setShowDeleteConfirm(false);
      onClose();
      // Background refetch for eventual consistency
      queryClient.invalidateQueries({ queryKey: ["use-cases"] });
      queryClient.invalidateQueries({ queryKey: ["use-cases-summary"] });
      queryClient.invalidateQueries({ queryKey: ["monthly-consumption"] });
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (name.trim()) {
      updateMutation.mutate({
        name,
        description,
        owner,
        tags,
        stage,
        start_date: startDate || null,
        end_date: endDate || null,
        live_date: liveDate || null,
      });
    }
  };

  // Auto-set live_date when stage changes to Live
  const handleStageChange = (newStage: UseCaseStage) => {
    setStage(newStage);
    if (newStage === 'Live' && !liveDate) {
      setLiveDate(new Date().toISOString().split('T')[0]);
    }
  };

  const addTag = () => {
    if (newTagKey.trim() && newTagValue.trim()) {
      setTags(prev => ({ ...prev, [newTagKey.trim()]: newTagValue.trim() }));
      setNewTagKey("");
      setNewTagValue("");
    }
  };

  const removeTag = (key: string) => {
    setTags(prev => {
      const newTags = { ...prev };
      delete newTags[key];
      return newTags;
    });
  };

  if (!isOpen || !useCase) return null;

  const tagKeys = Object.keys(existingTags);
  const tagValues = newTagKey ? existingTags[newTagKey] || [] : [];

  return createPortal(
    <div
      className="animate-backdrop fixed inset-0 z-50 overflow-y-auto bg-black/50"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="flex min-h-full items-center justify-center p-4" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div
        className="animate-dialog relative w-full max-w-4xl rounded-xl bg-white shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-gray-200 px-6 py-4">
          <div>
            <h2 className="text-xl font-semibold text-gray-900">Edit Use Case</h2>
            <p className="text-sm text-gray-500">Update details and tags</p>
          </div>
          <button
            onClick={() => { setUpdateError(null); onClose(); }}
            className="rounded-lg p-2 text-gray-500 hover:bg-gray-100 hover:text-gray-600 transition-colors"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Content */}
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          {updateError && (
            <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-600">
              {updateError}
            </div>
          )}

          <div>
            <label className="block text-sm font-medium text-gray-700">Name *</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-400 focus:outline-none"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700">Description</label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-400 focus:outline-none"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700">Owner</label>
            <input
              type="text"
              value={owner}
              onChange={(e) => setOwner(e.target.value)}
              className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-400 focus:outline-none"
            />
          </div>

          {/* Stage and Dates Section */}
          <div className="rounded-lg border border-gray-200 p-4 space-y-4 bg-gray-50">
            <div>
              <label className="block text-sm font-medium text-gray-700">Stage</label>
              <select
                value={stage}
                onChange={(e) => handleStageChange(e.target.value as UseCaseStage)}
                className="mt-1 w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm focus:border-gray-400 focus:outline-none"
              >
                <option value="Planned">Planned</option>
                <option value="Development">Development</option>
                <option value="Live">Live</option>
                <option value="Inactive">Inactive</option>
              </select>
            </div>

            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                id="start-today-edit"
                checked={startToday}
                onChange={(e) => {
                  setStartToday(e.target.checked);
                  if (e.target.checked) {
                    setStartDate(new Date().toISOString().split('T')[0]);
                  }
                }}
                className="h-4 w-4 rounded border-gray-300 text-orange-600 focus:ring-orange-500"
              />
              <label htmlFor="start-today-edit" className="text-sm font-medium text-gray-700">
                Start use case today
              </label>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700">
                  <Calendar className="inline h-4 w-4 mr-1" />
                  Start Date
                </label>
                <input
                  type="date"
                  value={startDate}
                  onChange={(e) => {
                    setStartDate(e.target.value);
                    setStartToday(false);
                  }}
                  disabled={startToday}
                  className="mt-1 w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm focus:border-gray-400 focus:outline-none disabled:bg-gray-200 disabled:text-gray-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700">
                  <Calendar className="inline h-4 w-4 mr-1" />
                  End Date
                </label>
                <input
                  type="date"
                  value={endDate}
                  onChange={(e) => setEndDate(e.target.value)}
                  className="mt-1 w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm focus:border-gray-400 focus:outline-none"
                />
              </div>
            </div>

            {stage === 'Live' && (
              <div>
                <label className="block text-sm font-medium text-gray-700">
                  <PlayCircle className="inline h-4 w-4 mr-1 text-green-600" />
                  Go-Live Date
                </label>
                <input
                  type="date"
                  value={liveDate}
                  onChange={(e) => setLiveDate(e.target.value)}
                  className="mt-1 w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm focus:border-gray-400 focus:outline-none"
                />
                <p className="mt-1 text-xs text-gray-500">This date will be marked on the consumption chart</p>
              </div>
            )}
          </div>

          {/* Tags Section */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              <Tag className="inline h-4 w-4 mr-1" />
              Tags
            </label>

            {/* Existing Tags */}
            {Object.keys(tags).length > 0 && (
              <div className="flex flex-wrap gap-2 mb-3">
                {Object.entries(tags).map(([key, value]) => (
                  <span
                    key={key}
                    className="inline-flex items-center gap-1 rounded-full bg-orange-100 px-3 py-1 text-sm text-orange-800"
                  >
                    <span className="font-medium">{key}:</span> {value}
                    <button
                      type="button"
                      onClick={() => removeTag(key)}
                      className="ml-1 text-orange-600 hover:text-orange-800"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </span>
                ))}
              </div>
            )}

            {/* Add New Tag */}
            <div className="flex gap-2">
              <div className="flex-1 relative" ref={keyDropdownRef}>
                <input
                  type="text"
                  value={newTagKey}
                  onChange={(e) => {
                    setNewTagKey(e.target.value);
                    setShowKeyDropdown(true);
                    if (e.target.value !== newTagKey) {
                      setNewTagValue("");
                    }
                  }}
                  onFocus={() => setShowKeyDropdown(true)}
                  onBlur={() => setTimeout(() => setShowKeyDropdown(false), 150)}
                  placeholder="Tag key (e.g., team, project)"
                  className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-400 focus:outline-none focus:ring-1 focus:ring-blue-400"
                />
                {showKeyDropdown && tagKeys.filter(k => !newTagKey || k.toLowerCase().includes(newTagKey.toLowerCase())).length > 0 && (
                  <div className="absolute z-50 mt-1 w-full max-h-40 overflow-y-auto rounded-md border border-gray-200 bg-white shadow-lg">
                    {tagKeys.filter(k => !newTagKey || k.toLowerCase().includes(newTagKey.toLowerCase())).map(key => (
                      <button
                        key={key}
                        type="button"
                        className="w-full px-3 py-2 text-left text-sm hover:bg-blue-50 text-gray-700"
                        onMouseDown={(e) => {
                          e.preventDefault();
                          setNewTagKey(key);
                          setNewTagValue("");
                          setShowKeyDropdown(false);
                        }}
                      >
                        {key}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <div className="flex-1 relative" ref={valueDropdownRef}>
                <input
                  type="text"
                  value={newTagValue}
                  onChange={(e) => {
                    setNewTagValue(e.target.value);
                    setShowValueDropdown(true);
                  }}
                  onFocus={() => setShowValueDropdown(true)}
                  onBlur={() => setTimeout(() => setShowValueDropdown(false), 150)}
                  placeholder="Tag value"
                  className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-400 focus:outline-none focus:ring-1 focus:ring-blue-400"
                />
                {showValueDropdown && tagValues.filter(v => !newTagValue || v.toLowerCase().includes(newTagValue.toLowerCase())).length > 0 && (
                  <div className="absolute z-50 mt-1 w-full max-h-40 overflow-y-auto rounded-md border border-gray-200 bg-white shadow-lg">
                    {tagValues.filter(v => !newTagValue || v.toLowerCase().includes(newTagValue.toLowerCase())).map(val => (
                      <button
                        key={val}
                        type="button"
                        className="w-full px-3 py-2 text-left text-sm hover:bg-blue-50 text-gray-700"
                        onMouseDown={(e) => {
                          e.preventDefault();
                          setNewTagValue(val);
                          setShowValueDropdown(false);
                        }}
                      >
                        {val}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <button
                type="button"
                onClick={addTag}
                disabled={!newTagKey.trim() || !newTagValue.trim()}
                className="rounded-md bg-gray-100 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-200 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Add
              </button>
            </div>
            <p className="mt-2 text-xs text-gray-500">
              Select from existing tags or add custom ones
            </p>
          </div>

          {/* Assigned Objects Section */}
          <div className="rounded-lg border border-gray-200 p-4 space-y-3 bg-gray-50">
            <div className="flex items-center justify-between">
              <label className="block text-sm font-medium text-gray-700">
                <Package className="inline h-4 w-4 mr-1" />
                Assigned Objects ({useCaseDetails?.objects?.length || 0})
              </label>
              <button
                type="button"
                onClick={() => setShowObjectPicker(!showObjectPicker)}
                className="inline-flex items-center gap-1 rounded-md bg-white border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50"
              >
                <Plus className="h-4 w-4" />
                Add Object
              </button>
            </div>

            {/* Object Picker */}
            {showObjectPicker && (
              <div className="rounded-lg border border-gray-300 bg-white p-3 space-y-3">
                <div className="flex gap-2">
                  <select
                    value={selectedObjectType}
                    onChange={(e) => setSelectedObjectType(e.target.value)}
                    className="rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-gray-400 focus:outline-none"
                  >
                    {OBJECT_TYPE_OPTIONS.map(opt => (
                      <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                  </select>
                  <button
                    type="button"
                    onClick={() => setShowObjectPicker(false)}
                    className="ml-auto text-gray-500 hover:text-gray-600"
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>

                {objectsLoading ? (
                  <div className="flex justify-center py-4">
                    <div className="h-5 w-5 animate-spin rounded-full border-2 border-gray-200 border-t-orange-600" />
                  </div>
                ) : (
                  <div className="max-h-48 overflow-y-auto space-y-1">
                    {availableObjects?.objects?.length === 0 ? (
                      <p className="text-sm text-gray-500 py-2">No {selectedObjectType}s found</p>
                    ) : (
                      availableObjects?.objects?.map(obj => {
                        const isAssigned = useCaseDetails?.objects?.some(
                          o => o.object_id === obj.object_id && o.object_type === obj.object_type
                        );
                        return (
                          <div
                            key={`${obj.object_type}-${obj.object_id}`}
                            className={`flex items-center justify-between p-2 rounded-md ${isAssigned ? 'bg-green-50' : 'hover:bg-gray-50'}`}
                          >
                            <div className="flex-1 min-w-0">
                              <p className="text-sm font-medium text-gray-900 truncate">{obj.object_name}</p>
                              <p className="text-xs text-gray-500 truncate">{obj.object_id}</p>
                            </div>
                            {isAssigned ? (
                              <span className="text-xs text-green-600 font-medium">Added</span>
                            ) : addingObjectId === obj.object_id ? (
                              <div className="flex items-center gap-1 px-2 py-1">
                                <div className="h-3 w-3 animate-spin rounded-full border-2 border-orange-300 border-t-orange-600" />
                                <span className="text-xs text-orange-600">Adding...</span>
                              </div>
                            ) : (
                              <button
                                type="button"
                                onClick={() => assignObjectMutation.mutate(obj)}
                                disabled={assignObjectMutation.isPending}
                                className="rounded-md bg-orange-100 px-2 py-1 text-xs font-medium text-orange-700 hover:bg-orange-200 disabled:opacity-50"
                              >
                                Add
                              </button>
                            )}
                          </div>
                        );
                      })
                    )}
                  </div>
                )}
              </div>
            )}

            {/* List of assigned objects */}
            {useCaseDetails?.objects && useCaseDetails.objects.length > 0 ? (
              <div className="space-y-2">
                {useCaseDetails.objects.map(obj => {
                  const typeOption = OBJECT_TYPE_OPTIONS.find(o => o.value === obj.object_type);
                  const Icon = typeOption?.icon || Package;
                  return (
                    <div
                      key={obj.mapping_id}
                      className="flex items-center justify-between p-2 rounded-md bg-white border border-gray-200"
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        <Icon className="h-4 w-4 text-gray-500 flex-shrink-0" />
                        <div className="min-w-0">
                          <p className="text-sm font-medium text-gray-900 truncate">
                            {obj.object_name || obj.object_id}
                          </p>
                          <p className="text-xs text-gray-500">{obj.object_type}</p>
                        </div>
                      </div>
                      <button
                        type="button"
                        onClick={() => removeObjectMutation.mutate(obj.mapping_id)}
                        disabled={removeObjectMutation.isPending}
                        className="text-gray-500 hover:text-red-600 p-1"
                        title="Remove from use case"
                      >
                        <X className="h-4 w-4" />
                      </button>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="text-sm text-gray-500 text-center py-2">
                No objects assigned yet. Click "Add Object" to associate resources with this use case.
              </p>
            )}
          </div>

          {/* Spend Info */}
          <div className="rounded-lg bg-gray-50 p-4">
            <div className="grid grid-cols-3 gap-4 text-center">
              <div>
                <p className="text-xs text-gray-500">Spend</p>
                <p className="text-lg font-semibold text-gray-900">{formatCurrency(useCase.total_spend)}</p>
              </div>
              <div>
                <p className="text-xs text-gray-500">DBUs</p>
                <p className="text-lg font-semibold text-gray-900">{formatNumber(useCase.total_dbus)}</p>
              </div>
              <div>
                <p className="text-xs text-gray-500">Objects</p>
                <p className="text-lg font-semibold text-gray-900">{useCase.object_count}</p>
              </div>
            </div>
          </div>

          {/* Actions */}
          <div className="flex items-center justify-between gap-3 pt-2">
            {/* Delete Button */}
            <div>
              {showDeleteConfirm ? (
                <div className="flex items-center gap-2">
                  <span className="text-sm text-red-600">Delete this use case?</span>
                  <button
                    type="button"
                    onClick={() => deleteMutation.mutate()}
                    disabled={deleteMutation.isPending}
                    className="rounded-lg bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-50"
                  >
                    {deleteMutation.isPending ? "Deleting..." : "Confirm"}
                  </button>
                  <button
                    type="button"
                    onClick={() => setShowDeleteConfirm(false)}
                    className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50"
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => setShowDeleteConfirm(true)}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-red-300 px-3 py-2 text-sm font-medium text-red-600 hover:bg-red-50 transition-colors"
                >
                  <Trash2 className="h-4 w-4" />
                  Delete
                </button>
              )}
            </div>

            {/* Save/Cancel Buttons */}
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={onClose}
                className="rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={updateMutation.isPending}
                className="btn-brand rounded-lg px-4 py-2 text-sm font-medium text-white transition-colors disabled:opacity-50"
              >
                {updateMutation.isPending ? "Saving..." : "Save Changes"}
              </button>
            </div>
          </div>
        </form>
      </div>
      </div>
    </div>,
    document.body
  );
}

export default function UseCases() {
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [selectedUseCase, setSelectedUseCase] = useState<UseCaseSummary | null>(null);
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  const queryClient = useQueryClient();

  // Quick delete mutation for table row
  const quickDeleteMutation = useMutation({
    mutationFn: async (useCaseId: string) => {
      const response = await fetch(`/api/use-cases/use-cases/${useCaseId}`, {
        method: "DELETE",
      });
      if (!response.ok) throw new Error("Failed to delete use case");
      return response.json();
    },
    onMutate: async (useCaseId: string) => {
      // Optimistically remove from caches so it disappears immediately
      queryClient.setQueryData<UseCasesListResponse>(["use-cases"], (old) =>
        old ? { ...old, use_cases: old.use_cases.filter((uc) => uc.use_case_id !== useCaseId), count: old.count - 1 } : old
      );
      queryClient.setQueryData<UseCasesSummaryResponse>(["use-cases-summary"], (old) =>
        old ? { ...old, use_cases: old.use_cases.filter((uc) => uc.use_case_id !== useCaseId), count: old.count - 1 } : old
      );
    },
    onSuccess: () => {
      setDeleteConfirmId(null);
      // Background refetch for eventual consistency
      queryClient.invalidateQueries({ queryKey: ["use-cases"] });
      queryClient.invalidateQueries({ queryKey: ["use-cases-summary"] });
      queryClient.invalidateQueries({ queryKey: ["monthly-consumption"] });
    },
  });

  // Fetch use cases list
  const { data: _useCases, isLoading } = useQuery<UseCasesListResponse>({
    queryKey: ["use-cases"],
    queryFn: async () => {
      const response = await fetch("/api/use-cases/use-cases?status=active");
      if (!response.ok) throw new Error("Failed to fetch use cases");
      return response.json();
    },
  });

  // Fetch use cases with spend analytics
  const { data: summary, isLoading: summaryLoading } = useQuery<UseCasesSummaryResponse>({
    queryKey: ["use-cases-summary"],
    queryFn: async () => {
      const response = await fetch("/api/use-cases/analytics/summary");
      if (!response.ok) throw new Error("Failed to fetch summary");
      return response.json();
    },
  });

  // Fetch monthly consumption data
  const { data: monthlyData } = useQuery<MonthlyConsumptionResponse>({
    queryKey: ["monthly-consumption"],
    queryFn: async () => {
      const response = await fetch("/api/use-cases/monthly-consumption");
      if (!response.ok) throw new Error("Failed to fetch monthly consumption");
      return response.json();
    },
  });

  // Fetch Databricks account tags (custom_tags from billing)
  const { data: billingTagsData } = useQuery<{ tags: Record<string, string[]>; count: number }>({
    queryKey: ["available-tags"],
    queryFn: async () => {
      const response = await fetch("/api/tagging/available-tags");
      if (!response.ok) return { tags: {}, count: 0 };
      return response.json();
    },
  });

  // Prepare pie chart data
  const pieData = summary?.use_cases.map((uc, idx) => ({
    name: uc.name,
    value: uc.total_spend,
    percentage: uc.percentage,
    fill: COLORS[idx % COLORS.length],
  })) || [];

  // Prepare monthly chart data with formatted month labels
  const monthlyChartData = monthlyData?.months.map(m => ({
    ...m,
    monthLabel: new Date(m.month + '-01').toLocaleDateString('en-US', { month: 'short', year: '2-digit' }),
  })) || [];

  // Collect all tags: Databricks account tags + tags from existing use cases
  const existingTags: Record<string, string[]> = (() => {
    const tagMap: Record<string, Set<string>> = {};

    // Add Databricks account tags (custom_tags from billing)
    for (const [k, values] of Object.entries(billingTagsData?.tags || {})) {
      if (!tagMap[k]) tagMap[k] = new Set();
      for (const v of values) {
        if (v) tagMap[k].add(v);
      }
    }

    // Add tags from existing use cases
    const allUseCases = [
      ...(summary?.use_cases || []),
      ...(_useCases?.use_cases || []),
    ];
    for (const uc of allUseCases) {
      if (uc.tags && typeof uc.tags === 'object') {
        for (const [k, v] of Object.entries(uc.tags)) {
          if (!tagMap[k]) tagMap[k] = new Set();
          if (v) tagMap[k].add(String(v));
        }
      }
    }

    return Object.fromEntries(
      Object.entries(tagMap).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => [k, [...v].sort()])
    );
  })();

  if (isLoading || summaryLoading) {
    return (
      <div className="flex h-64 flex-col items-center justify-center gap-3">
        <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-200" style={{ borderTopColor: C.lava }} />
        <p className="text-sm text-gray-500">Loading use cases...</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
        <PageHero
          icon={<Package />}
          title="Use Cases"
          subtitle="Track spend by project and use case"
          action={
            <button
              onClick={() => setShowCreateModal(true)}
              className="btn-brand inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium text-white transition-colors"
            >
              <Plus className="h-4 w-4" />
              New Use Case
            </button>
          }
        />

      {/* Summary Cards */}
      {summary && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
            <div className="flex items-center">
              <div className="flex h-12 w-12 items-center justify-center rounded-lg bg-orange-100">
                <Package className="h-6 w-6 text-orange-600" />
              </div>
              <div className="ml-4">
                <p className="text-sm font-medium text-gray-500">Active Use Cases</p>
                <p className="text-2xl font-semibold text-gray-900">{summary.count}</p>
              </div>
            </div>
          </div>

          <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
            <div className="flex items-center">
              <div className="flex h-12 w-12 items-center justify-center rounded-lg bg-orange-100">
                <TrendingUp className="h-6 w-6 text-orange-600" />
              </div>
              <div className="ml-4">
                <p className="text-sm font-medium text-gray-500">Total Spend</p>
                <p className="text-2xl font-semibold text-gray-900">{formatCurrency(summary.total_spend)}</p>
              </div>
            </div>
          </div>

          <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
            <div className="flex items-center">
              <div className="flex h-12 w-12 items-center justify-center rounded-lg bg-orange-100">
                <Users className="h-6 w-6 text-orange-600" />
              </div>
              <div className="ml-4">
                <p className="text-sm font-medium text-gray-500">Total Objects</p>
                <p className="text-2xl font-semibold text-gray-900">
                  {summary.use_cases.reduce((acc, uc) => acc + uc.object_count, 0)}
                </p>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Use Cases List */}
      <div className="rounded-lg bg-white border " style={{ borderColor: C.hairline }}>
        <div className="border-b px-6 py-4" style={{ borderColor: C.hairline }}>
          <h2 className="text-lg font-semibold text-gray-900">All Use Cases</h2>
        </div>

        {isLoading || summaryLoading ? (
          <div className="flex h-64 items-center justify-center">
            <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-200" style={{ borderTopColor: C.lava }} />
          </div>
        ) : summary && summary.use_cases.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">
                    Name
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">
                    Stage
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">
                    Owner
                  </th>
                  <th className="px-6 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">
                    Objects
                  </th>
                  <th className="px-6 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">
                    DBUs
                  </th>
                  <th className="px-6 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">
                    Spend
                  </th>
                  <th className="px-6 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">
                    % of Total
                  </th>
                  <th className="px-6 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200 bg-white">
                {summary.use_cases.map((uc) => (
                  <tr
                    key={uc.use_case_id}
                    className="hover:bg-gray-50 cursor-pointer"
                    onClick={() => {
                      setSelectedUseCase(uc);
                      setShowEditModal(true);
                    }}
                  >
                    <td className="px-6 py-4 max-w-xs">
                      <div>
                        <div className="font-medium text-gray-900 truncate">{uc.name}</div>
                        {uc.description && (
                          <div className="text-sm text-gray-500 line-clamp-2">{uc.description}</div>
                        )}
                      </div>
                    </td>
                    <td className="whitespace-nowrap px-6 py-4">
                      <StageBadge stage={uc.stage || 'Development'} />
                    </td>
                    <td className="whitespace-nowrap px-6 py-4 text-sm text-gray-500">
                      {uc.owner || "-"}
                    </td>
                    <td className="whitespace-nowrap px-6 py-4 text-right text-sm text-gray-900">
                      {uc.object_count}
                    </td>
                    <td className="whitespace-nowrap px-6 py-4 text-right text-sm text-gray-500">
                      {formatNumber(uc.total_dbus)}
                    </td>
                    <td className="whitespace-nowrap px-6 py-4 text-right text-sm font-medium text-gray-900">
                      {formatCurrency(uc.total_spend)}
                    </td>
                    <td className="whitespace-nowrap px-6 py-4 text-right text-sm text-gray-500">
                      {uc.percentage.toFixed(1)}%
                    </td>
                    <td className="whitespace-nowrap px-6 py-4 text-right text-sm">
                      <div className="flex items-center justify-end gap-2" onClick={(e) => e.stopPropagation()}>
                        {deleteConfirmId === uc.use_case_id ? (
                          <>
                            <span className="text-xs text-red-600">Delete?</span>
                            <button
                              onClick={() => quickDeleteMutation.mutate(uc.use_case_id)}
                              disabled={quickDeleteMutation.isPending}
                              className="rounded bg-red-600 px-2 py-1 text-xs font-medium text-white hover:bg-red-700 disabled:opacity-50"
                            >
                              {quickDeleteMutation.isPending ? "..." : "Yes"}
                            </button>
                            <button
                              onClick={() => setDeleteConfirmId(null)}
                              className="rounded border border-gray-300 px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50"
                            >
                              No
                            </button>
                          </>
                        ) : (
                          <button
                            onClick={() => setDeleteConfirmId(uc.use_case_id)}
                            className="rounded p-1.5 text-gray-500 hover:bg-red-50 hover:text-red-600 transition-colors"
                            title="Delete use case"
                          >
                            <Trash2 className="h-4 w-4" />
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="flex h-64 flex-col items-center justify-center text-center">
            <Package className="h-12 w-12 text-gray-500" />
            <p className="mt-2 text-sm font-medium text-gray-900">No use cases yet</p>
            <p className="mt-1 text-sm text-gray-500">Create your first use case to start tracking spend</p>
            <button
              onClick={() => setShowCreateModal(true)}
              className="btn-brand mt-4 inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium text-white transition-colors"
            >
              <Plus className="h-4 w-4" />
              Create Use Case
            </button>
          </div>
        )}
      </div>

      {/* Spend Distribution Chart - moved below table */}
      {summary && summary.use_cases.length > 0 && (
        <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
          <h2 className="mb-4 text-lg font-semibold text-gray-900">Spend Distribution by Use Case</h2>
          <ResponsiveContainer width="100%" height={300}>
            <PieChart>
              <Pie
                data={pieData}
                cx="50%"
                cy="50%"
                innerRadius={60}
                outerRadius={100}
                paddingAngle={2}
                dataKey="value"
                label={({ name, percent }) => `${name}: ${((percent || 0) * 100).toFixed(1)}%`}
              >
                {pieData.map((entry, index) => (
                  <Cell key={`cell-${index}`} fill={entry.fill} />
                ))}
              </Pie>
              <Tooltip formatter={(value) => formatCurrency(value as number)} />
            </PieChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Monthly Consumption Chart */}
      {monthlyChartData.length > 0 && (
        <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
          <div className="mb-4">
            <h2 className="text-lg font-semibold text-gray-900">Monthly Consumption by Use Case</h2>
            <p className="text-sm text-gray-500">Monthly spend with use case go-live markers</p>
          </div>
          <ResponsiveContainer width="100%" height={350}>
            <BarChart data={monthlyChartData} margin={{ top: 30, right: 30, left: 20, bottom: 5 }}>
              <XAxis
                dataKey="monthLabel"
                tick={{ fontSize: 12 }}
                tickLine={{ stroke: C.hairline }}
              />
              <YAxis
                tickFormatter={(value) => `$${(value / 1000).toFixed(0)}k`}
                tick={{ fontSize: 12 }}
                tickLine={{ stroke: C.hairline }}
              />
              <Tooltip
                formatter={(value) => [formatCurrency(value as number), 'Total Spend']}
                labelFormatter={(label) => `Month: ${label}`}
              />
              <Legend />
              <Bar
                dataKey="total_spend"
                name="Total Spend"
                fill={C.lava}
                radius={[4, 4, 0, 0]}
              />
              {/* Reference lines for use case go-live dates */}
              {monthlyData?.live_events.map((event, idx) => {
                const monthLabel = new Date(event.month + '-01').toLocaleDateString('en-US', { month: 'short', year: '2-digit' });
                return (
                  <ReferenceLine
                    key={`live-${event.use_case_id}-${idx}`}
                    x={monthLabel}
                    stroke={C.s3}
                    strokeWidth={2}
                    strokeDasharray="4 4"
                    label={{
                      value: `${event.use_case_name} Live`,
                      position: 'top',
                      fill: C.s3,
                      fontSize: 10,
                      fontWeight: 500,
                    }}
                  />
                );
              })}
            </BarChart>
          </ResponsiveContainer>

          {/* Legend for go-live markers */}
          {monthlyData?.live_events && monthlyData.live_events.length > 0 && (
            <div className="mt-4 pt-4 border-t border-gray-200">
              <div className="flex items-center gap-2 text-sm text-gray-600">
                <div className="flex items-center gap-1">
                  <div className="w-4 h-0.5 bg-green-500" style={{ borderTop: '2px dashed #10B981' }} />
                  <span>Go-Live Events:</span>
                </div>
                <div className="flex flex-wrap gap-2">
                  {monthlyData.live_events.map((event, idx) => (
                    <span
                      key={`legend-${idx}`}
                      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-green-100 text-green-800 text-xs"
                    >
                      <PlayCircle className="h-3 w-3" />
                      {event.use_case_name} ({event.live_date})
                    </span>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Create Modal */}
      <CreateUseCaseModal
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        existingTags={existingTags}
      />

      {/* Edit Modal */}
      <EditUseCaseModal
        useCase={selectedUseCase}
        isOpen={showEditModal}
        onClose={() => {
          setShowEditModal(false);
          setSelectedUseCase(null);
        }}
        existingTags={existingTags}
      />
    </div>
  );
}
