"""Exception tuples that match BOTH the stdlib sqlite3 and sqlcipher3 drivers (they define separate classes)."""
import sqlite3

try:  # optional: only present when database encryption is available
    import sqlcipher3.dbapi2 as _sqlcipher
except ImportError:  # pragma: no cover
    _sqlcipher = None


def _both(name: str) -> tuple:
    classes = (getattr(sqlite3, name),)
    if _sqlcipher is not None:
        classes += (getattr(_sqlcipher, name),)
    return classes


IntegrityError = _both("IntegrityError")
OperationalError = _both("OperationalError")
DatabaseError = _both("DatabaseError")
