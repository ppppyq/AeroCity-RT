# Provenance

AeroCity-RT provides a deterministic procedural 3D city generator, canonical scene schema, Wavefront OBJ exporter, interactive Three.js web visualizer, and Sionna RT ray-tracing integration. The canonical coordinate system is right-handed, metres, and +Z-up.

The radio stage uses Sionna RT with native CUDA/OptiX, a transparent disconnected multilayer measurement mesh, two equal-area triangles per XY cell, and reduction to `[Tx, Z, Y, X]`. These choices are part of the repository implementation and are recorded in the generated manifests.

The UMa and UMi deployment presets are calibration references based on 3GPP TR 38.901 v18.0.0, especially Table 7.2-1 and the calibration tables:
https://www.etsi.org/deliver/etsi_tr/138900_138999/138901/18.00.00_60/tr_138901v180000p.pdf
They are not claims that one parameter set describes every commercial network. Low-altitude communication and ISAC presets use explicit `simulation_proxy` engineering defaults where complete commercial parameters are not publicly standardized.

The repository source and documentation are licensed under the MIT License in [LICENSE](LICENSE). Vendored frontend libraries in `src/wireless_city_factory/web/vendor/` (Three.js and Lucide) retain their respective MIT and ISC licenses.
