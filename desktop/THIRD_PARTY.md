# Desktop assets

The application mark in `app.svg` is a local vector implementation of the Plate|Fold paper/plate motif. Icons are generated from it by the Tauri CLI. The interface is implemented in local HTML/CSS and does not load remote design scripts or fonts.

Sora and JetBrains Mono are provided by `@fontsource/sora` and `@fontsource/jetbrains-mono` 5.3.0. Both use the SIL Open Font License 1.1. Their license texts are included in `platefold/fonts`, shipped in Python and desktop packages. The UI bundles Fontsource's font assets through Vite; the PDF renderer embeds the three vendored Latin WOFF2 faces. The PDF font cache signature includes the embedded font data.

Tauri and its JavaScript API use MIT or Apache-2.0 licensing. The locked dependencies and their source license files remain available through Cargo and npm. Chromium's license material is retained with its bundled runtime. Mermaid's license remains in `platefold/vendor`.

The document preview uses pypdfium2 5.13.0 and its bundled PDFium library to display the exported first page as an image. The package's license directory, including PDFium and its third-party build licenses, is retained under the frozen runtime's `pypdfium2-5.13.0.dist-info/licenses` directory.

Authored CSS is parsed with tinycss2 and webencodings. Their BSD license texts are retained in each distribution's `dist-info/licenses` directory in both frozen runtimes.

DOCX semantic conversion uses Mammoth 1.12.1 and its Cobble dependency under their BSD licenses. Cobble 0.1.4's wheel omits its declared BSD-2-Clause text, so Plate|Fold retains the upstream notice beside Cobble's frozen metadata and in the container documentation. OOXML validation and XML parsing use defusedxml 0.7.1 under the Python Software Foundation License. Other frozen dependencies retain their wheel license metadata. Mammoth output is treated as untrusted and sanitized before rendering.

XLSX semantic reading uses openpyxl 3.1.5 and et-xmlfile 2.0.0 under their MIT licenses. Frozen runtimes retain each wheel's license metadata. Plate|Fold validates the OOXML package before openpyxl reads cells and never invokes external workbook links, macros or OLE objects.
