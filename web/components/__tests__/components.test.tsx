import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api";
import type { LiveDevice } from "@/lib/live";
import type { Rule } from "@/lib/types";

import { defaultDevice } from "../Dashboard";
import { COMPLETE_COVERAGE } from "../EnergyCard";
import { loginErrorMessage } from "../LoginForm";
import { EMPTY_RULE_FORM, RuleChangedError, newRuleBody, ruleBody, ruleError, type NewRuleForm } from "../RulesPage";

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

describe("new rule form", () => {
  const form = (over: Partial<NewRuleForm>): NewRuleForm => ({ ...EMPTY_RULE_FORM, name: "Yeni", deviceId: "dev-1", ...over });
  const bodyOf = (f: NewRuleForm) => {
    const r = newRuleBody(f);
    if ("error" in r) throw new Error(r.error);
    return r.body;
  };

  it("builds a threshold rule, reading a decimal comma", () => {
    const body = bodyOf(form({ threshold: "80,5", clear: "75", duration: "30" }));
    expect(body).toMatchObject({ kind: "threshold", metric: "temperature_c", operator: ">", threshold: 80.5, clear_threshold: 75, duration_s: 30, window_s: null, device_id: "dev-1" });
  });
  it("refuses a closing threshold on the wrong side", () => {
    expect(newRuleBody(form({ threshold: "80", clear: "85" }))).toEqual({ error: "Kapanma eşiği eşikten büyük olamaz." });
    expect(newRuleBody(form({ operator: "<", threshold: "10", clear: "5" }))).toEqual({ error: "Kapanma eşiği eşikten küçük olamaz." });
  });
  it("refuses an empty or ambiguous number instead of making it zero", () => {
    expect("error" in newRuleBody(form({ threshold: "", clear: "75" }))).toBe(true);
    expect("error" in newRuleBody(form({ threshold: "1.000", clear: "75" }))).toBe(true);
  });
  it("makes a reactive ratio rule on reactive power with a window in seconds", () => {
    const body = bodyOf(form({ kind: "reactive_ratio", metric: "current_a", threshold: "0,18", clear: "0,15", windowMin: "15" }));
    expect(body).toMatchObject({ metric: "reactive_power_kvar", operator: ">", window_s: 900 });
    expect("error" in newRuleBody(form({ kind: "reactive_ratio", threshold: "0,18", clear: "0,15", windowMin: "90" }))).toBe(true);
  });
  it("makes a silence rule without thresholds, between 10 and 600 s", () => {
    const body = bodyOf(form({ kind: "no_data", duration: "45" }));
    expect(body).toMatchObject({ kind: "no_data", operator: ">", threshold: 0, clear_threshold: 0, duration_s: 45, window_s: null });
    expect("error" in newRuleBody(form({ kind: "no_data", duration: "5" }))).toBe(true);
    expect("error" in newRuleBody(form({ kind: "no_data", duration: "601" }))).toBe(true);
  });
  it("needs a name and a device", () => {
    expect(newRuleBody(form({ name: "  " }))).toEqual({ error: "Kurala bir ad verin." });
    expect(newRuleBody(form({ deviceId: "" }))).toEqual({ error: "Bir cihaz seçin." });
  });
});
