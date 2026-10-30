export const ROUTE_BASE = "/creamdeck-hardened";

// Game Mode serves routed pages under /routes, so the page path is /routes/creamdeck-hardened/<appid>.
export function appIdFromPath(pathname: string): string {
  return pathname.match(/^(?:\/routes)?\/creamdeck-hardened\/([1-9][0-9]{0,9})\/?$/)?.[1] || "";
}

// The router param updates when Steam swaps one CreamDeck page for another in place;
// window.location alone does not re-render the page. An invalid param never falls back.
export function pageAppId(param: unknown, pathname: string): string {
  return typeof param === "string" ? appIdFromPath(`${ROUTE_BASE}/${param}`) : appIdFromPath(pathname);
}
