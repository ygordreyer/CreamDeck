import { ButtonItem, Dropdown, Focusable, PanelSection, PanelSectionRow, ToggleField, useParams } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import { acknowledgeLaunchRestore, allowGame, checked, GamePage, getGamePage, installSmokeAPI, resolveLaunchByHand, uninstallSmokeAPI } from "../utils/backend";
import { pageAppId } from "../utils/route";
import { changeLaunchOptions, getAppLaunchOptions } from "../utils/wineDllOverride";

// Keyed on the app id so every game gets fresh page state.
export default function CreamDeckRoute() {
  const params = typeof useParams === "function" ? useParams<{ appid?: string }>() : undefined;
  const appId = pageAppId(params?.appid, window.location.pathname);
  return <CreamDeckPage key={appId} appId={appId} />;
}

function CreamDeckPage({ appId }: { appId: string }) {
  const [page, setPage] = useState<GamePage>({ found: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [mode, setMode] = useState("direct");
  const [proxy, setProxy] = useState("version");
  const [manualLaunch, setManualLaunch] = useState(false);
  const [enabled, setEnabled] = useState<string[]>([]);
  const inFlight = useRef(false);
  const mounted = useRef(true);

  async function refresh() {
    const result = await getGamePage(appId);
    if (!result.found) throw new Error(result.error || "Installed game not found");
    if (mounted.current) {
      setPage(result);
      setEnabled(result.record?.enabled_dlc_ids || []);
    }
  }
  async function acknowledgeRestore() {
    const result = checked(await acknowledgeLaunchRestore(appId));
    setPage((old) => ({ ...old, record: result.record }));
  }
  async function run(action: () => Promise<void>) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      if (!appId) throw new Error("Invalid Steam app ID");
      await action();
    } catch (failure) {
      if (mounted.current) setError(String(failure));
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  useEffect(() => {
    mounted.current = true;
    void run(refresh);
    return () => { mounted.current = false; };
  }, [appId]);

  return <Focusable style={{ padding: "56px 32px", height: "100%", boxSizing: "border-box", overflowY: "auto" }}>
    <h1>CreamDeck Hardened</h1>
    <p>{page.name || `Steam app ${appId}`} | {page.status || "Status unavailable"}</p>
    {error && <p role="alert" style={{ color: "#ffb4b4" }}>{error}</p>}
    {message && <p role="status">{message}</p>}
    <PanelSection title="Recovery">
      <PanelSectionRow><ButtonItem disabled={busy} onClick={() => void run(refresh)}>Refresh status</ButtonItem></PanelSectionRow>
      <PanelSectionRow><ButtonItem disabled={busy} onClick={() => void run(async () => {
        const result = checked(await uninstallSmokeAPI(appId));
        if (result.record) {
          setPage((old) => ({ ...old, record: result.record }));
          setMessage((result.warnings || []).join("; "));
          const launchMessage = await changeLaunchOptions(Number(appId), result.record, true, acknowledgeRestore);
          setMessage([...(result.warnings || []), launchMessage || "Game files removed"].join("; "));
        }
        await refresh();
      })}>Remove / recover SmokeAPI</ButtonItem></PanelSectionRow>
      <p>Changed files are preserved. If reported, run Verify integrity in Steam. Recovery requires a valid settings record.</p>
      {page.record && (page.record.mode === "proxy" || page.record.launch_restore === "pending") && <p>Original launch options: <code>{JSON.stringify(page.record.original_launch_options)}</code></p>}
      {page.record?.launch_restore === "pending" && <>
        <p>Launch-option restoration is pending. The saved original is retained and reinstall is blocked.</p>
        <PanelSectionRow><ButtonItem disabled={busy} onClick={() => void run(async () => {
          if (page.record) setMessage(await changeLaunchOptions(Number(appId), page.record, true, acknowledgeRestore));
          await refresh();
        })}>Retry launch-option restoration</ButtonItem></PanelSectionRow>
        <PanelSectionRow><ButtonItem disabled={busy} onClick={() => void run(async () => {
          checked(await resolveLaunchByHand(appId, true));
          await refresh();
        })}>I restored the original launch options by hand</ButtonItem></PanelSectionRow>
      </>}
    </PanelSection>
    <PanelSection title="Owner allowlist">
      <p>Never use on online games or games with anti-cheat. Marker scans cannot prove a game is safe.</p>
      <PanelSectionRow><ToggleField label="I confirm this is an offline single-player game with no anti-cheat" checked={confirmed} onChange={setConfirmed} /></PanelSectionRow>
      <PanelSectionRow><ButtonItem disabled={busy || !confirmed || !page.found} onClick={() => void run(async () => {
        checked(await allowGame(appId, confirmed));
        await refresh();
      })}>{page.allowed ? "Game is allowlisted" : "Add this game to owner allowlist"}</ButtonItem></PanelSectionRow>
    </PanelSection>
    <PanelSection title="SmokeAPI">
      <p>Close the game first. One recorded game at a time. DLC files must already be installed.</p>
      <PanelSectionRow><Dropdown selectedOption={mode} rgOptions={[{ label: "Direct", data: "direct" }, { label: "Proxy", data: "proxy" }]} onChange={(x) => setMode(x.data)} /></PanelSectionRow>
      {mode === "proxy" && <PanelSectionRow><Dropdown selectedOption={proxy} rgOptions={["version", "winmm", "winhttp"].map((x) => ({ label: x, data: x }))} onChange={(x) => setProxy(x.data)} /></PanelSectionRow>}
      <p>Proxy mode supports empty options, %command%, or a simple path followed by %command%. Other syntax requires manual setup.</p>
      {mode === "proxy" && <PanelSectionRow><ToggleField label="For complex options, I will set the Wine override and restore the original manually" checked={manualLaunch} onChange={setManualLaunch} /></PanelSectionRow>}
      {(page.dlcs || []).map((dlc) => <PanelSectionRow key={dlc.id}><ToggleField label={dlc.name} checked={enabled.includes(dlc.id)} onChange={(on) => setEnabled((old) => on ? [...old, dlc.id] : old.filter((x) => x !== dlc.id))} /></PanelSectionRow>)}
      {!page.dlcs?.length && <p>No DLC metadata found. Installing with zero selected DLC is allowed.</p>}
      <PanelSectionRow><ButtonItem disabled={busy || !page.allowed || !page.found || page.record?.launch_restore === "pending" || (!!page.record && page.record.phase !== "removed")} onClick={() => void run(async () => {
        const original = await getAppLaunchOptions(Number(appId));
        const result = checked(await installSmokeAPI(appId, mode, proxy, enabled, original, manualLaunch));
        if (result.record) {
          setPage((old) => ({ ...old, record: result.record }));
          setMessage(await changeLaunchOptions(Number(appId), result.record, false) || "SmokeAPI installed");
        }
        await refresh();
      })}>{busy ? "Working..." : "Install SmokeAPI"}</ButtonItem></PanelSectionRow>
    </PanelSection>
  </Focusable>;
}
