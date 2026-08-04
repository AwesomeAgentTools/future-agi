import { useQuery } from "@tanstack/react-query";
import { apiPath } from "src/api/contracts/api-surface";
import axios from "src/utils/axios";

export const CAPABILITIES_QUERY_KEY = ["capabilities"];

export function useCapabilities() {
  return useQuery({
    queryKey: CAPABILITIES_QUERY_KEY,
    queryFn: () => axios.get(apiPath("/api/capabilities/")),
    select: (res) => res.data,
    staleTime: Infinity,
    retry: 1,
  });
}

/**
 * Convenience: is a single capability allowed for this deployment/org?
 * Backed by the same cached /api/capabilities/ query.
 *
 *   const { allowed, isLoading } = useFeatureAllowed("turing_models");
 */
export function useFeatureAllowed(featureId) {
  const { data, isLoading } = useCapabilities();
  return {
    allowed: data?.features?.[featureId]?.allowed === true,
    isLoading,
  };
}
