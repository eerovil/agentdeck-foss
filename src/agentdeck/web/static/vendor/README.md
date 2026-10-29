# Vendored libraries

Copied in whole, unchanged, so the deck needs no outside download. Each diagram frame
loads them (#3822); `static_files.py` serves this folder with CORS so a frame with no
origin of its own can load the ELK module.

| File | Package | Version | Licence | From |
| --- | --- | --- | --- | --- |
| `mermaid.min.js` | `mermaid` | 12.2.0 | MIT | `https://registry.npmjs.org/mermaid/-/mermaid-12.2.0.tgz`, `package/dist/mermaid.min.js` |
| `mermaid-elk/` | `@mermaid-js/layout-elk` | 1.0.1 | MIT (bundles ELK, EPL-2.0) | `https://registry.npmjs.org/@mermaid-js/layout-elk/-/layout-elk-1.0.1.tgz`, `package/dist/mermaid-layout-elk.esm.min.mjs` and its `chunks/mermaid-layout-elk.esm.min/`, without source maps |

To update, fetch the new tarball, replace the same files, and check a diagram file's
full-screen view (`<file>.md?bare=1`) still draws with ELK (`body[data-layout="elk"]`).
