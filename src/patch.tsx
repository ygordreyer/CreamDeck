/**
 * Adapted from CheatDeck (https://github.com/SheffeyG/CheatDeck/blob/main/src/patch.tsx),
 * itself credited to decky-steamgriddb's contextMenuPatch.tsx. Same react-patching
 * technique, renamed for the "CreamDeck Hardened" menu item and its own route.
 */

import {
  afterPatch,
  type Export,
  fakeRenderComponent,
  findInReactTree,
  findInTree,
  findModuleByExport,
  MenuItem,
  Navigation,
  type Patch,
} from "@decky/ui";
import type { FC } from "react";

const MENU_ITEM_KEY = "creamdeck-hardened";
const isValidAppId = (appid: unknown): appid is number =>
  typeof appid === "number" && Number.isSafeInteger(appid) && appid > 0;
type MenuPatchStatus = "unseen" | "working" | "failed";
// A discovered menu that never renders is indistinguishable from a cold open.
let patchStatus: MenuPatchStatus = "unseen";
const statusListeners = new Set<(status: MenuPatchStatus) => void>();
export const menuPatchAvailable = () => patchStatus === "working";
export const menuPatchStatus = () => patchStatus;
export const reportMenuPatchFailure = () => setMenuPatchStatus("failed");
export function subscribeMenuPatchStatus(listener: (status: MenuPatchStatus) => void) {
  statusListeners.add(listener);
  return () => { statusListeners.delete(listener); };
}
function setMenuPatchStatus(status: MenuPatchStatus) {
  if (patchStatus === status) return;
  patchStatus = status;
  statusListeners.forEach((listener) => listener(status));
}
function menuNotSeen() {
  // Skipping an unrelated menu must not clear a failure before a successful splice.
  if (patchStatus !== "failed") setMenuPatchStatus("unseen");
}

// Always add before "Properties..."
const spliceMenuItem = (children: any[], appid: number) => {
  if (!isValidAppId(appid)) throw new Error("Invalid app id");
  const propertiesMenuItemIdx = children.findIndex((item) =>
    findInReactTree(item, (x) => x?.onSelected?.toString().includes("AppProperties")),
  );
  children.splice(
    propertiesMenuItemIdx < 0 ? children.length : propertiesMenuItemIdx,
    0,
    <MenuItem
      key={MENU_ITEM_KEY}
      onSelected={() => {
        Navigation.Navigate(`/creamdeck-hardened/${appid}`);
      }}
    >
      CreamDeck Hardened
    </MenuItem>,
  );
  setMenuPatchStatus("working");
};

// Check if correct menu by looking at the code of the onSelected function.
// Should be enough to ignore the screenshots and other menus.
const isOpeningAppContextMenu = (items: any[]) => {
  if (!items?.length) {
    return false;
  }
  return !!findInReactTree(items, (x) => x?.props?.onSelected?.toString().includes("launchSource"));
};

const canPatchAppMenu = (items: any[], appid: number | undefined): appid is number => {
  if (!isOpeningAppContextMenu(items) || !isValidAppId(appid)) {
    menuNotSeen();
    return false;
  }
  return true;
};

const handleItemDupes = (items: any[]) => {
  const existingIdx = items.findIndex((x: any) => x?.key === MENU_ITEM_KEY);
  if (existingIdx !== -1) items.splice(existingIdx, 1);
};

const patchMenuItems = (menuItems: any[], appid: number) => {
  let overviewAppid: number | undefined;
  let appAppid: number | undefined;
  // find the first menu component that has the correct appid, sometimes the one passed is cached from another context menu
  for (const item of menuItems) {
    const suppliedAppid = item?._owner?.pendingProps?.overview?.appid;
    if (suppliedAppid !== undefined) {
      if (!isValidAppId(suppliedAppid)) throw new Error("Invalid app id");
      if (overviewAppid === undefined && suppliedAppid !== appid) overviewAppid = suppliedAppid;
    }
  }
  findInTree(menuItems, (x) => {
    const suppliedAppid = x?.app?.appid;
    if (suppliedAppid !== undefined) {
      if (!isValidAppId(suppliedAppid)) throw new Error("Invalid app id");
      if (appAppid === undefined) appAppid = suppliedAppid;
    }
    // Validate every supplied identity before inserting, including those after the first match.
    return false;
  }, { walkable: ["props", "children"] });
  spliceMenuItem(menuItems, overviewAppid ?? appAppid ?? appid);
};

/**
 * Patches the game library context menu to add a "CreamDeck Hardened" entry.
 * @param LibraryContextMenu The game context menu.
 * @returns A patch object; call `.unpatch()` to remove it when the plugin dismounts.
 */
let activePatch: { unpatch: () => void } | undefined;
const contextMenuPatch = (LibraryContextMenu: any) => {
  if (activePatch) return activePatch;
  if (!LibraryContextMenu?.prototype) { reportMenuPatchFailure(); return undefined; }
  const nested: Patch[] = [];
  const patchedPrototypes = new WeakSet<object>();
  let currentAppId: number | undefined;
  let disposed = false;
  const patches: {
    outer?: Patch;
    inner?: Patch;
    unpatch: () => void;
  } = {
    unpatch: () => {
      return null;
    },
  };
  patches.outer = afterPatch(LibraryContextMenu.prototype, "render", (_: Record<string, unknown>[], component: any) => {
    try {
    let appid: unknown;
    if (component?._owner?.pendingProps?.overview) {
      appid = component._owner.pendingProps.overview.appid;
    } else {
      const foundApp = findInTree(component?.props?.children, (x) => x?.app?.appid, { walkable: ["props", "children"] });
      if (foundApp) {
        appid = foundApp.app.appid;
      }
    }
    if (!isValidAppId(appid)) {
      currentAppId = undefined;
      menuNotSeen();
      return component;
    }
    currentAppId = appid;

    if (!patches.inner) {
      patches.inner = afterPatch(component, "type", (_: any, ret: any) => {
        try {
        const prototype = ret?.type?.prototype;
        if (!prototype) { reportMenuPatchFailure(); return ret; }
        if (patchedPrototypes.has(prototype)) return ret;
        patchedPrototypes.add(prototype);
        nested.push(afterPatch(prototype, "render", (_: any, ret2: any) => {
          const menuItems = ret2?.props?.children?.[0];
          if (!Array.isArray(menuItems)) { reportMenuPatchFailure(); return ret2; }
          if (!canPatchAppMenu(menuItems, currentAppId)) return ret2;
          try {
            handleItemDupes(menuItems);
            patchMenuItems(menuItems, currentAppId);
          } catch (error) {
            currentAppId = undefined;
            reportMenuPatchFailure();
            return ret2;
          }
          return ret2;
        }));

        // when Steam decides to refresh the app overview
        nested.push(afterPatch(prototype, "shouldComponentUpdate", ([nextProps]: any, shouldUpdate: any) => {
          try {
            if (!isValidAppId(currentAppId)) { menuNotSeen(); return shouldUpdate; }
            if (!Array.isArray(nextProps?.children)) return shouldUpdate;
            if (!canPatchAppMenu(nextProps.children, currentAppId)) return shouldUpdate;
            handleItemDupes(nextProps.children);
            if (shouldUpdate === true) patchMenuItems(nextProps.children, currentAppId);
          } catch (error) {
            currentAppId = undefined;
            reportMenuPatchFailure();
            return shouldUpdate;
          }


          return shouldUpdate;
        }));
        return ret;
        } catch { reportMenuPatchFailure(); return ret; }
      });
    } else {
      if (Array.isArray(component?.props?.children) && canPatchAppMenu(component.props.children, currentAppId)) {
        try { handleItemDupes(component.props.children); spliceMenuItem(component.props.children, currentAppId); } catch { reportMenuPatchFailure(); }
      }
    }
    return component;
    } catch { reportMenuPatchFailure(); return component; }
  });
  patches.unpatch = () => {
    if (disposed) return;
    disposed = true;
    for (const patch of nested.splice(0).reverse()) { try { patch.unpatch(); } catch { /* Continue removing all handles. */ } }
    try { patches.inner?.unpatch(); } catch { /* Continue removing all handles. */ }
    try { patches.outer?.unpatch(); } finally { activePatch = undefined; setMenuPatchStatus("unseen"); }
  };
  activePatch = patches;
  return patches;
};

/**
 * Game context menu component, located via its module export (there's no
 * documented/stable public API for it, so this relies on locating the render
 * function by looking for known internal identifiers, same as CheatDeck/decky-steamgriddb).
 */
export const LibraryContextMenu = (() => {
  try {
    const module = findModuleByExport((e: Export) => e?.toString?.().includes("().LibraryContextMenu"));
    if (!module) return undefined;
    const sibling = Object.values(module).find((x) => x?.toString().includes("navigator:"));
    return sibling ? fakeRenderComponent(sibling as FC)?.type : undefined;
  } catch { return undefined; }
})();

export default contextMenuPatch;
