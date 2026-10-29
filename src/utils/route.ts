export const ROUTE_BASE = "/creamdeck-hardened";

// Game Mode serves routed pages under /routes, so the page path is /routes/creamdeck-hardened/<appid>.
export function appIdFromPath(pathname: string): string {
  return pathname.match(/^(?:\/routes)?\/creamdeck-hardened\/([1-9][0-9]{0,9})\/?$/)?.[1] || "";
}
