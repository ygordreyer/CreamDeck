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
let menuAvailable = false;
const statusListeners = new Set<(available: boolean) => void>();
export const menuPatchAvailable = () => menuAvailable;
export function subscribeMenuPatchStatus(listener: (available: boolean) => void) {
  statusListeners.add(listener);
  return () => { statusListeners.delete(listener); };
}
function setMenuAvailable(available: boolean) {
  if (menuAvailable === available) return;
  menuAvailable = available;
  statusListeners.forEach((listener) => listener(available));
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
  setMenuAvailable(true);
};

// Check if correct menu by looking at the code of the onSelected function.
// Should be enough to ignore the screenshots and other menus.
const isOpeningAppContextMenu = (items: any[]) => {
  if (!items?.length) {
    return false;
  }
  return !!findInReactTree(items, (x) => x?.props?.onSelected?.toString().includes("launchSource"));
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
  if (!LibraryContextMenu?.prototype) { setMenuAvailable(false); return undefined; }
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
      setMenuAvailable(false);
      return component;
    }
    currentAppId = appid;

    if (!patches.inner) {
      patches.inner = afterPatch(component, "type", (_: any, ret: any) => {
        try {
        const prototype = ret?.type?.prototype;
        if (!prototype) { setMenuAvailable(false); return ret; }
        if (patchedPrototypes.has(prototype)) return ret;
        patchedPrototypes.add(prototype);
        nested.push(afterPatch(prototype, "render", (_: any, ret2: any) => {
          const menuItems = ret2?.props?.children?.[0];
          if (!Array.isArray(menuItems)) { setMenuAvailable(false); return ret2; }
          if (!isOpeningAppContextMenu(menuItems) || !isValidAppId(currentAppId)) { setMenuAvailable(false); return ret2; }
          try {
            handleItemDupes(menuItems);
            patchMenuItems(menuItems, currentAppId);
          } catch (error) {
            currentAppId = undefined;
            setMenuAvailable(false);
            return ret2;
          }
          return ret2;
        }));

        // when Steam decides to refresh the app overview
        nested.push(afterPatch(prototype, "shouldComponentUpdate", ([nextProps]: any, shouldUpdate: any) => {
          try {
            if (!isValidAppId(currentAppId)) { setMenuAvailable(false); return shouldUpdate; }
            if (!Array.isArray(nextProps?.children)) return shouldUpdate;
            handleItemDupes(nextProps.children);
            if (shouldUpdate === true) patchMenuItems(nextProps.children, currentAppId);
          } catch (error) {
            currentAppId = undefined;
            setMenuAvailable(false);
            return shouldUpdate;
          }


          return shouldUpdate;
        }));
        return ret;
        } catch { setMenuAvailable(false); return ret; }
      });
    } else {
      if (isValidAppId(currentAppId) && Array.isArray(component?.props?.children)) {
        try { handleItemDupes(component.props.children); spliceMenuItem(component.props.children, currentAppId); } catch { setMenuAvailable(false); }
      }
    }
    return component;
    } catch { setMenuAvailable(false); return component; }
  });
  patches.unpatch = () => {
    if (disposed) return;
    disposed = true;
    for (const patch of nested.splice(0).reverse()) { try { patch.unpatch(); } catch { /* Continue removing all handles. */ } }
    try { patches.inner?.unpatch(); } catch { /* Continue removing all handles. */ }
    try { patches.outer?.unpatch(); } finally { activePatch = undefined; setMenuAvailable(false); }
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
