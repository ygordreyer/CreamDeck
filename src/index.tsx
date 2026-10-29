import { routerHook } from "@decky/api";
import { definePlugin, staticClasses } from "@decky/ui";
import { FaUnlock as PluginIcon } from "react-icons/fa";

import contextMenuPatch, { LibraryContextMenu } from "./patch";
import CreamDeckPage from "./views/CreamDeckPage";

const ROUTE = "/creamdeck-hardened/:appid";

function QuickAccessContent() {
  return <div style={{ padding: "1em" }}>Open an installed game's library menu and select CreamDeck Hardened. Offline single-player games with no anti-cheat only.</div>;
}

export default definePlugin(() => {
  const menuPatch = contextMenuPatch(LibraryContextMenu);

  try { routerHook.addRoute(ROUTE, CreamDeckPage, { exact: true }); }
  catch (error) { menuPatch?.unpatch(); throw error; }

  return {
    name: "CreamDeck Hardened",
    title: <div className={staticClasses.Title}>CreamDeck Hardened</div>,
    content: <QuickAccessContent />,
    icon: <PluginIcon />,
    onDismount() {
      try { routerHook.removeRoute(ROUTE); }
      finally { menuPatch?.unpatch(); }
    },
  };
});
