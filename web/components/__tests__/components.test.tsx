import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api";
import type { LiveDevice } from "@/lib/live";
import type { Rule } from "@/lib/types";

import { defaultDevice } from "../Dashboard";
import { COMPLETE_COVERAGE } from "../EnergyCard";
import { loginErrorMessage } from "../LoginForm";
import { RuleChangedError, ruleBody, ruleError } from "../RulesPage";

const device = (id: string, type: string): LiveDevice => ({
  id,
  site_id: "s",
  key: id,
  name: id,
  type,
  is_active: true,
  online: true,
  last_seen: null,
  latest: {},
});

describe("sign-in messages", () => {
  it("says the e-mail or password is wrong without saying which", () => {
    expect(loginErrorMessage(new ApiError(401, "unauthorized", "x"))).toBe("E-posta ya da parola hatalı.");
  });
  it("shows how long to wait after too many attempts", () => {
    expect(loginErrorMessage(new ApiError(429, "too_many_requests", "x", 300))).toContain("5 dk 0 sn");
  });
  it("does not blame the user for a server that is down", () => {
    expect(loginErrorMessage(new ApiError(503, "unavailable", "x"))).toContain("Sunucu");
    expect(loginErrorMessage(new TypeError("Failed to fetch"))).toContain("Bağlantınızı");
  });
});

describe("dashboard", () => {
  it("charts the main panel (the energy analyzer) first", () => {
    const list = [device("k1", "compressor"), device("pano", "energy_analyzer"), device("c1", "chiller")];
    expect(defaultDevice(list)?.id).toBe("pano");
  });
  it("falls back to the first device, and to nothing for an empty site", () => {
    expect(defaultDevice([device("k1", "compressor")])?.id).toBe("k1");
    expect(defaultDevice([])).toBeUndefined();
  });
  it("calls a day complete only when 95 % of its minutes have data", () => {
    expect(COMPLETE_COVERAGE).toBe(0.95);
  });
});

describe("rule editing", () => {
  const rule: Rule = {
    id: "r1",
    device_id: "d1",
    device_name: "Kompresör-1",
    site_id: "s",
    name: "Kompresör-1 yüksek sıcaklık",
    kind: "threshold",
    metric: "temperature_c",
    operator: ">",
    threshold: 80,
    clear_threshold: 75,
    duration_s: 30,
    window_s: null,
    severity: "critical",
    enabled: true,
  };

  it("a PUT body carries every field of the rule, without the ids", () => {
    const body = ruleBody(rule, { duration_s: 45 });
    expect(body).toEqual({
      name: rule.name,
      kind: "threshold",
      metric: "temperature_c",
      operator: ">",
      threshold: 80,
      clear_threshold: 75,
      duration_s: 45,
      window_s: null,
      severity: "critical",
      enabled: true,
    });
    expect("id" in body || "device_id" in body).toBe(false);
  });

  it("explains a refused rule in words", () => {
    expect(ruleError(new ApiError(409, "conflict", "x"))).toContain("etkin başka bir kural");
    expect(ruleError(new ApiError(403, "forbidden", "x"))).toContain("yetkiniz yok");
    expect(ruleError(new ApiError(422, "validation_error", "clear_threshold must not be above"))).toContain("clear_threshold");
    expect(ruleError(new Error("network"))).toContain("Tekrar deneyin");
  });
});

describe("rule values", () => {
  it("shows a ratio rule's numbers as a ratio, not as kVAr", async () => {
    const { formatRuleValue } = await import("@/lib/format");
    expect(formatRuleValue("reactive_ratio", "reactive_power_kvar", 0.18)).toBe("0,180");
    expect(formatRuleValue("threshold", "temperature_c", 80)).toBe("80,0 °C");
    expect(formatRuleValue("no_data", "temperature_c", 122)).toBe("2 dk 2 sn"); // a silence, not a °C
  });
});

describe("rule saving", () => {
  it("tells the editor that somebody else changed the rule meanwhile", () => {
    expect(ruleError(new RuleChangedError())).toContain("başka biri");
  });
});
