import { routerHook } from "@decky/api";
import { ButtonItem, definePlugin, Navigation, Router, staticClasses } from "@decky/ui";
import { useEffect, useState } from "react";
import { FaUnlock as PluginIcon } from "react-icons/fa";

import contextMenuPatch, { LibraryContextMenu, menuPatchAvailable, subscribeMenuPatchStatus } from "./patch";
import CreamDeckPage from "./views/CreamDeckPage";
import { getRecordedGames, RecordedGame } from "./utils/backend";

const ROUTE = "/creamdeck-hardened/:appid";

function QuickAccessContent() {
  const [available, setAvailable] = useState(menuPatchAvailable());
  const [help, setHelp] = useState(false);
  const [games, setGames] = useState<RecordedGame[]>([]);
  const [listStatus, setListStatus] = useState("Loading recorded games");
  useEffect(() => subscribeMenuPatchStatus(setAvailable), []);
  useEffect(() => {
    let mounted = true;
    void getRecordedGames().then((result) => {
      if (!mounted) return;
      if (result.error) { setListStatus("Recorded games unavailable"); return; }
      const records = result.games.filter((game) => /^[1-9][0-9]{0,9}$/.test(game.app_id)).map((game) => {
        const name = typeof game.name === "string" ? game.name.trim() : "";
        return { ...game, name: name && name.length <= 256 ? name : `Steam app ${game.app_id}`,
          state: typeof game.state === "string" ? game.state : "Unavailable" };
      });
      setGames(records);
      setListStatus(records.length ? "" : "No recorded games");
    }, () => { if (mounted) setListStatus("Recorded games unavailable"); });
    return () => { mounted = false; };
  }, []);
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
    {games.map((game) => <ButtonItem key={game.app_id} onClick={() => {
      Navigation.CloseSideMenus(); Navigation.Navigate(`/creamdeck-hardened/${game.app_id}`);
    }}>{game.name} ({game.state})</ButtonItem>)}
    {listStatus && <p role="status">{listStatus}</p>}
    <ButtonItem onClick={() => setHelp((old) => !old)}>(?)</ButtonItem>
    {help && <p>Open the selected library game, the running game, or a recorded game. Offline single-player games with no anti-cheat only.</p>}
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
