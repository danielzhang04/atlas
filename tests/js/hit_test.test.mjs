import assert from "node:assert/strict";
import test from "node:test";

globalThis.__ATLAS_TEST__ = true;
await import("../../ui/app.js");

const hitTest = globalThis.__atlasHitTest;
const geometry = {
  centerX: 100,
  centerY: 100,
  innerRadius: 40,
  outerRadius: 50,
  segments: [
    {index: 0, start: 0, end: Math.PI / 4},
    {index: 1, start: 5.8, end: 6.4},
  ],
};

test("outer-ring hit testing respects radius and angular gaps", () => {
  assert.equal(typeof hitTest, "function");
  assert.equal(hitTest(145, 100, geometry, 1), 0);
  assert.equal(hitTest(100, 145, geometry, 1), -1);
  assert.equal(hitTest(139, 100, geometry, 1), -1);
  assert.equal(hitTest(151, 100, geometry, 1), -1);
});

test("outer-ring hit testing handles angle bounds that wrap through zero", () => {
  assert.equal(hitTest(145, 100, geometry, 2), 0);
  assert.equal(hitTest(145, 96, geometry, 2), 1);
  assert.equal(hitTest(100, 100, geometry, 2), -1);
});
