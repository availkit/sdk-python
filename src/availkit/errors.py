"""Redacted errors. Never attach a raw HTTP exception, URL, header or body."""

from typing import Literal

ErrorKind = Literal["input", "api", "transport", "protocol"]
Outcome = Literal["not_sent", "rejected", "unknown"]


class AvailKitError(Exception):
    """A safe diagnostic; inspect ``outcome`` before deciding what to do next.

    ``unknown`` can mean the server committed a mutation. Neither this exception
    nor the client replays anything. Log only kind, code, status and outcome;
    traceback tools configured to capture local variables can expose caller data.
    """

    def __init__(self, kind: ErrorKind, code: str, status: int | None = None):
        self.kind = kind
        self.code = code
        self.status = status
        self.outcome: Outcome = (
            "not_sent" if kind == "input" else
            "rejected" if kind == "api" and status is not None
            and 400 <= status < 500 and code != "api_error" else "unknown"
        )
        super().__init__(f"AvailKit {kind} error ({code})")

    def as_dict(self) -> dict[str, str | int | None]:
        return {"kind": self.kind, "code": self.code,
                "status": self.status, "outcome": self.outcome}
