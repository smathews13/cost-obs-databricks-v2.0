import { describe, expect, it } from "vitest";
import { appIconSize } from "../appTiles";

describe("appIconSize", () => {
  it("keeps every tile between 32px and 56px", () => {
    expect(appIconSize(0, 14)).toBe(32);
    expect(appIconSize(14, 14)).toBe(56);
    expect(appIconSize(7, 14)).toBe(44);
  });

  it("stays small when nothing has spend or the max is stale", () => {
    expect(appIconSize(14, 0)).toBe(32);
    expect(appIconSize(500, 14)).toBe(56);
    expect(appIconSize(Number.NaN, 14)).toBe(32);
  });
});
