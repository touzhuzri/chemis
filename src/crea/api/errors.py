"""Structured API errors (Phase 4 §56).

Whatever failed underneath -- RDKit refusing a SMILES, the document
pipeline refusing bytes, the repository refusing a write -- the client
sees one shape:

    {"error": {"code": "...", "message": "...", "detail": "..."}}

Never an exception name, never a stack trace, never a SQL string.
"""

from __future__ import annotations


class ApiError(Exception):
    """An error with a stable code and a sentence for a person."""

    def __init__(self, code: str, message: str, detail: str = "",
                 status_code: int = 400) -> None:
        self.code = code
        self.message = message
        self.detail = detail
        self.status_code = status_code
        super().__init__(message)

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message,
                          "detail": self.detail}}


def not_found(what: str) -> ApiError:
    return ApiError("NOT_FOUND", f"{what} 不存在", status_code=404)


def invalid_request(message: str, detail: str = "") -> ApiError:
    return ApiError("INVALID_REQUEST", message, detail, status_code=400)


def invalid_structure(detail: str = "") -> ApiError:
    return ApiError("INVALID_STRUCTURE", "无效的化学结构输入", detail,
                    status_code=400)


def invalid_molecule(detail: str = "") -> ApiError:
    return ApiError("INVALID_MOLECULE", "无效的分子结构", detail,
                    status_code=400)


def conformer_failed(detail: str = "") -> ApiError:
    return ApiError("CONFORMER_GENERATION_FAILED", "无法生成 3D 构象", detail,
                    status_code=422)


def molecule_not_found(detail: str = "") -> ApiError:
    return ApiError("MOLECULE_NOT_FOUND", "分子记录不存在", detail,
                    status_code=404)


__all__ = [
    "ApiError",
    "conformer_failed",
    "invalid_molecule",
    "invalid_request",
    "invalid_structure",
    "molecule_not_found",
    "not_found",
]
