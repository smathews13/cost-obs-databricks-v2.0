import { afterEach, describe, expect, it } from "vitest";
import { DEFAULT_VISIBILITY, firstVisibleDashboardTab, loadTabVisibility } from "../appSettings";

afterEach(() => localStorage.clear());

describe("loadTabVisibility", () => {
  it("drops saved tabs that no longer exist, such as the retired Use Cases tab", () => {
    localStorage.setItem("coc-tab-visibility", JSON.stringify({ "use-cases": true, dbu: false }));
    const v = loadTabVisibility();
    expect(v).not.toHaveProperty("use-cases");
    expect(v.dbu).toBe(false);
    expect(firstVisibleDashboardTab(v)).toBe("sql");
  });

  it("falls back to defaults when nothing is saved", () => {
    expect(loadTabVisibility()).toEqual(DEFAULT_VISIBILITY);
  });
});
