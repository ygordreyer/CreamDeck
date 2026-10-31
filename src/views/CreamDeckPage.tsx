import { ButtonItem, Dropdown, Focusable, PanelSection, PanelSectionRow, ToggleField, useParams } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import { acknowledgeLaunchRestore, acknowledgeLaunchSetup, allowGame, checked, DlcMetadata, GamePage, getGameMetadata, getGamePage, installSmokeAPI, InstallRecord, resolveLaunchByHand, saveDlcSelection, uninstallSmokeAPI } from "../utils/backend";
import { pageAppId } from "../utils/route";
import { changeLaunchOptions, getAppLaunchOptions } from "../utils/wineDllOverride";

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
  const [stale, setStale] = useState(false);
  const [metadata, setMetadata] = useState<DlcMetadata>({ state: "loading", dlcs: [] });
  const [help, setHelp] = useState(false);
  const inFlight = useRef(false);
  const mounted = useRef(true);
  const generation = useRef(0);

  async function refresh() {
    const request = ++generation.current;
    setStale(true);
    try {
      const result = await getGamePage(appId);
      if (!result.found) throw new Error(result.error || "Installed game not found");
      if (!mounted.current || request !== generation.current) return;
      setPage(result);
      setStale(false);
      setEnabled(result.record?.enabled_dlc_ids || []);
      if (result.record?.phase === "installed") setMode(result.record.mode);
      setProxy(result.record?.phase === "installed" ? result.record.proxy_name
        : (old) => result.available_proxies?.includes(old) ? old : result.available_proxies?.[0] || "");
      setMetadata({ state: "loading", dlcs: [] });
      // Metadata cannot hold local status, removal, or setup retry hostage.
      void getGameMetadata(appId).then((data) => {
        if (mounted.current && request === generation.current) setMetadata(data);
      }, () => {
        if (mounted.current && request === generation.current) setMetadata({ state: "unavailable", dlcs: [] });
      });
    } catch (failure) {
      if (mounted.current) setStale(true);
      throw failure;
    }
  }
  async function acknowledgeRestore() {
    const result = checked(await acknowledgeLaunchRestore(appId));
    if (mounted.current) setPage((old) => ({ ...old, record: result.record }));
  }
  async function applySetup(record: InstallRecord) {
    const details = await changeLaunchOptions(Number(appId), record, false, async () => {
      const result = checked(await acknowledgeLaunchSetup(appId, false));
      if (mounted.current) setPage((old) => ({ ...old, record: result.record }));
    });
    if (mounted.current) setMessage(details);
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
    return () => { mounted.current = false; generation.current++; };
  }, [appId]);

  const record = page.record;
  const active = !!record && record.phase !== "removed";
  const installed = record?.phase === "installed";
  const setupPending = installed && record.mode === "proxy" && record.launch_setup === "pending";
  const restorePending = record?.launch_restore === "pending";
  const locked = busy || stale || !page.found;
  const editable = !locked && (!active || (installed && page.status === "installed" && !record?.config_update));
  const unsaved = installed && [...enabled].sort().join(",") !== [...(record.enabled_dlc_ids || [])].sort().join(",");
  const dlcs = new Map((metadata.state === "ready" ? metadata.dlcs : []).map((dlc) => [dlc.id, dlc]));
  for (const id of new Set([...(record?.enabled_dlc_ids || []), ...enabled])) {
    if (!dlcs.has(id)) dlcs.set(id, { id, name: `DLC ${id}` });
  }
  const reason = busy ? "Working" : stale ? "Refresh status first" : !page.found ? "Game unavailable"
    : restorePending ? "Restore launch options first" : active ? "Remove existing install first"
    : !page.allowed ? "Allow this game first" : mode === "proxy" && !page.available_proxies?.includes(proxy) ? "No launch helper available" : "";
  const status = stale ? (page.found ? "Status stale" : error.toLowerCase().includes("storage unavailable") ? "Storage unavailable" : "Status unavailable")
    : !page.found ? "Loading status" : restorePending ? "Restoration pending" : setupPending ? "Setup pending"
    : record?.config_update ? "DLC update interrupted" : installed && page.status !== "installed" ? "Recovery needed"
    : installed ? "Installed" : active ? "Recovery needed" : "Not installed";

  return <Focusable style={{ padding: "56px 24px", height: "100%", boxSizing: "border-box", overflowY: "auto", overflowWrap: "anywhere" }}>
    <h1>{page.name || `Steam app ${appId}`}</h1>
    <p role="status">{status}</p>
    {error && <p role="alert">Action failed. See (?)</p>}
    {unsaved && <p role="status">Unsaved selection</p>}
    <ButtonItem onClick={() => setHelp((old) => !old)}>(?)</ButtonItem>
    {help && <Focusable role="dialog" aria-label="CreamDeck help" style={{ overflowWrap: "anywhere", whiteSpace: "normal" }}>
      <h2>Help</h2>
      <p>Close the game before changing files. Use only offline single-player games with no anti-cheat. Marker scans cannot prove a game is safe. DLC files must already be installed.</p>
      <p>DLC switches edit the selection. Save applies it to this game only. Refresh discards unsaved edits. Metadata may be unavailable; saved DLC IDs remain available.</p>
      <p>Replace game files (direct mode) replaces the Steam API DLL. Add a launch helper (proxy mode) needs a free DLL name in the top game folder and a Wine launch override. Helper 1 is version.dll, Helper 2 is winmm.dll, and Helper 3 is winhttp.dll. Simple options can be applied automatically. For complex options, enable manual setup and confirm after setting the override in Steam Properties.</p>
      <p>Finish launch setup retries Steam options without reinstalling files. Owner edits are preserved. Confirm manual setup only after applying the override yourself.</p>
      <p>Remove preserves changed files. When recovery reports a mismatch, run Verify integrity in Steam. Confirm manual restore only after restoring the saved original in Steam Properties.</p>
      {record && <p>Saved original launch options: <code style={{ overflowWrap: "anywhere", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{JSON.stringify(record.original_launch_options)}</code></p>}
      {record?.mode === "proxy" && <p>Required override: <code style={{ overflowWrap: "anywhere" }}>{`WINEDLLOVERRIDES="${record.proxy_name}=n,b"`}</code></p>}
      {page.status && <p>{page.status}</p>}
      {error && <p>{error.replace(/[\r\n\u0085\u2028\u2029]+/g, " ")}</p>}
      {message && <p>{message}</p>}
      <ButtonItem onClick={() => setHelp(false)}>Close help</ButtonItem>
    </Focusable>}
    <PanelSection title="DLC selection">
      {metadata.state === "loading" && <p>Loading DLC</p>}
      {metadata.state === "unavailable" && <p>DLC metadata unavailable</p>}
      {metadata.state === "ready" && !metadata.dlcs.length && <p>No DLC listed</p>}
      {[...dlcs.values()].map((dlc) => <PanelSectionRow key={dlc.id}><ToggleField disabled={!editable} label={dlc.name} checked={enabled.includes(dlc.id)} onChange={(on) => setEnabled((old) => on ? [...new Set([...old, dlc.id])] : old.filter((x) => x !== dlc.id))} /></PanelSectionRow>)}
    </PanelSection>
    <PanelSection title="SmokeAPI">
      {setupPending && <PanelSectionRow><ButtonItem disabled={locked} onClick={() => void run(async () => { await applySetup(record); await refresh(); })}>Finish launch setup</ButtonItem></PanelSectionRow>}
      {setupPending && record.set_launch_options === null && <PanelSectionRow><ButtonItem disabled={locked} onClick={() => void run(async () => {
        checked(await acknowledgeLaunchSetup(appId, true)); await refresh();
      })}>Confirm manual setup</ButtonItem></PanelSectionRow>}
      {installed ? <PanelSectionRow><ButtonItem disabled={locked || (!unsaved && !record.config_update) || page.status !== "installed" && !record.config_update} onClick={() => void run(async () => {
        checked(await saveDlcSelection(appId, enabled)); await refresh();
      })}>Save DLC selection</ButtonItem></PanelSectionRow> : <>
        <PanelSectionRow><ButtonItem disabled={busy || !!reason} onClick={() => void run(async () => {
          const original = await getAppLaunchOptions(Number(appId));
          const result = checked(await installSmokeAPI(appId, mode, mode === "direct" ? "version" : proxy, enabled, original, manualLaunch));
          if (result.record && mounted.current) {
            setPage((old) => ({ ...old, status: "installed", record: result.record }));
            await applySetup(result.record);
          }
          await refresh();
        })}>Install</ButtonItem></PanelSectionRow>
        {reason && <p>{reason}</p>}
      </>}
    </PanelSection>
    <PanelSection title="Options">
      {!active ? <>
        <PanelSectionRow><label>Install mode</label><Dropdown menuLabel="Install mode" disabled={locked} selectedOption={mode} rgOptions={[{ label: "Replace game files", data: "direct" }, { label: "Add a launch helper", data: "proxy" }]} onChange={(x) => setMode(x.data)} /></PanelSectionRow>
        {mode === "proxy" && <>
          <PanelSectionRow><label>Launch helper</label><Dropdown menuLabel="Launch helper" disabled={locked} selectedOption={proxy} rgOptions={(page.available_proxies || []).map((x) => ({ label: `Helper ${{ version: 1, winmm: 2, winhttp: 3 }[x] || "?"}`, data: x }))} onChange={(x) => setProxy(x.data)} /></PanelSectionRow>
          <PanelSectionRow><ToggleField disabled={locked} label="Manual launch setup" checked={manualLaunch} onChange={setManualLaunch} /></PanelSectionRow>
        </>}
      </> : <p>{record.mode === "direct" ? "Game files replaced" : "Launch helper added"}</p>}
      {!page.allowed && <>
        <PanelSectionRow><ToggleField disabled={locked} label="Offline single-player, no anti-cheat" checked={confirmed} onChange={setConfirmed} /></PanelSectionRow>
        <PanelSectionRow><ButtonItem disabled={locked || !confirmed} onClick={() => void run(async () => { checked(await allowGame(appId, confirmed)); await refresh(); })}>Allow game</ButtonItem></PanelSectionRow>
      </>}
      <PanelSectionRow><ButtonItem disabled={busy} onClick={() => void run(refresh)}>Refresh</ButtonItem></PanelSectionRow>
      {active && <PanelSectionRow><ButtonItem disabled={locked} onClick={() => void run(async () => {
        const result = checked(await uninstallSmokeAPI(appId));
        if (result.record && mounted.current) {
          setPage((old) => ({ ...old, status: result.record?.phase === "removed" ? "not installed" : "recovery needed", record: result.record }));
          setMessage((result.warnings || []).join("; "));
          await changeLaunchOptions(Number(appId), result.record, true, acknowledgeRestore);
        }
        await refresh();
      })}>{installed && page.status === "installed" ? "Remove" : "Recover files"}</ButtonItem></PanelSectionRow>}
      {restorePending && <>
        <PanelSectionRow><ButtonItem disabled={locked} onClick={() => void run(async () => {
          if (record) setMessage(await changeLaunchOptions(Number(appId), record, true, acknowledgeRestore));
          await refresh();
        })}>Restore launch options</ButtonItem></PanelSectionRow>
        <PanelSectionRow><ButtonItem disabled={locked} onClick={() => void run(async () => { checked(await resolveLaunchByHand(appId, true)); await refresh(); })}>Confirm manual restore</ButtonItem></PanelSectionRow>
      </>}
    </PanelSection>
  </Focusable>;
}
