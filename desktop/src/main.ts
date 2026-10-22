import "@fontsource/sora/400.css";
import "@fontsource/sora/600.css";
import "@fontsource/jetbrains-mono/400.css";
import "./style.css";
import { mount } from "./view";
import { HttpTransport } from "./http";
import { desktop } from "./transport";
import { initializeDocumentLanguage } from "./i18n";

initializeDocumentLanguage();

mount(
  document.querySelector<HTMLDivElement>("#app")!,
  "__TAURI_INTERNALS__" in window ? desktop : new HttpTransport(),
);
