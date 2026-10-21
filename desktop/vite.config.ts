import { defineConfig } from "vite";

export default defineConfig({
  build: {
    // The server's strict CSP permits fonts from this origin, not data URLs.
    // Keep every bundled font as a separately served asset regardless of size.
    assetsInlineLimit: 0,
  },
});
