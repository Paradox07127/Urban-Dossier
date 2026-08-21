import { useCallback, useEffect, useState } from 'react';

import type {
  ChartSpec,
  ComparisonDeltaMap,
  DetailPreviewResponse,
  RadiusMeters,
} from '../../types';


export interface CompareResponse {
  point_a: DetailPreviewResponse;
  point_b: DetailPreviewResponse;
  deltas: Record<string, number | null>;
  delta_map?: ComparisonDeltaMap;
  chart_specs?: Record<string, ChartSpec>;
}

function samePoint(
  a: DetailPreviewResponse['target'],
  b: DetailPreviewResponse['target'],
): boolean {
  return a.latitude === b.latitude && a.longitude === b.longitude;
}

export function useComparison(
  preview: DetailPreviewResponse | null,
  radiusM: RadiusMeters,
  priorityOrder: string[],
) {
  const [pinnedPreview, setPinnedPreview] = useState<DetailPreviewResponse | null>(null);
  const [pinnedTitle, setPinnedTitle] = useState('');
  const [serverComparison, setServerComparison] = useState<CompareResponse | null>(null);
  const [comparisonLoading, setComparisonLoading] = useState(false);
  const [comparisonError, setComparisonError] = useState<string | null>(null);
  const comparisonActive = Boolean(
    pinnedPreview?.target &&
      preview?.target &&
      !samePoint(pinnedPreview.target, preview.target),
  );

  useEffect(() => {
    setServerComparison(null);
    setComparisonError(null);
    setComparisonLoading(false);
    const pointA = pinnedPreview?.target;
    const pointB = preview?.target;
    if (!pointA || !pointB || samePoint(pointA, pointB)) return;

    const controller = new AbortController();
    setComparisonLoading(true);
    fetch('/api/compare-points', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: controller.signal,
      body: JSON.stringify({
        point_a: { latitude: pointA.latitude, longitude: pointA.longitude },
        point_b: { latitude: pointB.latitude, longitude: pointB.longitude },
        radius_m: radiusM,
        priority_order: priorityOrder,
        time_window_days: 365,
      }),
    })
      .then((response) =>
        response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)),
      )
      .then((data: CompareResponse) => {
        if (!controller.signal.aborted) setServerComparison(data);
      })
      .catch((reason) => {
        if (!controller.signal.aborted) {
          setServerComparison(null);
          setComparisonError(
            reason instanceof Error ? reason.message : 'Comparison request failed',
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setComparisonLoading(false);
      });
    return () => controller.abort();
  }, [pinnedPreview, preview, radiusM, priorityOrder]);

  const pin = useCallback((value: DetailPreviewResponse, title: string) => {
    setPinnedPreview(value);
    setPinnedTitle(title || 'Pinned');
  }, []);

  const clear = useCallback(() => {
    setPinnedPreview(null);
    setPinnedTitle('');
    setServerComparison(null);
    setComparisonLoading(false);
    setComparisonError(null);
  }, []);

  return {
    pinnedPreview,
    pinnedTitle,
    serverComparison,
    comparisonActive,
    comparisonLoading,
    comparisonError,
    pin,
    clear,
  };
}
