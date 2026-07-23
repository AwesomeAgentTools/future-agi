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
