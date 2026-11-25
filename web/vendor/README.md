# Vendored third-party libraries

These are unmodified upstream builds, kept here so the dashboard works offline.
Do not edit them. To upgrade, replace the whole directory with the new version,
recompute the hash with `openssl dgst -sha384 -binary FILE | openssl base64 -A`,
and update the `integrity` attribute in `../index.html`.

| File | Upstream | Bytes | SRI (sha384) |
|---|---|---|---|
| `maplibre-gl-4.7.1/maplibre-gl.js` | https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js | 803086 | `SYKAG6cglRMN0RVvhNeBY0r3FYKNOJtznwA0v7B5Vp9tr31xAHsZC0DqkQ/pZDmj` |
| `maplibre-gl-4.7.1/maplibre-gl.css` | https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css | 65534 | `MinO0mNliZ3vwppuPOUnGa+iq619pfMhLVUXfC4LHwSCvF9H+6P/KO4Q7qBOYV5V` |
| `uplot-1.6.31/uPlot.iife.min.js` | https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/uPlot.iife.min.js | 50312 | `XMP23k7YSCFr13Xrkxh4IO0v1W+8fB6AP+3Vycxphp2Z0V2vocAZHyuGaFCysrtM` |
| `uplot-1.6.31/uPlot.min.css` | https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/uPlot.min.css | 1857 | `IfV0B7MIOYuO95kO9G5ySKPz/85zqFNOAs8iy4tkK5zd9izhJAB8b7lHrwYqqmYE` |

Licences: MapLibre GL JS is BSD-3-Clause, uPlot is MIT.
