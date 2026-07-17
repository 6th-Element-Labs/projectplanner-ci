"""Optional process-cut services (Phase 2+).

Package boundaries land here before any live traffic cut. The ``_skeleton``
module is the reusable FastAPI + health + deploy template (ARCH-MS-73). The
``auth`` module is the first real BC cut (ARCH-MS-75) — side-by-side on
``:8121``; ``tasks`` is live on ``:8122``; and ``coord`` is the read-only
side-by-side process on ``:8123`` (ARCH-MS-105). A service is not an edge path
until its cutover task explicitly enables Caddy routing.
"""
