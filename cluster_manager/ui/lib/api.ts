import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

// Types matching backend models
export interface ClusterSummary {
  cluster_id: string;
  cluster_name: string;
  state: string;
  creator_user_name: string | null;
  node_type_id: string | null;
  driver_node_type_id: string | null;
  num_workers: number | null;
  autoscale: { min_workers: number; max_workers: number } | null;
  spark_version: string | null;
  cluster_source: string | null;
  start_time: string | null;
  last_activity_time: string | null;
  uptime_minutes: number;
  estimated_dbu_per_hour: number;
  policy_id: string | null;
  workspace_name: string | null;
  workspace_url: string | null;
}

export interface ClusterDetail extends ClusterSummary {
  terminated_time: string | null;
  termination_reason: string | null;
  state_message: string | null;
  default_tags: Record<string, string>;
  custom_tags: Record<string, string>;
  spark_conf: Record<string, string>;
  spark_env_vars: Record<string, string>;
  policy_id: string | null;
  data_security_mode: string | null;
}

export interface ClusterEvent {
  cluster_id: string;
  timestamp: string;
  event_type: string;
  details: Record<string, unknown>;
}

export interface ClusterMetricsSummary {
  total_clusters: number;
  running_clusters: number;
  pending_clusters: number;
  terminated_clusters: number;
  total_running_workers: number;
  estimated_hourly_dbu: number;
}

export interface IdleClusterAlert {
  cluster_id: string;
  cluster_name: string;
  idle_duration_minutes: number;
  estimated_wasted_dbu: number;
  recommendation: string;
}

export interface OptimizationRecommendation {
  cluster_id: string;
  cluster_name: string;
  issue: string;
  recommendation: string;
  potential_savings: string;
  priority: string;
}

export interface BillingSummary {
  total_dbu: number;
  estimated_cost_usd: number;
  period_start: string;
  period_end: string;
  currency: string;
}

export interface ClusterBillingUsage {
  cluster_id: string;
  cluster_name: string | null;
  total_dbu: number;
  estimated_cost_usd: number;
  usage_date_start: string;
  usage_date_end: string;
}

export interface BillingTrend {
  date: string;
  dbu: number;
  estimated_cost_usd: number;
}

export interface TopConsumer {
  cluster_id: string;
  cluster_name: string | null;
  total_dbu: number;
  estimated_cost_usd: number;
  percentage_of_total: number;
}

export interface ClusterPolicySummary {
  policy_id: string;
  name: string;
  definition: string | null;
  description: string | null;
  creator_user_name: string | null;
  created_at_timestamp: string | null;
  is_default: boolean;
}

export interface ClusterPolicyDetail extends ClusterPolicySummary {
  definition_json: Record<string, unknown>;
  max_clusters_per_user: number | null;
  policy_family_id: string | null;
  policy_family_definition_overrides: string | null;
}

export interface ClusterActionResponse {
  success: boolean;
  message: string;
  cluster_id: string;
}

// Optimization types
export interface OptimizationSummary {
  total_clusters_analyzed: number;
  oversized_clusters: number;
  underutilized_clusters: number;
  total_potential_monthly_savings: number;
  recommendations_count: number;
  last_analysis_time: string;
}

export interface OversizedClusterAnalysis {
  cluster_id: string;
  cluster_name: string;
  cluster_type: string;
  current_workers: number;
  avg_efficiency_score: number;
  avg_daily_dbu: number;
  recommended_workers: number;
  potential_dbu_savings: number;
  potential_cost_savings: number;
}

export interface JobClusterRecommendation {
  source_cluster_id: string;
  source_cluster_name: string;
  target_cluster_id: string;
  target_cluster_name: string;
  job_count: number;
  reason: string;
  estimated_savings: string;
}

export interface ScheduleOptimizationRecommendation {
  cluster_id: string;
  cluster_name: string;
  current_auto_terminate_minutes: number | null;
  recommended_auto_terminate_minutes: number;
  avg_idle_time_per_day_minutes: number;
  peak_usage_hours: number[];
  reason: string;
}

// Live cluster metrics types
export interface ClusterMetricsPoint {
  timestamp: string;
  cpu_percent: number;
  memory_percent: number;
  cpu_user_percent: number;
  cpu_system_percent: number;
  cpu_wait_percent: number;
  network_sent_bytes: number;
  network_received_bytes: number;
}

export interface NodeMetricsSnapshot {
  instance_id: string;
  node_type: string;
  is_driver: boolean;
  cpu_percent: number;
  memory_percent: number;
  network_sent_bytes: number;
  network_received_bytes: number;
}

export interface ClusterMetricsResponse {
  cluster_id: string;
  time_series: ClusterMetricsPoint[];
  current_nodes: NodeMetricsSnapshot[];
  minutes: number;
}

// API functions
async function fetchApi<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options?.headers,
    },
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: "Unknown error" }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }

  return response.json();
}

// Query hooks
export function useClusters(state?: string, clusterIds?: string[]) {
  const params = new URLSearchParams();
  if (state) params.set("state", state);
  if (clusterIds?.length) clusterIds.forEach((id) => params.append("cluster_ids", id));
  const qs = params.toString() ? `?${params}` : "";
  return useQuery({
    queryKey: ["clusters", state, clusterIds],
    queryFn: () => fetchApi<ClusterSummary[]>(`/api/clusters${qs}`),
    refetchInterval: 30000,
  });
}

export function useCluster(clusterId: string, workspaceUrl?: string | null) {
  const params = workspaceUrl ? `?workspace_url=${encodeURIComponent(workspaceUrl)}` : "";
  return useQuery({
    queryKey: ["cluster", clusterId, workspaceUrl],
    queryFn: () => fetchApi<ClusterDetail>(`/api/clusters/${clusterId}${params}`),
    enabled: !!clusterId,
    refetchInterval: 10000,
  });
}

export function useClusterEvents(clusterId: string, limit = 50, workspaceUrl?: string | null) {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  if (workspaceUrl) params.set("workspace_url", workspaceUrl);
  return useQuery({
    queryKey: ["cluster-events", clusterId, limit, workspaceUrl],
    queryFn: () =>
      fetchApi<{ events: ClusterEvent[]; total_count: number }>(
        `/api/clusters/${clusterId}/events?${params}`
      ),
    enabled: !!clusterId,
  });
}

export function useClusterMetrics(clusterId: string, minutes = 60, workspaceUrl?: string | null) {
  const params = new URLSearchParams();
  params.set("minutes", String(minutes));
  if (workspaceUrl) params.set("workspace_url", workspaceUrl);
  return useQuery({
    queryKey: ["cluster-metrics", clusterId, minutes, workspaceUrl],
    queryFn: () =>
      fetchApi<ClusterMetricsResponse>(`/api/clusters/${clusterId}/metrics?${params}`),
    enabled: !!clusterId,
    refetchInterval: 60000,
  });
}

export function useMetricsSummary() {
  return useQuery({
    queryKey: ["metrics-summary"],
    queryFn: () => fetchApi<ClusterMetricsSummary>("/api/metrics/summary"),
    refetchInterval: 30000,
  });
}

export function useIdleClusters() {
  return useQuery({
    queryKey: ["idle-clusters"],
    queryFn: () => fetchApi<IdleClusterAlert[]>("/api/metrics/idle-clusters"),
    refetchInterval: 60000,
  });
}

export function useRecommendations() {
  return useQuery({
    queryKey: ["recommendations"],
    queryFn: () => fetchApi<OptimizationRecommendation[]>("/api/metrics/recommendations"),
    refetchInterval: 60000,
  });
}

export function useBillingSummary(days = 30) {
  return useQuery({
    queryKey: ["billing-summary", days],
    queryFn: () => fetchApi<BillingSummary>(`/api/billing/summary?days=${days}`),
    refetchInterval: 300000, // 5 minutes
  });
}

export function useBillingByCluster(days = 30, limit = 50) {
  return useQuery({
    queryKey: ["billing-by-cluster", days, limit],
    queryFn: () =>
      fetchApi<ClusterBillingUsage[]>(`/api/billing/by-cluster?days=${days}&limit=${limit}`),
    refetchInterval: 300000,
  });
}

export function useBillingTrend(days = 30) {
  return useQuery({
    queryKey: ["billing-trend", days],
    queryFn: () => fetchApi<BillingTrend[]>(`/api/billing/trend?days=${days}`),
    refetchInterval: 300000,
  });
}

export function useTopConsumers(days = 30, limit = 10) {
  return useQuery({
    queryKey: ["top-consumers", days, limit],
    queryFn: () => fetchApi<TopConsumer[]>(`/api/billing/top-consumers?days=${days}&limit=${limit}`),
    refetchInterval: 300000,
  });
}

export function usePolicies(clusterIds?: string[]) {
  const params = new URLSearchParams();
  if (clusterIds?.length) clusterIds.forEach((id) => params.append("cluster_ids", id));
  const qs = params.toString() ? `?${params}` : "";
  return useQuery({
    queryKey: ["policies", clusterIds],
    queryFn: () => fetchApi<ClusterPolicySummary[]>(`/api/policies${qs}`),
  });
}

export function usePolicy(policyId: string | null) {
  return useQuery({
    queryKey: ["policy", policyId],
    queryFn: () => fetchApi<ClusterPolicyDetail>(`/api/policies/${policyId}`),
    enabled: !!policyId,
  });
}

// Mutation hooks
export function useStartCluster() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ clusterId, workspaceUrl }: { clusterId: string; workspaceUrl?: string | null }) => {
      const params = workspaceUrl ? `?workspace_url=${encodeURIComponent(workspaceUrl)}` : "";
      return fetchApi<ClusterActionResponse>(`/api/clusters/${clusterId}/start${params}`, { method: "POST" });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["clusters"] });
      queryClient.invalidateQueries({ queryKey: ["metrics-summary"] });
    },
  });
}

export function useStopCluster() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ clusterId, workspaceUrl }: { clusterId: string; workspaceUrl?: string | null }) => {
      const params = workspaceUrl ? `?workspace_url=${encodeURIComponent(workspaceUrl)}` : "";
      return fetchApi<ClusterActionResponse>(`/api/clusters/${clusterId}/stop${params}`, { method: "POST" });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["clusters"] });
      queryClient.invalidateQueries({ queryKey: ["metrics-summary"] });
    },
  });
}

// Optimization hooks
export function useOptimizationSummary(clusterIds?: string[]) {
  const params = new URLSearchParams();
  if (clusterIds?.length) clusterIds.forEach((id) => params.append("cluster_ids", id));
  const qs = params.toString() ? `?${params}` : "";
  return useQuery({
    queryKey: ["optimization-summary", clusterIds],
    queryFn: () => fetchApi<OptimizationSummary>(`/api/optimization/summary${qs}`),
    refetchInterval: 60000,
  });
}

export function useOversizedClusters(minWorkers = 10, clusterIds?: string[]) {
  const params = new URLSearchParams();
  params.set("min_workers", String(minWorkers));
  if (clusterIds?.length) clusterIds.forEach((id) => params.append("cluster_ids", id));
  return useQuery({
    queryKey: ["oversized-clusters", minWorkers, clusterIds],
    queryFn: () =>
      fetchApi<OversizedClusterAnalysis[]>(`/api/optimization/oversized-clusters?${params}`),
    refetchInterval: 60000,
  });
}

export function useJobRecommendations(clusterIds?: string[]) {
  const params = new URLSearchParams();
  if (clusterIds?.length) clusterIds.forEach((id) => params.append("cluster_ids", id));
  const qs = params.toString() ? `?${params}` : "";
  return useQuery({
    queryKey: ["job-recommendations", clusterIds],
    queryFn: () => fetchApi<JobClusterRecommendation[]>(`/api/optimization/job-recommendations${qs}`),
    refetchInterval: 60000,
  });
}

export function useScheduleRecommendations(clusterIds?: string[]) {
  const params = new URLSearchParams();
  if (clusterIds?.length) clusterIds.forEach((id) => params.append("cluster_ids", id));
  const qs = params.toString() ? `?${params}` : "";
  return useQuery({
    queryKey: ["schedule-recommendations", clusterIds],
    queryFn: () =>
      fetchApi<ScheduleOptimizationRecommendation[]>(`/api/optimization/schedule-recommendations${qs}`),
    refetchInterval: 60000,
  });
}

// Spark Configuration types
export type SparkConfigImpact = "performance" | "cost" | "reliability" | "memory";
export type SparkConfigSeverity = "high" | "medium" | "low";

export interface SparkConfigRecommendation {
  cluster_id: string;
  cluster_name: string;
  setting: string;
  current_value: string | null;
  recommended_value: string;
  impact: SparkConfigImpact;
  severity: SparkConfigSeverity;
  reason: string;
  documentation_link: string | null;
}

export interface ClusterSparkConfigAnalysis {
  cluster_id: string;
  cluster_name: string;
  spark_version: string | null;
  is_photon_enabled: boolean;
  aqe_enabled: boolean | null;
  total_issues: number;
  recommendations: SparkConfigRecommendation[];
}

export function useSparkConfigRecommendations(includeNoIssues = false, clusterIds?: string[]) {
  const params = new URLSearchParams();
  params.set("include_no_issues", String(includeNoIssues));
  if (clusterIds?.length) clusterIds.forEach((id) => params.append("cluster_ids", id));
  return useQuery({
    queryKey: ["spark-config-recommendations", includeNoIssues, clusterIds],
    queryFn: () =>
      fetchApi<ClusterSparkConfigAnalysis[]>(
        `/api/optimization/spark-config-recommendations?${params}`
      ),
    refetchInterval: 60000,
  });
}

// Cost Optimization types
export type CostOptimizationCategory = "spot_instances" | "node_type" | "storage" | "autoscaling" | "serverless";
export type CostRecommendationSeverity = "high" | "medium" | "low";

export interface CostOptimizationRecommendation {
  cluster_id: string;
  cluster_name: string;
  category: CostOptimizationCategory;
  current_state: string;
  recommendation: string;
  estimated_savings_percent: number;
  severity: CostRecommendationSeverity;
  reason: string;
  implementation_steps: string[];
}

export interface ClusterCostAnalysis {
  cluster_id: string;
  cluster_name: string;
  cloud_provider: string;
  node_type_id: string | null;
  driver_node_type_id: string | null;
  num_workers: number;
  uses_spot_instances: boolean;
  spot_bid_price: number | null;
  first_on_demand: number | null;
  availability_zone: string | null;
  ebs_volume_type: string | null;
  total_recommendations: number;
  total_potential_savings_percent: number;
  recommendations: CostOptimizationRecommendation[];
}

export function useCostRecommendations(includeNoIssues = false, clusterIds?: string[]) {
  const params = new URLSearchParams();
  params.set("include_no_issues", String(includeNoIssues));
  if (clusterIds?.length) clusterIds.forEach((id) => params.append("cluster_ids", id));
  return useQuery({
    queryKey: ["cost-recommendations", includeNoIssues, clusterIds],
    queryFn: () =>
      fetchApi<ClusterCostAnalysis[]>(
        `/api/optimization/cost-recommendations?${params}`
      ),
    refetchInterval: 60000,
  });
}

// Autoscaling Optimization types
export type AutoscalingIssueType =
  | "wide_range"
  | "narrow_range"
  | "high_minimum"
  | "no_autoscaling"
  | "inefficient_range";
export type AutoscalingSeverity = "high" | "medium" | "low";

export interface AutoscalingRecommendation {
  cluster_id: string;
  cluster_name: string;
  issue_type: AutoscalingIssueType;
  current_config: string;
  recommendation: string;
  estimated_savings_percent: number;
  severity: AutoscalingSeverity;
  reason: string;
  implementation_steps: string[];
}

export interface ClusterAutoscalingAnalysis {
  cluster_id: string;
  cluster_name: string;
  cluster_type: string;
  has_autoscaling: boolean;
  min_workers: number | null;
  max_workers: number | null;
  current_workers: number;
  autoscale_range: number | null;
  range_ratio: number | null;
  auto_terminate_minutes: number | null;
  total_issues: number;
  total_potential_savings_percent: number;
  recommendations: AutoscalingRecommendation[];
}

export function useAutoscalingRecommendations(includeNoIssues = false, clusterIds?: string[]) {
  const params = new URLSearchParams();
  params.set("include_no_issues", String(includeNoIssues));
  if (clusterIds?.length) clusterIds.forEach((id) => params.append("cluster_ids", id));
  return useQuery({
    queryKey: ["autoscaling-recommendations", includeNoIssues, clusterIds],
    queryFn: () =>
      fetchApi<ClusterAutoscalingAnalysis[]>(
        `/api/optimization/autoscaling-recommendations?${params}`
      ),
    refetchInterval: 60000,
  });
}

// Node Type Right-Sizing types
export type NodeTypeCategory =
  | "memory_optimized"
  | "compute_optimized"
  | "general_purpose"
  | "gpu"
  | "storage_optimized"
  | "unknown";

export type NodeTypeIssueType =
  | "oversized_driver"
  | "undersized_driver"
  | "wrong_category"
  | "overprovisioned"
  | "mismatched_driver_worker"
  | "gpu_underutilized"
  | "legacy_instance";

export type NodeTypeSeverity = "high" | "medium" | "low";

export interface NodeTypeRecommendation {
  cluster_id: string;
  cluster_name: string;
  issue_type: NodeTypeIssueType;
  current_config: string;
  recommended_config: string;
  estimated_savings_percent: number;
  severity: NodeTypeSeverity;
  reason: string;
  implementation_steps: string[];
}

export interface NodeTypeSpec {
  instance_type: string;
  category: NodeTypeCategory;
  vcpus: number | null;
  memory_gb: number | null;
  gpu_count: number | null;
  generation: string | null;
  size: string | null;
}

export interface ClusterNodeTypeAnalysis {
  cluster_id: string;
  cluster_name: string;
  cluster_type: string;
  cloud_provider: string;
  worker_node_type: string | null;
  worker_node_category: NodeTypeCategory;
  worker_spec: NodeTypeSpec | null;
  driver_node_type: string | null;
  driver_node_category: NodeTypeCategory;
  driver_spec: NodeTypeSpec | null;
  num_workers: number;
  uses_same_driver_worker: boolean;
  total_issues: number;
  total_potential_savings_percent: number;
  recommendations: NodeTypeRecommendation[];
}

export function useNodeTypeRecommendations(includeNoIssues = false, clusterIds?: string[]) {
  const params = new URLSearchParams();
  params.set("include_no_issues", String(includeNoIssues));
  if (clusterIds?.length) clusterIds.forEach((id) => params.append("cluster_ids", id));
  return useQuery({
    queryKey: ["node-type-recommendations", includeNoIssues, clusterIds],
    queryFn: () =>
      fetchApi<ClusterNodeTypeAnalysis[]>(
        `/api/optimization/node-type-recommendations?${params}`
      ),
    refetchInterval: 60000,
  });
}

// Live OTel Metrics types
export interface LiveNodeMetric {
  cluster_id: string;
  instance_id: string;
  is_driver: boolean;
  node_type: string | null;
  ts: string;
  cpu_user_percent: number | null;
  cpu_system_percent: number | null;
  cpu_wait_percent: number | null;
  mem_used_percent: number | null;
  mem_swap_percent: number | null;
  network_sent_bytes: number | null;
  network_received_bytes: number | null;
  disk_used_percent: number | null;
  load_1m: number | null;
  load_5m: number | null;
  load_15m: number | null;
}

export interface ClusterLiveStatus {
  cluster_id: string;
  node_count: number;
  latest_ts: string;
  avg_cpu: number | null;
  avg_mem: number | null;
  max_cpu: number | null;
  max_mem: number | null;
  is_stale: boolean;
}

export interface LiveAlert {
  cluster_id: string;
  instance_id: string;
  is_driver: boolean;
  alert_type: string;
  value: number;
  threshold: number;
  ts: string;
}

export function useLiveActiveClusters() {
  return useQuery({
    queryKey: ["live-active-clusters"],
    queryFn: () => fetchApi<ClusterLiveStatus[]>("/api/live-metrics/active"),
    refetchInterval: 15000,
  });
}

export function useLiveClusterMetrics(clusterId: string) {
  return useQuery({
    queryKey: ["live-cluster-metrics", clusterId],
    queryFn: () => fetchApi<LiveNodeMetric[]>(`/api/live-metrics/${clusterId}`),
    enabled: !!clusterId,
    refetchInterval: 15000,
  });
}

export function useLiveClusterHistory(clusterId: string, minutes = 60) {
  return useQuery({
    queryKey: ["live-cluster-history", clusterId, minutes],
    queryFn: () =>
      fetchApi<LiveNodeMetric[]>(`/api/live-metrics/${clusterId}/history?minutes=${minutes}`),
    enabled: !!clusterId,
    refetchInterval: 30000,
  });
}

export function useLiveAlerts() {
  return useQuery({
    queryKey: ["live-alerts"],
    queryFn: () => fetchApi<LiveAlert[]>("/api/live-metrics/alerts"),
    refetchInterval: 15000,
  });
}

// Workspace info
export interface WorkspaceInfo {
  host: string;
  org_id: string | null;
}

export function useWorkspaceInfo() {
  return useQuery({
    queryKey: ["workspace-info"],
    queryFn: () => fetchApi<WorkspaceInfo>("/api/workspace/info"),
    staleTime: Infinity, // Workspace info doesn't change
  });
}
