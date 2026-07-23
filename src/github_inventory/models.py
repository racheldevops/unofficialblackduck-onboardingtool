from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class ClientStats:
    requests: int
    graphql_cost: int
    retries: int
