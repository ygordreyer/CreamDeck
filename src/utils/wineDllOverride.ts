import type { InstallRecord } from "./backend";

export function getAppLaunchOptions(appId: number): Promise<string> {
  return new Promise((resolve, reject) => {
    let settled = false;
    let registration: { unregister(): void } | undefined;
    const timer = setTimeout(() => finish(undefined), 5000);
    function finish(value: string | undefined) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      registration?.unregister();
      if (value === undefined) reject(new Error("Launch options unavailable or timed out. Use Steam Properties manually"));
      else resolve(value);
    }
    try {
      registration = SteamClient.Apps.RegisterForAppDetails(appId, (details) =>
        finish(details ? (details.strLaunchOptions ?? "") : undefined));
      // Steam can invoke the callback before registration returns.
      if (settled) registration.unregister();
    } catch (error) {
      settled = true;
      clearTimeout(timer);
      registration?.unregister();
      reject(error);
    }
  });
}

export function mergeWineDllOverride(original: string, proxy: string): string {
  if (!["version", "winmm", "winhttp"].includes(proxy)) throw new Error("Invalid proxy");
  if (original.includes("WINEDLLOVERRIDES") || (original !== "" && original !== "%command%" && !/^[A-Za-z0-9_./~-]+ %command%$/.test(original))) {
    throw new Error("Complex launch options. Set the Wine override by hand");
  }
  return `WINEDLLOVERRIDES="${proxy}=n,b" ${original || "%command%"}`;
}

export async function changeLaunchOptions(appId: number, record: InstallRecord, restore: boolean,
  acknowledge?: () => Promise<void>): Promise<string> {
  if (record.app_id !== String(appId)) throw new Error("Install record belongs to a different game");
  if (record.mode !== "proxy") {
    if (restore) await acknowledge?.();
    return "";
  }
  if (record.set_launch_options === null) {
    return restore ? "Restore your saved original launch options manually in Steam Properties"
      : `Launch options left untouched. Set WINEDLLOVERRIDES="${record.proxy_name}=n,b" manually in Steam Properties`;
  }
  const expected = restore ? record.set_launch_options : record.original_launch_options;
  const desired = restore ? record.original_launch_options : record.set_launch_options;
  if (!restore && mergeWineDllOverride(record.original_launch_options, record.proxy_name) !== desired) {
    throw new Error("Invalid recorded launch options");
  }
  const current = await getAppLaunchOptions(appId);
  if (current === desired) {
    if (restore) await acknowledge?.();
    return "Launch options already match";
  }
  if (current !== expected) return "Launch options changed; left untouched. Restore the original manually in Steam Properties";
  SteamClient.Apps.SetAppLaunchOptions(appId, desired);
  const after = await getAppLaunchOptions(appId);
  if (after !== desired) throw new Error("Steam did not confirm the launch-option change. Check Steam Properties");
  if (restore) await acknowledge?.();
  return restore ? "Original launch options restored" : "Wine launch override applied";
}
