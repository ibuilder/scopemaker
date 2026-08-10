/**
 * Reordering the outline, in a real DOM.
 *
 * This exists because of a bug that shipped in 1.5.0 and was found by hand,
 * not by any test: the keyboard move buttons treated every item as a sibling.
 * Nesting in the editor is *flat* -- every item is a sibling in one <ul> and
 * depth is carried by data-parent-id, not by the markup -- so "the item below
 * this one" is often the item's own first child. The reorder endpoint assigns
 * positions per parent, which makes a cross-depth swap unrepresentable: the
 * row moved on screen, the page announced "Order saved", and a reload put it
 * back.
 *
 * The Python suite could not catch that. It asserts the buttons exist and are
 * labelled, which they were.
 *
 * These load the real static/js/app.js rather than a copy or an extracted
 * helper, so the file that ships is the file under test.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { JSDOM } from "jsdom";
import { afterEach, describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const APP_JS = readFileSync(
  resolve(here, "../../scopemaker/static/js/app.js"),
  "utf8",
);

/**
 * A fresh window per test.
 *
 * app.js registers delegated listeners on `document`, so reusing one document
 * stacks another handler every time the script loads and a single click then
 * fires several moves. The first version of this file did exactly that and
 * produced convincing failures that were entirely the harness -- including one
 * that looked like a real bug in the code under test. One window per test is
 * the only honest way to run this.
 *
 * @param {Array<[string, string|null]>} rows [id, parentId] in document order
 */
function editor(rows, { failSave = false } = {}) {
  const items = rows
    .map(([id, parentId]) => {
      const child = parentId ? " item--child" : "";
      return `
        <li class="item${child}" draggable="true"
            data-item-id="${id}" data-parent-id="${parentId || ""}">
          <span class="item__label">${id}</span>
          <span class="item__text">Text for ${id}</span>
          <div class="item__actions">
            <button type="button" data-move="up">up</button>
            <button type="button" data-move="down">down</button>
          </div>
        </li>`;
    })
    .join("");

  const dom = new JSDOM(
    `<!doctype html><html><body>
       <input name="csrf_token" value="test-token">
       <p id="reorder-status" role="status" aria-live="polite"></p>
       <ul class="itemlist" data-reorder
           data-section-key="summary"
           data-reorder-url="/scopes/abc/items/reorder">${items}</ul>
     </body></html>`,
    { runScripts: "dangerously" },
  );

  const sent = [];
  dom.window.fetch = (url, options) => {
    sent.push({ url, body: JSON.parse(options.body) });
    return Promise.resolve({ ok: !failSave });
  };

  const doc = dom.window.document;
  const script = doc.createElement("script");
  script.textContent = APP_JS;
  doc.head.appendChild(script);

  // A freshly constructed JSDOM is still "loading" when the script runs, so
  // app.js's ready() defers everything to DOMContentLoaded -- which fires on a
  // later tick, after a synchronous click in a test. Firing it here attaches
  // the handlers now. If readyState had already been complete, ready() would
  // have run inline and there would be no listener for this to reach, so it
  // cannot double-register either way.
  doc.dispatchEvent(new dom.window.Event("DOMContentLoaded", { bubbles: true }));

  return {
    dom,
    doc,
    sent,
    order: () =>
      [...doc.querySelectorAll(".item[data-item-id]")].map((el) =>
        el.getAttribute("data-item-id"),
      ),
    click: (id, direction) =>
      doc
        .querySelector(`[data-item-id="${id}"] [data-move="${direction}"]`)
        .click(),
    status: () => doc.getElementById("reorder-status").textContent,
  };
}

let open = [];

afterEach(() => {
  open.forEach((dom) => dom.window.close());
  open = [];
});

function make(...args) {
  const harness = editor(...args);
  open.push(harness.dom);
  return harness;
}

describe("keyboard reordering", () => {
  it("swaps two adjacent top-level items", () => {
    const t = make([
      ["a", null],
      ["b", null],
      ["c", null],
    ]);

    t.click("b", "up");

    expect(t.order()).toEqual(["b", "a", "c"]);
  });

  it("moves a parent past its next sibling, carrying its children", () => {
    // The 1.5.0 bug. The element below "b" is b1, its own child; moving down
    // must skip the whole subtree and land after "c".
    const t = make([
      ["a", null],
      ["b", null],
      ["b1", "b"],
      ["b2", "b"],
      ["c", null],
    ]);

    t.click("b", "down");

    expect(t.order()).toEqual(["a", "c", "b", "b1", "b2"]);
  });

  it("moves a parent up past a preceding sibling, carrying its children", () => {
    const t = make([
      ["a", null],
      ["b", null],
      ["b1", "b"],
      ["b2", "b"],
    ]);

    t.click("b", "up");

    expect(t.order()).toEqual(["b", "b1", "b2", "a"]);
  });

  it("reorders children within their parent without escaping it", () => {
    const t = make([
      ["a", null],
      ["a1", "a"],
      ["a2", "a"],
      ["b", null],
    ]);

    t.click("a1", "down");

    expect(t.order()).toEqual(["a", "a2", "a1", "b"]);
  });

  it("carries grandchildren, not just children", () => {
    // Three levels. Deciding "is this a descendant" by comparing parent ids
    // against the moving item is not enough here: a1x's parent is a1, not a,
    // so only walking the ancestry chain gets this right.
    const t = make([
      ["a", null],
      ["a1", "a"],
      ["a1x", "a1"],
      ["a2", "a"],
      ["b", null],
    ]);

    t.click("a1", "down");

    expect(t.order()).toEqual(["a", "a2", "a1", "a1x", "b"]);
  });

  it("moves a whole branch without disturbing what is nested inside it", () => {
    const t = make([
      ["a", null],
      ["a1", "a"],
      ["a1x", "a1"],
      ["b", null],
    ]);

    t.click("a", "down");

    expect(t.order()).toEqual(["b", "a", "a1", "a1x"]);
  });

  it("refuses to move the last item of a level past the end", () => {
    const t = make([
      ["a", null],
      ["b", null],
      ["b1", "b"],
    ]);

    t.click("b", "down");

    expect(t.order()).toEqual(["a", "b", "b1"]);
    expect(t.status()).toMatch(/last item at this level/i);
    expect(t.sent).toHaveLength(0);
  });

  it("refuses to move the first item of a level above the start", () => {
    const t = make([
      ["a", null],
      ["a1", "a"],
      ["a2", "a"],
    ]);

    t.click("a1", "up");

    expect(t.order()).toEqual(["a", "a1", "a2"]);
    expect(t.status()).toMatch(/first item at this level/i);
    expect(t.sent).toHaveLength(0);
  });

  it("a first child does not jump out above its own parent", () => {
    const t = make([
      ["a", null],
      ["a1", "a"],
      ["b", null],
    ]);

    t.click("a1", "up");

    expect(t.order()).toEqual(["a", "a1", "b"]);
  });

  it("keeps focus on the button that was pressed", () => {
    const t = make([
      ["a", null],
      ["b", null],
    ]);

    t.click("b", "up");

    const focused = t.doc.activeElement;
    expect(focused.getAttribute("data-move")).toBe("up");
    expect(focused.closest(".item").getAttribute("data-item-id")).toBe("b");
  });
});

describe("what gets persisted", () => {
  it("posts every item with its parent, in the new order", () => {
    const t = make([
      ["a", null],
      ["b", null],
      ["b1", "b"],
    ]);

    t.click("b", "up");

    expect(t.sent).toHaveLength(1);
    expect(t.sent[0].url).toBe("/scopes/abc/items/reorder");
    expect(t.sent[0].body.section_key).toBe("summary");
    expect(t.sent[0].body.order).toEqual([
      { id: "b", parent_id: null },
      { id: "b1", parent_id: "b" },
      { id: "a", parent_id: null },
    ]);
  });

  it("never reparents anything", () => {
    // Reordering moves things; it must not change the tree. The endpoint
    // trusts these values.
    const t = make([
      ["a", null],
      ["b", null],
      ["b1", "b"],
      ["c", null],
    ]);

    t.click("b", "down");

    const parents = Object.fromEntries(
      t.sent[0].body.order.map((entry) => [entry.id, entry.parent_id]),
    );
    expect(parents).toEqual({ a: null, b: null, b1: "b", c: null });
  });

  it("reports a failed save rather than leaving it silent", async () => {
    const t = make([["a", null], ["b", null]], { failSave: true });

    t.click("b", "up");
    await new Promise((r) => setTimeout(r, 0));

    expect(t.status()).toMatch(/could not save/i);
    expect(t.doc.getElementById("reorder-status").className).toMatch(/error/);
  });
});
