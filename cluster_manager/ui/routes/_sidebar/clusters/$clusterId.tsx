import { createFileRoute, Link, useRouter } from "@tanstack/react-router";
import {
  Activity,
  AlertCircle,
  ArrowLeft,
  Calendar,
  ChevronDown,
  ChevronRight,
  Clock,
  Cpu,
  ExternalLink,
  HardDrive,
  Loader2,
  MemoryStick,
  Network,
  Play,
  RefreshCw,
  Settings,
  Shield,
  Square,
  Tag,
  Users,
} from "lucide-react";
import { useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { toast } from "sonner";

import {
  LiveNodeMetric,
  useCluster,
  useClusterEvents,
  useClusterMetrics,
  useLiveClusterHistory,
  useStartCluster,
  useStopCluster,
} from "@/lib/api";
import { cn, formatDateTime, formatDuration, formatNumber } from "@/lib/utils";

const stateColors: Record<string, { bg: string; text: string }> = {
  RUNNING: { bg: "bg-green-100 dark:bg-green-900/30", text: "text-green-700 dark:text-green-400" },
  PENDING: { bg: "bg-yellow-100 dark:bg-yellow-900/30", text: "text-yellow-700 dark:text-yellow-400" },
  RESTARTING: { bg: "bg-blue-100 dark:bg-blue-900/30", text: "text-blue-700 dark:text-blue-400" },
  RESIZING: { bg: "bg-blue-100 dark:bg-blue-900/30", text: "text-blue-700 dark:text-blue-400" },
  TERMINATING: { bg: "bg-orange-100 dark:bg-orange-900/30", text: "text-orange-700 dark:text-orange-400" },
  TERMINATED: { bg: "bg-gray-100 dark:bg-gray-800", text: "text-gray-600 dark:text-gray-400" },
  ERROR: { bg: "bg-red-100 dark:bg-red-900/30", text: "text-red-700 dark:text-red-400" },
};

function InfoRow({
  icon: Icon,
  label,
  value,
  link,
}: {
  icon: React.ElementType;
  label: string;
  value: React.ReactNode;
  link?: string;
}) {
  const content = (
    <div className="flex items-start gap-3 py-2">
      <Icon className="h-4 w-4 text-muted-foreground mt-0.5" />
      <div className="flex-1 min-w-0">
        <p className="text-sm text-muted-foreground">{label}</p>
        <p className="font-medium truncate">{value || "-"}</p>
      </div>
      {link && <ExternalLink className="h-4 w-4 text-muted-foreground" />}
    </div>
  );

  if (link) {
    return (
      <a href={link} target="_blank" rel="noopener noreferrer" className="hover:bg-muted rounded-lg px-2 -mx-2">
        {content}
      </a>
    );
  }

  return <div className="px-2 -mx-2">{content}</div>;
}

const TIME_RANGES = [
  { label: "15m", minutes: 15 },
  { label: "1h", minutes: 60 },
  { label: "6h", minutes: 360 },
] as const;

function formatTime(timestamp: string) {
  return new Date(timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function LiveMetricsSection({ clusterId, workspaceUrl }: { clusterId: string; workspaceUrl?: string }) {
  const [minutes, setMinutes] = useState(60);
  const { data, isLoading } = useClusterMetrics(clusterId, minutes, workspaceUrl);

  if (isLoading) {
    return (
      <div className="bg-card rounded-lg border p-6">
        <div className="flex items-center gap-2">
          <Loader2 className="h-4 w-4 animate-spin" />
          <span className="text-sm text-muted-foreground">Loading metrics...</span>
        </div>
      </div>
    );
  }

  if (!data || data.time_series.length === 0) {
    return (
      <div className="bg-card rounded-lg border p-6">
        <h2 className="text-lg font-semibold mb-2">Live Metrics</h2>
        <p className="text-sm text-muted-foreground">
          No metrics available. Data appears once the cluster has been running for a few minutes.
        </p>
      </div>
    );
  }

  return (
    <div className="bg-card rounded-lg border p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Live Metrics</h2>
        <div className="flex gap-1">
          {TIME_RANGES.map((r) => (
            <button
              key={r.minutes}
              onClick={() => setMinutes(r.minutes)}
              className={cn(
                "px-3 py-1 text-sm rounded-md transition-colors",
                minutes === r.minutes
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted hover:bg-muted/80 text-muted-foreground"
              )}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {/* CPU Chart */}
      <div>
        <h3 className="text-sm font-medium text-muted-foreground mb-2 flex items-center gap-1.5">
          <Cpu size={14} /> CPU Usage (%)
        </h3>
        <ResponsiveContainer width="100%" height={180}>
          <AreaChart data={data.time_series}>
            <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
            <XAxis
              dataKey="timestamp"
              tickFormatter={formatTime}
              tick={{ fontSize: 11 }}
              className="text-muted-foreground"
            />
            <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} unit="%" />
            <Tooltip
              labelFormatter={formatTime}
              formatter={(value: number, name: string) => [`${value.toFixed(1)}%`, name]}
            />
            <Area
              type="monotone"
              dataKey="cpu_system_percent"
              stackId="cpu"
              fill="#f97316"
              stroke="#f97316"
              fillOpacity={0.4}
              name="System"
            />
            <Area
              type="monotone"
              dataKey="cpu_user_percent"
              stackId="cpu"
              fill="#3b82f6"
              stroke="#3b82f6"
              fillOpacity={0.4}
              name="User"
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {/* Memory Chart */}
      <div>
        <h3 className="text-sm font-medium text-muted-foreground mb-2 flex items-center gap-1.5">
          <MemoryStick size={14} /> Memory Usage (%)
        </h3>
        <ResponsiveContainer width="100%" height={180}>
          <AreaChart data={data.time_series}>
            <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
            <XAxis
              dataKey="timestamp"
              tickFormatter={formatTime}
              tick={{ fontSize: 11 }}
              className="text-muted-foreground"
            />
            <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} unit="%" />
            <Tooltip
              labelFormatter={formatTime}
              formatter={(value: number) => [`${value.toFixed(1)}%`, "Memory"]}
            />
            <Area
              type="monotone"
              dataKey="memory_percent"
              fill="#8b5cf6"
              stroke="#8b5cf6"
              fillOpacity={0.3}
              name="Memory"
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {/* Node Table */}
      {data.current_nodes.length > 0 && (() => {
        const sorted = [...data.current_nodes].sort((a, b) =>
          (b.is_driver ? 1 : 0) - (a.is_driver ? 1 : 0)
        );
        let wIdx = 0;
        return (
        <div>
          <h3 className="text-sm font-medium text-muted-foreground mb-2">
            Current Nodes ({sorted.length})
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b text-left">
                  <th className="py-2 pr-4 font-medium">Role</th>
                  <th className="py-2 pr-4 font-medium">Node Type</th>
                  <th className="py-2 pr-4 font-medium text-right">CPU %</th>
                  <th className="py-2 pr-4 font-medium text-right">Mem %</th>
                  <th className="py-2 font-medium text-right">Net Out</th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((node) => {
                  if (!node.is_driver) wIdx++;
                  const label = node.is_driver ? "Driver" : `Worker ${wIdx}`;
                  return (
                  <tr key={node.instance_id} className="border-b last:border-0">
                    <td className="py-2 pr-4">
                      <span className={cn(
                        "px-2 py-0.5 rounded text-xs font-medium",
                        node.is_driver
                          ? "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400"
                          : "bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-400"
                      )}>
                        {label}
                      </span>
                    </td>
                    <td className="py-2 pr-4 font-mono text-xs">{node.node_type}</td>
                    <td className="py-2 pr-4 text-right">
                      <span className={cn(
                        node.cpu_percent > 80 ? "text-red-600 dark:text-red-400" :
                        node.cpu_percent > 50 ? "text-yellow-600 dark:text-yellow-400" :
                        "text-green-600 dark:text-green-400"
                      )}>
                        {node.cpu_percent.toFixed(1)}%
                      </span>
                    </td>
                    <td className="py-2 pr-4 text-right">
                      <span className={cn(
                        node.memory_percent > 85 ? "text-red-600 dark:text-red-400" :
                        node.memory_percent > 60 ? "text-yellow-600 dark:text-yellow-400" :
                        "text-green-600 dark:text-green-400"
                      )}>
                        {node.memory_percent.toFixed(1)}%
                      </span>
                    </td>
                    <td className="py-2 text-right text-muted-foreground">
                      {formatBytes(node.network_sent_bytes)}
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
        );
      })()}

      <ExpandedMetricsSection clusterId={clusterId} minutes={minutes} />
    </div>
  );
}

interface AggregatedPoint {
  timestamp: string;
  disk_used_percent: number | null;
  disk_io_time_ms: number | null;
  disk_ops_read: number | null;
  disk_ops_write: number | null;
  network_sent_bytes: number | null;
  network_received_bytes: number | null;
  network_errors: number | null;
  network_drops: number | null;
  load_1m: number | null;
  load_5m: number | null;
  load_15m: number | null;
  mem_swap_percent: number | null;
  paging_in: number | null;
  paging_out: number | null;
  process_count: number | null;
  inodes_used_percent: number | null;
}

function aggregateLiveMetrics(rows: LiveNodeMetric[]): AggregatedPoint[] {
  const buckets = new Map<string, LiveNodeMetric[]>();
  for (const row of rows) {
    const key = new Date(row.ts).toISOString().slice(0, 16);
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key)!.push(row);
  }

  const sorted = [...buckets.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  return sorted.map(([ts, nodes]) => {
    const avg = (field: keyof LiveNodeMetric) => {
      const vals = nodes.map((r) => r[field] as number | null).filter((v) => v != null);
      return vals.length > 0 ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
    };
    const sum = (field: keyof LiveNodeMetric) => {
      const vals = nodes.map((r) => r[field] as number | null).filter((v) => v != null);
      return vals.length > 0 ? vals.reduce((a, b) => a + b, 0) : null;
    };

    return {
      timestamp: ts,
      disk_used_percent: avg("disk_used_percent"),
      disk_io_time_ms: avg("disk_io_time_ms"),
      disk_ops_read: sum("disk_ops_read"),
      disk_ops_write: sum("disk_ops_write"),
      network_sent_bytes: sum("network_sent_bytes"),
      network_received_bytes: sum("network_received_bytes"),
      network_errors: sum("network_errors"),
      network_drops: sum("network_drops"),
      load_1m: avg("load_1m"),
      load_5m: avg("load_5m"),
      load_15m: avg("load_15m"),
      mem_swap_percent: avg("mem_swap_percent"),
      paging_in: sum("paging_in"),
      paging_out: sum("paging_out"),
      process_count: sum("process_count"),
      inodes_used_percent: avg("inodes_used_percent"),
    };
  });
}

function CollapsibleSection({
  title,
  icon: Icon,
  open,
  onToggle,
  children,
}: {
  title: string;
  icon: React.ElementType;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="border-t pt-4">
      <button
        onClick={onToggle}
        className="flex items-center gap-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors w-full text-left"
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <Icon size={14} />
        {title}
      </button>
      {open && <div className="mt-3 space-y-4">{children}</div>}
    </div>
  );
}

function ExpandedMetricsSection({ clusterId, minutes }: { clusterId: string; minutes: number }) {
  const [diskOpen, setDiskOpen] = useState(false);
  const [networkOpen, setNetworkOpen] = useState(false);
  const [systemOpen, setSystemOpen] = useState(false);

  const { data: rawData } = useLiveClusterHistory(clusterId, minutes);

  const data = rawData && rawData.length > 0 ? aggregateLiveMetrics(rawData) : [];
  const hasData = data.length > 0;

  return (
    <div className="space-y-2">
      <CollapsibleSection title="Disk" icon={HardDrive} open={diskOpen} onToggle={() => setDiskOpen(!diskOpen)}>
        {!hasData ? (
          <p className="text-sm text-muted-foreground">No OTel disk data available.</p>
        ) : (
          <>
            <div>
              <h4 className="text-xs text-muted-foreground mb-1">Disk Usage (%)</h4>
              <ResponsiveContainer width="100%" height={140}>
                <AreaChart data={data}>
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                  <XAxis dataKey="timestamp" tickFormatter={formatTime} tick={{ fontSize: 10 }} />
                  <YAxis domain={[0, 100]} tick={{ fontSize: 10 }} unit="%" />
                  <Tooltip labelFormatter={formatTime} formatter={(v: number) => [`${v.toFixed(1)}%`]} />
                  <Area type="monotone" dataKey="disk_used_percent" fill="#ef4444" stroke="#ef4444" fillOpacity={0.3} name="Disk %" />
                </AreaChart>
              </ResponsiveContainer>
            </div>
            <div>
              <h4 className="text-xs text-muted-foreground mb-1">Disk I/O (ops)</h4>
              <ResponsiveContainer width="100%" height={140}>
                <LineChart data={data}>
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                  <XAxis dataKey="timestamp" tickFormatter={formatTime} tick={{ fontSize: 10 }} />
                  <YAxis tick={{ fontSize: 10 }} />
                  <Tooltip labelFormatter={formatTime} />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Line type="monotone" dataKey="disk_ops_read" stroke="#3b82f6" dot={false} name="Read ops" />
                  <Line type="monotone" dataKey="disk_ops_write" stroke="#f97316" dot={false} name="Write ops" />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </>
        )}
      </CollapsibleSection>

      <CollapsibleSection title="Network" icon={Network} open={networkOpen} onToggle={() => setNetworkOpen(!networkOpen)}>
        {!hasData ? (
          <p className="text-sm text-muted-foreground">No OTel network data available.</p>
        ) : (
          <>
            <div>
              <h4 className="text-xs text-muted-foreground mb-1">Network I/O (bytes)</h4>
              <ResponsiveContainer width="100%" height={140}>
                <AreaChart data={data}>
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                  <XAxis dataKey="timestamp" tickFormatter={formatTime} tick={{ fontSize: 10 }} />
                  <YAxis tick={{ fontSize: 10 }} tickFormatter={(v) => formatBytes(v)} />
                  <Tooltip labelFormatter={formatTime} formatter={(v: number) => [formatBytes(v)]} />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Area type="monotone" dataKey="network_sent_bytes" fill="#3b82f6" stroke="#3b82f6" fillOpacity={0.3} name="Sent" />
                  <Area type="monotone" dataKey="network_received_bytes" fill="#10b981" stroke="#10b981" fillOpacity={0.3} name="Received" />
                </AreaChart>
              </ResponsiveContainer>
            </div>
            <div>
              <h4 className="text-xs text-muted-foreground mb-1">Errors &amp; Drops</h4>
              <ResponsiveContainer width="100%" height={100}>
                <LineChart data={data}>
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                  <XAxis dataKey="timestamp" tickFormatter={formatTime} tick={{ fontSize: 10 }} />
                  <YAxis tick={{ fontSize: 10 }} />
                  <Tooltip labelFormatter={formatTime} />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Line type="monotone" dataKey="network_errors" stroke="#ef4444" dot={false} name="Errors" />
                  <Line type="monotone" dataKey="network_drops" stroke="#f59e0b" dot={false} name="Drops" />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </>
        )}
      </CollapsibleSection>

      <CollapsibleSection title="System" icon={Activity} open={systemOpen} onToggle={() => setSystemOpen(!systemOpen)}>
        {!hasData ? (
          <p className="text-sm text-muted-foreground">No OTel system data available.</p>
        ) : (
          <>
            <div>
              <h4 className="text-xs text-muted-foreground mb-1">Load Average</h4>
              <ResponsiveContainer width="100%" height={140}>
                <LineChart data={data}>
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                  <XAxis dataKey="timestamp" tickFormatter={formatTime} tick={{ fontSize: 10 }} />
                  <YAxis tick={{ fontSize: 10 }} />
                  <Tooltip labelFormatter={formatTime} />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Line type="monotone" dataKey="load_1m" stroke="#8b5cf6" dot={false} name="1m" />
                  <Line type="monotone" dataKey="load_5m" stroke="#6366f1" dot={false} name="5m" />
                  <Line type="monotone" dataKey="load_15m" stroke="#a855f7" dot={false} name="15m" />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <div>
              <h4 className="text-xs text-muted-foreground mb-1">Swap &amp; Paging</h4>
              <ResponsiveContainer width="100%" height={120}>
                <LineChart data={data}>
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                  <XAxis dataKey="timestamp" tickFormatter={formatTime} tick={{ fontSize: 10 }} />
                  <YAxis tick={{ fontSize: 10 }} />
                  <Tooltip labelFormatter={formatTime} />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Line type="monotone" dataKey="mem_swap_percent" stroke="#ec4899" dot={false} name="Swap %" />
                  <Line type="monotone" dataKey="paging_in" stroke="#14b8a6" dot={false} name="Page In" />
                  <Line type="monotone" dataKey="paging_out" stroke="#f97316" dot={false} name="Page Out" />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <h4 className="text-xs text-muted-foreground mb-1">Processes</h4>
                <ResponsiveContainer width="100%" height={100}>
                  <LineChart data={data}>
                    <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                    <XAxis dataKey="timestamp" tickFormatter={formatTime} tick={{ fontSize: 10 }} />
                    <YAxis tick={{ fontSize: 10 }} />
                    <Tooltip labelFormatter={formatTime} />
                    <Line type="monotone" dataKey="process_count" stroke="#0ea5e9" dot={false} name="Count" />
                  </LineChart>
                </ResponsiveContainer>
              </div>
              <div>
                <h4 className="text-xs text-muted-foreground mb-1">Inodes Used (%)</h4>
                <ResponsiveContainer width="100%" height={100}>
                  <AreaChart data={data}>
                    <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                    <XAxis dataKey="timestamp" tickFormatter={formatTime} tick={{ fontSize: 10 }} />
                    <YAxis domain={[0, 100]} tick={{ fontSize: 10 }} unit="%" />
                    <Tooltip labelFormatter={formatTime} formatter={(v: number) => [`${v.toFixed(1)}%`]} />
                    <Area type="monotone" dataKey="inodes_used_percent" fill="#f59e0b" stroke="#f59e0b" fillOpacity={0.3} name="Inodes %" />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </div>
          </>
        )}
      </CollapsibleSection>
    </div>
  );
}

function ClusterDetailPage() {
  const { clusterId } = Route.useParams();
  const { workspace_url: workspaceUrl } = Route.useSearch();
  const router = useRouter();
  const { data: cluster, isLoading, error, refetch } = useCluster(clusterId, workspaceUrl);
  const { data: eventsData } = useClusterEvents(clusterId, 20, workspaceUrl);
  const startCluster = useStartCluster();
  const stopCluster = useStopCluster();

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  if (error || !cluster) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <AlertCircle className="h-12 w-12 text-destructive mb-4" />
        <h2 className="text-lg font-semibold mb-2">Cluster not found</h2>
        <p className="text-muted-foreground mb-4">{error?.message || "The cluster could not be loaded"}</p>
        <Link to="/clusters" className="flex items-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-lg">
          <ArrowLeft size={16} />
          Back to Clusters
        </Link>
      </div>
    );
  }

  const isRunning = cluster.state === "RUNNING";
  const isTerminated = cluster.state === "TERMINATED";
  const isTransitioning = ["PENDING", "RESTARTING", "RESIZING", "TERMINATING"].includes(cluster.state);
  const colors = stateColors[cluster.state] || stateColors.TERMINATED;

  const handleStart = () => {
    startCluster.mutate({ clusterId, workspaceUrl }, {
      onSuccess: (data) => toast.success(data.message),
      onError: (error) => toast.error(`Failed to start: ${error.message}`),
    });
  };

  const handleStop = () => {
    stopCluster.mutate({ clusterId, workspaceUrl }, {
      onSuccess: (data) => toast.success(data.message),
      onError: (error) => toast.error(`Failed to stop: ${error.message}`),
    });
  };

  const workersDisplay = cluster.autoscale
    ? `${cluster.autoscale.min_workers}-${cluster.autoscale.max_workers} (autoscale)`
    : `${cluster.num_workers ?? 0} (fixed)`;

  const allTags = { ...cluster.default_tags, ...cluster.custom_tags };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-4">
          <button
            onClick={() => router.history.back()}
            className="p-2 hover:bg-muted rounded-lg transition-colors"
            title="Go back"
          >
            <ArrowLeft size={20} />
          </button>
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-2xl font-bold">{cluster.cluster_name}</h1>
              <span className={cn("px-3 py-1 rounded-full text-sm font-medium", colors.bg, colors.text)}>
                {cluster.state}
              </span>
            </div>
            <p className="text-muted-foreground">
              {cluster.cluster_id} &bull; Created by {cluster.creator_user_name || "Unknown"}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => refetch()}
            className="p-2 hover:bg-muted rounded-lg transition-colors"
            title="Refresh"
          >
            <RefreshCw size={18} />
          </button>
        </div>
      </div>

      {/* Action Buttons */}
      <div className="flex gap-3">
        <button
          onClick={handleStart}
          disabled={!isTerminated || startCluster.isPending}
          className={cn(
            "flex items-center gap-2 px-4 py-2 rounded-lg font-medium transition-colors",
            isTerminated
              ? "bg-green-600 hover:bg-green-700 text-white"
              : "bg-muted text-muted-foreground cursor-not-allowed"
          )}
        >
          {startCluster.isPending ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
          Start Cluster
        </button>
        <button
          onClick={handleStop}
          disabled={!isRunning || isTransitioning || stopCluster.isPending}
          className={cn(
            "flex items-center gap-2 px-4 py-2 rounded-lg font-medium transition-colors",
            isRunning && !isTransitioning
              ? "bg-secondary hover:bg-secondary/80 text-secondary-foreground"
              : "bg-muted text-muted-foreground cursor-not-allowed"
          )}
        >
          {stopCluster.isPending ? <Loader2 size={16} className="animate-spin" /> : <Square size={16} />}
          Stop Cluster
        </button>
      </div>

      {/* State Message */}
      {cluster.state_message && (
        <div className="p-4 bg-muted rounded-lg">
          <p className="text-sm">{cluster.state_message}</p>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Configuration */}
        <div className="lg:col-span-2 space-y-6">
          <div className="bg-card rounded-lg border p-6">
            <h2 className="text-lg font-semibold mb-4">Configuration</h2>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <InfoRow icon={Users} label="Workers" value={workersDisplay} />
              <InfoRow icon={Cpu} label="Node Type" value={cluster.node_type_id} />
              <InfoRow icon={Cpu} label="Driver Type" value={cluster.driver_node_type_id || cluster.node_type_id} />
              <InfoRow icon={Settings} label="Spark Version" value={cluster.spark_version} />
              <InfoRow icon={Shield} label="Data Security Mode" value={cluster.data_security_mode} />
              {cluster.policy_id && (
                <InfoRow
                  icon={Shield}
                  label="Policy"
                  value={cluster.policy_id}
                  link={`/policies/${cluster.policy_id}`}
                />
              )}
            </div>
          </div>

          {/* Live Metrics - only for running clusters */}
          {isRunning && <LiveMetricsSection clusterId={clusterId} workspaceUrl={workspaceUrl} />}

          {/* Timing */}
          <div className="bg-card rounded-lg border p-6">
            <h2 className="text-lg font-semibold mb-4">Timing</h2>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <InfoRow
                icon={Calendar}
                label="Start Time"
                value={cluster.start_time ? formatDateTime(cluster.start_time) : "-"}
              />
              <InfoRow
                icon={Clock}
                label="Uptime"
                value={cluster.uptime_minutes > 0 ? formatDuration(cluster.uptime_minutes) : "-"}
              />
              <InfoRow
                icon={Calendar}
                label="Last Activity"
                value={cluster.last_activity_time ? formatDateTime(cluster.last_activity_time) : "-"}
              />
              {cluster.terminated_time && (
                <InfoRow
                  icon={Calendar}
                  label="Terminated Time"
                  value={formatDateTime(cluster.terminated_time)}
                />
              )}
            </div>
            {cluster.termination_reason && (
              <div className="mt-4 p-3 bg-muted rounded-lg">
                <p className="text-sm text-muted-foreground">Termination Reason</p>
                <p className="text-sm">{cluster.termination_reason}</p>
              </div>
            )}
          </div>

          {/* Tags */}
          {Object.keys(allTags).length > 0 && (
            <div className="bg-card rounded-lg border p-6">
              <h2 className="text-lg font-semibold mb-4">Tags</h2>
              <div className="flex flex-wrap gap-2">
                {Object.entries(allTags).map(([key, value]) => (
                  <span
                    key={key}
                    className="inline-flex items-center gap-1.5 px-2.5 py-1 bg-muted rounded-full text-sm"
                  >
                    <Tag size={12} />
                    <span className="font-medium">{key}:</span>
                    <span className="text-muted-foreground">{value}</span>
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Sidebar */}
        <div className="space-y-6">
          {/* Metrics Card */}
          <div className="bg-card rounded-lg border p-6">
            <h2 className="text-lg font-semibold mb-4">Metrics</h2>
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground">Est. DBU/hour</span>
                <span className="font-medium">{formatNumber(cluster.estimated_dbu_per_hour)}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground">Source</span>
                <span className="font-medium">{cluster.cluster_source || "-"}</span>
              </div>
            </div>
          </div>

          {/* Recent Events */}
          <div className="bg-card rounded-lg border p-6">
            <h2 className="text-lg font-semibold mb-4">Recent Events</h2>
            {eventsData?.events && eventsData.events.length > 0 ? (
              <div className="space-y-3">
                {eventsData.events.slice(0, 10).map((event, idx) => (
                  <div key={idx} className="flex items-start gap-3 text-sm">
                    <div className="w-2 h-2 rounded-full bg-primary mt-1.5 flex-shrink-0" />
                    <div className="flex-1 min-w-0">
                      <p className="font-medium">{event.event_type}</p>
                      <p className="text-xs text-muted-foreground">
                        {formatDateTime(event.timestamp)}
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">No recent events</p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export const Route = createFileRoute("/_sidebar/clusters/$clusterId")({
  component: ClusterDetailPage,
  validateSearch: (search: Record<string, unknown>): { workspace_url?: string } => ({
    workspace_url: (search.workspace_url as string) || undefined,
  }),
});
