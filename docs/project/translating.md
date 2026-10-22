# Translating Plate|Fold

Plate|Fold ships an English interface today. Other languages can be added when a human translator and a human reviewer are ready to maintain a complete catalog. Do not use a generative-AI system or a machine-translation service to produce or complete a catalog.

The interface language is deliberately separate from the document being exported:

- `--ui-locale` and `PLATEFOLD_UI_LOCALE` select command-line interface messages.
- The desktop follows the browser or operating-system interface locale.
- `--locale` and the HTTP `locale_hint` choose a regional paper default when the user has not chosen a paper size. They do not translate the interface or describe the source document.
- Source-document language and PDF language semantics belong to the document and accessibility pipeline. Plate|Fold never infers them from the interface locale.

Machine-readable JSON keys, HTTP fields, command names, option values, event names, statuses, error codes and export-report fields stay in English and do not change with the interface language. User content, paths, filenames and document metadata are not translated.

## Add a human translation

1. Copy `platefold/locales/en.json` to a file named for the target BCP 47 language tag, such as `fr.json` or `pt-BR.json`.
2. Keep every message ID and placeholder unchanged. Translate only the string values. A catalog must contain the complete English ID set before it can ship; runtime fallback still protects users when a regional catalog is absent.
3. Set `locale`, the language's own `name`, and `direction` (`ltr` or `rtl`). Set `translation_method` to `human`, list the human translator and reviewer in `reviewed_by`, and record the review date in `reviewed_at` as `YYYY-MM-DD`.
4. Preserve placeholders such as `{count}`, `{name}` and `{status}` exactly. For plural messages, use the target language's Unicode CLDR plural categories and always include `other`.
5. Have a fluent human reviewer check the catalog in context. Review narrow and wide windows, 200% zoom, long filenames, keyboard navigation and, where applicable, right-to-left layout and a native screen reader. Coordinate source-document language or reading-order findings in the accessibility issue rather than encoding them as interface behavior.

Plate|Fold does not expose a language picker while English is the only human-reviewed catalog. Once another catalog is accepted, locale selection can be made visible without changing catalog IDs or the machine protocols.

## Validate a catalog

Run the catalog contract and focused tests:

```console
uv run python tools/check_i18n.py
uv run pytest tests/test_i18n.py
cd desktop
npm ci
npm run build
```

The catalog check rejects missing or extra IDs, changed placeholders, incomplete human-review metadata and pseudolocales in the shipped catalog directory. The browser integration suite builds a deterministic mechanical pseudolocale in memory to exercise expansion and right-to-left layout. That data is a test fixture only: do not copy it into a catalog or treat it as a translation.

In a merge request, name the translator and reviewer, describe the in-context checks they ran, and call out any wording whose meaning depends on Plate|Fold terminology. A green automated check establishes catalog shape and runtime behavior; it does not establish translation quality.
