"""ML post-processing and nowcasting (ADR 0008).

Quantile post-processing of upstream NWP forecasts (P10/P50/P90) with physical
constraints, a leak-free temporal training store, holdout scoring against the
raw sources, versioned text artifacts, and a nowcaster interface. LightGBM is
an optional dependency (``[ml]`` extra), imported only when a model is trained
or loaded. Outputs are FORECAST with full provenance; humans decide.
"""
