from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from countersign.core.config import settings


@lru_cache
def engine() -> Engine:
    return create_engine(settings().database_url, pool_pre_ping=True, pool_size=10, max_overflow=10, future=True)


@lru_cache
def _factory() -> sessionmaker[Session]:
    return sessionmaker(bind=engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    s = _factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def ping() -> bool:
    try:
        with engine().connect() as c:
            c.execute(text("select 1"))
        return True
    except Exception:
        return False
