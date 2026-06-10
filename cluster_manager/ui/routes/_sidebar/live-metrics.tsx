import { createFileRoute } from "@tanstack/react-router";
import { Activity, AlertTriangle, Radio, Server } from "lucide-react";
import { useState } from "react";

import {
  ClusterLiveStatus,
  LiveAlert,
  LiveNodeMetric,
  useLiveActiveClusters,
  useLiveAlerts,
  useLiveClusterHistory,
} from "@/lib/api";
import { useMonitoredClusters } from "@/lib/monitored-clusters-context";
import { cn } from "@/lib/utils";

function formatTimestamp(ts: string): string {
  const date = new Date(ts);
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function formatTimeAgo(ts: string): string {
  const seconds = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function StatusBadge({ isStale }: { isStale: boolean }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium",
        isStale
          ? "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300"
          : "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300"
      )}
    >
      <Radio size={10} className={isStale ? "" : "animate-pulse"} />
      {isStale ? "Stale" : "Live"}
    </span>
  );
}

function MetricBar({ value, max = 100, color }: { value: number | null; max?: number; color: string }) {
  const pct = value != null ? Math.min((value / max) * 100, 100) : 0;
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-2 bg-muted rounded-full overflow-hidden">
        <div className={cn("h-full rounded-full transition-all", color)} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs font-mono w-12 text-right">{value != null ? `${value.toFixed(1)}%` : "—"}</span>
    </div>
  );
}

function AlertsBanner({ alerts }: { alerts: LiveAlert[] }) {
  if (alerts.length === 0) return null;

  return (
    <div className="bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-800 rounded-lg p-4 mb-6">
      <div className="flex items-center gap-2 mb-2">
        <AlertTriangle size={16} className="text-red-600 dark:text-red-400" />
        <span className="font-medium text-red-800 dark:text-red-200">
          {alerts.length} Active Alert{alerts.length > 1 ? "s" : ""}
        </span>
      </div>
      <div className="space-y-1">
        {alerts.slice(0, 5).map((alert, i) => (
          <div key={i} className="text-sm text-red-700 dark:text-red-300">
            <span className="font-mono">{alert.cluster_id.slice(0, 12)}...</span>
            {" "}
            {alert.is_driver ? "(driver)" : "(worker)"} —{" "}
            {alert.alert_type === "high_cpu" && `CPU ${alert.value}%`}
            {alert.alert_type === "high_memory" && `Memory ${alert.value}%`}
            {alert.alert_type === "high_disk" && `Disk ${alert.value}%`}
            {" > "}
            {alert.threshold}% threshold
          </div>
        ))}
        {alerts.length > 5 && (
          <div className="text-sm text-red-600 dark:text-red-400">
            +{alerts.length - 5} more alerts
          </div>
        )}
      </div>
    </div>
  );
}

function ClusterTable({
  clusters,
  onSelect,
  selectedId,
}: {
  clusters: ClusterLiveStatus[];
  onSelect: (id: string) => void;
  selectedId: string | null;
}) {
  return (
    <div className="border rounded-lg overflow-hidden">
      <table className="w-full text-sm">
        <thead className="bg-muted/50">
          <tr>
            <th className="px-4 py-2 text-left font-medium">Cluster</th>
            <th className="px-4 py-2 text-left font-medium">Status</th>
            <th className="px-4 py-2 text-left font-medium">Nodes</th>
            <th className="px-4 py-2 text-left font-medium">Avg CPU</th>
            <th className="px-4 py-2 text-left font-medium">Avg Mem</th>
            <th className="px-4 py-2 text-left font-medium">Peak CPU</th>
            <th className="px-4 py-2 text-left font-medium">Last Update</th>
          </tr>
        </thead>
        <tbody>
          {clusters.map((cluster) => (
            <tr
              key={cluster.cluster_id}
              className={cn(
                "border-t cursor-pointer hover:bg-muted/30 transition-colors",
                selectedId === cluster.cluster_id && "bg-muted/50"
              )}
              onClick={() => onSelect(cluster.cluster_id)}
            >
              <td className="px-4 py-2 font-mono text-xs">{cluster.cluster_id}</td>
              <td className="px-4 py-2">
                <StatusBadge isStale={cluster.is_stale} />
              </td>
              <td className="px-4 py-2">{cluster.node_count}</td>
              <td className="px-4 py-2">
                <MetricBar value={cluster.avg_cpu} color="bg-blue-500" />
              </td>
              <td className="px-4 py-2">
                <MetricBar value={cluster.avg_mem} color="bg-purple-500" />
              </td>
              <td className="px-4 py-2">
                <span className={cn(
                  "font-mono text-xs",
                  cluster.max_cpu && cluster.max_cpu > 80 ? "text-red-600 font-bold" : ""
                )}>
                  {cluster.max_cpu != null ? `${cluster.max_cpu.toFixed(1)}%` : "—"}
                </span>
              </td>
              <td className="px-4 py-2 text-xs text-muted-foreground">
                {formatTimeAgo(cluster.latest_ts)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function NodeCard({ instanceId, metrics, roleLabel, isDriver }: {
  instanceId: string;
  metrics: LiveNodeMetric[];
  roleLabel: string;
  isDriver: boolean;
}) {
  const latest = metrics[metrics.length - 1];
  const totalCpu = (latest.cpu_user_percent || 0) + (latest.cpu_system_percent || 0);

  return (
    <div className={cn(
      "border rounded-lg p-3",
      isDriver ? "border-blue-200 dark:border-blue-800 bg-blue-50/30 dark:bg-blue-950/20" : ""
    )}>
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className={cn(
            "px-1.5 py-0.5 text-xs rounded font-medium",
            isDriver
              ? "bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300"
              : "bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-400"
          )}>
            {roleLabel}
          </span>
          <span className="font-mono text-[10px] text-muted-foreground">{instanceId}</span>
          {latest.node_type && (
            <span className="text-[10px] text-muted-foreground">{latest.node_type}</span>
          )}
        </div>
        <span className="text-[10px] text-muted-foreground">
          {formatTimestamp(latest.ts)}
        </span>
      </div>
      <div className="grid grid-cols-3 gap-3">
        <div>
          <div className="text-[10px] text-muted-foreground mb-0.5">CPU</div>
          <MetricBar value={totalCpu} color={totalCpu > 80 ? "bg-red-500" : "bg-blue-500"} />
        </div>
        <div>
          <div className="text-[10px] text-muted-foreground mb-0.5">Memory</div>
          <MetricBar
            value={latest.mem_used_percent}
            color={(latest.mem_used_percent || 0) > 90 ? "bg-red-500" : "bg-purple-500"}
          />
        </div>
        <div>
          <div className="text-[10px] text-muted-foreground mb-0.5">Disk</div>
          <MetricBar
            value={latest.disk_used_percent}
            color={(latest.disk_used_percent || 0) > 90 ? "bg-red-500" : "bg-amber-500"}
          />
        </div>
      </div>
      <div className="mt-1.5 flex items-center gap-1 text-[10px] text-muted-foreground">
        <span>CPU:</span>
        {metrics.slice(-12).map((m, i) => {
          const cpu = (m.cpu_user_percent || 0) + (m.cpu_system_percent || 0);
          const bar = cpu > 80 ? "█" : cpu > 60 ? "▇" : cpu > 40 ? "▅" : cpu > 20 ? "▃" : "▁";
          return <span key={i} className={cpu > 80 ? "text-red-500" : ""}>{bar}</span>;
        })}
      </div>
    </div>
  );
}

function ClusterDetail({ clusterId }: { clusterId: string }) {
  const { data: history, isLoading } = useLiveClusterHistory(clusterId, 30);

  if (isLoading) {
    return <div className="p-4 text-muted-foreground">Loading metrics...</div>;
  }

  if (!history || history.length === 0) {
    return <div className="p-4 text-muted-foreground">No metric data available for this cluster.</div>;
  }

  // Group by instance
  const byInstance = new Map<string, LiveNodeMetric[]>();
  for (const m of history) {
    const key = m.instance_id;
    if (!byInstance.has(key)) byInstance.set(key, []);
    byInstance.get(key)!.push(m);
  }

  // Separate driver and workers
  const driver: [string, LiveNodeMetric[]] | null = Array.from(byInstance.entries())
    .find(([, ms]) => ms[ms.length - 1]?.is_driver) || null;
  const workers = Array.from(byInstance.entries())
    .filter(([, ms]) => !ms[ms.length - 1]?.is_driver);

  // Cluster-level aggregates from latest readings
  const allLatest = Array.from(byInstance.values()).map(ms => ms[ms.length - 1]);
  const nodeCount = allLatest.length;
  const avgCpu = allLatest.reduce((s, n) => s + (n.cpu_user_percent || 0) + (n.cpu_system_percent || 0), 0) / nodeCount;
  const maxCpu = Math.max(...allLatest.map(n => (n.cpu_user_percent || 0) + (n.cpu_system_percent || 0)));
  const avgMem = allLatest.reduce((s, n) => s + (n.mem_used_percent || 0), 0) / nodeCount;
  const maxMem = Math.max(...allLatest.map(n => n.mem_used_percent || 0));
  const maxLoad = Math.max(...allLatest.map(n => n.load_1m || 0));

  return (
    <div className="border rounded-lg mt-4 overflow-hidden">
      {/* Cluster Aggregate Header */}
      <div className="bg-muted/30 border-b px-4 py-3">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2">
            <Server size={16} className="text-primary" />
            <span className="font-medium text-sm">{clusterId}</span>
            <span className="text-xs text-muted-foreground">
              {nodeCount} node{nodeCount > 1 ? "s" : ""} ({driver ? "1 driver" : "no driver"} + {workers.length} worker{workers.length !== 1 ? "s" : ""})
            </span>
          </div>
          <span className="text-xs text-muted-foreground">
            {formatTimestamp(allLatest[0]?.ts || "")}
          </span>
        </div>
        {/* Aggregate metrics */}
        <div className="grid grid-cols-5 gap-4">
          <div>
            <div className="text-[10px] text-muted-foreground uppercase tracking-wide">Avg CPU</div>
            <div className={cn("text-lg font-bold", avgCpu > 80 ? "text-red-600" : avgCpu > 50 ? "text-yellow-600" : "text-green-600")}>
              {avgCpu.toFixed(1)}%
            </div>
          </div>
          <div>
            <div className="text-[10px] text-muted-foreground uppercase tracking-wide">Peak CPU</div>
            <div className={cn("text-lg font-bold", maxCpu > 80 ? "text-red-600" : "text-muted-foreground")}>
              {maxCpu.toFixed(1)}%
            </div>
          </div>
          <div>
            <div className="text-[10px] text-muted-foreground uppercase tracking-wide">Avg Memory</div>
            <div className={cn("text-lg font-bold", avgMem > 85 ? "text-red-600" : avgMem > 60 ? "text-yellow-600" : "text-green-600")}>
              {avgMem.toFixed(1)}%
            </div>
          </div>
          <div>
            <div className="text-[10px] text-muted-foreground uppercase tracking-wide">Peak Memory</div>
            <div className={cn("text-lg font-bold", maxMem > 85 ? "text-red-600" : "text-muted-foreground")}>
              {maxMem.toFixed(1)}%
            </div>
          </div>
          <div>
            <div className="text-[10px] text-muted-foreground uppercase tracking-wide">Load (1m)</div>
            <div className="text-lg font-bold text-muted-foreground">
              {maxLoad.toFixed(2)}
            </div>
          </div>
        </div>
      </div>

      {/* Node Group: Driver + Workers */}
      <div className="p-4 space-y-3">
        {/* Driver */}
        {driver && (
          <NodeCard
            instanceId={driver[0]}
            metrics={driver[1]}
            roleLabel="Driver"
            isDriver={true}
          />
        )}

        {/* Workers */}
        {workers.length > 0 && (
          <div>
            <div className="flex items-center gap-2 mb-2">
              <div className="h-px flex-1 bg-border" />
              <span className="text-[10px] text-muted-foreground font-medium uppercase tracking-wide">
                Workers ({workers.length})
              </span>
              <div className="h-px flex-1 bg-border" />
            </div>
            <div className={cn(
              "grid gap-2",
              workers.length <= 2 ? "grid-cols-1" : workers.length <= 4 ? "grid-cols-2" : "grid-cols-2 lg:grid-cols-3"
            )}>
              {workers.map(([instanceId, metrics], idx) => (
                <NodeCard
                  key={instanceId}
                  instanceId={instanceId}
                  metrics={metrics}
                  roleLabel={`Worker ${idx + 1}`}
                  isDriver={false}
                />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function LiveMetricsPage() {
  const { data: clusters, isLoading: loadingClusters } = useLiveActiveClusters();
  const { data: alerts } = useLiveAlerts();
  const { setSelectedIds } = useMonitoredClusters();
  const [selectedCluster, setSelectedCluster] = useState<string | null>(null);

  const handleClusterSelect = (id: string) => {
    setSelectedCluster(id);
    setSelectedIds([id]);
  };

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <Activity className="h-8 w-8 text-primary" />
          <div>
            <h1 className="text-2xl font-bold">Live Metrics</h1>
            <p className="text-sm text-muted-foreground">
              Real-time CPU, memory, and disk metrics from OTel-enabled clusters
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Radio size={12} className="animate-pulse text-green-500" />
          Auto-refresh: 15s
        </div>
      </div>

      {/* Alerts */}
      {alerts && <AlertsBanner alerts={alerts} />}

      {/* Summary cards */}
      {clusters && clusters.length > 0 && (
        <div className="grid grid-cols-4 gap-4 mb-6">
          <div className="border rounded-lg p-3">
            <div className="text-xs text-muted-foreground">Monitored Clusters</div>
            <div className="text-2xl font-bold">{clusters.length}</div>
          </div>
          <div className="border rounded-lg p-3">
            <div className="text-xs text-muted-foreground">Total Nodes</div>
            <div className="text-2xl font-bold">
              {clusters.reduce((s, c) => s + c.node_count, 0)}
            </div>
          </div>
          <div className="border rounded-lg p-3">
            <div className="text-xs text-muted-foreground">Avg CPU</div>
            <div className="text-2xl font-bold">
              {(clusters.reduce((s, c) => s + (c.avg_cpu || 0), 0) / clusters.length).toFixed(1)}%
            </div>
          </div>
          <div className="border rounded-lg p-3">
            <div className="text-xs text-muted-foreground">Active Alerts</div>
            <div className={cn("text-2xl font-bold", alerts && alerts.length > 0 ? "text-red-600" : "")}>
              {alerts?.length || 0}
            </div>
          </div>
        </div>
      )}

      {/* Cluster table */}
      {loadingClusters ? (
        <div className="flex items-center justify-center p-12 text-muted-foreground">
          Loading live metrics...
        </div>
      ) : !clusters || clusters.length === 0 ? (
        <div className="border rounded-lg p-12 text-center">
          <Activity className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
          <h3 className="text-lg font-medium mb-2">No Live Metrics Yet</h3>
          <p className="text-muted-foreground max-w-md mx-auto">
            Deploy the OTel init script to your clusters to start collecting real-time metrics.
            See the policy template in <code>cluster_manager/otel/</code> for setup instructions.
          </p>
        </div>
      ) : (
        <>
          <ClusterTable
            clusters={clusters}
            onSelect={handleClusterSelect}
            selectedId={selectedCluster}
          />
          {selectedCluster && <ClusterDetail clusterId={selectedCluster} />}
        </>
      )}
    </div>
  );
}

export const Route = createFileRoute("/_sidebar/live-metrics")({
  component: LiveMetricsPage,
});
