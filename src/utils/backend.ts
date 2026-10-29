import { callable } from "@decky/api";

export interface InstallRecord {
  app_id: string;
  phase: "installing" | "installed" | "removing" | "removed";
  launch_restore: "pending" | "acknowledged" | "manual";
  mode: "direct" | "proxy";
  proxy_name: string;
  original_launch_options: string;
  set_launch_options: string | null;
  enabled_dlc_ids: string[];
}
export interface GamePage {
  found: boolean;
  name?: string;
  status?: string;
  allowed?: boolean;
  record?: InstallRecord | null;
  dlcs?: { id: string; name: string }[];
  error?: string;
}
export interface OperationResult {
  success: boolean;
  error?: string;
  record?: InstallRecord;
  warnings?: string[];
}
export const getGamePage = callable<[string], GamePage>("get_game_page");
export const allowGame = callable<[string, boolean], OperationResult>("allow_game");
export const installSmokeAPI = callable<[string, string, string, string[], string, boolean], OperationResult>("install_smokeapi");
export const uninstallSmokeAPI = callable<[string], OperationResult>("uninstall_smokeapi");
export const acknowledgeLaunchRestore = callable<[string], OperationResult>("acknowledge_launch_restore");
export const resolveLaunchByHand = callable<[string, boolean], OperationResult>("resolve_launch_by_hand");

export function checked(result: OperationResult): OperationResult {
  if (!result.success) throw new Error(result.error || "Operation refused");
  return result;
}
