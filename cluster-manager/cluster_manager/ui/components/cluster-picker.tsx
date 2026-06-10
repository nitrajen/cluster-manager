import { useRef, useState } from "react";
import { Check, ChevronDown, Filter, Radio, Search, X } from "lucide-react";

import { useMonitoredClusters } from "@/lib/monitored-clusters-context";
import { cn } from "@/lib/utils";

export function ClusterPicker() {
  const {
    clusters,
    isLoading,
    selectedIds,
    isAllSelected,
    hasSelection,
    toggleCluster,
    selectAll,
    clearSelection,
  } = useMonitoredClusters();

  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const containerRef = useRef<HTMLDivElement>(null);

  if (isLoading || clusters.length === 0) return null;

  const filtered = search
    ? clusters.filter((c) => c.cluster_id.toLowerCase().includes(search.toLowerCase()))
    : clusters;

  const label = isAllSelected
    ? `All monitored (${clusters.length})`
    : hasSelection
      ? `${selectedIds.length} of ${clusters.length} clusters`
      : "No clusters selected";

  return (
    <div ref={containerRef} className="relative mb-4">
      {/* Trigger bar */}
      <div className="flex items-center gap-2 px-3 py-2 bg-muted/50 border rounded-lg">
        <Filter size={14} className="text-muted-foreground" />
        <button
          onClick={() => setOpen(!open)}
          className="flex items-center gap-2 text-sm font-medium hover:text-primary transition-colors"
        >
          <span>{label}</span>
          <ChevronDown size={14} className={cn("transition-transform", open && "rotate-180")} />
        </button>

        {hasSelection && (
          <button
            onClick={selectAll}
            className="ml-auto text-xs text-muted-foreground hover:text-primary flex items-center gap-1"
          >
            <X size={12} />
            Clear filter
          </button>
        )}
      </div>

      {/* Dropdown */}
      {open && (
        <>
          {/* Backdrop */}
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />

          <div className="absolute top-full left-0 mt-1 w-80 bg-popover border rounded-lg shadow-lg z-20 overflow-hidden">
            {/* Search */}
            <div className="p-2 border-b">
              <div className="flex items-center gap-2 px-2 py-1.5 bg-muted/50 rounded">
                <Search size={14} className="text-muted-foreground" />
                <input
                  type="text"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search clusters..."
                  className="flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
                  autoFocus
                />
              </div>
            </div>

            {/* Quick actions */}
            <div className="flex items-center gap-2 px-3 py-2 border-b">
              <button
                onClick={selectAll}
                className={cn(
                  "text-xs px-2 py-1 rounded transition-colors",
                  isAllSelected
                    ? "bg-primary text-primary-foreground"
                    : "bg-muted hover:bg-muted/80"
                )}
              >
                All
              </button>
              <button
                onClick={clearSelection}
                className={cn(
                  "text-xs px-2 py-1 rounded transition-colors",
                  selectedIds.length === 0
                    ? "bg-primary text-primary-foreground"
                    : "bg-muted hover:bg-muted/80"
                )}
              >
                None
              </button>
              <span className="ml-auto text-xs text-muted-foreground">
                {selectedIds.length} selected
              </span>
            </div>

            {/* Cluster list */}
            <div className="max-h-64 overflow-y-auto">
              {filtered.map((cluster) => {
                const isSelected = selectedIds.includes(cluster.cluster_id);
                return (
                  <button
                    key={cluster.cluster_id}
                    onClick={() => toggleCluster(cluster.cluster_id)}
                    className="w-full flex items-center gap-2 px-3 py-2 hover:bg-muted/50 transition-colors text-left"
                  >
                    <div
                      className={cn(
                        "w-4 h-4 border rounded flex items-center justify-center flex-shrink-0",
                        isSelected
                          ? "bg-primary border-primary text-primary-foreground"
                          : "border-muted-foreground/30"
                      )}
                    >
                      {isSelected && <Check size={10} />}
                    </div>
                    <span className="font-mono text-xs truncate flex-1">
                      {cluster.cluster_id}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      {cluster.node_count}n
                    </span>
                    <Radio
                      size={10}
                      className={cn(
                        cluster.is_stale
                          ? "text-yellow-500"
                          : "text-green-500 animate-pulse"
                      )}
                    />
                  </button>
                );
              })}
              {filtered.length === 0 && (
                <div className="px-3 py-4 text-center text-sm text-muted-foreground">
                  No clusters match "{search}"
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
