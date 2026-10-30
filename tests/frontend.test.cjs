const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");
const ts = require("typescript");

function load(file, globals = {}) {
  const code = ts.transpileModule(fs.readFileSync(file, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const context = { exports: {}, setTimeout, clearTimeout, ...globals };
  vm.runInNewContext(code, context, { filename: file });
  return context.exports;
}
const wine = (extra) => load("src/utils/wineDllOverride.ts", extra);
const record = { mode: "proxy", proxy_name: "version", original_launch_options: "", set_launch_options: 'WINEDLLOVERRIDES="version=n,b" %command%' };

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
    const install = nodes.find((node) => node.type === "ButtonItem" && node.props.children === "Install SmokeAPI");
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
