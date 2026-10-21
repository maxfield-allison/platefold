// Unicode CLDR measurementData; mirrored by platefold/paper.py.
import { t } from "./i18n";

const letterRegions = new Set(
  "BZ CA CL CO CR GT MX NI PA PH PR SV US VE".split(" "),
);

export function suggestedPaper(language: string): string {
  try {
    const region = new Intl.Locale(language).region;
    return region && letterRegions.has(region) ? "letter" : "a4";
  } catch {
    return "a4";
  }
}

export const paperOptions = [
  ["auto", t("paper.automatic")],
  ["letter", t("paper.letter")],
  ["legal", t("paper.legal")],
  ["tabloid", t("paper.tabloid")],
  ["a5", t("paper.a5")],
  ["a4", t("paper.a4")],
  ["a3", t("paper.a3")],
  ["a2", t("paper.a2")],
  ["a1", t("paper.a1")],
  ["a0", t("paper.a0")],
  ["ansi-c", t("paper.ansi_c")],
  ["ansi-d", t("paper.ansi_d")],
  ["ansi-e", t("paper.ansi_e")],
  ["uniform-letter", t("paper.uniform_legacy")],
];
