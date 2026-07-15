"""Optional process-cut services (Phase 2+).

Package boundaries land here before any live traffic cut. The ``_skeleton``
module is the reusable FastAPI + health + deploy template (ARCH-MS-73). The
``auth`` module is the first real BC cut (ARCH-MS-75) — side-by-side on
``:8121``; production Caddy cutover is ARCH-MS-76. Nothing under this package
is the live edge path until a cutover task explicitly enables Caddy routing.
"""
