import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { ClusterLiveStatus, useLiveActiveClusters } from "./api";

interface MonitoredClustersContextValue {
  clusters: ClusterLiveStatus[];
  isLoading: boolean;
  selectedIds: string[];
  isAllSelected: boolean;
  hasSelection: boolean;
  toggleCluster: (id: string) => void;
  selectAll: () => void;
  clearSelection: () => void;
  setSelectedIds: (ids: string[]) => void;
}

const MonitoredClustersContext = createContext<MonitoredClustersContextValue | null>(null);

export function MonitoredClustersProvider({ children }: { children: ReactNode }) {
  const { data: clusters = [], isLoading } = useLiveActiveClusters();
  const [selectedIds, setSelectedIds] = useState<string[] | null>(null);

  const allIds = useMemo(() => clusters.map((c) => c.cluster_id), [clusters]);

  // null means "all selected" (default state)
  const effectiveIds = selectedIds ?? allIds;
  const isAllSelected = selectedIds === null || selectedIds.length === allIds.length;
  const hasSelection = selectedIds !== null && selectedIds.length > 0 && selectedIds.length < allIds.length;

  const toggleCluster = useCallback(
    (id: string) => {
      setSelectedIds((prev) => {
        const current = prev ?? allIds;
        if (current.includes(id)) {
          return current.filter((x) => x !== id);
        }
        return [...current, id];
      });
    },
    [allIds]
  );

  const selectAll = useCallback(() => setSelectedIds(null), []);
  const clearSelection = useCallback(() => setSelectedIds([]), []);

  const value = useMemo(
    () => ({
      clusters,
      isLoading,
      selectedIds: effectiveIds,
      isAllSelected,
      hasSelection,
      toggleCluster,
      selectAll,
      clearSelection,
      setSelectedIds: (ids: string[]) => setSelectedIds(ids),
    }),
    [clusters, isLoading, effectiveIds, isAllSelected, hasSelection, toggleCluster, selectAll, clearSelection]
  );

  return (
    <MonitoredClustersContext.Provider value={value}>{children}</MonitoredClustersContext.Provider>
  );
}

export function useMonitoredClusters() {
  const ctx = useContext(MonitoredClustersContext);
  if (!ctx) {
    throw new Error("useMonitoredClusters must be used within MonitoredClustersProvider");
  }
  return ctx;
}
