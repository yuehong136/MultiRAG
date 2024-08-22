# -*- coding: utf-8 -*-
import os
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, DateTime, BigInteger
from sqlalchemy import create_engine, Column
from sqlalchemy.orm import sessionmaker, declarative_base
from api.settings import DATABASE

database_config = DATABASE

# DATABASE_URL = "postgresql://postgres:123456@127.0.0.1:5432/postgres"
DATABASE_URL = (
    f"{database_config['name']}://"
    f"{database_config['user']}:{database_config['password']}@"
    f"{database_config['host']}:{database_config['port']}/"
    f"{database_config.get('dbname', 'postgres')}"
)


engine = create_engine(
    DATABASE_URL,
    client_encoding='utf8',
    pool_size=database_config.get('pool_size', 10),
    max_overflow=database_config.get('max_overflow', 20),
    pool_timeout=database_config.get('pool_timeout', 30),
    pool_recycle=database_config.get('pool_recycle', 1800),
    echo=False
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class BaseModel(Base):
    __abstract__ = True

    id = Column(String, primary_key=False, nullable=False, index=True, default=lambda: str(uuid.uuid4()))
    create_time = Column(BigInteger, nullable=True, index=True, default=lambda: int(datetime.now(timezone.utc).timestamp() * 1000))
    create_date = Column(DateTime, nullable=True, index=True, default=datetime.now(timezone.utc))
    update_time = Column(BigInteger, nullable=True, index=True, default=lambda: int(datetime.now(timezone.utc).timestamp() * 1000),
                         onupdate=lambda: int(datetime.now(timezone.utc).timestamp() * 1000))
    update_date = Column(DateTime, nullable=True, index=True, default=datetime.now(timezone.utc), onupdate=datetime.now(timezone.utc))

    def to_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}