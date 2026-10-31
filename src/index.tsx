import { routerHook } from "@decky/api";
import { ButtonItem, definePlugin, Navigation, Router, staticClasses } from "@decky/ui";
import { useEffect, useState } from "react";
import { FaUnlock as PluginIcon } from "react-icons/fa";

import contextMenuPatch, { LibraryContextMenu, menuPatchAvailable, subscribeMenuPatchStatus } from "./patch";
import CreamDeckPage from "./views/CreamDeckPage";

const ROUTE = "/creamdeck-hardened/:appid";

function QuickAccessContent() {
  const [available, setAvailable] = useState(menuPatchAvailable());
  const [help, setHelp] = useState(false);
  useEffect(() => subscribeMenuPatchStatus(setAvailable), []);
  const current = window.location.pathname.match(/\/(?:library\/app|creamdeck-hardened)\/([1-9][0-9]{0,9})\/?$/)?.[1];
  let running: string | undefined;
  try { running = Router?.MainRunningApp?.appid; } catch { /* Steam may not expose the running-app store. */ }
  const appId = current || (/^[1-9][0-9]{0,9}$/.test(String(running)) ? String(running) : undefined);
  return <div style={{ padding: "1em", overflowWrap: "anywhere" }}>
    <ButtonItem disabled={!appId} onClick={() => {
      if (appId) { Navigation.CloseSideMenus(); Navigation.Navigate(`/creamdeck-hardened/${appId}`); }
    }}>Open game</ButtonItem>
    {!appId && <p>Select or run a game</p>}
    {!available && <p role="status">Library menu unavailable</p>}
    <ButtonItem onClick={() => setHelp((old) => !old)}>(?)</ButtonItem>
    {help && <p>Open the selected library game or the running game. Offline single-player games with no anti-cheat only.</p>}
  </div>;
}

export default definePlugin(() => {
  let menuPatch: ReturnType<typeof contextMenuPatch>;
  try { menuPatch = contextMenuPatch(LibraryContextMenu); } catch { /* QAM remains available. */ }
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
