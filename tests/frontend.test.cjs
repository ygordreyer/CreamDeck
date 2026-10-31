const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");
const ts = require("typescript");
const path = require("node:path");
function source(file) {
  return fs.readFileSync(process.env.CREAMDECK_BASELINE ? path.join(process.env.CREAMDECK_BASELINE, file) : file, "utf8");
}

function load(file, globals = {}) {
  const code = ts.transpileModule(source(file), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const context = { exports: {}, setTimeout, clearTimeout, ...globals };
  vm.runInNewContext(code, context, { filename: file });
  return context.exports;
}
const wine = (extra) => load("src/utils/wineDllOverride.ts", extra);
const record = { app_id: "123", mode: "proxy", proxy_name: "version", original_launch_options: "", set_launch_options: 'WINEDLLOVERRIDES="version=n,b" %command%' };

function pageHarness({ page = { found: true, name: "Fake Game", status: "not installed", allowed: true, available_proxies: ["version", "winhttp"], dlcs: [{ id: "456", name: "Long DLC name for the selected game" }] }, metadata = { state: "ready", dlcs: page.dlcs || [] }, backend: overrides = {}, launch = {} } = {}) {
  const slots = [], effects = [], calls = [];
  let cursor = 0, tree;
  const backend = {
    checked(result) { if (!result.success) throw Error(result.error); return result; },
    async getGamePage() { return structuredClone(page); },
    async getGameMetadata() { return metadata; },
    async saveDlcSelection(id, ids) { calls.push(["save", id, Array.from(ids)]); page.record.enabled_dlc_ids = Array.from(ids); return { success: true, record: structuredClone(page.record) }; },
    async installSmokeAPI(id, mode) { calls.push(["install", id]); page.record = { ...record, phase: "installed", mode, enabled_dlc_ids: [], launch_restore: "acknowledged", launch_setup: "pending" }; page.status = "installed"; return { success: true, record: structuredClone(page.record) }; },
    async acknowledgeLaunchSetup(id) { calls.push(["setup", id]); page.record.launch_setup = "acknowledged"; return { success: true, record: structuredClone(page.record) }; },
    ...overrides,
  };
  const ui = Object.fromEntries(["ButtonItem", "Dropdown", "Focusable", "PanelSection", "PanelSectionRow", "ToggleField"].map(x => [x, x]));
  const module = load("src/views/CreamDeckPage.tsx", { window: { location: { pathname: "/creamdeck-hardened/123" } }, require(id) {
    if (id === "@decky/ui") return ui;
    if (id === "react") return {
      useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = initial; return [slots[i], value => { slots[i] = typeof value === "function" ? value(slots[i]) : value; }]; },
      useRef(value) { const i = cursor++; return slots[i] ||= { current: value }; },
      useEffect(callback, deps) { const i = cursor++; if (!slots[i] || deps.some((x, j) => x !== slots[i][j])) { slots[i] = deps; effects.push(callback); } },
    };
    if (id === "react/jsx-runtime") return { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
    if (id === "../utils/backend") return backend;
    if (id === "../utils/route") return { pageAppId: () => "123" };
    if (id === "../utils/wineDllOverride") return { getAppLaunchOptions: async () => "", changeLaunchOptions: async (_id, _record, _restore, ack) => { await ack?.(); return ""; }, ...launch };
    throw Error(id);
  } });
  const route = module.default();
  function render() { cursor = 0; tree = route.type(route.props); return tree; }
  function nodes(node = tree, result = []) { if (Array.isArray(node)) node.forEach(x => nodes(x ?? null, result)); else if (node && typeof node === "object") { result.push(node); nodes(node.props?.children ?? null, result); } return result; }
  function text(node = tree) { if (Array.isArray(node)) return node.map(x => text(x ?? null)).join(" "); if (node && typeof node === "object") return text(node.props?.children ?? null); return node == null || typeof node === "boolean" ? "" : String(node); }
  async function settle() { effects.splice(0).forEach(fn => fn()); await new Promise(resolve => setImmediate(resolve)); render(); }
  async function click(label) { const button = nodes().find(x => x.type === "ButtonItem" && [label, { Install: "Install SmokeAPI", Refresh: "Refresh status" }[label]].includes(text(x))); assert.ok(button, `missing button: ${label}`); assert.ok(!button.props.disabled, `disabled button: ${label}`); button.props.onClick(); await settle(); }
  render();
  return { render, settle, click, nodes, text, calls, page, backend, get tree() { return tree; } };
}

test("installed DLC edits stay unsaved until Save and survive refresh", async () => {
  const h = pageHarness({ page: { found: true, name: "Fake Game", status: "installed", allowed: true, record: { ...record, mode: "direct", phase: "installed", enabled_dlc_ids: [], launch_restore: "acknowledged" }, dlcs: [{ id: "456", name: "DLC row" }] } });
  await h.settle();
  h.nodes().find(x => x.type === "ToggleField" && x.props.label === "DLC row").props.onChange(true);
  h.render();
  assert.match(h.text(), /Unsaved selection/);
  await h.click("Save DLC selection");
  assert.deepEqual(h.calls, [["save", "123", ["456"]]]);
  await h.click("Refresh");
  assert.equal(h.nodes().find(x => x.type === "ToggleField" && x.props.label === "DLC row").props.checked, true);
});

test("failed proxy setup stays pending and retry only applies the override", async () => {
  let attempts = 0;
  let options = "";
  const h = pageHarness({ launch: wine({ SteamClient: { Apps: {
    RegisterForAppDetails(_id, callback) { callback({ strLaunchOptions: options }); return { unregister() {} }; },
    SetAppLaunchOptions(id, value) { assert.equal(id, 123); if (++attempts === 1) throw Error("Steam write failed"); options = value; },
  } } }) });
  await h.settle();
  h.nodes().find(x => x.type === "Dropdown").props.onChange({ data: "proxy" }); h.render();
  await h.click("Install");
  assert.match(h.text(), /Proxy setup pending/);
  await h.click("Apply launch override");
  assert.equal(h.calls.filter(x => x[0] === "install").length, 1);
  assert.deepEqual(h.calls.at(-1), ["setup", "123"]);
  assert.equal(options, record.set_launch_options);
  assert.doesNotMatch(h.text(), /Proxy setup pending/);
});

test("failed refresh marks stale and disables mutations until refresh succeeds", async () => {
  const h = pageHarness(); await h.settle();
  h.backend.getGamePage = async () => ({ found: false, error: "game folder missing" });
  await h.click("Refresh");
  assert.match(h.text(), /Status stale/);
  assert.ok(h.nodes().filter(x => x.type === "ButtonItem" && !["Refresh", "(?)"].includes(h.text(x))).every(x => x.props.disabled));
  assert.ok(h.nodes().filter(x => ["ToggleField", "Dropdown"].includes(x.type)).every(x => x.props.disabled));
  h.backend.getGamePage = async () => h.page;
  await h.click("Refresh"); assert.doesNotMatch(h.text(), /Status stale/);
});

test("metadata loading, unavailable and genuine zero DLC are distinct", async () => {
  const loading = pageHarness({ backend: { getGameMetadata: () => new Promise(() => {}) } }); await loading.settle();
  assert.match(loading.text(), /Loading DLC/);
  const unavailable = pageHarness({ metadata: { state: "unavailable", dlcs: [] } }); await unavailable.settle();
  assert.match(unavailable.text(), /DLC metadata unavailable/);
  const empty = pageHarness({ metadata: { state: "ready", dlcs: [] } }); await empty.settle();
  assert.match(empty.text(), /No DLC listed/);
});

test("release publication follows build, tests and package verification", () => {
  const workflow = source(".github/workflows/build-publish.yml");
  for (const step of ["Install dependencies", "Build", "Verify backend and frontend", "Stage and verify plugin package"]) {
    assert.ok(workflow.indexOf(`- name: ${step}`) < workflow.indexOf("- name: Commit and tag version bump"), step);
  }
  assert.match(JSON.parse(source("plugin.json")).publish.description, /per-game/i);
});

function qam({ path = "/routes/library/app/123", running, available = false } = {}) {
  const navigation = [];
  const module = load("src/index.tsx", { window: { location: { pathname: path } }, require(id) {
    if (id === "@decky/api") return { routerHook: { addRoute() {} } };
    if (id === "@decky/ui") return { ButtonItem: "ButtonItem", definePlugin: fn => fn, staticClasses: {}, Router: { MainRunningApp: running ? { appid: running } : undefined }, Navigation: { CloseSideMenus() {}, Navigate(value) { navigation.push(value); } } };
    if (id === "react") return { useState: value => [value, () => {}], useEffect() {} };
    if (id === "react/jsx-runtime") return { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
    if (id === "react-icons/fa") return {};
    if (id === "./patch") return { default: () => undefined, menuPatchAvailable: () => available, subscribeMenuPatchStatus() {} };
    if (id === "./views/CreamDeckPage") return {};
    throw Error(id);
  } });
  const content = module.QuickAccessContent ? undefined : module.default().content;
  const tree = module.QuickAccessContent ? module.QuickAccessContent() : content.type(content.props);
  assert.ok(tree, "QAM exposes a control component");
  return { tree, navigation };
}

// Decky adapters emit semantic HTML without mounting Steam or opening a browser.
function dom(node) {
  if (Array.isArray(node)) return node.map(dom).join("");
  if (node == null || typeof node === "boolean") return "";
  const escape = value => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
  if (typeof node !== "object") return escape(node);
  const p = node.props || {};
  if (!node.type) return dom(p.children);
  const tags = { Focusable: "div", PanelSection: "section", PanelSectionRow: "div", ButtonItem: "button", ToggleField: "input", Dropdown: "select" };
  const tag = tags[node.type] || node.type;
  let attrs = "";
  for (const key of ["role", "aria-label", "disabled", "checked"]) {
    if (p[key]) attrs += ` ${key}="${escape(p[key])}"`;
  }
  if (p.style) attrs += ` style="${escape(JSON.stringify(p.style))}"`;
  let content = dom(p.children);
  if (node.type === "PanelSection") content = `<h2>${escape(p.title)}</h2>` + content;
  if (node.type === "ToggleField") return `<label>${escape(p.label)}<input type="checkbox"${attrs}></label>\n`;
  if (node.type === "Dropdown") content = p.rgOptions.map(x => `<option${x.data === p.selectedOption ? ' selected="true"' : ""}>${escape(x.label)}</option>`).join("");
  if (node.type === "Dropdown") attrs += ` aria-label="${escape(p.menuLabel || "")}"`;
  return `<${tag}${attrs}>${content}</${tag}>\n`;
}

test("QAM opens the current or running game and shows a menu-unavailable fallback", () => {
  for (const [path, running, expected] of [["/routes/library/app/123", "456", "123"], ["/routes/library/home", "456", "456"], ["/routes/library/home", undefined, undefined]]) {
    const h = qam({ path, running });
    const open = [].concat(h.tree.props.children).find(x => x?.type === "ButtonItem" && x.props.children === "Open game");
    assert.ok(open);
    assert.equal(open.props.disabled, !expected);
    open.props.onClick();
    assert.deepEqual(h.navigation, expected ? [`/creamdeck-hardened/${expected}`] : []);
    assert.match(dom(h.tree), /Library menu unavailable/);
  }
  assert.doesNotMatch(dom(qam({ available: true }).tree), /Library menu unavailable/);
});

test("installed mode is loaded, dropdowns are labeled, and an unavailable proxy gives one blocking reason", async () => {
  const active = pageHarness({ page: { found: true, name: "Fake", status: "installed", allowed: true, record: { ...record, phase: "installed", enabled_dlc_ids: [] } } });
  await active.settle();
  assert.match(active.text(), /Mode:  proxy/);
  active.page.record.phase = "removed";
  active.page.status = "not installed";
  active.page.available_proxies = ["version"];
  await active.click("Refresh");
  assert.equal(active.nodes().find(x => x.type === "Dropdown").props.selectedOption, "proxy");
  const h = pageHarness({ page: { found: true, name: "Fake", status: "not installed", allowed: true, available_proxies: [] } });
  await h.settle();
  h.nodes().find(x => x.type === "Dropdown").props.onChange({ data: "proxy" }); h.render();
  assert.deepEqual(h.nodes().filter(x => x.type === "Dropdown").map(x => x.props.menuLabel), ["Install mode", "Proxy DLL"]);
  assert.match(h.text(), /No free proxy name/);
  assert.ok(h.nodes().find(x => x.type === "ButtonItem" && h.text(x) === "Install").props.disabled);
});

test("plugin description describes per-game installs", () => {
  assert.match(JSON.parse(source("plugin.json")).publish.description, /per-game/i);
});

test("controls precede options, disabled reason is visible, help owns explanations and long launch options", async () => {
  const h = pageHarness({ page: { found: true, name: "Selected game", status: "not installed", allowed: false, available_proxies: [] } });
  await h.settle();
  const html = dom(h.tree);
  assert.ok(html.indexOf("Selected game") < html.indexOf("DLC selection"));
  assert.ok(html.indexOf("DLC selection") < html.indexOf(">Install</button>"));
  assert.ok(html.indexOf(">Install</button>") < html.indexOf("Options"));
  assert.match(h.text(), /Allow this game first/);
  assert.doesNotMatch(h.text(), /DLC files must already|Marker scans|Original launch options/);
  assert.ok(!h.nodes().some(x => x.type === "ButtonItem" && /Remove|Recover/.test(h.text(x))));
  const active = pageHarness({ page: { found: true, name: "Selected game", status: "installed", allowed: true, record: { ...record, phase: "installed", enabled_dlc_ids: [], original_launch_options: "x".repeat(8192) } } });
  await active.settle();
  assert.match(active.text(), /Installed \(proxy\)/);
  assert.doesNotMatch(active.text(), /x{20}/);
  await active.click("(?)");
  assert.ok(active.nodes().some(x => x.type === "code" && x.props.style.overflowWrap === "anywhere"));
  assert.match(active.text(), /x{8192}/);
});

test("DOM snapshots cover the twelve offline page states and metadata transitions", async () => {
  const snapshots = {};
  snapshots["01-qam-active"] = dom(qam().tree);
  snapshots["02-qam-no-active"] = dom(qam({ path: "/routes/library/home" }).tree);
  snapshots["03-initial-loading"] = dom(pageHarness().tree);
  const base = { found: true, name: "Selected game", status: "not installed", allowed: true, available_proxies: ["version", "winhttp"] };
  const installed = { ...record, phase: "installed", enabled_dlc_ids: ["456"], launch_restore: "acknowledged", launch_setup: "acknowledged" };
  const fixtures = [
    ["04-not-allowlisted", { ...base, allowed: false }],
    ["05-ready-long-dlc", { ...base, dlcs: [{ id: "456", name: "A long DLC name with plenty of words to wrap in a narrow panel" }, { id: "789", name: "Second DLC" }] }],
    ["06-installed-direct", { ...base, status: "installed", record: { ...installed, mode: "direct" } }],
    ["07-installed-proxy", { ...base, status: "installed", record: installed }],
    ["08-proxy-setup-pending", { ...base, status: "installed", record: { ...installed, launch_setup: "pending" } }],
    ["09-metadata-unavailable", { ...base, status: "installed", record: installed }, { state: "unavailable", dlcs: [] }],
    ["10-restoration-pending", { ...base, record: { ...installed, phase: "removed", launch_restore: "pending" } }],
    ["11-storage-unavailable", { found: false, error: "Game storage unavailable" }],
    ["12-open-help", { ...base, status: "installed", record: installed }],
    ["13-metadata-loading", base, null],
    ["14-zero-dlc", base, { state: "ready", dlcs: [] }],
  ];
  for (const [name, page, metadata] of fixtures) {
    const h = pageHarness({ page, metadata: metadata || { state: "ready", dlcs: page.dlcs || [] }, ...(metadata === null ? { backend: { getGameMetadata: () => new Promise(() => {}) } } : {}) });
    await h.settle();
    if (name === "12-open-help" && h.nodes().some(x => x.type === "ButtonItem" && h.text(x) === "(?)")) await h.click("(?)");
    snapshots[name] = dom(h.tree);
    if (name === "07-installed-proxy") {
      h.backend.getGamePage = async () => ({ found: false, error: "missing game folder" });
      await h.click("Refresh"); snapshots["15-failed-refresh"] = dom(h.tree);
    }
  }
  const file = "tests/snapshots/page-states.json";
  if (process.env.UPDATE_SNAPSHOTS) { fs.mkdirSync("tests/snapshots", { recursive: true }); fs.writeFileSync(file, JSON.stringify(snapshots, null, 2) + "\n"); }
  assert.deepEqual(snapshots, JSON.parse(fs.readFileSync(file, "utf8")));
});

test("launch-option wrapper validation accepts only supported syntax", () => {
  const { mergeWineDllOverride: merge } = wine();
  assert.equal(merge("", "version"), record.set_launch_options);
  assert.equal(merge("%command%", "version"), record.set_launch_options);
  assert.equal(merge("/home/deck/launch.sh %command%", "version"), 'WINEDLLOVERRIDES="version=n,b" /home/deck/launch.sh %command%');
  for (const original of ['WINEDLLOVERRIDES="foo=n" %command%', "WINEDLLOVERRIDES='version=b' %command%", "echo x; %command%", '"/path with space" %command%']) {
    assert.throws(() => merge(original, "version"));
  }
  assert.throws(() => merge("", "../../victim"));
});

test("synchronous app-details callbacks unregister once", async () => {
  let removed = 0;
  const { getAppLaunchOptions } = wine({ SteamClient: { Apps: { RegisterForAppDetails(_id, callback) {
    callback({ strLaunchOptions: "original" });
    return { unregister() { removed++; } };
  } } } });
  assert.equal(await getAppLaunchOptions(123), "original");
  assert.equal(removed, 1);
});

test("app-details timeout rejects and unregisters", async () => {
  let timeout;
  let removed = 0;
  const { getAppLaunchOptions } = wine({
    setTimeout(callback, ms) { assert.equal(ms, 5000); timeout = callback; return 1; }, clearTimeout() {},
    SteamClient: { Apps: { RegisterForAppDetails() { return { unregister() { removed++; } }; } } },
  });
  const pending = getAppLaunchOptions(123);
  timeout();
  await assert.rejects(pending, /timed out/);
  assert.equal(removed, 1);
});

test("launch options restored only when still equal to plugin change", async () => {
  for (const current of [record.set_launch_options, "user changed options"]) {
    let value = current;
    const writes = [];
    const { changeLaunchOptions } = wine({ SteamClient: { Apps: {
      RegisterForAppDetails(_id, callback) { callback({ strLaunchOptions: value }); return { unregister() {} }; },
      SetAppLaunchOptions(id, text) { writes.push([id, text]); value = text; },
    } } });
    const result = await changeLaunchOptions(123, record, true);
    if (current === record.set_launch_options) assert.deepEqual(writes, [[123, ""]]);
    else { assert.deepEqual(writes, []); assert.match(result, /left untouched/); }
  }
});

test("install launch-option write refuses concurrent user edits", async () => {
  const { changeLaunchOptions } = wine({ SteamClient: { Apps: {
    RegisterForAppDetails(_id, callback) { callback({ strLaunchOptions: "user edited" }); return { unregister() {} }; },
    SetAppLaunchOptions() { assert.fail("must not write"); },
  } } });
  assert.match(await changeLaunchOptions(123, record, false), /left untouched/);
});

test("launch-option changes reject a different game's record before Steam access or acknowledgment", async () => {
  const { changeLaunchOptions } = wine({ SteamClient: { Apps: {
    RegisterForAppDetails() { assert.fail("must not read either game's launch options"); },
    SetAppLaunchOptions() { assert.fail("must not write either game's launch options"); },
  } } });
  for (const mode of ["direct", "proxy"]) {
    for (const restore of [false, true]) {
      await assert.rejects(changeLaunchOptions(456, { ...record, mode }, restore,
        async () => { assert.fail("must not acknowledge another game"); }), /different game/);
    }
  }
});

test("installing and restoring one game's launch options preserves the other's exact options", async () => {
  for (const firstMode of ["direct", "proxy"]) {
    for (const secondMode of ["direct", "proxy"]) {
      for (const removed of [123, 456]) {
        const options = new Map([[123, "/home/deck/a %command%"], [456, "/home/deck/b %command%"]]);
        const writes = [];
        const { changeLaunchOptions, mergeWineDllOverride } = wine({ SteamClient: { Apps: {
          RegisterForAppDetails(id, callback) {
            callback({ strLaunchOptions: options.get(id) });
            return { unregister() {} };
          },
          SetAppLaunchOptions(id, text) { writes.push([id, text]); options.set(id, text); },
        } } });
        const records = new Map([[123, firstMode], [456, secondMode]].map(([id, mode]) => [id, {
          ...record, app_id: String(id), mode, original_launch_options: options.get(id),
          set_launch_options: mode === "proxy" ? mergeWineDllOverride(options.get(id), "version") : null,
        }]));
        const original = new Map(options);
        await Promise.all([...records].map(([id, saved]) => changeLaunchOptions(id, saved, false)));
        const survivor = removed === 123 ? 456 : 123;
        const survivorOptions = options.get(survivor);
        writes.length = 0;
        const acknowledgments = [];
        await changeLaunchOptions(removed, records.get(removed), true, async () => acknowledgments.push(removed));
        assert.equal(options.get(removed), original.get(removed));
        assert.equal(options.get(survivor), survivorOptions);
        assert.ok(writes.every(([id]) => id === removed));
        assert.deepEqual(acknowledgments, [removed]);
        await changeLaunchOptions(survivor, records.get(survivor), true);
        assert.deepEqual(options, original);
      }
    }
  }
});

test("manual launch-option mode never reads or writes Steam options", async () => {
  const { changeLaunchOptions } = wine({ SteamClient: { Apps: {
    RegisterForAppDetails() { assert.fail("manual mode must not read"); },
    SetAppLaunchOptions() { assert.fail("manual mode must not write"); },
  } } });
  const manual = { ...record, set_launch_options: null, original_launch_options: "complex original" };
  assert.match(await changeLaunchOptions(123, manual, false), /left untouched/);
  assert.match(await changeLaunchOptions(123, manual, true), /saved original/);
});

test("restore acknowledgment follows Steam confirmation and never follows a failed restore", async () => {
  for (const outcome of ["success", "already restored", "edited", "write failed", "read failed", "manual"]) {
    let value = outcome === "already restored" ? record.original_launch_options
      : outcome === "edited" ? "owner edit" : record.set_launch_options;
    let acknowledgments = 0;
    const events = [];
    const { changeLaunchOptions } = wine({ SteamClient: { Apps: {
      RegisterForAppDetails(_id, callback) {
        events.push("read:" + value);
        if (outcome === "read failed") throw Error("Steam unavailable");
        callback({ strLaunchOptions: value });
        return { unregister() {} };
      },
      SetAppLaunchOptions(_id, text) {
        events.push("write");
        if (outcome !== "write failed") value = text;
      },
    } } });
    const active = outcome === "manual" ? { ...record, set_launch_options: null } : record;
    const restore = changeLaunchOptions(123, active, true, async () => {
      events.push("ack");
      acknowledgments++;
      assert.equal(value, record.original_launch_options);
    });
    if (["write failed", "read failed"].includes(outcome)) await assert.rejects(restore);
    else await restore;
    assert.equal(acknowledgments, ["success", "already restored"].includes(outcome) ? 1 : 0, outcome);
    if (acknowledgments) assert.equal(events.at(-1), "ack");
  }
});

test("failed acknowledgment remains a failed recovery and can be retried", async () => {
  const { changeLaunchOptions } = wine({ SteamClient: { Apps: {
    RegisterForAppDetails(_id, callback) { callback({ strLaunchOptions: "" }); return { unregister() {} }; },
    SetAppLaunchOptions() { assert.fail("already restored"); },
  } } });
  await assert.rejects(changeLaunchOptions(123, record, true, async () => { throw Error("backend unavailable"); }), /backend unavailable/);
  let acknowledged = false;
  await changeLaunchOptions(123, record, true, async () => { acknowledged = true; });
  assert.equal(acknowledged, true);
});

test("direct-mode recovery always acknowledges without reading or writing Steam options", async () => {
  for (const current of ["", "owner edit"]) {
    let acknowledgments = 0;
    const { changeLaunchOptions } = wine({ SteamClient: { Apps: {
      RegisterForAppDetails() { assert.fail("direct mode does not owe restoration, even after an owner edit"); },
      SetAppLaunchOptions() { assert.fail("direct mode never changed launch options"); },
    } } });
    const direct = { ...record, mode: "direct", set_launch_options: null, original_launch_options: current };
    assert.equal(await changeLaunchOptions(123, direct, false), "");
    assert.equal(acknowledgments, 0);
    assert.equal(await changeLaunchOptions(123, direct, true, async () => { acknowledgments++; }), "");
    assert.equal(acknowledgments, 1);
  }
});

test("the page offers only free backend proxy names and disables an unavailable selection", () => {
  for (const available of [["version", "winhttp"], []]) {
    let hook = 0;
    const backend = {};
    const ui = Object.fromEntries(["ButtonItem", "Dropdown", "Focusable", "PanelSection", "PanelSectionRow", "ToggleField"].map((name) => [name, name]));
    const module = load("src/views/CreamDeckPage.tsx", { window: { location: { pathname: "/creamdeck-hardened/123" } }, require(id) {
      if (id === "@decky/ui") return ui;
      if (id === "react") return {
        useEffect() {}, useRef(value) { return { current: value }; },
        useState(initial) {
          const index = hook++;
          const value = index === 0 ? { found: true, allowed: true, available_proxies: available }
            : index === 5 ? "proxy" : initial;
          return [value, () => {}];
        },
      };
      if (id === "react/jsx-runtime") return { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
      if (id === "../utils/backend") return backend;
      if (id === "../utils/route") return { pageAppId: () => "123" };
      if (id === "../utils/wineDllOverride") return {};
      throw Error(id);
    } });
    const route = module.default();
    const tree = route.type(route.props);
    const nodes = [];
    function walk(node) {
      if (Array.isArray(node)) return node.forEach(walk);
      if (!node || typeof node !== "object") return;
      nodes.push(node);
      walk(node.props?.children);
    }
    walk(tree);
    const choices = nodes.find((node) => node.type === "Dropdown" && node.props.selectedOption === "version");
    assert.ok(choices, "proxy dropdown rendered");
    assert.deepEqual(Array.from(choices.props.rgOptions, (option) => option.data), available);
    assert.equal(choices.props.rgOptions.some((option) => option.data === "winmm"), false);
    const install = nodes.find((node) => node.type === "ButtonItem" && node.props.children === "Install");
    assert.equal(install.props.disabled, available.length === 0);
  }
});

test("all patch handles are removed and repeated installation is guarded", () => {
  const handles = [];
  const ui = {
    afterPatch(object, method, callback) {
      const handle = { object, method, callback, removed: 0, unpatch() { this.removed++; } };
      handles.push(handle);
      return handle;
    },
    findModuleByExport() {},
    findInTree() {}, findInReactTree() {},
    MenuItem() {}, Navigation: {},
  };
  const module = load("src/patch.tsx", { require(id) {
    if (id === "@decky/ui") return ui;
    if (id === "react/jsx-runtime") return { jsx() {} };
    throw Error(id);
  } });
  function Library() {}
  const result = module.default(Library);
  assert.equal(result, module.default(Library));
  assert.equal(handles.length, 1);
  const component = { _owner: { pendingProps: { overview: { appid: 123 } } } };
  handles[0].callback([], component);
  assert.equal(handles.length, 2);
  const ret = { type: { prototype: {} } };
  handles[1].callback([], ret);
  handles[1].callback([], ret);
  assert.equal(handles.length, 4);
  assert.doesNotThrow(() => handles[0].callback([], null));
  assert.doesNotThrow(() => handles[1].callback([], null));
  result.unpatch();
  result.unpatch();
  assert.deepEqual(handles.map((x) => x.removed), [1, 1, 1, 1]);
  module.default(Library).unpatch();
  assert.equal(handles.length, 5);
});

function menuPatchHarness() {
  const callbacks = [], status = [], navigation = [];
  const module = load("src/patch.tsx", { require(id) {
    if (id === "@decky/ui") return {
      afterPatch(object, method, callback) { callbacks.push({ object, method, callback }); return { unpatch() {} }; },
      findModuleByExport() {},
      findInTree(items, predicate) {
        return (Array.isArray(items) ? items : [items]).find(predicate);
      },
      findInReactTree(items, predicate) {
        return (Array.isArray(items) ? items.flatMap(item => [item, item?.props]) : [items, items?.props]).find(predicate);
      },
      MenuItem: "MenuItem", Navigation: { Navigate(route) { navigation.push(route); } },
    };
    if (id === "react/jsx-runtime") return { jsx: (type, props, key) => ({ type, props, key }) };
    throw Error(id);
  } });
  module.subscribeMenuPatchStatus(value => status.push(value));
  const handle = module.default(function Library() {});
  const outer = callbacks[0].callback;
  function hook() {
    outer([], { _owner: { pendingProps: { overview: { appid: 123 } } } });
    callbacks[1].callback([], { type: { prototype: {} } });
  }
  function insert() {
    const items = [{ props: { onSelected: function launchSource() {} } }];
    callbacks.find(item => item.method === "render" && item !== callbacks[0]).callback([], { props: { children: [items] } });
    return items;
  }
  return { module, callbacks, status, navigation, handle, outer, hook, insert };
}

test("menu patch stays unavailable until successful insertion", () => {
  const h = menuPatchHarness();
  assert.equal(h.module.menuPatchAvailable(), false);
  h.hook();
  assert.equal(h.module.menuPatchAvailable(), false);
  const items = h.insert();
  assert.equal(items.filter(item => item.key === "creamdeck-hardened").length, 1);
  assert.equal(h.module.menuPatchAvailable(), true);
  assert.deepEqual(h.status, [true]);
  h.handle.unpatch();
});

test("inner function component reports menu unavailable", () => {
  const h = menuPatchHarness();
  h.hook();
  h.insert();
  assert.equal(h.module.menuPatchAvailable(), true);
  h.callbacks[1].callback([], { type: () => null });
  assert.equal(h.module.menuPatchAvailable(), false);
  h.handle.unpatch();
});

test("missing app identity reports menu unavailable", () => {
  const h = menuPatchHarness();
  h.hook();
  h.insert();
  assert.equal(h.module.menuPatchAvailable(), true);
  h.outer([], { props: { children: [] } });
  assert.equal(h.module.menuPatchAvailable(), false);
  h.handle.unpatch();
});

for (const path of ["outer", "render", "shouldComponentUpdate"]) {
  test(`invalid app ids are rejected and valid ids recover through ${path}`, async (t) => {
    const invalidIds = [
      ["undefined", undefined], ["null", null], ["zero", 0], ["empty string", ""],
      ["negative", -1], ["NaN", NaN], ["fraction", 1.5], ["numeric string", "123"],
      ["unsafe integer", Number.MAX_SAFE_INTEGER + 1], ["infinity", Infinity],
      ["boolean", true], ["object", {}], ["array", []], ["bigint", 123n], ["symbol", Symbol("appid")],
    ];
    for (const [label, appid] of invalidIds) {
      await t.test(label, () => {
        const h = menuPatchHarness();
        try {
          h.hook();
          const validEntry = h.insert().find(item => item.key === "creamdeck-hardened");
          assert.ok(validEntry);
          validEntry.props.onSelected();
          assert.deepEqual(h.navigation, ["/creamdeck-hardened/123"]);
          assert.equal(h.module.menuPatchAvailable(), true);
          h.navigation.length = 0;

          function render(id) {
            const items = [{ props: { onSelected: function launchSource() {} } }];
            h.outer([], {
              _owner: { pendingProps: { overview: { appid: id } } },
              props: { children: path === "outer" ? items : [] },
            });
            if (path === "render") {
              h.callbacks.find(item => item.method === "render" && item !== h.callbacks[0])
                .callback([], { props: { children: [items] } });
            } else if (path === "shouldComponentUpdate") {
              h.callbacks.find(item => item.method === path).callback([{ children: items }], true);
            }
            return items;
          }

          const invalidItems = render(appid);
          for (const item of invalidItems.filter(item => item.key === "creamdeck-hardened")) item.props.onSelected();
          assert.deepEqual(h.navigation, [], "invalid identity must not create a navigable entry");
          assert.equal(invalidItems.some(item => item.key === "creamdeck-hardened"), false);
          assert.equal(h.module.menuPatchAvailable(), false);
          assert.equal(h.insert().some(item => item.key === "creamdeck-hardened"), false, "inner render must not reuse a stale identity");
          assert.equal(h.module.menuPatchAvailable(), false);

          const recovered = render(456).filter(item => item.key === "creamdeck-hardened");
          assert.equal(recovered.length, 1);
          assert.equal(h.module.menuPatchAvailable(), true);
          recovered[0].props.onSelected();
          assert.deepEqual(h.navigation, ["/creamdeck-hardened/456"]);
          assert.deepEqual(h.status, [true, false, true]);
        } finally {
          h.handle.unpatch();
        }
      });
    }
  });
}

for (const path of ["render", "shouldComponentUpdate"]) {
  test(`invalid menu identity cannot override a valid app id through ${path}`, () => {
    for (const identity of [{ _owner: { pendingProps: { overview: { appid: -1 } } } }, { app: { appid: "456" } }]) {
      const h = menuPatchHarness();
      try {
        h.hook();
        h.insert();
        const items = [{ props: { onSelected: function launchSource() {} } }, identity];
        const callback = h.callbacks.find(item => item.method === path && item !== h.callbacks[0]).callback;
        if (path === "render") callback([], { props: { children: [items] } });
        else callback([{ children: items }], true);
        assert.equal(items.some(item => item.key === "creamdeck-hardened"), false);
        assert.equal(h.module.menuPatchAvailable(), false);
        assert.equal(h.insert().some(item => item.key === "creamdeck-hardened"), false, "invalid override must clear the cached identity");
        h.outer([], { _owner: { pendingProps: { overview: { appid: 789 } } } });
        const recovered = h.insert().find(item => item.key === "creamdeck-hardened");
        assert.ok(recovered);
        recovered.props.onSelected();
        assert.deepEqual(h.navigation, ["/creamdeck-hardened/789"]);
        assert.equal(h.module.menuPatchAvailable(), true);
      } finally {
        h.handle.unpatch();
      }
    }
  });
}

for (const path of ["render", "shouldComponentUpdate"]) {
  for (const shape of ["overview", "app"]) {
    test(`supplied inner ${shape} app ids are validated through ${path}`, async (t) => {
      const identity = (value) => shape === "overview"
        ? { _owner: { pendingProps: { overview: value } } }
        : { app: value };
      function withMenu(run) {
        const h = menuPatchHarness();
        try {
          h.hook();
          h.insert();
          assert.equal(h.module.menuPatchAvailable(), true);
          const callback = h.callbacks.find(item => item.method === path && item !== h.callbacks[0]).callback;
          function render(identities) {
            const items = [{ props: { onSelected: function launchSource() {} } }, ...identities];
            if (path === "render") callback([], { props: { children: [items] } });
            else callback([{ children: items }], true);
            return items.filter(item => item.key === "creamdeck-hardened");
          }
          function reject(identities) {
            const entries = render(identities);
            for (const entry of entries) entry.props.onSelected();
            assert.deepEqual(h.navigation, [], "invalid inner identity must not create a navigable entry");
            assert.equal(entries.length, 0);
            assert.equal(h.module.menuPatchAvailable(), false);
            assert.equal(render([]).length, 0, "invalid inner identity must clear the cached id in this hook");
            assert.equal(h.insert().some(item => item.key === "creamdeck-hardened"), false, "inner render must not reuse the cached id");
            assert.equal(h.module.menuPatchAvailable(), false);

            h.outer([], { _owner: { pendingProps: { overview: { appid: 456 } } } });
            const recovered = render([identity({ appid: 789 })]);
            assert.equal(recovered.length, 1);
            assert.equal(h.module.menuPatchAvailable(), true);
            recovered[0].props.onSelected();
            assert.deepEqual(h.navigation, ["/creamdeck-hardened/789"]);
            assert.deepEqual(h.status, [true, false, true]);
          }
          run({ h, render, reject });
        } finally {
          h.handle.unpatch();
        }
      }
      for (const [label, appid] of [
        ["null", null], ["zero", 0], ["empty string", ""], ["NaN", NaN],
        ["negative", -1], ["fraction", 1.5], ["numeric string", "123"],
      ]) {
        await t.test(label, () => withMenu(({ reject }) => reject([identity({ appid })])));
      }
      for (const [label, value] of [["absent appid", {}], ["undefined appid", { appid: undefined }]]) {
        await t.test(label, () => withMenu(({ h, render }) => {
          const entries = render([identity(value)]);
          assert.equal(entries.length, 1);
          assert.equal(h.module.menuPatchAvailable(), true);
          entries[0].props.onSelected();
          assert.deepEqual(h.navigation, ["/creamdeck-hardened/123"]);
          assert.deepEqual(h.status, [true]);
        }));
      }
      await t.test("valid supplied id overrides cache without duplicates", () => withMenu(({ h, render }) => {
        const entries = render([identity({ appid: 456 })]);
        assert.equal(entries.length, 1);
        assert.equal(h.module.menuPatchAvailable(), true);
        entries[0].props.onSelected();
        assert.deepEqual(h.navigation, ["/creamdeck-hardened/456"]);
        assert.equal(render([identity({ appid: 456 }), entries[0]]).length, 1);
      }));
      await t.test("invalid id after valid id is still rejected", () => withMenu(({ reject }) => {
        reject([identity({ appid: 456 }), identity({ appid: 0 })]);
      }));
      await t.test("invalid id in the other identity shape is still rejected", () => withMenu(({ reject }) => {
        const other = shape === "overview" ? { app: { appid: 0 } }
          : { _owner: { pendingProps: { overview: { appid: 0 } } } };
        reject([identity({ appid: 456 }), other]);
      }));
    });
  }
}

test("insertion exceptions report menu unavailable in every hook", () => {
  for (const hook of ["render", "shouldComponentUpdate", "outer"]) {
    const h = menuPatchHarness();
    h.hook();
    h.insert();
    const items = Object.freeze([{ props: { onSelected: function launchSource() {} } }]);
    if (hook === "outer") h.outer([], { _owner: { pendingProps: { overview: { appid: 123 } } }, props: { children: items } });
    else {
      const callback = h.callbacks.find(item => item.method === hook && item !== h.callbacks[0]).callback;
      if (hook === "render") callback([], { props: { children: [items] } });
      else callback([{ children: items }], true);
    }
    assert.equal(h.module.menuPatchAvailable(), false, hook);
    h.handle.unpatch();
  }
});

test("changed Steam menu structure reports patch unavailable", () => {
  const callbacks = [], status = [];
  const module = load("src/patch.tsx", { require(id) {
    if (id === "@decky/ui") return { afterPatch(_object, _method, callback) { callbacks.push(callback); return { unpatch() {} }; }, findModuleByExport() {}, findInTree() {}, findInReactTree(items) { return Array.isArray(items) ? items[0] : undefined; } };
    if (id === "react/jsx-runtime") return { jsx() {} };
    throw Error(id);
  } });
  module.subscribeMenuPatchStatus(value => status.push(value));
  const handle = module.default(function Library() {});
  callbacks[0]([], { _owner: { pendingProps: { overview: { appid: 123 } } } });
  callbacks[1]([], { type: { prototype: {} } });
  callbacks[2]([], { props: { children: [[{}]] } });
  callbacks[2]([], { props: { children: ["changed structure"] } });
  assert.equal(module.menuPatchAvailable(), false);
  assert.deepEqual(status, [true, false]);
  handle.unpatch();
});

test("app id parses from Steam's routed page path", () => {
  const { appIdFromPath } = load("src/utils/route.ts");
  // Game Mode serves every page under /routes (https://steamloopback.host/routes/...).
  assert.equal(appIdFromPath("/routes/creamdeck-hardened/1145350"), "1145350");
  assert.equal(appIdFromPath("/routes/creamdeck-hardened/1145350/"), "1145350");
  assert.equal(appIdFromPath("/creamdeck-hardened/1145350"), "1145350");
  for (const bad of ["/routes/creamdeck-hardened/0123", "/routes/creamdeck-hardened/12345678901", "/routes/creamdeck-hardened/12x",
    "/routes/creamdeck-hardened/../1", "/x/routes/creamdeck-hardened/1", "/routes/creamdeck/1", "/routes/library/app/1145350"]) {
    assert.equal(appIdFromPath(bad), "", bad);
  }
});

test("the route param picks the page's game, so an in-place route change cannot keep the old one", () => {
  const { pageAppId } = load("src/utils/route.ts");
  assert.equal(pageAppId("482400", "/routes/creamdeck-hardened/617290"), "482400");
  assert.equal(pageAppId(undefined, "/routes/creamdeck-hardened/617290"), "617290");
  for (const bad of ["0123", "12345678901", "12x", "../1", ""]) {
    assert.equal(pageAppId(bad, "/routes/creamdeck-hardened/617290"), "", bad);
  }
});
