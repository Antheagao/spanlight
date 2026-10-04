"""spanlight: self-hosted LLM observability.

The stdlib-only tracing SDK lives here; the server lives in
`spanlight.server` (install the `server` extra for it).
"""

from .sdk import DEFAULT_PRICES, Span, Spanlight, Trace, estimate_cost

__version__ = "0.1.0"
__all__ = ["DEFAULT_PRICES", "Span", "Spanlight", "Trace", "estimate_cost"]
