"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "./api";
import { SAFETY_REFRESH_MS } from "./hooks";
import { useToast } from "./toast";
import type { Alarm, AlarmDetail, AlarmState, Page } from "./types";

/** Open alarms = active + acknowledged (an acknowledged alarm is still open until it clears).
 * The filter is the state, not a time range: an alarm that has been open for a day is still
 * listed. */
export function useOpenAlarms(siteId: string) {
  const active = useQuery({
    queryKey: ["alarms", "active", siteId],
    queryFn: () => api.get<Page<Alarm>>(`/alarms?state=active&site_id=${siteId}&size=100`),
    refetchInterval: SAFETY_REFRESH_MS,
  });
  const acknowledged = useQuery({
    queryKey: ["alarms", "acknowledged", siteId],
    queryFn: () => api.get<Page<Alarm>>(`/alarms?state=acknowledged&site_id=${siteId}&size=100`),
    refetchInterval: SAFETY_REFRESH_MS,
  });
  const items = [...(active.data?.items ?? []), ...(acknowledged.data?.items ?? [])].sort((a, b) =>
    b.opened_at.localeCompare(a.opened_at),
  );
  return {
    items,
    isPending: active.isPending || acknowledged.isPending,
    isError: active.isError || acknowledged.isError,
    refetch: () => Promise.all([active.refetch(), acknowledged.refetch()]),
  };
}

export interface HistoryFilter {
  state: AlarmState | "";
  from: string; // yyyy-mm-dd (a day in the site's zone) or ""
  to: string;
  page: number;
}

export const HISTORY_PAGE_SIZE = 15;

export function useAlarmHistory(siteId: string, filter: HistoryFilter, bounds: { from?: string; to?: string }) {
  const params = new URLSearchParams({ site_id: siteId, size: String(HISTORY_PAGE_SIZE), page: String(filter.page) });
  if (filter.state) params.set("state", filter.state);
  if (bounds.from) params.set("from", bounds.from);
  if (bounds.to) params.set("to", bounds.to);
  return useQuery({
    queryKey: ["alarms", "history", siteId, params.toString()],
    queryFn: () => api.get<Page<Alarm>>(`/alarms?${params.toString()}`),
    placeholderData: (previous) => previous,
  });
}

export function useAlarmDetail(alarmId: string | null) {
  return useQuery({
    queryKey: ["alarm", alarmId],
    enabled: alarmId !== null,
    queryFn: () => api.get<AlarmDetail>(`/alarms/${alarmId}`),
  });
}

export function useAcknowledge() {
  const queryClient = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: (alarmId: string) => api.post<Alarm>(`/alarms/${alarmId}/ack`),
    onSuccess: (_alarm, alarmId) => {
      toast("success", "Alarm onaylandı.");
      void queryClient.invalidateQueries({ queryKey: ["alarms"] });
      void queryClient.invalidateQueries({ queryKey: ["alarm", alarmId] });
    },
    onError: (error, alarmId) => {
      if (error instanceof ApiError && error.status === 409) {
        toast("info", "Alarm başkası tarafından onaylandı ya da kapandı; liste yenilendi.");
      } else if (error instanceof ApiError && error.status === 403) {
        toast("warning", "Alarmı onaylama yetkiniz yok.");
      } else {
        toast("warning", "Alarm onaylanamadı. Tekrar deneyin.");
      }
      void queryClient.invalidateQueries({ queryKey: ["alarms"] });
      void queryClient.invalidateQueries({ queryKey: ["alarm", alarmId] });
    },
  });
}
