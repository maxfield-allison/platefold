type PluralEntry = Partial<Record<Intl.LDMLPluralRule, string>> & {
  other: string;
};
type MessageEntry = string | PluralEntry;
export interface CatalogData {
  schema_version: 1;
  locale: string;
  name: string;
  direction: "ltr" | "rtl";
  translation_method: "source" | "human" | "mechanical-test";
  messages: Record<string, MessageEntry>;
}

declare global {
  interface Window {
    __PLATEFOLD_I18N_TEST__?: CatalogData;
  }
}

const rawCatalogs = import.meta.glob("../../platefold/locales/*.json", {
  eager: true,
  query: "?raw",
  import: "default",
}) as Record<string, string>;
const shippedCatalogs = Object.values(rawCatalogs).map(
  (raw) => JSON.parse(raw) as CatalogData,
);
const englishCatalog = shippedCatalogs.find((item) => item.locale === "en");
if (!englishCatalog)
  throw new Error("The English interface catalog is unavailable");
const english: CatalogData = englishCatalog;

function chooseCatalog(requested: readonly string[]): CatalogData {
  const byLocale = new Map(
    shippedCatalogs.map((item) => [item.locale.toLocaleLowerCase(), item]),
  );
  for (const request of requested) {
    const normalized = request.replaceAll("_", "-").toLocaleLowerCase();
    const exact = byLocale.get(normalized);
    if (exact) return exact;
    const language = byLocale.get(normalized.split("-", 1)[0]);
    if (language) return language;
  }
  return english;
}

const testCatalog = window.__PLATEFOLD_I18N_TEST__;
const catalog =
  testCatalog?.schema_version === 1
    ? testCatalog
    : chooseCatalog(navigator.languages);
const fallback = english.messages;
const fallbackIds = new Map<string, string>();
for (const [id, entry] of Object.entries(fallback))
  if (typeof entry === "string") fallbackIds.set(entry, id);

export const interfaceLocale = catalog.locale;
export const interfaceDirection = catalog.direction;

function template(id: string, count?: number): string {
  const entry = catalog.messages[id] ?? fallback[id];
  if (typeof entry === "string") return entry;
  if (entry && count !== undefined) {
    const category = new Intl.PluralRules(interfaceLocale).select(count);
    return entry[category] ?? entry.other;
  }
  const missing = fallback["i18n.unknown_message"];
  return typeof missing === "string"
    ? missing.replace("{id}", id)
    : `Missing interface message: ${id}`;
}

function interpolate(
  source: string,
  values: Record<string, string | number> = {},
): string {
  return source.replace(/\{([A-Za-z][A-Za-z0-9_]*)\}/g, (_match, name) => {
    if (!(name in values))
      throw new Error(`Missing interface placeholder: ${name}`);
    return String(values[name]);
  });
}

export function t(
  id: string,
  values: Record<string, string | number> = {},
): string {
  return interpolate(template(id), values);
}

export function tOr(
  id: string,
  fallbackText: string,
  values: Record<string, string | number> = {},
): string {
  if (!(id in catalog.messages) && !(id in fallback))
    return interpolate(fallbackText, values);
  return t(id, values);
}

/** Translate an exact backend fallback when it has a stable catalog ID. */
export function localizeLiteral(value: unknown): string {
  const source = String(value);
  const id = fallbackIds.get(source);
  return id ? t(id) : source;
}

export function plural(
  id: string,
  count: number,
  values: Record<string, string | number> = {},
): string {
  return interpolate(template(id, count), {
    count: number(count),
    ...values,
  });
}

export function number(
  value: number,
  options: Intl.NumberFormatOptions = {},
): string {
  return new Intl.NumberFormat(interfaceLocale, options).format(value);
}

export function date(
  value: Date | number,
  options: Intl.DateTimeFormatOptions = { dateStyle: "medium" },
): string {
  return new Intl.DateTimeFormat(interfaceLocale, options).format(value);
}

export function html(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function initializeDocumentLanguage(): void {
  document.documentElement.lang = interfaceLocale;
  document.documentElement.dir = interfaceDirection;
}

/** Replace static English fallback text without treating catalog text as HTML. */
export function localizeTree(root: ParentNode): void {
  const translated = (value: string) => {
    const start = value.match(/^\s*/)?.[0] ?? "";
    const end = value.match(/\s*$/)?.[0] ?? "";
    const source = value.trim();
    const id = fallbackIds.get(source);
    return id ? `${start}${t(id)}${end}` : value;
  };
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node: Node | null;
  while ((node = walker.nextNode()))
    node.nodeValue = translated(node.nodeValue ?? "");
  for (const element of root.querySelectorAll<HTMLElement>("*"))
    for (const attribute of ["aria-label", "placeholder", "title"])
      if (element.hasAttribute(attribute))
        element.setAttribute(
          attribute,
          translated(element.getAttribute(attribute)!),
        );
}
