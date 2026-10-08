import { describe, expect, it } from "vitest";

import { parseDecimal, parseInteger } from "../numbers";
import { isUuid } from "../uuid";

describe("parseDecimal", () => {
  it("reads dot and comma decimals", () => {
    expect(parseDecimal("80")).toBe(80);
    expect(parseDecimal("0,18")).toBe(0.18);
    expect(parseDecimal(" -3.5 ")).toBe(-3.5);
    expect(parseDecimal("0,180")).toBe(0.18); // a leading zero is plainly a decimal
    expect(parseDecimal("1000")).toBe(1000);
  });
  it("refuses what Number() would turn into 0 or into something else", () => {
    for (const bad of ["", "   ", "abc", "1e3", "1.000", "1,234", "80.500", "1.234,5", "1,", ",5", "--1", "Infinity", "0x10"]) {
      expect(parseDecimal(bad)).toBeNull();
    }
  });
});

describe("parseInteger", () => {
  it("reads whole numbers", () => {
    expect(parseInteger("30")).toBe(30);
    expect(parseInteger(" 0 ")).toBe(0);
  });
  it("refuses empty, negative, fractional and huge input", () => {
    for (const bad of ["", " ", "-1", "1.5", "1,5", "abc", "99999999999999999999"]) {
      expect(parseInteger(bad)).toBeNull();
    }
  });
});

describe("isUuid", () => {
  it("accepts a UUID and nothing a person could use to change an API path", () => {
    expect(isUuid("0b185aa9-47f5-5341-aa5e-f0dedd1b6720")).toBe(true);
    for (const bad of ["../../auth/me", "0b185aa9", "", null, undefined, "0b185aa9-47f5-5341-aa5e-f0dedd1b6720/x"]) {
      expect(isUuid(bad as string | null | undefined)).toBe(false);
    }
  });
});
